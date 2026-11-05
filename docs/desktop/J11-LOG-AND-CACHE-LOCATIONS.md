# 日志与 WebView 缓存位置（J11 / P5）

**结论：本期采用"明确豁免 + 路径与清理说明"，不改写目录策略。**

理由：日志与 WebView 缓存由宿主运行时（Tauri/WebView2）决定，写到操作系统用户目录是**平台契约**的一部分；把它搬进 Lab 会让"正式安装后的行为"和"验收时的行为"不一致。因此记录位置、范围与清理责任，而不是改路径。

---

## 1. 位置（Windows）

| 内容 | 路径 | 决定者 |
|---|---|---|
| 应用日志 | `%LOCALAPPDATA%\com.quintessence.desktop\logs\` | `app_log_dir()`（`apps/desktop/src-tauri/src/lib.rs:62`） |
| 日志（调试覆盖） | `QUINTESSENCE_DESKTOP_LOG_DIR` 指向的绝对目录 | `logs.rs:38`；**仅 debug 构建生效** |
| WebView2 用户数据 | `%LOCALAPPDATA%\com.quintessence.desktop\EBWebView\` | WebView2 运行时（Tauri 默认） |
| 桌面偏好 | `app_local_data_dir()`（`host_settings.rs:126`） | 同上根目录 |

`com.quintessence.desktop` 即 `tauri.conf.json` 的 `identifier`。**换 identifier 会换目录**——这是升级时最容易忽略的一条。

## 2. 内容范围与私人数据

- 日志是**宿主日志**：启动、桥接错误码、模型调用诊断、锁状态。
- 日志**不含**：API Key（写入前脱敏）、私人 Persona/Memory 正文、PCS 数据库行内容。
- 日志**含**：库路径、namespace、会话 id、错误码、时间戳。**路径本身可推断用户名**，这是唯一可识别的信息。
- `logs::scope_dir` 会把日志按身份分目录；切换实例不混写。
- WebView2 目录含缓存、localStorage、IndexedDB（界面状态），**不含 PCS 业务库**（业务库在实例目录内）。

## 3. 清理

关闭应用后：

```powershell
# 只清日志（安全）
Remove-Item "$env:LOCALAPPDATA\com.quintessence.desktop\logs" -Recurse -Force

# 连同 WebView 缓存一起清（会丢界面本地状态，不丢业务库）
Remove-Item "$env:LOCALAPPDATA\com.quintessence.desktop\EBWebView" -Recurse -Force
```

清理后重启即可重建。**不要**为了"干净"去删实例目录里的 `pcs.sqlite3`、删除账本或 anchor——那三件是恢复安全的前提（见 PS06 部署说明）。

## 4. 验收方式

1. 启动应用 → 在 `app.info`（或宿主日志首行）确认日志目录。
2. 确认目录存在且含本次启动的记录。
3. 断网/关闭应用后按上面命令清理 → 重启 → 目录重建、应用正常运行。
4. 断言：日志里搜不到 API Key；搜不到 Persona 正文。

## 5. 已知限制

- 未提供"把日志重定向到任意目录"的**发布版**配置项（仅 debug 环境变量）。若将来需要，应作为独立配置项设计，而不是靠在 Lab 里改路径。
- WebView2 目录名由运行时决定，Tauri 升级可能改变其布局。
