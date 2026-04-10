# Codex Session Viewer

本目录提供一个独立的本地 Web 工具，用来浏览 `~/.codex/sessions/*.jsonl` 里的 Codex 会话。

重点能力：

- 按 `日期 > session > turn` 分层查看
- 重点展示每轮交互的 prompt 组成
- 区分 `base instructions`、`developer/runtime 注入`、`memory 注入`、`user instructions`、`human prompt`
- 结构化展示 `Skill Trace`，区分 skill 是否命中、命中原因，以及是否实际读取了 `SKILL.md`
- 展示 `turn_context`、token 使用、tool calls、tool outputs、assistant messages、raw records
- 提供一个“Approx Full Prompt”视图，尽可能接近模型在 turn 开始时看到的输入

## 运行

```bash
cd /Users/microTT/toto/codex-session-viewer
npm start
```

默认监听 `http://127.0.0.1:59111`。

可选环境变量：

- `PORT`: 自定义端口
- `HOST`: 自定义监听地址，默认 `127.0.0.1`
- `SESSIONS_ROOT`: 自定义 session 根目录，默认 `~/.codex/sessions`

## launchd 常驻

如果要让服务在 macOS 上常驻，并在登录后自动拉起：

```bash
cd /Users/microTT/toto/codex-session-viewer
./install-launchd.sh
```

默认会安装为用户级 LaunchAgent：

- label: `com.micrott.codex-session-viewer`
- plist: `~/Library/LaunchAgents/com.micrott.codex-session-viewer.plist`
- stdout log: `/Users/microTT/toto/codex-session-viewer/logs/launchagent.stdout.log`
- stderr log: `/Users/microTT/toto/codex-session-viewer/logs/launchagent.stderr.log`

可选参数：

- `--node-bin <path>`: 指定 Node 路径
- `--host <host>`: 覆盖监听地址
- `--port <port>`: 覆盖监听端口
- `--sessions-root <path>`: 覆盖 session 根目录
- `--dry-run`: 只打印生成的 plist，不安装

卸载：

```bash
cd /Users/microTT/toto/codex-session-viewer
./uninstall-launchd.sh
```

## 说明

这里展示的是“基于本地 session 文件可复原的 prompt 结构”，不是底层 HTTP 请求体的逐字节回放。
原因是：

- 真正的网络请求受 TLS 保护
- 本地 `sessions/*.jsonl` 已经包含了足够多的上下文信息
- 某些内部序列化细节并不会原样落盘

因此工具里使用了 `Approx Full Prompt` 这个命名，而不是宣称这是唯一精确的原始请求体。
