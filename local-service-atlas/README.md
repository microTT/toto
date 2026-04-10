# Local Service Atlas

本地常驻服务导航页，默认启动在 `http://127.0.0.1:9114`。

功能：

- 自动扫描当前机器上的 TCP 监听端口
- 尝试识别 HTTP 服务并读取页面标题
- 为常用端口保存长期别名，配置保存在 `config/services.json`

启动：

```bash
cd /Users/microTT/toto/local-service-atlas
npm start
```

LaunchAgent:

- 模板文件：`launchd/com.micrott.local-service-atlas.plist`
- 目标路径：`~/Library/LaunchAgents/com.micrott.local-service-atlas.plist`
- 日志路径：`logs/launchagent.stdout.log` 和 `logs/launchagent.stderr.log`
