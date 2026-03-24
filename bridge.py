#!/usr/bin/env python3
"""WeChat-Claude Code Bridge.

Bridges WeChat ClawBot messages to Claude Code and other AI agent CLIs.
No OpenClaw needed — directly uses iLink API.

Usage:
    python bridge.py              # Login and start bridge (default: claude)
    python bridge.py -w /path     # Set working directory
    python bridge.py --logout     # Clear credentials
"""

import argparse
import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ilink_client import ILinkClient
from memory_store import MemoryStore
from scheduler import Scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "wechat-claude-bridge"
SESSION_FILE = CONFIG_DIR / "sessions.json"
MEDIA_DIR = CONFIG_DIR / "media"
PERSONA_FILE = CONFIG_DIR / "persona.json"

# Per-user state
_sessions: dict[str, str] = {}  # user_id -> session_id
_user_agent: dict[str, str] = {}  # user_id -> agent_key
_sessions_lock = threading.Lock()

# Runtime mutable working directory
_working_dir: str | None = None
_workdir_lock = threading.Lock()

_executor = ThreadPoolExecutor(max_workers=8)

# OpenClaw-inspired subsystems
_memory = MemoryStore()
_scheduler = Scheduler()
_personas: dict[str, str] = {}  # user_id -> persona string


def _load_personas() -> None:
    try:
        _personas.update(json.loads(PERSONA_FILE.read_text()))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass


def _save_personas() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    PERSONA_FILE.write_text(json.dumps(_personas, indent=2, ensure_ascii=False))
    try:
        os.chmod(PERSONA_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ── Agent Definitions ───────────────────────────────────────────

AGENTS: dict[str, dict] = {
    "claude": {
        "name": "Claude Code",
        "binary": "claude",
        "build_cmd": lambda msg, sid: _build_claude_cmd(msg, sid),
        "use_stdin": True,
        "parse_output": lambda stdout, uid: _parse_claude_output(stdout, uid),
    },
    "codex": {
        "name": "Codex CLI",
        "binary": "codex",
        "build_cmd": lambda msg, sid: ["codex", "-q", msg],
        "use_stdin": False,
        "parse_output": lambda stdout, uid: stdout.strip() or "[No response]",
    },
    "gemini": {
        "name": "Gemini CLI",
        "binary": "gemini",
        "build_cmd": lambda msg, sid: ["gemini", "-p", msg],
        "use_stdin": False,
        "parse_output": lambda stdout, uid: stdout.strip() or "[No response]",
    },
    "aider": {
        "name": "Aider",
        "binary": "aider",
        "build_cmd": lambda msg, sid: ["aider", "--message", msg, "--yes"],
        "use_stdin": False,
        "parse_output": lambda stdout, uid: stdout.strip() or "[No response]",
    },
}


def _build_claude_cmd(message: str, session_id: str | None) -> list[str]:
    """Build Claude Code CLI command."""
    cmd = ["claude", "-p", "--output-format", "json"]
    if session_id:
        cmd.extend(["--resume", session_id])
    return cmd


def _get_user_agent(user_id: str) -> str:
    return _user_agent.get(user_id, "claude")


def _find_binary(name: str) -> str | None:
    """Check if a CLI binary exists on PATH."""
    return shutil.which(name)


# ── Session Persistence ─────────────────────────────────────────


def _load_sessions() -> None:
    try:
        with _sessions_lock:
            _sessions.update(json.loads(SESSION_FILE.read_text()))
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to load sessions: %s", e)


def _save_sessions() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _sessions_lock:
        SESSION_FILE.write_text(json.dumps(_sessions, indent=2))
    try:
        os.chmod(SESSION_FILE, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


# ── Markdown → Plain Text ───────────────────────────────────────


def md_to_plain(text: str) -> str:
    """Convert markdown to WeChat-friendly plain text."""
    text = re.sub(r"```\w*\n?", "", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*_]{3,}\s*$", "--------", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ── Continuous Typing Indicator ─────────────────────────────────


def _typing_loop(
    client: ILinkClient,
    to_user: str,
    context_token: str,
    stop_event: threading.Event,
) -> None:
    """Send typing indicator every 5 seconds until stop_event is set."""
    while not stop_event.is_set():
        try:
            client.send_typing(to_user, context_token)
        except Exception:
            break
        stop_event.wait(5)


# ── Claude Code Output Parsing ──────────────────────────────────


def _parse_claude_output(stdout: str, user_id: str) -> str:
    """Parse Claude CLI JSON output, extract text and session_id."""
    if not stdout.strip():
        return "[No response from Claude Code]"

    try:
        data = json.loads(stdout)
        session_id = data.get("session_id")
        if session_id:
            with _sessions_lock:
                _sessions[user_id] = session_id
            _save_sessions()

        result_text = data.get("result", "")
        if not result_text:
            result_text = data.get("text", data.get("content", str(data)))
        return result_text if result_text else "[Empty response]"
    except json.JSONDecodeError:
        # NDJSON fallback (streaming output)
        lines = stdout.strip().splitlines()
        text_parts = []
        for line in lines:
            try:
                obj = json.loads(line)
                if obj.get("type") == "result":
                    sid = obj.get("session_id")
                    if sid:
                        with _sessions_lock:
                            _sessions[user_id] = sid
                        _save_sessions()
                    text_parts.append(obj.get("result", ""))
                elif obj.get("type") == "assistant" and "content" in obj:
                    for block in obj["content"]:
                        if block.get("type") == "text":
                            text_parts.append(block["text"])
            except (json.JSONDecodeError, TypeError, KeyError):
                text_parts.append(line)
        return "\n".join(text_parts) if text_parts else stdout.strip()


# ── Agent Invocation ────────────────────────────────────────────


def call_agent(
    message: str,
    user_id: str,
    working_dir: str | None = None,
    image_paths: list[Path] | None = None,
) -> str:
    """Call the user's selected AI agent CLI and return the response."""
    agent_key = _get_user_agent(user_id)
    agent = AGENTS.get(agent_key)
    if not agent:
        return f"[Unknown agent: {agent_key}]"

    binary = _find_binary(agent["binary"])
    if not binary:
        return f"[{agent['name']} not found. Install it first.]"

    with _sessions_lock:
        session_id = _sessions.get(user_id) if agent_key == "claude" else None

    if agent_key == "claude":
        logger.info("Claude session: %s", session_id[:12] if session_id else "new")

    # Append image instructions for Claude to read the files
    if image_paths:
        paths_str = ", ".join(str(p) for p in image_paths)
        img_note = (
            f"\n\nThe user sent {len(image_paths)} image(s). "
            f"Use the Read tool to view: {paths_str}\n"
            f"Describe what you see and respond to the user's message."
        )
        message += img_note

    cmd = agent["build_cmd"](message, session_id)
    # Replace binary name with full path
    cmd[0] = binary

    # Build system prompt with memory + persona
    if agent_key == "claude":
        sys_parts = []
        persona = _personas.get(user_id, "")
        if persona:
            sys_parts.append(f"Persona: {persona}")
        mem_ctx = _memory.get_context()
        if mem_ctx:
            sys_parts.append(mem_ctx)
        if sys_parts:
            cmd.extend(["--append-system-prompt", "\n".join(sys_parts)])

    # Pass user message via stdin (clean, no context mixing)
    stdin_data = None
    if agent["use_stdin"]:
        stdin_data = message

    try:
        result = subprocess.run(
            cmd,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=working_dir,
        )

        if result.returncode != 0:
            stderr = result.stderr.strip()
            # Claude session expired -> retry
            if (
                agent_key == "claude"
                and session_id
                and ("session" in stderr.lower() and "not found" in stderr.lower())
            ):
                logger.warning("Session expired, starting fresh.")
                with _sessions_lock:
                    _sessions.pop(user_id, None)
                _save_sessions()
                retry_cmd = _build_claude_cmd(message, None)
                retry_cmd[0] = binary
                # Re-add system prompt context
                sys_parts = []
                persona = _personas.get(user_id, "")
                if persona:
                    sys_parts.append(f"Persona: {persona}")
                mem_ctx = _memory.get_context()
                if mem_ctx:
                    sys_parts.append(mem_ctx)
                if sys_parts:
                    retry_cmd.extend(["--append-system-prompt", "\n".join(sys_parts)])
                result = subprocess.run(
                    retry_cmd,
                    input=stdin_data,
                    capture_output=True,
                    text=True,
                    timeout=300,
                    cwd=working_dir,
                )

            if result.returncode != 0:
                logger.error("%s error (exit %d)", agent["name"], result.returncode)
                if stderr:
                    logger.error("%s stderr: %s", agent["name"], stderr[:500])
                return f"[{agent['name']} error. Check bridge logs for details.]"

        return agent["parse_output"](result.stdout, user_id)

    except subprocess.TimeoutExpired:
        return f"[{agent['name']} timed out after 5 minutes]"
    except FileNotFoundError:
        return f"[{agent['name']} CLI not found]"


# ── Image Handling ──────────────────────────────────────────────


def _handle_images(client: ILinkClient, message: dict) -> list[Path]:
    """Download images from message, return list of saved file paths."""
    media_items = client.extract_media(message)
    saved: list[Path] = []
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    for item in media_items:
        if item["type"] == "image" and (
            item.get("cdn_url") or item.get("encrypt_query_param")
        ):
            ext = ".jpg"
            path = MEDIA_DIR / f"img_{uuid.uuid4().hex[:12]}{ext}"
            if client.download_media(
                item.get("cdn_url", ""),
                item.get("aes_key", ""),
                path,
                encrypt_query_param=item.get("encrypt_query_param", ""),
            ):
                saved.append(path)
                logger.info(
                    "Downloaded image: %s (%d bytes)", path.name, path.stat().st_size
                )
        elif item["type"] == "file" and item.get("cdn_url"):
            raw_name = item.get("filename", f"file_{int(time.time())}")
            safe_name = Path(raw_name).name  # strip path components
            if not safe_name or safe_name.startswith("."):
                safe_name = f"file_{int(time.time() * 1000)}"
            path = MEDIA_DIR / safe_name
            if client.download_media(item["cdn_url"], item.get("aes_key", ""), path):
                saved.append(path)
                logger.info("Downloaded file: %s", path.name)

    return saved


# ── Session Management ──────────────────────────────────────────


def list_claude_sessions(working_dir: str | None = None) -> str:
    """List recent Claude Code sessions."""
    binary = _find_binary("claude")
    if not binary:
        return "[Claude Code CLI not found]"
    try:
        result = subprocess.run(
            [binary, "sessions", "list", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=working_dir,
        )
        if result.returncode != 0:
            return "[Failed to list sessions]"

        sessions = json.loads(result.stdout) if result.stdout.strip() else []
        if not sessions:
            return "No active sessions."

        lines = ["Recent Claude Code Sessions:\n"]
        for i, s in enumerate(sessions[:10], 1):
            sid = s.get("id", s.get("session_id", "?"))
            summary = s.get("summary", s.get("name", ""))[:40]
            ts = s.get("updated_at", s.get("timestamp", ""))[:19]
            lines.append(f"  {i}. [{sid[:8]}] {summary} ({ts})")
        lines.append("\nUse /use <number> to switch session")
        return "\n".join(lines)
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        return "[Failed to list sessions]"


def pick_session(choice: str, user_id: str, working_dir: str | None = None) -> str:
    """Switch to a session by number or ID."""
    binary = _find_binary("claude")
    if not binary:
        return "[Claude Code CLI not found]"
    try:
        result = subprocess.run(
            [binary, "sessions", "list", "--output-format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=working_dir,
        )
        sessions = json.loads(result.stdout) if result.stdout.strip() else []
    except Exception:
        return "[Failed to list sessions]"

    if not sessions:
        return "No sessions available."

    target = None
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(sessions):
            target = sessions[idx]
    except ValueError:
        for s in sessions:
            sid = s.get("id", s.get("session_id", ""))
            if sid.startswith(choice):
                target = s
                break

    if not target:
        return f"Session '{choice}' not found. Use /sessions to list."

    session_id = target.get("id", target.get("session_id", ""))
    summary = target.get("summary", target.get("name", ""))[:40]
    with _sessions_lock:
        _sessions[user_id] = session_id
    _save_sessions()
    return f"Switched to session: [{session_id[:8]}] {summary}"


# ── Message Handler ─────────────────────────────────────────────


def handle_message(
    client: ILinkClient,
    msg: dict,
) -> None:
    """Handle a single incoming WeChat message (runs in thread)."""
    global _working_dir
    from_user = msg.get("from_user_id", "unknown")
    context_token = msg.get("context_token", "")

    try:
        text = client.extract_text(msg) or ""

        # Download images/files (best-effort)
        image_paths: list[Path] = []
        try:
            image_paths = _handle_images(client, msg)
        except Exception as e:
            logger.warning("Image download failed: %s", e)

        # Handle voice messages
        try:
            for item in client.extract_media(msg):
                if item["type"] == "voice" and item.get("text"):
                    text = (text + "\n" + item["text"]) if text else item["text"]
        except Exception as e:
            logger.warning("Voice extraction failed: %s", e)

        if not text.strip() and not image_paths:
            return

        # Debug: log raw item_list types
        raw_items = msg.get("item_list", [])
        item_types = [i.get("type") for i in raw_items]
        if any(t != 1 for t in item_types):
            logger.info(
                "Raw item_list types: %s, keys: %s",
                item_types,
                [list(i.keys()) for i in raw_items],
            )

        logger.info(
            "Message from %s (%d chars, %d images)",
            from_user[:16],
            len(text),
            len(image_paths),
        )

        cmd = text.strip()
        cmd_lower = cmd.lower()

        with _workdir_lock:
            working_dir = _working_dir

        # ── Special commands ──
        if cmd_lower in ("/reset", "/clear"):
            with _sessions_lock:
                _sessions.pop(from_user, None)
            _save_sessions()
            client.send_text(from_user, context_token, "Session cleared.")
            return

        if cmd_lower == "/status":
            with _sessions_lock:
                sid = _sessions.get(from_user, "")[:8] or "none"
            agent_name = AGENTS.get(_get_user_agent(from_user), {}).get("name", "?")
            client.send_text(
                from_user,
                context_token,
                f"Bridge: running\nAgent: {agent_name}\nSession: {sid}\nWorking dir: {working_dir or '(default)'}",
            )
            return

        if cmd_lower == "/sessions":
            client.send_text(
                from_user, context_token, list_claude_sessions(working_dir)
            )
            return

        if cmd_lower.startswith("/use "):
            client.send_text(
                from_user,
                context_token,
                pick_session(cmd[5:].strip(), from_user, working_dir),
            )
            return

        if cmd_lower == "/new":
            with _sessions_lock:
                _sessions.pop(from_user, None)
            _save_sessions()
            client.send_text(from_user, context_token, "New session started.")
            return

        if cmd_lower.startswith("/workdir"):
            parts = cmd.split(maxsplit=1)
            if len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    f"Current: {working_dir or '(default)'}\nUsage: /workdir /path/to/project",
                )
                return
            new_path = Path(parts[1].strip()).expanduser()
            if not new_path.is_dir():
                client.send_text(
                    from_user, context_token, f"Directory not found: {parts[1].strip()}"
                )
                return
            with _workdir_lock:
                _working_dir = str(new_path)
            client.send_text(
                from_user, context_token, f"Working directory changed to: {new_path}"
            )
            return

        if cmd_lower in ("/agent", "/agents"):
            current = _get_user_agent(from_user)
            lines = ["Available agents:\n"]
            for key, agent in AGENTS.items():
                installed = "ok" if _find_binary(agent["binary"]) else "not installed"
                marker = " <-- current" if key == current else ""
                lines.append(f"  {key}: {agent['name']} ({installed}){marker}")
            lines.append("\nUse /agent <name> to switch")
            client.send_text(from_user, context_token, "\n".join(lines))
            return

        if cmd_lower.startswith("/agent "):
            agent_key = cmd[7:].strip().lower()
            if agent_key not in AGENTS:
                client.send_text(
                    from_user,
                    context_token,
                    f"Unknown agent: {agent_key}\nAvailable: {', '.join(AGENTS)}",
                )
                return
            agent = AGENTS[agent_key]
            if not _find_binary(agent["binary"]):
                client.send_text(
                    from_user,
                    context_token,
                    f"{agent['name']} not installed ({agent['binary']} not in PATH)",
                )
                return
            _user_agent[from_user] = agent_key
            with _sessions_lock:
                _sessions.pop(from_user, None)
            _save_sessions()
            client.send_text(from_user, context_token, f"Switched to {agent['name']}")
            return

        # ── Memory commands ──
        if cmd_lower.startswith("/remember "):
            content = cmd[10:].strip()
            client.send_text(from_user, context_token, _memory.remember(content))
            return

        if cmd_lower.startswith("/forget "):
            keyword = cmd[8:].strip()
            client.send_text(from_user, context_token, _memory.forget(keyword))
            return

        if cmd_lower == "/memory":
            client.send_text(from_user, context_token, _memory.list_memories())
            return

        if cmd_lower.startswith("/search "):
            query = cmd[8:].strip()
            client.send_text(from_user, context_token, _memory.search(query))
            return

        if cmd_lower == "/log":
            client.send_text(from_user, context_token, _memory.get_today_log())
            return

        # ── Persona commands ──
        if cmd_lower == "/persona":
            p = _personas.get(from_user, "")
            client.send_text(
                from_user,
                context_token,
                f"Current persona: {p}"
                if p
                else "No persona set.\nUse /persona <description>",
            )
            return

        if cmd_lower.startswith("/persona "):
            persona_text = cmd[9:].strip()
            _personas[from_user] = persona_text
            _save_personas()
            client.send_text(from_user, context_token, f"Persona set: {persona_text}")
            return

        # ── Scheduler commands ──
        if cmd_lower.startswith("/remind "):
            parts = cmd[8:].strip().split(maxsplit=1)
            if len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    "Usage: /remind <time> <message>\nExample: /remind 17:00 Go home",
                )
                return
            client.send_text(
                from_user,
                context_token,
                _scheduler.add_reminder(from_user, parts[0], parts[1]),
            )
            return

        if cmd_lower.startswith("/every "):
            parts = cmd[7:].strip().split(maxsplit=1)
            if len(parts) < 2:
                client.send_text(
                    from_user,
                    context_token,
                    "Usage: /every <interval> <message>\nExample: /every 30m Check server",
                )
                return
            run_claude = parts[1].startswith("!")
            msg = parts[1][1:].strip() if run_claude else parts[1]
            client.send_text(
                from_user,
                context_token,
                _scheduler.add_interval(from_user, parts[0], msg, run_claude),
            )
            return

        if cmd_lower.startswith("/cron "):
            # /cron 0 9 * * 1-5 Good morning
            cron_parts = cmd[6:].strip().split(maxsplit=5)
            if len(cron_parts) < 6:
                client.send_text(
                    from_user,
                    context_token,
                    "Usage: /cron <min> <hour> <day> <mon> <wday> <message>\nExample: /cron 0 9 * * 1-5 Good morning",
                )
                return
            cron_expr = " ".join(cron_parts[:5])
            msg = cron_parts[5]
            run_claude = msg.startswith("!")
            if run_claude:
                msg = msg[1:].strip()
            client.send_text(
                from_user,
                context_token,
                _scheduler.add_cron(from_user, cron_expr, msg, run_claude),
            )
            return

        if cmd_lower == "/jobs":
            client.send_text(from_user, context_token, _scheduler.list_jobs(from_user))
            return

        if cmd_lower.startswith("/cancel "):
            job_id = cmd[8:].strip()
            client.send_text(
                from_user, context_token, _scheduler.cancel_job(from_user, job_id)
            )
            return

        if cmd_lower == "/help":
            client.send_text(
                from_user,
                context_token,
                "Session:\n"
                "  /sessions /use <n> /new /reset\n"
                "Agent:\n"
                "  /agent /agent <x> /workdir <p>\n"
                "Memory:\n"
                "  /remember <text> /forget <key>\n"
                "  /memory /search <q> /log\n"
                "Persona:\n"
                "  /persona /persona <desc>\n"
                "Schedule:\n"
                "  /remind <time> <msg>\n"
                "  /every <interval> <msg>\n"
                "  /cron <expr> <msg>\n"
                "  /jobs /cancel <id>\n"
                "Other:\n"
                "  /status /help\n"
                "\nPrefix msg with ! in /every /cron to run through Claude.\n"
                "Anything else is sent to the current agent.",
            )
            return

        # ── Forward to AI agent ──
        stop_typing = threading.Event()
        typing_thread = threading.Thread(
            target=_typing_loop,
            args=(client, from_user, context_token, stop_typing),
            daemon=True,
        )
        typing_thread.start()

        try:
            response = call_agent(text, from_user, working_dir, image_paths)
            response = md_to_plain(response)
        finally:
            stop_typing.set()
            typing_thread.join(timeout=1)

        client.send_text(from_user, context_token, response)
        logger.info("Replied to %s (%d chars)", from_user[:16], len(response))

        # Log conversation to daily memory
        try:
            _memory.log_conversation(text[:200], response[:200])
        except Exception:
            pass

    except Exception as e:
        logger.error("handle_message error: %s", e, exc_info=True)
        try:
            client.send_text(
                from_user, context_token, "[Internal error, please try again]"
            )
        except Exception:
            pass


# ── Main Bridge Loop ────────────────────────────────────────────


def run_bridge(working_dir: str | None = None) -> None:
    """Main bridge loop: poll WeChat -> call agent -> reply."""
    global _working_dir
    _working_dir = working_dir

    client = ILinkClient()

    if not client.is_logged_in:
        print("No saved login found. Starting QR code login...\n")
        client.login()

    _load_sessions()
    _load_personas()

    # Scheduler callback: send message (and optionally run Claude) when job fires
    def _on_job_fire(user_id: str, message: str, run_claude: bool) -> None:
        try:
            if run_claude:
                response = call_agent(message, user_id, _working_dir, None)
                response = md_to_plain(response)
                text = f"[Scheduled] {response}"
            else:
                text = f"[Reminder] {message}"
            if not client.send_text(user_id, "", text):
                logger.warning(
                    "Failed to deliver scheduled message to %s", user_id[:16]
                )
        except Exception as e:
            logger.error("Scheduler callback error: %s", e)

    _scheduler.set_callback(_on_job_fire)
    _scheduler.start()

    print("\n=== WeChat-Claude Code Bridge ===")
    print(f"Working directory: {working_dir or '(default)'}")
    print(f"Default agent: {AGENTS['claude']['name']}")
    print(f"Memory: {_memory.get_context()[:30] or '(empty)'}...")
    print(f"Scheduled jobs: {len(_scheduler._jobs)}")
    print("Listening for WeChat messages... (Ctrl+C to stop)\n")

    consecutive_errors = 0
    max_consecutive_errors = 10

    try:
        while True:
            try:
                messages = client.poll_messages()
                consecutive_errors = 0

                for msg in messages:
                    _executor.submit(handle_message, client, msg)

            except KeyboardInterrupt:
                raise
            except Exception as e:
                consecutive_errors += 1
                logger.error(
                    "Poll error (%d/%d): %s",
                    consecutive_errors,
                    max_consecutive_errors,
                    e,
                )
                err_str = str(e).lower()
                if "401" in err_str or "unauthorized" in err_str:
                    logger.warning("Token may have expired. Re-login with --login")

                if consecutive_errors >= max_consecutive_errors:
                    logger.critical("Too many consecutive errors, stopping.")
                    break
                time.sleep(min(2**consecutive_errors, 30))

    except KeyboardInterrupt:
        print("\nStopping bridge...")
    finally:
        _save_sessions()
        _scheduler.stop()
        _executor.shutdown(wait=False)
        client.close()
        print("Bridge stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(description="WeChat ClawBot <-> AI Agent bridge")
    parser.add_argument("--logout", action="store_true", help="Clear login credentials")
    parser.add_argument("--login", action="store_true", help="Force re-login")
    parser.add_argument(
        "--workdir", "-w", type=str, default=None, help="Working directory"
    )
    args = parser.parse_args()

    if args.logout:
        client = ILinkClient()
        client.logout()
        client.close()
        print("Logged out.")
        return

    if args.login:
        client = ILinkClient()
        client.logout()
        client.login()
        client.close()
        print("Login complete.")
        return

    run_bridge(working_dir=args.workdir)


if __name__ == "__main__":
    main()
