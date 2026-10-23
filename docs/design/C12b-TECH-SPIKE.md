# C12b-TECH — Tauri 壳、PCS Sidecar 与完整打包技术验证

> 状态：技术验证报告（Spike），**不是** C12b 正式 Consult UI 实施，也**不是** C12b/C12c 的编码批准。
>
> 基线：`b6d01f0`（C11、C12a 已关闭；全量 844 tests / OK；gate 175 files）。
>
> 日期：2026-10-23。
>
> 本文只记录仓库外的 Lab Spike。正式仓库只新增本文件并更新 `tools/source-files.json`；不新增 PCS 契约、迁移、错误码或业务源码。
>
> 路径约定：`<Lab>` 是仓库外部的 Lab 根目录（`QUINTESSENCE-Lab`），`<spike>` 是 `<Lab>/c12b-tech-spike`，`<repo>` 是正式 QUINTESSENCE 仓库，`<install>` 是一次实际安装的目录。本文不写开发机绝对路径、用户目录或安装机路径。
>
> 数据约定：全部 Persona、Input、Belief、Context、Attempt、Artifact 和回复正文均为**合成 Lab 数据**；不使用正式私人实例，不调用真实外部 provider，不写入公开仓库。

## 0. 一页结论

### 0.1 结论摘要

| 问题 | 结论 | 主要证据 |
|---|---|---|
| Tauri 2 + WebView2 壳可用吗？ | **L0 与 L1 可用**；L2 未验证 | `pnpm tauri dev` 链路、安装包、安装后 self-check、UI 截图 |
| PyInstaller one-folder sidecar 完整吗？ | **完整**，30 个文件随安装包部署 | 安装后 30/30 文件、字节、SHA256 与构建清单完全一致 |
| `externalBin` 能承载 one-folder 吗？ | **不能**；它只部署单个可执行文件 | 探针位于安装根目录；完整 sidecar 位于 `resources\sidecar\pcs-bridge` |
| 安装后是否依赖开发机 Python？ | **L1 不依赖**；以 stripped PATH + bogus Python 变量启动通过 | 安装后 self-check、`frozen=true`、`ConsultFacade` 实际运行 |
| 第二个 consult 会排队吗？ | **不会**；立即 `BUSY`，不产生第二个 interaction/Attempt | 延迟模型阻塞测试、计数断言、ping 仍响应 |
| 帧处理是否把一次 read 当完整消息？ | **不假设**；分块、粘连、截断、非法长度、非法 JSON/UTF-8 均有测试 | 13 个 framing 测试 |
| P3 是真实 ConsultFacade 吗？ | **是**；真实 Context/Interaction/Attempt/Artifact/Store | Store 交叉核对、2 个 ok Attempt、2 个 Artifact |
| 前端能伪造授权/目的地吗？ | **不能**；16 个权威字段被拒绝，计数不变 | 授权负例、D3 绑定测试 |
| 安装包含私人数据/凭据/DB 吗？ | **不含**（已扫描） | sidecar、app exe、installer、安装目录四类扫描 |
| 完成 C12b UI 了吗？ | **没有**；本卡禁止 | 只有 Lab 技术壳，无正式 Consult/Review UI |

### 0.2 验证等级

| 等级 | 本 Spike 结论 | 依据 |
|---|---|---|
| L0 开发环境可运行 | **PASS** | `pnpm tauri dev`：Vite 1420 监听、debug app 进程、sidecar 进程、默认 Lab DB 25 张表 |
| L1 已打包可运行 | **PASS** | NSIS 静默安装、安装后 self-check、真实 `consult.interact`、Store 证据、卸载 |
| L2 干净系统已验证 | **UNVERIFIED** | 只有 Windows 11 Home 开发机；没有 Windows Sandbox、VM 或独立机器 |
| WebView2 缺失场景 | **UNVERIFIED** | 机器已安装 155.0.4283.62；无法在干净环境卸载/缺失测试 |
| 干净 Windows 无全局 Python | **UNVERIFIED** | 同上；L1 只证明安装后不**使用**开发机 Python |

> L0/L1 不写成 L2。没有用“开发机已有 WebView2”证明缺失兼容性。

### 0.3 最重要技术发现

1. **`externalBin` 是单文件槽位。** Tauri 把 `external-probe.exe` 放在安装根目录；不能把整个 `pcs-bridge` one-folder 放进去。完整 sidecar 用 `bundle.resources` 部署到 `resources\sidecar\pcs-bridge`。
2. **Windows 上 `resource_dir()` 实际返回安装根目录，不是 `<install>\resources`。** Rust 同时尝试 `<resource_dir>\resources\sidecar\pcs-bridge` 和 `<resource_dir>\sidecar\pcs-bridge`；实际解析到 `<install>\resources\sidecar\pcs-bridge`，且断言位于安装目录和 `resources` 下。
3. **one-folder 内容必须整体部署。** 只复制 `pcs-bridge.exe` 会丢失 `python312.dll`、`_internal`、SQL migrations；本 Spike 用安装后清单逐文件校验（30/30，含字节和 SHA256）。
4. **Rust release 二进制会嵌入依赖的 Cargo registry 源码路径。** 第一版 release exe 被扫描出这类路径；最终构建加入 `--remap-path-prefix`，重扫为 0。
5. **BUSY 与真实 Facade 可以同时成立。** 单一执行槽 + 专用 SQLite 工作线程既保证 SQLite 线程亲和性，也让第二个 consult 在 reader 线程立即得到 `BUSY`，同时 ping 仍可响应。

## 1. 环境、工具链与构建材料

### 1.1 实测环境

| 项 | 值 |
|---|---|
| OS | Windows 11 Home（中文），10.0.26100，x64 |
| CPU / 架构 | x86_64（MSVC 目标） |
| Node | v24.19.0 |
| pnpm | 11.28.5 |
| npm | 11.17.0 |
| Rust | rustc 1.99.1（2026-10-13） |
| Cargo | 1.99.1（2026-08-27） |
| rustup | 1.29.1 |
| Tauri CLI | 2.12.4 |
| Tauri crate | 2.12.5 |
| wry / tao | 0.57.1 / 0.37.2 |
| Python | 3.12.10 |
| PyInstaller | 6.16.0 |
| NSIS | 3.13（Lab 本地缓存，仅构建期） |
| WebView2 Runtime | 155.0.4283.62 |
| PCS 源码基线 | `b6d01f0` |

WebView2 检测来自 HKLM 的 EdgeUpdate 客户端键，并额外核对运行时目录中的 `msedgewebview2.exe` 文件版本；两者一致。

### 1.2 构建命令（Lab）

```powershell
# 在 <spike>
python -m venv .venv
.venv\Scripts\python.exe -m pip install pyinstaller==6.16.0 pillow

# one-folder sidecar
powershell -File scripts\build_sidecar.ps1

# 前端与 Tauri 安装包
pnpm install
pnpm build
powershell -File scripts\build_app.ps1

# 安装、安装后 self-check、Store 证据、卸载
powershell -File scripts\verify_install.ps1 `
  -Installer "artifacts\QUINTESSENCE C12b Tech Spike_0.1.0_x64-setup.exe"
```

补充构建说明：

- `scripts/prefetch_crates.py` 只用于本机网络缓慢时并行填充 Cargo 缓存；它不改变依赖解析，也不进入安装包。
- NSIS 3.13 通过 `bundle.useLocalToolsDir=true` 放在 `<spike>/src-tauri/target/.tauri/NSIS`，避免构建时下载超时；它不进入安装包。
- 前端锁定：`pnpm-lock.yaml`；Rust 锁定：`src-tauri/Cargo.lock`；Python 构建锁定：PyInstaller 6.16.0 与 PCS 源码 `b6d01f0`。

### 1.3 Lab 材料清单

保留在 `<spike>`，**不提交仓库**：

| 材料 | 位置（相对 `<spike>`） |
|---|---|
| Sidecar 源码 | `sidecar/framing.py`、`host_profile.py`、`lab_runtime.py`、`pcs_bridge.py` |
| Bridge/协议测试 | `tests/harness.py`、`test_framing.py`、`test_busy.py`、`test_authorization.py`、`test_e2e_consult.py`、`test_packaged.py` |
| 前端 | `src/main.ts`、`src/style.css`、`index.html`、`vite.config.ts`、`tsconfig.json` |
| Rust 宿主 | `src-tauri/src/lib.rs`、`sidecar.rs`、`paths.rs`、`commands.rs`、`selfcheck.rs`、`src-tauri/tests/host.rs` |
| 测试用 child | `src-tauri/src/bin/fake_sidecar.rs`（仅 `host-tests` feature 构建，不进入安装包） |
| Tauri 配置 | `src-tauri/tauri.conf.json`、`src-tauri/capabilities/default.json` |
| 构建/验证脚本 | `scripts/build_sidecar.ps1`、`build_app.ps1`、`verify_install.ps1`、`measure.ps1`、`scan_private.ps1`、`l0_dev_probe.ps1`、`l2_probe.ps1`、`detect_webview2.ps1` |
| Sidecar 构建产物 | `sidecar/dist/pcs-bridge/`（含 `_internal`） |
| 安装包 | `artifacts/QUINTESSENCE C12b Tech Spike_0.1.0_x64-setup.exe` |
| 原始证据 | `evidence/*.json`、`evidence/*.log`、`evidence/installed-tree.txt`、`evidence/ui-screenshot.png` |
| 本地工具缓存 | `build/`、`src-tauri/target/.tauri/`、`.venv/`、`node_modules/` |

### 1.4 校验和

| 产物 | SHA256 | 字节 |
|---|---|---|
| `sidecar/dist/pcs-bridge/pcs-bridge.exe` | `368fb896dc2c2610fb3067fee37c0890a7b850635a053915cc0ceb6f0c831adf` | 2,380,634 |
| 安装包 `QUINTESSENCE C12b Tech Spike_0.1.0_x64-setup.exe` | `a2004801fea3b87d3a2ec13350cf988e8266eff1bb2fcd181cae3dd8aa1437f0` | 8,661,013 |
| 安装后 app exe | `de44dc15cfb41f21bb759652331cdc393bd1b68d6f2528099f863a6f5d5bbcbf` | 3,227,648 |
| 构建期未打 bundle 标记的 app exe | `1dbffa9a39a09f1fd34bdee85f358bf7ed303e9626863ad2ef1731338c07b2cc` | 3,227,648 |
| PCS 源码 | `b6d01f069adedcad26f2b08c7244173f8c7e7e2d` | — |

安装后 app exe 与构建期 app exe 的差异来自 Tauri 在 bundle 阶段写入的 bundle 类型信息；安装后哈希用于安装验证。

## 2. 架构与进程

### 2.1 进程图

```text
┌────────────────────────────────────────────────────────────┐
│ WebView2 (Vite/TypeScript)                                 │
│  - 只调用 app_info / bridge_ping / consult_interact        │
│  - 不 import PCS，不读 SQLite，不执行 SQL，不持有 shell    │
└───────────────────────────┬────────────────────────────────┘
                            │ Tauri invoke（typed commands）
┌───────────────────────────▼────────────────────────────────┐
│ Tauri 2 Rust 可信宿主（单窗口）                             │
│  - resource_dir() -> <install>/resources/sidecar/pcs-bridge│
│  - spawn pcs-bridge.exe --db <Lab SQLite>                  │
│  - current_dir = sidecar 目录                              │
│  - 4-byte BE length + UTF-8 JSON over stdin/stdout         │
│  - pending map / 120s deadline / crash-timeout 结果未知     │
└───────────────────────────┬────────────────────────────────┘
                            │ stdio framed desktop.v1
┌───────────────────────────▼────────────────────────────────┐
│ PyInstaller one-folder Python sidecar                      │
│  - 最小 reader loop + 单一 consult 执行槽                  │
│  - 专用 DB 线程持有 sqlite3 连接                           │
│  - bridge adapter -> 正式 ConsultFacade                    │
│  - ContextService / CognitionQueryService / DirectHttpModel│
│  - CognitionStore -> Lab SQLite                            │
└───────────────────────────┬────────────────────────────────┘
                            │
                  ┌─────────▼─────────┐
                  │ Lab SQLite（合成） │
                  └───────────────────┘
```

没有 localhost 监听；没有远程页面；没有通用 shell/文件/HTTP 能力。安装器的 WebView2 下载引导是安装期行为，不是应用运行期监听。

### 2.2 可信边界

| 边界 | 实现 |
|---|---|
| WebView 无 shell | `capabilities/default.json` 只给 `core:default`；没有 shell/fs/http/opener/process 插件 |
| WebView 不读 PCS/DB | 前端只调用三个 Rust commands；PCS 和 SQLite 在 Python sidecar |
| 目的地由宿主提供 | sidecar 常量 manifest + store 中的 active authorization；请求字段不能改变 |
| 单一执行槽 | sidecar 的 `consult` 锁 + reader 线程 + 一个 DB worker；第二个请求立即 `BUSY` |
| 超时/崩溃结果未知 | Rust 终止 child，pending 请求返回 `result_unknown=true`；不自动重发 |
| 外部文本纯文本 | UI 用 `textContent`；不把回复注入 HTML/Markdown/脚本 |
| 无默认网络监听 | 协议只走 stdin/stdout；`pcs-bridge` 不 bind 端口；模型传输为脚本化 no-socket |

### 2.3 `desktop.v1` 帧与信封

帧：

```text
+----------------+--------------------------------------+
| 4-byte BE u32  | UTF-8 JSON body（1 .. 1,048,576 bytes）|
+----------------+--------------------------------------+
```

请求：

```json
{"v":"desktop.v1","id":"req-1","method":"consult.interact",
 "params":{"question":"合成问题"}}
```

成功响应：

```json
{"v":"desktop.v1","id":"req-1","ok":true,"result":{...}}
```

错误响应：

```json
{"v":"desktop.v1","id":"req-1","ok":false,
 "error":{"code":"BUSY","message":"...","result_unknown":false}}
```

桥本地错误码（不是 PCS 契约错误码）：`INVALID_REQUEST`、`INVALID_JSON`、`INVALID_PARAMS`、`DUPLICATE_REQUEST_ID`、`AUTHORITY_FIELD_REJECTED`、`METHOD_NOT_FOUND`、`RESERVED_NOT_IMPLEMENTED`、`BUSY`、`INTERNAL_ERROR`；宿主补充 `TIMEOUT`、`SIDECAR_EXIT`、`PROTOCOL_ERROR`、`SIDECAR_IO`、`SIDECAR_UNAVAILABLE`。

### 2.4 方法白名单

| 类别 | 方法 | 行为 |
|---|---|---|
| 实现 | `app.info` | 返回 app/协议/namespace/destination/limits/methods/reserved/PCS facade 信息 |
| 实现 | `bridge.ping` | 返回 pong、pid、协议、uptime、frozen |
| 实现 | `consult.interact` | 真实 `ConsultFacade.interact`，单槽执行 |
| 保留 | `consult.result`、`consult.history`、`consult.cancel`、`review.generate`、`review.commit`、`review.reject`、`review.result`、`work.scan`、`experience.list`、`experience.read`、`memory.query`、`persona.read`、`shell.exec`、`fs.read`、`fs.write`、`app.open` | `RESERVED_NOT_IMPLEMENTED`，不伪造 |
| 未知 | 其他 | `METHOD_NOT_FOUND` |

### 2.5 启动与关闭

- Rust 在 setup 中解析资源目录、计算 sidecar 文件清单/哈希、spawn child。
- child `current_dir` 是 sidecar 目录；`--db` 是宿主决定的 Lab DB 路径。
- child 环境清除 `PYTHONPATH`、`PYTHONHOME`、`PYTHONSTARTUP`、`PYTHONUSERBASE`、`PYTHONEXECUTABLE` 等 Python 变量。
- 窗口关闭 / `RunEvent::Exit` 时 Rust 终止 child；正常 `self-check` 退出也走同一路径。
- 单实例/跨进程 DB 独占未实现；见 §10 的 gap。

## 3. P1 — one-folder、资源部署与安装后运行

### 3.1 PyInstaller one-folder 内容

构建方式：PyInstaller 6.16.0 `--onedir`，staging 目录只包含 sidecar 四个模块和 `quintessence_pcs` 源码；SQL migrations 作为数据放在 `_MEIPASS/resources/migrations`。

产物 `sidecar/dist/pcs-bridge/`：

| 类别 | 数量 | 说明 |
|---|---:|---|
| 总文件 | 30 | 含 exe |
| `pcs-bridge.exe` | 1 | 2,380,634 bytes |
| `_internal` 文件 | 29 | 含 Python runtime、扩展、DLL、migrations |
| `.dll` + `.pyd` | 18 | `python312.dll`、`sqlite3.dll`、`_sqlite3.pyd`、`_ssl.pyd`、`libssl-3.dll`、`libcrypto-3.dll`、`VCRUNTIME140*.dll` 等 |
| migration SQL | 10 | `0001`..`0010` |
| 目录总大小 | 20,759,284 bytes（约 19.80 MiB） | 安装包压缩前 |

内容清单逐文件记录在 `<spike>/evidence/sidecar-files.json`，含相对路径、字节、SHA256。

**不是**只把 `sidecar.exe` 放进 `externalBin`。可执行文件、运行时、DLL、`_internal` 和 migration 全部随 one-folder 部署；安装后清单逐文件比对 30/30 通过。

### 3.2 `externalBin` 与 `resources` 的实际关系

配置：

```json
{
  "bundle": {
    "resources": {
      "../sidecar/dist/pcs-bridge": "resources/sidecar/pcs-bridge"
    },
    "externalBin": ["bin/external-probe"]
  }
}
```

实测安装布局（相对 `<install>`）：

```text
external-probe.exe
quintessence-c12b-tech-spike.exe
uninstall.exe
resources\
  sidecar\
    pcs-bridge\
      pcs-bridge.exe
      _internal\
        python312.dll
        base_library.zip
        *.pyd
        *.dll
        resources\migrations\*.sql
```

结论：

- **`externalBin` 只部署单个可执行文件**。`external-probe.exe` 最终位于安装根目录，证明这一点；它没有携带任何配套目录。它不是 PCS sidecar，只是路径关系探针。
- **one-folder sidecar 用 `resources` 部署**。安装后 `resources\sidecar\pcs-bridge` 含 30 个文件，与构建清单 30/30 一致。
- 本 Spike **没有**声称 `externalBin` 完成 one-folder 部署，也没有把 `pcs-bridge.exe` 单独放进 `externalBin`。
- C12b 正式实现应删除 `external-probe`，除非仍需保留一条打包回归探针。

### 3.3 安装后路径解析

| 项 | 实测 |
|---|---|
| `resource_dir()` | `<install>`（安装根目录；不是 `<install>\resources`） |
| 解析候选 | `<resource_dir>\resources\sidecar\pcs-bridge`、`<resource_dir>\sidecar\pcs-bridge`、`<install>\resources\sidecar\pcs-bridge` |
| 实际 sidecar 目录 | `<install>\resources\sidecar\pcs-bridge` |
| 实际 exe | `<install>\resources\sidecar\pcs-bridge\pcs-bridge.exe` |
| `current_dir` | 上述 sidecar 目录 |
| `--db` | 宿主传入的 Lab DB（self-check 用 `<install-test-data>\lab.sqlite`；正常 UI 默认 `%LOCALAPPDATA%\com.quintessence.c12btechspike\lab.sqlite`） |
| `dev_fallback` | `false` |
| `sidecar_under_install` | `true` |
| `sidecar_under_resources` | `true` |
| 安装后 exe SHA256 | `368fb896dc2c2610fb3067fee37c0890a7b850635a053915cc0ceb6f0c831adf` |
| `_internal` 文件数 | 29（构建 29） |
| `.dll`+`.pyd` | 18（构建 18） |
| migrations | 10（构建 10） |
| 逐文件清单比对 | **30/30，字节和 SHA256 全部一致** |

断言发生在 Rust 解析路径后、spawn 前：目录必须在安装目录下，且路径必须经过 `resources`；同时要求 `pcs-bridge.exe` 与 `_internal` 同时存在。`target/debug`、`node_modules`、Cargo/Node 开发目录、Lab 源目录不会作为 release 解析结果。

### 3.4 安装后运行（L1）

`verify_install.ps1` 的流程：

1. NSIS 静默安装到 `<install>`。
2. 清空 Python 相关环境：PATH 只保留 Windows 系统目录；`PYTHONPATH`、`PYTHONHOME` 设为不存在的占位路径。
3. 运行安装后 exe 的 `--self-check`，传入独立 Lab DB。
4. 读取 self-check JSON；用 PCS Store API 读取同一个 DB。
5. 静默卸载；检查残留和 Lab DB 保留。

结果：

| 检查 | 结果 |
|---|---|
| 安装器退出码 | 0 |
| self-check 退出码 | 0 |
| self-check verdict | `PASS` |
| sidecar `frozen` | `true` |
| facade | `ConsultFacade` |
| consult state | `answered` |
| generations / queries | 2 / 1 |
| Store verdict | `PASS` |
| interaction result | `answered` |
| 安装目录 SQLite / `.py` / `.pdb` / 日志 / `node_modules` | 0 / 0 / 0 / 0 / 0 |
| 卸载退出码 | 0 |
| 卸载残留文件 | 0 |
| Lab DB 保留 | `true` |

self-check 还记录了：

- `sidecar_present=true`
- `sidecar_under_install=true`
- `sidecar_under_resources=true`
- `dev_fallback=false`
- `child_frozen_runtime=true`
- `parent_python_env_present=true`（这是父进程被故意设置 bogus 变量的记录，不是依赖）
- `facade_is_consult=true`
- `generations_two=true` / `query_once=true`

安装后的 WebView 也做了可视化验证：窗口标题为 `QUINTESSENCE · C12b 技术验证`，页面显示 `pong · pid …`、`desktop.v1`、`synthetic / synthetic / scripted-collab-v1`。这说明 WebView 通过 Tauri IPC 调到了 `app.info` / `bridge.ping`，不是只启动了 Rust 进程。截图在 `<spike>/evidence/ui-screenshot.png`（Lab 证据，不提交）。

### 3.5 干净 Windows

探测结果：

- 当前系统是 Windows 11 **Home**，版本 10.0.26100，x64。
- `WindowsSandbox.exe` 不存在；查询 `Containers-DisposableClientVM` 需要提升，当前会话不能完成。
- 没有 VirtualBox、VMware、QEMU；有 WSL，但 WSL 是 Linux 发行版，不是干净 Windows guest。
- 没有独立 Windows 机器可用。

因此：

> **L2 = UNVERIFIED。**
>
> 没有在“无全局 Python 的干净 Windows 10/11 x64”上运行安装包，也没有用干净环境证明缺失 DLL、WebView2 或路径行为。L1 的 stripped PATH/bogus Python 只证明安装后的 sidecar 不依赖开发机 Python，不足以声称干净系统兼容。

没有把 L0/L1 写成 L2；没有用“开发机已有 Python”或“开发机已有 WebView2”当作反证。

### 3.6 WebView2

| 项 | 结果 |
|---|---|
| 检测方式 | HKLM/HKCU EdgeUpdate 客户端键 + 运行时目录 `msedgewebview2.exe` 文件版本 |
| 安装版本 | 155.0.4283.62 |
| 安装器模式 | `downloadBootstrapper`（silent 默认 true） |
| 缺失场景 | **UNVERIFIED** |
| 缺失场景原因 | 机器级 runtime 已安装；没有干净 Windows guest 可卸载测试 |
| 引导方案 | NSIS 在缺失时下载并运行 Evergreen bootstrapper；完全离线机器应改用 `offlineInstaller` 变体再验证 |
| 离线变体 | 未构建、未验证 |

“开发机已有 WebView2”不作为兼容性证明；本报告只把它作为“检测到了什么”的证据。

## 4. P2 — BUSY、帧与宿主失败语义

### 4.1 单任务执行槽与 BUSY

实现：

- reader 线程只负责读帧、校验、分发。
- `consult.interact` 先非阻塞获取单一 consult 锁；拿不到就立即返回 `BUSY`，不排队。
- 拿到锁的请求被交给一个专用 DB worker 线程执行；SQLite 连接只在该线程使用（避免 `sqlite3` 的线程亲和问题）。
- 执行期间 `bridge.ping` / `app.info` 仍在 reader 线程响应。
- 第二个 consult 不注册 input、不建 Context、不建 interaction、不建 Attempt。

测试（`test_busy.py`）：

| 测试 | 断言 | 结果 |
|---|---|---|
| 第二个 consult | 第一个模型调用阻塞 2.5s 时发第二个；错误码 `BUSY`；不排队；不产生第二个 interaction/input/Context/Attempt | PASS |
| 第一个完成后 | 等待 0.8s，计数不再增长；第二个不会自动启动 | PASS |
| 重复 request_id | 运行中的同一 id 得到 `DUPLICATE_REQUEST_ID`，不是 `BUSY` | PASS |
| ping 响应性 | consult 运行期间 ping 在 < 1s 返回，证明 reader 未被阻塞 | PASS |

最终计数：1 个 interaction、1 个 input、2 个 Context（初始 + 最终）、2 个 Attempt（两次 generation）、2 个 Artifact；没有第二个 interaction。

### 4.2 帧处理测试

`test_framing.py` 共 13 项，全部 PASS：

| 场景 | 测试 | 结果 |
|---|---|---|
| 4 字节头跨多次 read | `test_chunked_header_and_body` | PASS |
| body 跨多次 read | `test_body_delivered_one_byte_per_read`（逐字节） | PASS |
| 一次 write 两个完整帧 | `test_two_frames_coalesced_in_one_write` | PASS |
| 头不完整 EOF | `test_truncated_header_at_eof_fails_and_does_not_execute`：exit 2，无响应，无 interaction/input | PASS |
| body 不足 EOF | `test_truncated_body_at_eof_fails_and_does_not_execute`：exit 2，半条命令不执行 | PASS |
| 长度 0 | `test_zero_length_frame_closes_connection`：关闭，exit 2 | PASS |
| 长度 > 1 MiB | `test_oversize_length_closes_without_allocating`：关闭，exit 2 | PASS |
| 非法 JSON | `test_invalid_json_returns_error_and_stays_alive`：`INVALID_JSON`，随后 ping 仍可用 | PASS |
| 非法 UTF-8 | `test_invalid_utf8_returns_error_and_stays_alive`：`INVALID_JSON`，进程不崩溃 | PASS |
| 重复 request_id | `test_duplicate_request_id_is_rejected`：`DUPLICATE_REQUEST_ID` | PASS |
| stdout 纯协议 | `test_stdout_is_protocol_only_and_stderr_has_no_body`：3 个帧、无尾随文本；stderr 不含问题/回复正文 | PASS |
| 干净 EOF | `test_clean_eof_between_frames_exits_zero` | PASS |
| 信封校验 | `test_envelope_validation`：v/id/method/params 非法分别拒绝 | PASS |

关键点：`read_frame` 用循环读满 4 字节头和声明 body；不把一次 `read` 的返回长度当帧边界。长度为 0/超限时在读 body 前拒绝；截断时退出且不执行半条命令。

### 4.3 宿主失败语义

Rust 集成测试（`src-tauri/tests/host.rs`，5 项）与单元测试（2 项）：

| 场景 | 断言 | 结果 |
|---|---|---|
| 超时 | 500ms deadline，child hang；返回 `TIMEOUT`、`result_unknown=true`；child 被终止 | PASS |
| child crash | child exit 3；pending 请求返回 `SIDECAR_EXIT`、`result_unknown=true` | PASS |
| 协议违规 | child 写 0 长度帧；`PROTOCOL_ERROR`、`result_unknown=true`、child 被杀 | PASS |
| 响应粘连 | 两个请求、两个响应在一个 write；两个都正确路由 | PASS |
| 响应分块 | 响应头/体分三次写；跨 read 重组 | PASS |
| 帧单元测试 | 长度 0/超限、截断、干净 EOF | PASS |

生产 deadline 是 120 秒；`QUINTESSENCE_SPIKE_REQUEST_TIMEOUT_MS` 只是 Lab 测试钩子，正式实现应移除或固定在宿主配置中。超时/崩溃/断帧/退出一律不自动重发非幂等 consult。

### 4.4 stdout / stderr

- stdout：只写 `desktop.v1` 帧；测试从字节 0 重新解析并断言无残留。
- stderr：只写时间戳、级别、方法/状态/耗时、错误类名；不写问题、回复、Prompt、SQLite 路径或合成 key。
- 宿主把 child 的 stderr 收集到有界 tail，仅用于本地诊断；不通过 UI 返回正文。
- WebView 没有前端日志正文；没有 localStorage/IndexedDB。

## 5. P3 — 真实 PCS 接线

### 5.1 合成数据与模型

| 项 | 内容 |
|---|---|
| namespace | `example.test.c12b.lab` |
| Persona | 合成 Profile，`example.synthetic.c12b.lab`，版本 `0.0.1`，经合成 approve event 发布/激活 |
| SelfView | 由 `SelfViewService.build_self_view` 生成 |
| Belief | 1 条 `capability:lab-file-read`，`supported`，scope `{"scene":"lab.synthetic"}` |
| Input | 每次 consult 由可信 Python 宿主 `register_input` 写入，正文是用户问题 |
| 模型 | `ScriptedHttpModel(DirectHttpModel)`，`provider=synthetic`、`model=scripted-collab-v1`、`collab.v1` |
| 模型传输 | 覆盖 `_send` 返回固定 OpenAI 形状 envelope；不打开 socket，`network_attempts=0`。manifest 的 `base_url` 是保留的回环占位地址（port 9），既不是监听端口也不会被 connect |
| 审计 | 真实 `reserve_model_attempt` / `finish_model_attempt` / `last_attempt_evidence`、真实 request hash、真实 Context/Attempt |

Belief 是直接 store fixture（C10/C07 没有“种一条 supported belief”的公共入口）；Consult 路径本身没有绕过 Facade。`pending_work=1` 来自合成 Profile 的 approve event，不是 consult 写入。

### 5.2 端到端路径

一次 `consult.interact`：

1. Bridge 校验请求，只接受 `question`。
2. 可信 Python 宿主注册 synthetic input。
3. `ConsultRequest` 由宿主构造：固定 `query_plan=BeliefCurrentQuery`，固定 `policy_ref=policy:collab.v1`。
4. `ConsultFacade.interact` 构建初始 Context。
5. 第一次 generation 返回 `{"action":"query"}`。
6. 真实 `CognitionQueryService` 查询 Belief。
7. 构建最终 Context（含 query material），绑定 interaction。
8. 第二次 generation 返回 `{"action":"none","reply":"..."}`。
9. 写 query artifact 与 reply artifact，完成 interaction。
10. Bridge 返回 interaction/context/generations/query/attempts/destination。

真实证据（安装后最终运行）：

| 对象 | ID / 值 |
|---|---|
| interaction | `d349f9c5-fa00-4550-9753-0d20b06f7d59` |
| interaction mode/result | `consult` / `answered` |
| interaction access/policy | `authorization:host:c12b-lab-synthetic` / `policy:collab.v1` |
| 初始 Context | `c370b970-ebc9-49ad-969e-13ddf802f281` |
| 最终 Context | `eaebf677-c6f4-42f0-9fdf-309a1c054783` |
| 初始 Attempt | `e1e3e288-f31c-412d-915e-3abe5325f631`，`ok`，context=`c370b9…` |
| 最终 Attempt | `f09fd41f-e459-4df5-9de4-08bb6c2c97c6`，`ok`，context=`eaebf6…` |
| query artifact | `c31cd352-646d-4aa4-92cf-2389aa2b4edd`，kind=`query_result`，origin=`deterministic_query`，attempt_ref=`null` |
| reply artifact | `3cdaecd7-e367-4adb-950b-a2d11f4af2e2`，kind=`reply`，origin=`model_output` |
| reply attempt_ref | 与最终 Attempt 的 `attempt_id` / `attempt_no` / `request_hash` / `authorization_id` 完全一致 |
| manifest hash | `sha256:3dddd92eb6e1dae90d792f5c5968e31ad504cc83d95577ff7d4fd864df9e6e10` |
| request hashes | 初始 `sha256:4f389c03…`；最终 `sha256:f8f8d5ea…` |

Store 计数（安装后）：

| 表 / 指标 | 值 |
|---|---:|
| `collab_interactions` | 1 |
| `collab_inputs` | 1 |
| `collab_artifacts` | 2 |
| `model_attempts` | 2 |
| `context_snapshots` | 2 |
| `pending_work` | 1（合成 Profile review event） |
| `work_progress` | 0 |
| `processed_commands` | 0 |
| `records` / `record_versions` | 3 / 3 |
| `event_journal` | 1 |
| current beliefs | 1 |

端到端测试（`test_e2e_consult.py`）在 consult 前后比较计数：除 consult 应有的 input 1、interaction 1、Context 2、Attempt 2、Artifact 2 外，records/record_versions/pending_work/work_progress/processed_commands/event_journal 全部不变。**没有 work/Belief/command 误写。**

### 5.3 授权负例

Bridge 在创建 input/Context/interaction/Attempt 之前拒绝以下前端字段：

`privacy_classes`、`privacy_class`、`purposes`、`purpose`、`disclose`、`grant`、`grants`、`authorization`、`authorization_ref`、`access_ref`、`provider`、`endpoint`、`base_url`、`model`、`manifest_hash`、`max_calls`、`budget`、`namespace`、`host_instance`、`policy_ref`、`query_plan`、`input_refs`、`interaction_id`、`context_id`、`confirmation`、`confirmed`、`destination`。

测试逐一发送 16 组字段（含 `privacy_classes`、`purposes`、`disclose`、`grant`、`authorization_ref`、`provider`、`endpoint`、`destination`、`confirmation`、`confirmed`、`namespace`、`access_ref`、`policy_ref`、`query_plan`、`input_refs`）：

- 全部返回 `AUTHORITY_FIELD_REJECTED`。
- 没有 `result`，没有额外正文，没有额外 interaction/Attempt。
- 拒绝前后全部计数相同。
- `disclose=false` / `confirmation=true` 等布尔/字符串也被拒绝；前端确认不是授权。

未知参数（例如 `temperature`）返回 `INVALID_PARAMS`；空 question、空白 question、非字符串、超 8000 字符、含 NUL 均拒绝。

### 5.4 D3 合成目的地绑定

| 检查 | 结果 |
|---|---|
| 目的地来源 | sidecar 主机常量 + store 中 active `model_authorizations`；不读请求 |
| destination label | `synthetic / synthetic / scripted-collab-v1` |
| confirmation | `host-configured` |
| provider/model | `synthetic` / `scripted-collab-v1` |
| 与 store 绑定 | active authorization 与两条 Attempt 的 `provider`、`model_requested`、`manifest_hash` 完全一致 |
| 前端可否改 | 不能；`provider`/`endpoint`/`model`/`destination` 等字段被拒绝 |
| 真实外部 provider | 未调用；脚本化 `_send` 不打开 socket |
| 网络字节 | `network_attempts=0`；C09 send gate 在 reservation 前仍执行 |
| 合成确认 | 绑定的是宿主 manifest/authorization，不是前端布尔值 |

### 5.5 reserved / 未实现

`consult.result`、`consult.history`、`consult.cancel`、Review、work/experience/memory/persona、shell/fs/app.open 全部只返回 `RESERVED_NOT_IMPLEMENTED`。测试确认这些调用不改变任何计数；没有伪造结果、没有伪造 restore、没有读本地缓存冒充正式结果。

## 6. 安全、隐私与失败语义

### 6.1 WebView 边界

CSP：

```text
default-src 'self';
script-src 'self';
style-src 'self' 'unsafe-inline';
img-src 'self' data:;
font-src 'self';
connect-src ipc: http://ipc.localhost;
object-src 'none';
base-uri 'self';
frame-ancestors 'none'
```

- 只加载本地 `dist` 资产；没有远程页面、远程脚本、远程字体。
- 没有 `innerHTML` 动态正文；回复、artifact refs、错误都用 `textContent`/`<pre>`。
- 没有 `localStorage`、`IndexedDB`、`sessionStorage`；没有前端日志正文。
- capability 只有 `core:default`；没有 shell/fs/http/opener/process 权限。
- Rust 只暴露 `app_info`、`bridge_ping`、`consult_interact` 三个 typed commands。

### 6.2 通信失败语义

| 情形 | 结果 | 是否自动重发 |
|---|---|---|
| 明确拒绝（BUSY、参数非法、授权字段、reserved） | 结构化 error，`result_unknown=false` | 否 |
| 超时（120s） | 终止 sidecar；`TIMEOUT`，`result_unknown=true` | 否 |
| sidecar crash/被杀/EOF | pending 请求 `SIDECAR_EXIT`，`result_unknown=true` | 否 |
| 截断/非法帧 | `PROTOCOL_ERROR`，终止 child，`result_unknown=true` | 否 |
| 写入失败 | `SIDECAR_IO`，终止 child，`result_unknown=true` | 否 |
| 桥内部异常 | `INTERNAL_ERROR`，`result_unknown=true` | 否 |

UI 对 `result_unknown=true` 显示固定警告：“结果未知：请求可能已经到达 sidecar，但没有收到完成响应。本壳不会自动重发 consult，也不会读取本地缓存冒充正式结果；目前没有权威恢复接口。”

没有权威恢复接口，因此没有 `consult.result`、没有 history、没有自动重试。

### 6.3 私有数据/凭据扫描

扫描对象：

- `sidecar/dist/pcs-bridge`
- `artifacts/QUINTESSENCE C12b Tech Spike_0.1.0_x64-setup.exe`
- 安装后的 `<install>`（33 文件）
- release app exe

结果：

| 检查 | 结果 |
|---|---|
| 开发机用户路径 / Lab 路径 / 仓库路径 | 0 |
| API key 形状 | 0 |
| 私钥标记 | 0 |
| 私人 DB 名 | 0 |
| 私人/API sentinel（Lab 外植入） | 0 |
| 安装目录 `.sqlite` / `.py` / `.pdb` / 日志 / `node_modules` | 0 / 0 / 0 / 0 / 0 |
| 安装目录文件数 | 33 |
| 安装目录字节 | 24,197,255 |

补充检查：

- Sidecar PYZ 的 184 个 code object 的 `co_filename`：**0 个绝对路径**，样本均为 `pcs_bridge.py`、`quintessence_pcs\foundation\migrations.py` 等相对名。
- 两个 Lab DB 的 375 / 134 个 text cell：**0 个 API key 形状命中**。
- Synthetic API key 由 sidecar 进程在运行时随机生成，只存在内存/环境，不写 DB、不写日志、不进安装包；脚本化传输不使用它。

已知工具链例外：

- 官方 CPython 3.12 的 Windows DLL 和 PyInstaller bootloader 会嵌入 CPython 上游构建机的用户目录字符串。这不是本开发机的用户目录，也不能在不修改官方二进制的情况下删除。扫描器只忽略**这个精确的上游字面量**；任何本地用户目录仍会失败。
- 第一版 release app exe 嵌入过 Cargo registry 源码路径。最终构建使用 `--remap-path-prefix` 把 Cargo home、用户目录、Spike 和仓库路径映射为 `cargo`/`user`/`lab`/`repo`；重扫为 0。这是一个真实的打包发现，正式 C12b 构建必须保留这条 remap。

### 6.4 网络边界

- 应用运行期没有 localhost 监听；`desktop.v1` 只走 stdio。
- 不因为“没有监听端口”就声称没有网络外发风险；模型适配器仍执行 C09 发送门：每次 reservation 前重新证明当前 consult Context、Profile/SelfView 版本、每个 collaboration source 的 Read/Use/Disclose basis；不通过就 0 HTTP、0 reservation、0 attempt。
- 本 Spike 的 `_send` 是脚本化无 socket 传输；真实 provider 未接入、未调用。
- 安装器可能下载 WebView2 bootstrapper；这是安装期行为，不是应用监听。

## 7. 安装、卸载与残留

| 项 | 实测 |
|---|---|
| 安装器 | `artifacts/QUINTESSENCE C12b Tech Spike_0.1.0_x64-setup.exe` |
| 安装器大小 | 8,661,013 bytes（约 8.26 MiB） |
| 安装方式 | NSIS silent `/S /D=<install>` |
| 安装后文件数 | 33 |
| 安装后大小 | 24,197,255 bytes（约 23.08 MiB） |
| 安装目录内容 | app exe、uninstall.exe、external-probe.exe、30 个 sidecar 文件 |
| 卸载方式 | `uninstall.exe /S` |
| 卸载退出码 | 0 |
| 卸载残留 | 0 文件 |
| 安装目录删除 | 是 |
| Lab DB 保留 | 是 |
| 用户运行数据 | 保留在安装目录之外（Lab DB / `%LOCALAPPDATA%`），不随卸载删除 |

安装器/安装目录不含 SQLite、`.py` 源码、`.pdb`、日志或 `node_modules`。`external-probe.exe` 是唯一一个非产品探针；正式 C12b 应删除或替换为正式打包回归项。

## 8. 资源测量

### 8.1 体积

| 项 | 字节 | MiB |
|---|---:|---:|
| 安装包 | 8,661,013 | 8.26 |
| 安装目录 | 24,197,255 | 23.08 |
| sidecar one-folder | 20,759,284 | 19.80 |
| sidecar exe | 2,380,634 | 2.27 |
| app exe | 3,227,648 | 3.08 |
| 前端 dist（3 文件） | 18,428 | 0.02 |

### 8.2 冷启动

| 指标 | 实测 |
|---|---|
| 安装后 self-check 进程墙钟（安装/启动/调用/退出） | 1,208 ms |
| 进程启动到 self-check 开始 | 560 ms |
| sidecar ready（首个 ping 成功） | 438 ms |
| self-check 调用总耗时（ping + info + consult） | 1,141 ms |
| 最终一次安装 self-check 的 `startup_ms` / `ready_ms` / `self_check_ms` | 1,081 / 514 / 1,739 ms |

冷启动存在机器缓存和进程调度差异；给出两次实测，不把单次数值当承诺。

### 8.3 三档进程内存

测量工具：PowerShell `Get-Process` 的 `WorkingSet64` / `PrivateMemorySize64`，对 app 进程及其所有 descendants 求和；shell 排除 `pcs-bridge` 及其 `conhost`，full bundle 包含；交互档在两次 generation、每次 6s 脚本延迟的 consult 期间每 250ms 采样，取峰值。共享页按进程重复计入，因此这是“进程求和”指标，不是机器唯一内存。

| 档位 | Working set（MiB） | Private bytes（MiB） |
|---|---:|---:|
| shell（idle，app + WebView2） | 385.68 | 185.34 |
| full bundle（idle，app + WebView2 + sidecar + conhost） | 424.57 | 207.95 |
| interaction（full，峰值） | 428.29 | 266.70 |
| interaction（shell，峰值） | 387.66 | 242.43 |

单进程参考（idle）：

| 进程 | Working set（MiB） | Private（MiB） |
|---|---:|---:|
| app exe | 25.79 | 6.64 |
| `pcs-bridge` | 31.94 | 21.41 |

原始采样在 `<spike>/evidence/resource-measurements.json`。交互档的 private bytes 高于 idle，主要来自 WebView2 页面渲染/合成回复；working set 增长较小。

## 9. 验证结果与验收对照

### 9.1 测试汇总

| 套件 | 数量 | 结果 |
|---|---:|---|
| Python framing | 13 | PASS |
| Python BUSY | 3 | PASS |
| Python 授权/D3/reserved | 5 | PASS |
| Python P3 端到端 | 1 | PASS |
| Python 小计（默认 discovery 执行） | 22 | PASS |
| Packaged sidecar（one-folder exe） | 2 | PASS |
| Rust host（2 unit + 5 integration） | 7 | PASS |
| **合计执行** | **31** | **全部 PASS** |

Python 默认 discovery 报告 `Ran 24 tests`，其中 2 项 packaged 测试因未设置 `PCS_BRIDGE_EXE` 被 skip；随后单独设置该变量运行 packed 测试 2 项，全部 PASS。因此实际执行是 22 + 2。

### 9.2 L0/L1/L2

| 等级 | 结论 | 证据 |
|---|---|---|
| L0 | **PASS** | `pnpm tauri dev`：Vite 1420 监听、debug app、sidecar 进程、默认 DB 25 表 |
| L1 | **PASS** | 安装包、安装后 self-check、真实 consult、Store、卸载、UI 截图；PATH/Python 变量被剥离 |
| L2 | **UNVERIFIED** | 无干净 Windows guest；见 §3.5 |
| WebView2 缺失 | **UNVERIFIED** | 机器已安装；见 §3.6 |

### 9.3 任务卡验收表

| # | 检查 | 结论 | 证据位置 |
|---|---|---|---|
| 1 | PyInstaller one-folder 完整产物可安装部署 | **PASS** | §3.1、§3.3、30/30 manifest |
| 2 | externalBin/resources/安装目录路径关系有证据 | **PASS** | §3.2、probe 在安装根、sidecar 在 resources |
| 3 | 安装后实际路径启动，无开发机 Python/绝对路径/开发目录 | **PASS（L1）** | §3.4、stripped PATH/bogus env、frozen |
| 4 | L0/L1/L2 分级如实；L2 未验证标 UNVERIFIED | **PASS** | §0.2、§3.5、§9.2 |
| 5 | WebView2 检测/缺失有结论或 UNVERIFIED | **PARTIAL / UNVERIFIED** | 检测 155.0.4283.62；缺失未构造 |
| 6 | BUSY 立即返回，第二个不排队/不产生 Attempt | **PASS** | §4.1 |
| 7 | 帧分块/粘连/截断/非法长度/一次读非完整消息测试通过 | **PASS** | §4.2 |
| 8 | stdout 纯协议、stderr 日志、超时/退出处理 | **PASS** | §4.2、§4.3、§4.4 |
| 9 | 真实 ConsultFacade 端到端；Context/Interaction/Attempt/Artifact 对齐 | **PASS** | §5.2 |
| 10 | 前端伪造授权/目的地不提升能力 | **PASS** | §5.3 |
| 11 | D3 合成目的地绑定宿主 provider/model | **PASS** | §5.4 |
| 12 | 结果未知不自动重发；无恢复不读本地缓存 | **PASS** | §6.2；UI 固定警告 |
| 13 | 安装包无私人数据/凭据/DB | **PASS** | §6.3、§7 |
| 14 | reserved/gap 不伪造；未进入 C12b UI | **PASS** | §2.4、§5.5、§10 |
| 15 | 全量/gate/工作区/未推送；Lab 可复现材料齐全 | **PASS** | 见 §9.4 |

### 9.4 仓库验证

在提交本报告后执行：

- `python tools/run_tests.py` -> **844 tests / OK**。
- `python tools/check_repository.py` -> **176 files / passed**（175 + 本文件）。
- `git status --porcelain --ignored` -> 空。
- 未 push。

提交信息：

```text
docs(desktop): record the C12b technical spike (C12b-TECH)
```

## 10. Reserved、已知限制与判断

### 10.1 Reserved / gap

| 类别 | 缺口 | 本 Spike 的处理 |
|---|---|---|
| Consult 结果/历史/取消 | C11 没有 `consult.result`、history、cancel、进度事件 | 返回 `RESERVED_NOT_IMPLEMENTED`；UI 不做“可取消/可恢复”假象 |
| Review | 未实现 Review 工作区、候选枚举/差异/批准/拒绝 | reserved；不画可点击按钮 |
| Work/Memory/Experience | 未实现 DSH Work、Memory 页面、experience UI | reserved |
| 恢复 | 超时/crash 后没有权威恢复接口 | 显示“结果未知”；不重发、不读缓存 |
| 跨进程 | 没有跨进程 single-instance / DB 独占锁 | 每次 app 实例各自 sidecar；见限制 |
| L2 | 没有干净 Windows guest | UNVERIFIED |
| WebView2 缺失 | 没有缺失环境 | UNVERIFIED；记录检测/引导方案 |
| 离线安装 | 没有构建 `offlineInstaller` 变体 | 记录为后续选择 |
| 代码签名 | 未签名 | 发布前需签名/SmartScreen 评估 |
| MSI | 只构建 NSIS | 未构建 WiX/MSI |
| externalBin 探针 | 安装包含 `external-probe.exe` | 仅路径证据；正式 C12b 删除 |
| 测试钩子 | `QUINTESSENCE_SPIKE_REQUEST_TIMEOUT_MS`、`PCS_C12B_MODEL_DELAY_MS` | Lab-only；正式实现移除或固定 |
| 真实 provider | 未接入、未调用 | 脚本化模型；C09 发送门保持 |
| 并发 consult | 不设计并发 | 单槽 BUSY；不排队 |

### 10.2 已知限制

1. **L2 未验证**：不能在报告或对外口径中称“干净 Windows 兼容通过”。
2. **WebView2 是外部依赖**：安装器使用 `downloadBootstrapper`；完全离线干净机器需要 `offlineInstaller` 变体并重新验证。
3. **未签名**：安装包没有代码签名；SmartScreen/企业策略可能拦截。
4. **单实例未实现**：两个 app 实例会各自启动 sidecar，可能对同一 SQLite 竞争；正式 C12b 需要 single-instance 或明确 DB 独占策略。
5. **控制台宿主**：sidecar 是 console subsystem；Rust 用 `CREATE_NO_WINDOW` 隐藏窗口，但进程树仍可能出现 `conhost`；不影响功能，正式实现可改为 windowed 或固定运行时。
6. **资源路径版本差异**：Windows `resource_dir()` 返回安装根目录；Tauri 版本变化可能改变布局；Rust 同时尝试两个候选并断言，未来升级仍需重跑安装验证。
7. **Rust 依赖路径**：必须保留 `--remap-path-prefix`，否则 release exe 会重新嵌入 Cargo registry 用户路径。
8. **CPython 上游路径**：官方 `python312.dll` / PyInstaller bootloader 内的上游构建机路径无法在不改官方二进制的情况下删除；扫描器只白名单精确上游字面量。
9. **合成数据边界**：Lab DB 是合成数据；正式 C12b 必须使用真实用户批准的数据路径和授权，不得把本 Spike 的常量/路径当生产配置。
10. **Belief fixture**：合成 Belief 直接写 store；正式 C12b 不应绕过 Review/C07 的正式写入路径。

### 10.3 Tauri 继续 / 切换判断

**建议继续 Tauri 2**，理由：

- P1/P2/P3 全链路在 L1 通过；one-folder + resources + Rust 路径解析可工作。
- 安装包 8.26 MiB，安装目录 23.08 MiB；冷启动约 1.2s；没有出现阻断性内存/启动问题。
- Rust 可信宿主边界清晰；WebView 没有 shell/fs/http 权限；CSP 可用。
- WebView2 已是机器级依赖；L1 可用。切换到 Electron 不会消除 Chromium 依赖，反而增大包体；Qt 会引入另一套 UI/打包栈，不能解决本次已通过的桥接问题。
- 继续的前置条件：C12b 正式实现必须先补 L2 干净系统验证和 WebView2 离线策略；再加入 single-instance、代码签名和正式授权/数据路径。

**没有在本 Spike 中切换技术栈。**

## 11. 明确未做的事

- 没有开始正式 C12b Consult UI 编码。
- 没有进入 C12b/C12c。
- 没有实现 DSH Work、Memory 页面、Experience-to-Skill。
- 没有修改 C06–C11 语义。
- 没有新增 PCS 契约、迁移或错误码。
- 没有 push。
- 没有使用正式私人实例、真实 provider、真实 Persona/Memory 正文。
- 没有把 DSH 源码复制进 QUINTESSENCE；未来只做独立适配器。

## 附录 A — 复现命令

```powershell
# 0) 准备
cd <spike>
python -m venv .venv
.venv\Scripts\python.exe -m pip install pyinstaller==6.16.0 pillow
pnpm install

# 1) sidecar（含 packaged 测试）
powershell -File scripts\build_sidecar.ps1

# 2) 协议/BUSY/授权/P3 测试（dev + packaged）
powershell -File scripts\run_lab_tests.ps1 -IncludePackaged

# 3) Rust host 测试
powershell -File scripts\run_rust_tests.ps1

# 4) 前端 + Tauri + NSIS
pnpm build
powershell -File scripts\build_app.ps1

# 5) 安装/自检/Store/卸载
powershell -File scripts\verify_install.ps1 `
  -Installer "artifacts\QUINTESSENCE C12b Tech Spike_0.1.0_x64-setup.exe"

# 6) L0、L2、WebView2、资源、扫描
powershell -File scripts\l0_dev_probe.ps1
powershell -File scripts\l2_probe.ps1
powershell -File scripts\detect_webview2.ps1
powershell -File scripts\measure.ps1
powershell -File scripts\scan_private.ps1
```

## 附录 B — 关键证据文件

| 文件（相对 `<spike>/evidence`） | 用途 |
|---|---|
| `environment.json` | OS/Node/pnpm/Rust/Tauri/Python/PyInstaller/WebView2 |
| `sidecar-build.json` | sidecar 产物大小、文件数、SHA256、PCS commit |
| `sidecar-files.json` | one-folder 30 文件逐文件字节/SHA256 |
| `frozen-code-paths.json` | 184 个 frozen code object 的路径检查 |
| `app-build.json` | app exe、安装包、rustflags、体积 |
| `installed-tree.txt` | 安装目录相对树 |
| `installed-sidecar-files.json` | 安装后 sidecar 30 文件清单 |
| `sidecar-manifest-compare.json` | 构建 vs 安装 30/30，missing/extra/changed 全空 |
| `installed-selfcheck.json` | 安装后 frozen/facade/路径/计时/断言 |
| `installed-store.json` | Interaction/Context/Attempt/Artifact/计数 |
| `ui-screenshot.json` / `.png` | 安装后 UI 与 WebView IPC 证据 |
| `l0-dev.json` | dev 链路 |
| `l2-probe.json` | 干净 Windows 探测（UNVERIFIED） |
| `webview2.json` | runtime 版本/检测/缺失状态 |
| `resource-measurements.json` | 体积/冷启动/三档内存原始采样 |
| `lab-tests.json` / `lab-tests.log` | 22 个 Python bridge/sidecar 测试 |
| `packaged-tests.log` | 2 个 packaged sidecar 测试 |
| `rust-tests.json` / `rust-tests.log` | 7 个 Rust host 测试 |
| `private-data-scan.json` | 安装包/安装目录/app/sidecar 扫描 |
| `db-key-scan.json` | Lab DB text cell 的 key 形状扫描 |

## 附录 C — 与任务卡停止条件的对照

| 停止条件 | 本 Spike 状态 |
|---|---|
| one-folder 无法随安装包部署，或只能靠开发机路径运行 | 未触发；30/30 文件安装，L1 通过 |
| externalBin/resources 路径不可解析/安装后缺失 DLL | 未触发；路径断言、29 `_internal`、18 DLL/pyd、self-check 通过 |
| 干净 Windows 无法验证且不能如实标 UNVERIFIED | 已如实标 UNVERIFIED；不宣称通过 |
| BUSY 变成排队自动执行，或并发产生第二个 Attempt | 未触发；BUSY 测试计数不变 |
| 帧处理把一次 read 当完整消息 | 未触发；分块/逐字节/粘连测试 |
| P3 只能 ping/echo/伪造响应 | 未触发；真实 Facade + Store 证据 |
| 前端可伪造授权/目的地提升能力 | 未触发；16 组负例全部拒绝 |
| 安装包含私人数据/凭据 | 未触发；四类扫描 0 命中 |
| 必须新增 PCS 契约/迁移/错误码 | 未触发；repo 业务代码未改 |
| 开始正式 C12b UI / push | 未触发；只有 Lab 技术壳，未 push |

## 附录 D — 版本/哈希速查

```text
PCS baseline            b6d01f0
sidecar exe SHA256      368fb896dc2c2610fb3067fee37c0890a7b850635a053915cc0ceb6f0c831adf
installer SHA256        a2004801fea3b87d3a2ec13350cf988e8266eff1bb2fcd181cae3dd8aa1437f0
installed app SHA256    de44dc15cfb41f21bb759652331cdc393bd1b68d6f2528099f863a6f5d5bbcbf
sidecar files           30 (29 _internal, 18 dll/pyd, 10 migrations)
installed files         33
installer bytes         8,661,013
install bytes           24,197,255
```

---

**结论：C12b-TECH Spike 在 L0/L1 完成，P1/P2/P3 全部通过；L2 与 WebView2 缺失场景如实标记 UNVERIFIED。建议进入一级验收，再由 GPT-6 二级复核；复核通过前不开始正式 C12b Consult UI 编码。**
