# C12b — Consult 工作区与可信桌面桥实施记录

> 状态：C12b 正式实施记录（Consult）。C12c Review、C13 未实施。
>
> 基线：`b646c67`（C11 已关闭；C12b-TECH 已关闭；C12a 设计稿已批准）。
>
> 日期：2026-10-24。
>
> 本文只记录 `apps/desktop/` 的正式实现、测试与 Lab 验证。所有 Persona、
> Input、Belief、Context、Attempt、Artifact 与回复正文均为合成 Lab 数据；
> 不使用正式私人实例，不调用真实外部 provider。

## 0. 一页结论

| 问题 | 结论 |
|---|---|
| Consult 是否走 C05b 正式输入登记？ | 是。可信 Python 宿主调用 `store.register_input`，前端不能指定 `input_refs` 或 `origin_kind`。 |
| 是否使用正式 `ConsultFacade`？ | 是。真实 Context、确定性查询、Interaction、Attempt、Artifact 与 Store 链路。 |
| 目的地是否由可信宿主派生？ | 是。`destination_id` 由 provider/model/manifest/policy/非密钥配置身份派生；前端不能改。 |
| 外部首次/变化是否确认？ | 是。确认记录在宿主 session 中；只有相同 `destination_id` 且有宿主记录才通过。 |
| WebView 能执行 shell/fs/http 吗？ | 不能。capability 只有 `core:default`；Rust 只暴露五个 typed commands（四个 `desktop.v1` 业务操作 + 一个本地 UI 偏好写入）。 |
| BUSY 是否排队？ | 否。第二个 consult 立即 `BUSY`，不建 interaction/input/Attempt。 |
| 单实例与数据锁？ | 有。Windows 命名互斥量 + sidecar 按 DB 路径的 OS 文件锁；第二个 sidecar 报 `DATA_LOCKED`。 |
| 打包是否完整？ | 是。PyInstaller one-folder 30 文件随 `bundle.resources` 部署；安装后逐文件 30/30 一致。 |
| 资源是否达暂定目标？ | 冷启动达标；空闲与交互内存未达 350/200/450 MiB 目标，已如实记录。 |
| L2/WebView2 缺失？ | UNVERIFIED。没有干净 Windows 访客机或缺失 runtime 的环境。 |
| 是否进入 C12c/C13？ | 没有。Review 只是占位说明，没有候选、批准或拒绝入口。 |

## 1. 范围

### 1.1 实施

- `apps/desktop/`：TypeScript/Vite Consult UI、Tauri 2 Rust 可信宿主、
  Python PCS sidecar、测试与验证脚本。
- `app.info`、`app.confirm_destination`、`consult.interact`、`bridge.ping`。
- 单窗口、固定 `Consult` / `Review` 工作区导航；Review 仅为 C12c 未实现
  占位。
- 一份私有 `desktop.host.v1` 宿主配置：namespace、host、access、隐私、
  用途、保留、策略、确定性查询计划、目的地与模型 manifest。
- 来源摘要与抽屉、目的地常驻、D3 外部确认、D4 中性等待、D7 Consult 不
  直达 Review。
- one-folder sidecar、NSIS 安装、安装后 L1 验证、资源测量、私有数据扫描。

### 1.2 非目标

- 不实现 C12c Review、候选枚举/差异/批准/拒绝、C13。
- 不实现 DSH Work/Memory/Skill、真实模型研究、Persona/Memory 扩展、
  语义检索或 ACL 平台。
- 不新增 PCS 契约、迁移、错误码；不修改 C06–C11 语义。
- 不把 Spike 探针、测试夹具、私人数据或凭据写入生产或安装包。
- 不 push，不进入 main。

## 2. 架构

```text
WebView2（TypeScript/Vite，无 shell/fs/http 能力）
  -> Tauri invoke（四个 `desktop.v1` 操作 + 一个本地 UI 偏好仓库命令）
Rust 可信宿主（单实例、sidecar 生命周期、framing、超时、session 校验）
  -> fixed Python PCS sidecar（PyInstaller one-folder）
     -> desktop.v1 stdio（4 字节大端长度 + UTF-8 JSON）
     -> DesktopRuntime（正式 PCS 服务）
        -> ConsultFacade
        -> CognitionStore（仓库外 SQLite）
```

- 没有 localhost 监听；应用运行期协议只走 stdin/stdout。
- WebView 只加载本地构建资产；CSP 禁止远程脚本、对象与 frame 嵌入。
- Rust 与 Python 宿主派生/核对 HostAccess、目的地、model authorization、
  policy；C09 发送门仍是最终授权检查。

### 2.1 方法白名单

| 方法 | 行为 |
|---|---|
| `app.info` | app/protocol/session、namespace/host/operator、宿主就绪、目的地 descriptor、工作区能力、limits、方法/保留方法、PCS 版本与迁移 |
| `app.confirm_destination` | 记录当前 session 对当前 `destination_id` 的确认；只表达用户意图，不是授权凭据 |
| `consult.interact` | 校验 session、问题、目的地确认；登记输入；调用正式 `ConsultFacade` |
| `bridge.ping` | 诊断响应、pid、协议、uptime、frozen、数据锁状态 |

保留但未实现：`consult.result`、`consult.history`、`consult.resume`、
`consult.cancel`、`review.generate`、`review.commit`、`review.reject`、
`review.result`。它们返回 `RESERVED_NOT_IMPLEMENTED`，不伪造结果。

禁止：`shell`/`exec`/`fs`/`db` 通用能力；WebView 无系统执行权限。

## 3. 可信输入与授权

### 3.1 前端请求

`consult.interact` 只接受三个字段：

```json
{"session_id":"...","question":"...","confirmed_destination_id":"..."}
```

`AUTHORITY_FIELD_REJECTED` 在创建 input/Context/Interaction/Attempt 之前
拒绝前端提供的隐私、用途、授权、provider、model、endpoint、namespace、
host、policy、query plan、input refs、interaction/context id、确认布尔值
和目的地对象等字段。

### 3.2 正式输入登记

可信 Python 宿主：

1. 校验问题非空、长度上限、无 NUL、UTF-8 可编码；
2. 调用 `store.register_input`，`origin_kind="user_statement"`，
   privacy/retention 来自私有 HostConfig；
3. 生成精确 `input_refs`；
4. 构造 `ConsultRequest`，`query_plan`/`policy_ref` 来自宿主配置；
5. 调用正式 `ConsultFacade.interact`。

登记失败：不创建 interaction，返回 `INPUT_REGISTRATION_FAILED`，UI 显示
明确失败。登记成功后 Consult 失败：输入行保留，interaction/Context/
Attempt 按 C11 生命周期写 failed/incomplete/stale，不伪装成功。

### 3.3 前端不可指定

`input_refs`、`origin_kind`、privacy、retention、namespace、
host_instance、policy_ref、query_plan、provider、model、endpoint、
manifest_hash、destination、confirmation、input/interaction/context id。

## 4. 目的地与确认

### 4.1 描述

`app.info.destination` 返回：

- `destination_id`：由 provider/model/model protocol/manifest hash/
  policy ref/非密钥配置指纹稳定派生（`sha256:` 前缀）；
- provider、model、external/local、`confirmation_required`、
  manifest_hash、policy_ref。

不返回密钥、base_url 或私密 endpoint。前端不能改 provider/model。

### 4.2 确认语义

- `confirmed_destination_id` 只表达用户操作意图。
- Python 宿主维护当前 `session_id` 的目的地确认记录；记录绑定 session +
  destination_id。
- `consult.interact` 同时要求：ID 等于当前 destination_id、宿主存在该
  session 的有效记录、本地目的地不需要确认。
- 伪造相同 ID 但没有宿主记录 → `CONFIRMATION_REQUIRED`。
- 外部首次、目的地变化、session 重建 → 需要重新确认。
- Rust 在转发前也会重新读取 `app.info` 并核对 session/destination；最终
  检查仍由 Python 宿主和 C09 发送门执行。

## 5. UI

### 5.1 外壳

- 单窗口；固定 `Consult` / `Review` 工作区导航；切换整页。
- 深色、安静、克制的品牌界面：黑色/炭黑背景、柔和白色文字、来自品牌图标
  的红色强调；红色只用于选中、发送与关键交互，不铺满界面。
- 顶栏常驻：QUINTESSENCE 原始品牌图标与名称、工作区导航、连接状态、当前
  目的地、设置入口。
- 会话主体是消息区：用户消息右对齐，AI 回复左对齐并显示可配置的对话显示
  名；每次单轮 Consult 只展示当前一轮。
- 底部是固定输入区：多行输入、发送按钮、发送目的地、来源与详情入口；主
  操作始终在首屏可见。
- namespace、host_instance、operator、retention、来源范围、Attempt/Context
  引用等技术信息移入右侧抽屉或设置页，不占据首屏。
- 来源抽屉为覆盖层，默认折叠；compact 下为全屏 sheet。

### 5.2 品牌与 AI 显示名

- 软件品牌统一为 `QUINTESSENCE`，窗口标题为 `QUINTESSENCE · Consult`。
- AI 对话显示名与软件品牌分离。默认中性值为“助手”；私有 HostConfig 可在
  `app.ai_display_name` 提供默认值；用户可在设置页修改并持久化到应用配置
  目录（仓库外、安装包外、PCS 数据实例外）。
- 修改显示名只影响界面文本，不改变 PCS namespace、来源身份、授权、Persona
  或目的地；显示名按纯文本处理，不进入 HTML/脚本。
- 桌面本地命令 `set_ai_display_name` 只写 UI 偏好文件；它不进入 PCS 契约，
  也不改变 `desktop.v1` 的业务方法白名单。
- 负责人提供的原始品牌图标按原比例缩放生成安装图标、任务栏图标与顶栏/
  空状态图；不重绘、不裁切。原始素材不入公开仓库，构建时从仓库外提供；
  对外分发权属仍需负责人确认。

### 5.3 状态

| 状态 | 展示与动作 |
|---|---|
| 输入/空状态 | 品牌图标、AI 问候、单轮说明；底部输入与发送；宿主未就绪时禁用 |
| 外部确认 | 输入区上方确认条；返回检查 / 确认发送 |
| 处理中 | 用户消息 + AI“正在思考…”；无取消/重试/子阶段 |
| answered | AI 消息正文、材料说明、来源摘要、复制回答、新提问 |
| waiting | AI 消息“等待（暂无完成回答）”；等待说明不冒充回答；只保留新提问 |
| 材料/查询说明 | 按 `status` + `material_status` 公开组合显示“可能不完整”“没有可披露结果”“不可用”；不是新状态 |
| failed/incomplete/stale | AI 消息中的原因标签、折叠技术详情 `phase:code`、下一步新提问 |
| unknown | 系统卡固定“结果未知”警告；不自动重发、不读本地缓存冒充结果 |
| busy | 系统卡明确未排队、未创建新 interaction；不伪造第一个结果 |
| not-ready | 系统卡说明宿主配置/授权/数据锁原因；重新连接；输入禁用 |

- 每次咨询是独立的一轮；发送新问题会替换当前消息区，不宣称多轮历史、恢复
  或上下文继承。
- Consult 完成区没有 Review 直达按钮，也没有保存候选/提交 Belief。
- Review 工作区只显示 C12c 未实现说明，没有可执行候选/批准/拒绝；Review
  时隐藏 Consult 输入区。
- 不使用 localStorage/IndexedDB/sessionStorage；刷新后显示无法恢复提示。
- 来源抽屉只显示正式公共字段：宿主登记的输入 ref、查询产物 ref、
  `query_source_refs`、回复产物 ref、ref/version/状态。完整 Context 来源、
  Attempt 明细与 Prompt 标注“暂不可用 / C12c 缺口”，不读 DB 拼装。

### 5.4 尺寸与输入法

- 窗口验收尺寸：880×560、1280×800、1600×1000（外框）。
- 高 DPI 200%、880×560 逻辑尺寸下截图与 IME 组合输入证据见 Lab。
- 组合输入期间 Ctrl+Enter 不触发发送；组合结束后才允许发送。
- 中文、长 ID、来源行不会横向滚动；主操作始终可见。

## 6. 单实例与数据锁

- Windows per-user 命名互斥量 `Local\QUINTESSENCE.Desktop.Consult.v1`：
  第二个 app 只聚焦第一个窗口并退出，setup 不执行，因此不启动第二个
  sidecar。
- sidecar 在打开 SQLite 前按数据库路径获取 OS 文件锁（PID/owner 元数据；
  句柄随进程退出释放）。
- 第二个 sidecar 不打开写连接，返回 `DATA_LOCKED`；stale 锁文件不阻止
  重启。
- 释放顺序与获取顺序相反；没有 app/sidecar 循环等待。
- 这是桌面实例对单数据实例的写协调，不是 PCS 全系统永久独占；未来其他
  受授权组件接入同一实例需要显式租约/协调路径。

## 7. 打包与部署

- sidecar：PyInstaller 6.16.0 one-folder，30 文件（29 `_internal`、
  18 DLL/pyd、10 migrations）。
- `externalBin` 不承载 one-folder；完整目录通过 `bundle.resources` 部署到
  `resources/sidecar/pcs-bridge`。
- 品牌图标由负责人提供的原始素材按比例缩放生成（不重绘、不裁切）；素材
  只存在于仓库外 Lab，生成物写入忽略目录。公开仓库不包含该二进制素材；
  对外分发权属需负责人确认。软件产品名为 `QUINTESSENCE`。
- 构建时通过 Tauri `--config` 注入 Lab 绝对资源路径，源码配置不写开发机
  绝对路径。
- 安装后逐文件清单比对 30/30（字节 + SHA256）；安装目录 32 文件。
- 无 external-probe、无 `.py`/`.pdb`/SQLite/日志/node_modules 进入安装目录。
- Rust release 构建保留 `--remap-path-prefix`（Cargo home、用户目录、
  仓库、Lab）；扫描只忽略官方 CPython/PyInstaller 上游构建机路径的精确
  字面量。
- 未签名开发包；代码签名、SmartScreen、更新机制与 MSI/离线安装变体不在
  C12b 范围内。
- L2 = UNVERIFIED；WebView2 缺失场景 = UNVERIFIED；离线安装 =
  UNVERIFIED。L1 通过不等于 L2。

## 8. 验证

### 8.1 自动化

| 套件 | 数量 | 结果 |
|---|---:|---|
| Python 全量（含新增 desktop bridge tests） | 909 | OK |
| 其中 desktop bridge tests | 65 | OK（2 项 packaged 测试默认 skip，构建脚本单独执行） |
| Rust host（9 unit + 5 integration，`--features host-tests`） | 14 | OK |
| Rust host（默认 `cargo test`） | 9 | OK |
| 前端 Node tests | 23 | OK |
| `pnpm build` | — | OK |

覆盖：帧分块/粘连/截断/0/超限/非法 JSON/UTF-8/重复 id；BUSY 不排队；
stdout 纯协议；16+ 权威字段拒绝；输入登记成功/失败生命周期；目的地确认
正负例、session 重建与目的地变化；真实 Context/Interaction/Attempt/
Artifact；数据锁双进程；单实例；安装包路径/清单/扫描；高 DPI 与 IME。

### 8.2 安装后证据（Lab）

- 安装：NSIS silent，安装后 32 文件 / 24,153,382 字节；sidecar 30/30
  清单一致；卸载退出码 0，安装目录删除，Lab DB 保留。
- Consult answered 三尺寸截图 + 来源抽屉；settings（AI 显示名）、waiting/
  failed/incomplete/confirm/review/not-ready 各一张。
- 真实 synthetic consult：1 interaction / 1 input / 2 Context / 2 Attempt
  / 2 Artifact；reply artifact attempt_ref 与最终 attempt 对齐。
- 输入登记：`origin_kind=user_statement`、`privacy_class=private`、
  `retention_policy=standard`。
- 目的地确认：无宿主记录拒绝；确认后通过；重启 session 失效；换目的地
  失效；本地不要求确认。
- BUSY/framing/unknown：Python/Rust 测试；unknown 不自动重发。
- 单实例：第二个 app 退出码 0，app 进程 1、sidecar 进程 1。
- 数据锁：第二个 sidecar `DATA_LOCKED`，consult 也 `DATA_LOCKED`。
- 高 DPI：dpr=2，逻辑 880×560 下 viewport 872×541；IME 组合期间 Ctrl+
  Enter 未发送（`imeGuard=true`）。
- 私有扫描：installer、安装目录、app exe、sidecar 全部 0 命中。
- 注入的合成私有/API sentinel：0 命中。

### 8.3 资源（证据驱动）

工具：PowerShell `Get-Process` 的 `WorkingSet64` / `PrivateMemorySize64`；
按 app 进程树求和，并补入本应用 WebView2 用户数据目录对应的进程；每档
7 次启动/运行（7 次均有效）；空闲取每次 5 个样本的中位数。

| 档位 | Working set (MiB) | Private (MiB) | 备注 |
|---|---:|---:|---|
| 冷启动（进程启动到 UI 输入可用） | — | — | 中位 1,498.6，区间 1,301.0–1,698.4 ms |
| full idle（无 CDP 参数，5 次） | 454.26（444.88–455.88） | 232.33（231.43–287.77） | 主要参考 |
| full idle（CDP 测试参数，7 次） | 455.61（453.77–457.30） | 233.43（231.46–287.98） | 仅测试仪器 |
| interaction peak（CDP，7 次） | 486.96（474.17–493.87） | 266.11（250.38–313.40） | 1,500 ms/代模型延迟 |
| shell idle（无 CDP） | 415.27 | — | app + WebView2 |
| WebView2 idle（CDP） | 390.66 | 204.25 | 仅 WebView2 进程 |
| sidecar idle（CDP） | 32.03 | 21.43 | Python sidecar |

Spike 基线（`docs/design/C12b-TECH-SPIKE.md` §8.3）：

- full idle 424.57 WS / 207.95 private；
- interaction peak 428.29 WS / 266.70 private；
- shell idle 385.68 WS / 185.34 private。

比较：

- 空闲 WS +7.1%，private +11.7%（相对 Spike full idle）。
- 交互峰值 WS +13.7%，private 与 Spike 基本持平（266.11 对 266.70）。
- 冷启动 1,499 ms 高于 Spike self-check 的 1,208 ms（测量路径和暗色 UI
  渲染不完全相同，只作为同机参考），但仍低于 2.5 s 暂定目标。

暂定目标：

| 目标 | 目标值 | 实测 | 结论 |
|---|---:|---:|---|
| 冷启动 | ≤ 2,500 ms | 1,498.6 ms 中位 | PASS |
| full idle WS | ≤ 350 MiB | 454.26 MiB | 未达 |
| full idle Private | ≤ 200 MiB | 232.33 MiB | 未达 |
| interaction peak WS | ≤ 450 MiB | 486.96 MiB | 未达 |

未达原因与判断：

1. WebView2 多进程基线本身约 390.66 MiB WS / 204.25 MiB private
   （6 个 `msedgewebview2` 进程），是内存主项；app 本体约 26 MiB、
   sidecar 约 32 MiB。
2. Spike 基线已是 424.57 MiB；350 MiB 的暂定目标低于已实测的
   WebView2 基线，并非本次实现造成的异常增长。
3. 暗色品牌界面与消息式布局比 Spike 的调试面板包含更多 DOM/CSS；默认窗口
   1280×800 也比 Spike 的 1120×820 像素更多。
4. 交互峰值增加主要来自回复渲染与合成查询结果，没有出现持续增长或崩溃。
5. 未为降低内存关闭 CSP、发送门、capability、沙箱或移除安全检查。

已采用：release + LTO + strip + `opt-level=s`；最小 JS bundle（约
39.2 KiB）；单 WebView；无远程页面/字体/脚本；关闭 devtools/远程内容
（远程调试只在验证环境通过环境变量开启）；sidecar 依赖裁剪（30 文件）；
无额外 UI 框架；来源抽屉与设置页按需构建。

建议预算：按当前 WebView2 版本，full idle WS 约 450–465 MiB、private 约
230–290 MiB、交互峰值 WS 约 485–495 MiB 更接近可实现区间；若必须达到
350/200，需要更换 WebView 运行时或操作系统级内存策略，不属于 C12b 的
安全检查可交换项。

## 9. 已知限制与缺口

- C12c Review：候选枚举/差异/批准/拒绝/重新许可发放均未实现。
- Consult 的 `consult.result`/history/resume/cancel 未实现；刷新/重启后
  无法恢复结果，不自动重发。
- Attempt 明细与完整 Context 来源没有正式公共读取接口；UI 标注缺口，
  不直接读 DB。
- L2、WebView2 缺失、离线安装 UNVERIFIED。
- 开发包未签名；SmartScreen/企业策略可能拦截。
- 生产宿主配置、模型授权发放、密钥注入不在本卡实现；由可信宿主侧
  提供。
- 真实外部 provider 未在测试中调用；Lab 使用无 socket 的脚本化传输，
  C09 发送门仍执行。
- 单实例/数据锁是桌面实例级写协调，不是 PCS 全局永久独占。

## 10. 复现（Lab，仓库外）

```text
<repo> = 正式仓库
<lab>  = 仓库外 Lab 构建根目录

python -m venv <lab>/build/c12b/venv
<lab>/build/c12b/venv/Scripts/python -m pip install pyinstaller==6.16.0
pnpm --dir <repo>/apps/desktop install

powershell -File <repo>/apps/desktop/scripts/build_sidecar.ps1
powershell -File <repo>/apps/desktop/scripts/build_app.ps1
powershell -File <repo>/apps/desktop/scripts/verify_install.ps1
powershell -File <repo>/apps/desktop/scripts/measure.ps1
powershell -File <repo>/apps/desktop/scripts/measure_idle_nocdp.ps1
powershell -File <repo>/apps/desktop/scripts/l2_probe.ps1
powershell -File <repo>/apps/desktop/scripts/detect_webview2.ps1
```

安装后截图、Store 证据、资源原始采样、扫描结果在 `<lab>/build/c12b/`
下的 `evidence/`，不进入仓库。

## 11. 明确未做的事

- 没有实现 C12c Review 或 C13。
- 没有新增 PCS 契约、迁移或错误码。
- 没有修改 C06–C11 语义。
- 没有使用正式私人实例、真实 provider 或私人 Persona/Memory 正文。
- 没有把 DSH 源码复制进本仓库。
- 没有 push，没有进入 main。
