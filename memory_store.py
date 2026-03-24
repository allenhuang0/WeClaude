"""Persistent memory system inspired by OpenClaw.

Stores long-term memories in MEMORY.md and daily conversation logs.
No vector DB — uses simple keyword matching for retrieval.
"""

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

MEMORY_DIR = Path.home() / ".config" / "wechat-claude-bridge" / "memory"
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
_lock = threading.Lock()


def _ensure_dir() -> None:
    """Create the memory directory if it does not exist."""
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


class MemoryStore:
    """Simple file-based memory system."""

    def __init__(self) -> None:
        _ensure_dir()

    def remember(self, content: str, category: str = "general") -> str:
        """Save a memory entry to MEMORY.md.

        Args:
            content: The fact/preference/decision to remember.
            category: Category tag (general, preference, fact, decision).

        Returns:
            Confirmation message.
        """
        _ensure_dir()
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- [{category}] {content} _(saved {timestamp})_\n"

        with _lock:
            existing = ""
            if MEMORY_FILE.exists():
                existing = MEMORY_FILE.read_text()

            if not existing:
                existing = "# Memory\n\n"

            existing += entry
            MEMORY_FILE.write_text(existing)

        return f"Remembered: {content}"

    def forget(self, keyword: str) -> str:
        """Remove memory entries matching keyword.

        Args:
            keyword: Keyword to search for in memory entries.

        Returns:
            Result message indicating how many entries were removed.
        """
        with _lock:
            if not MEMORY_FILE.exists():
                return "No memories found."

            lines = MEMORY_FILE.read_text().splitlines(keepends=True)
            original_count = len([line for line in lines if line.startswith("- ")])

            keyword_lower = keyword.lower()
            filtered = [
                line
                for line in lines
                if not (line.startswith("- ") and keyword_lower in line.lower())
            ]

            removed = original_count - len(
                [line for line in filtered if line.startswith("- ")]
            )
            if removed == 0:
                return f"No memories matching '{keyword}' found."

            MEMORY_FILE.write_text("".join(filtered))

        return f"Forgot {removed} memory entries matching '{keyword}'."

    def list_memories(self) -> str:
        """List all stored memories.

        Returns:
            Formatted string of all memories, or a message if empty.
        """
        with _lock:
            if not MEMORY_FILE.exists():
                return "No memories stored yet.\nUse /remember <content> to save."

            content = MEMORY_FILE.read_text().strip()
            if not content or content == "# Memory":
                return "No memories stored yet.\nUse /remember <content> to save."

        return content

    def get_context(self) -> str:
        """Get memory content for injection into Claude's prompt.

        Returns:
            Memory context string, or empty string if no memories.
        """
        with _lock:
            if not MEMORY_FILE.exists():
                return ""

            content = MEMORY_FILE.read_text().strip()
            if not content or content == "# Memory":
                return ""

        return f"[Persistent Memory]\n{content}\n"

    def log_conversation(self, user_msg: str, bot_reply: str) -> None:
        """Append a conversation exchange to today's daily log.

        Args:
            user_msg: The user's message (first 200 chars).
            bot_reply: The bot's reply (first 200 chars).
        """
        _ensure_dir()
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = MEMORY_DIR / f"{today}.md"
        timestamp = datetime.now().strftime("%H:%M")

        entry = (
            f"### {timestamp}\n"
            f"**User:** {user_msg[:200]}\n"
            f"**Bot:** {bot_reply[:200]}\n\n"
        )

        with _lock:
            if not log_file.exists():
                header = f"# Conversation Log — {today}\n\n"
                log_file.write_text(header + entry)
            else:
                with open(log_file, "a") as f:
                    f.write(entry)

    def get_today_log(self) -> str:
        """Get today's conversation log.

        Returns:
            Today's log content, or message if no log exists.
        """
        today = datetime.now().strftime("%Y-%m-%d")
        log_file = MEMORY_DIR / f"{today}.md"

        with _lock:
            if not log_file.exists():
                return "No conversation log for today."
            return log_file.read_text()

    def search(self, query: str) -> str:
        """Search memories and recent logs by keyword.

        Args:
            query: Search keyword.

        Returns:
            Matching entries from memory and recent logs.
        """
        query_lower = query.lower()
        results: list[str] = []

        with _lock:
            # Search MEMORY.md
            if MEMORY_FILE.exists():
                for line in MEMORY_FILE.read_text().splitlines():
                    if line.startswith("- ") and query_lower in line.lower():
                        results.append(f"[memory] {line}")

            # Search recent daily logs (last 3 days)
            for i in range(3):
                day = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
                log_file = MEMORY_DIR / f"{day}.md"
                if log_file.exists():
                    for line in log_file.read_text().splitlines():
                        if query_lower in line.lower() and line.strip():
                            results.append(f"[{day}] {line}")

        if not results:
            return f"No results for '{query}'."

        return f"Search results for '{query}':\n" + "\n".join(results[:20])
