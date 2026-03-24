# 小红书发布素材

## 标题（选一个）

A: 微信直连 Claude Code，通勤路上也能写代码
B: 用微信操控电脑上的 Claude Code，已开源
C: 做了个小工具：微信发消息就能让 Claude Code 帮你干活

## 正文

微信支持了 ClawBot 之后，我做了个桥接工具，把它直接连到 Claude Code。

不需要装 OpenClaw，不需要复杂配置。装好后扫个码，就可以在微信里跟 Claude Code 聊天了。

能做什么：
- 发文字：Claude Code 直接帮你操作代码
- 发语音：自动转文字再处理
- 发图片：Claude 能看懂你截的图
- /remind 17:00 下班：到点微信提醒你
- /sessions：手机上切换电脑里正在跑的会话

技术上就是用 Python 调微信的 iLink API 做长轮询，收到消息后转发给 Claude Code CLI。整个项目 2000 行，3 个依赖，没有遥测。

GitHub 已开源：github.com/allenhuang0/WeClaude

## 标签

#ClaudeCode #微信ClawBot #AI编程 #开源 #程序员工具 #远程编程 #Claude #AI开发工具

## 配图建议

1. 架构流程图（assets/flow.png）
2. 微信聊天截图（你和 ClawBot 的对话）
3. 终端运行截图（bridge.py 启动画面）
4. /help 命令列表截图（微信里发 /help 的回复）
