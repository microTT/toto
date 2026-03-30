# Codex Webhook Watch

这个目录放的是和 Codex 会话通知相关的一组脚本，避免后续 `scripts/` 目录下直接堆太多独立文件。

## 目录说明

- `codex-webhook-watch.mjs`
  - 轮询 `~/.codex/sessions/*.jsonl`
  - 识别 `task_complete` 和 `approval_needed` 事件
  - 按钉钉机器人要求发送 `markdown` 消息
- `install-codex-webhook-watch-launchd.sh`
  - 生成并安装 `launchd` 配置
  - 让 watcher 在 macOS 登录后自动常驻运行
- `uninstall-codex-webhook-watch-launchd.sh`
  - 停止并删除对应的 `launchd` 配置

## 当前支持的事件

- `task_complete`
  - Codex 一个 turn 完成时发送通知
- `approval_needed`
  - 只对“看起来真的在等人审批”的提权请求发送通知
  - 具体规则是：
    - 已在 approved prefix rules 里的命令不通知
    - 已进入 `guardian_assessment` 路径的自动审批不通知
    - 没有进入 `guardian_assessment`、并且短时间内仍未执行完成的 `require_escalated` 请求会通知

## 钉钉消息格式

脚本会直接发钉钉机器人 `markdown` 消息，消息正文里固定包含一行明文 `codex`，用于通过内容检测。

消息体大致是：

```json
{
  "msgtype": "markdown",
  "markdown": {
    "title": "Codex 任务完成",
    "text": "#### Codex 任务完成\ncodex\n..."
  },
  "at": {
    "atMobiles": [],
    "atUserIds": [],
    "isAtAll": false
  }
}
```

## 手动运行

先本地看消息体，不真正发出去：

```bash
node /Users/microTT/toto/scripts/codex-webhook-watch/codex-webhook-watch.mjs \
  --dry-run \
  --once \
  --replay
```

真实发送到钉钉：

```bash
node /Users/microTT/toto/scripts/codex-webhook-watch/codex-webhook-watch.mjs \
  --url 'https://oapi.dingtalk.com/robot/send?access_token=...' \
  --at-mobiles 150XXXXXXXX \
  --at-user-ids user123
```

## 常用参数

- `--url`
  - 钉钉机器人 webhook 地址
- `--events`
  - 逗号分隔，默认 `task_complete,approval_needed`
- `--interval`
  - 轮询间隔，默认 `1500`
- `--approval-wait`
  - 判定为“人工审批等待中”前的等待时间，默认 `2500`
- `--replay`
  - 从现有 session 文件头开始扫描
- `--dry-run`
  - 只打印消息体，不发请求
- `--once`
  - 扫描一次后退出
- `--at-mobiles`
  - 逗号分隔手机号列表
- `--at-user-ids`
  - 逗号分隔用户 ID 列表
- `--at-all`
  - 是否 @所有人

## 环境变量

也可以不在命令行里传，改用环境变量：

- `CODEX_WEBHOOK_URL`
- `CODEX_DINGTALK_AT_MOBILES`
- `CODEX_DINGTALK_AT_USER_IDS`
- `CODEX_DINGTALK_AT_ALL`
- `CODEX_WATCH_EVENTS`
- `CODEX_WATCH_INTERVAL_MS`

## launchd 常驻

安装：

```bash
CODEX_WEBHOOK_URL='https://oapi.dingtalk.com/robot/send?access_token=...' \
CODEX_DINGTALK_AT_MOBILES='150XXXXXXXX' \
CODEX_DINGTALK_AT_USER_IDS='user123' \
/Users/microTT/toto/scripts/codex-webhook-watch/install-codex-webhook-watch-launchd.sh
```

卸载：

```bash
/Users/microTT/toto/scripts/codex-webhook-watch/uninstall-codex-webhook-watch-launchd.sh
```

日志位置：

- `~/Library/Logs/codex-webhook-watch.log`
- `~/Library/Logs/codex-webhook-watch.err.log`

## 注意事项

- `launchd` 安装脚本会把 webhook 地址写入 `~/Library/LaunchAgents/com.micrott.codex-webhook-watch.plist`
- 如果你的钉钉机器人开启了“加签”，当前脚本还没有实现签名参数
- 如果升级了 Node 路径，建议重新执行安装脚本，让 `launchd` 配置更新到新的 `node` 路径
- 如果 access token 已经在聊天或日志里暴露，建议尽快轮换
