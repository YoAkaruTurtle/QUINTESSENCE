# C12c — Review 工作区、审批与权威恢复实施记录

> 状态：C12c 正式实施记录（Review UI + desktop bridge）。C13 未开始。
>
> 基线：`af509fb`（C12c-PRE 已关闭；C12b 已关闭；C12a 设计稿 APPROVED）。
>
> 日期：2026-10-24。
>
> 本文只记录 `apps/desktop/` 的 Review 实现、测试与 Lab 验证。所有 Work、
> Candidate、Belief、Context、来源与回执均为合成 Lab 数据；未使用正式私人
> 实例，未调用真实外部 provider，未 push，未合并 main。

## 0. 一页结论

| 问题 | 结论 |
|---|---|
| 是否完成跨 host_instance 读取/审批审计？ | 是。C07 读取边界是 namespace + privacy + purpose，审批/恢复边界额外要求 interaction host_instance；host B 可读候选但 resolve/reject/result 被拒绝。C12c 未新增或删除 host 检查。 |
| `can_*` 是否被当成权限？ | 否。按钮同时要求宿主 review.approve/review.reject、pending、v1、exact version、Work generation 一致且未提交中；`can_*` 只是内容提示。 |
| 是否修改 C07 resolve？ | 否。Facade/Service/Store 的 resolve、commit、result 路径未改。 |
| exact 绑定是否完整？ | 是。candidate_id + artifact_version + work_id + work_generation + current target 在 bridge 预检和 C07 事务内分别核对。 |
| 是否支持多版本/批量/取消/commit CLI？ | 否。非 v1 可展示但不可审批；批量、取消、`review.commit`、历史/feedback 均未实现。 |
| feedback 是否接线？ | 否。UI 无 feedback 输入；`review.generate` 收到 feedback 返回 `INVALID_PARAMS`；未复用 C05b 登记前不提供该功能。 |
| 未知结果是否自动重发？ | 否。超时/断帧/内部失败显示“结果未知”；已知 interaction 才调用 `review.result`，否则只刷新权威列表。 |
| REVISION_CONFLICT 是否准确？ | 是。bridge 预检把 target moved/current 不可用映射为 `REVISION_CONFLICT`，C07 自身抛出的 `REVISION_CONFLICT` 也原样映射；不自动重试。 |
| 是否直读 DB？ | 否。WebView 只调用 Rust typed commands；Python 只调用 ReviewFacade/PRE；UI 不读 SQLite、不读 payload。 |
| WAIT/Legacy/删除边界？ | WAIT 只显示回执且无候选；Legacy 只读、无候选/审批；candidate/current/source redacted 时隐藏正文并禁用依赖该内容的操作。 |
| 是否使用正式私人数据？ | 否。实验只使用仓库外 Lab 的 `build/c12c/` 隔离 PCS 数据；未读写任何非实验目录。 |
| 是否进入 C13？ | 否。 |

## 1. 范围

### 1.1 实施

- `apps/desktop/sidecar/host_config.py`：私有 `review.permissions`（既有 C07
  权限词汇的子集）与 Lab 专用 `review_script`；不新增 PCS 权限。
- `apps/desktop/sidecar/runtime.py`：ReviewFacade、HostAccess、review 模型
  适配器、review 方法投影与 exact 预检。
- `apps/desktop/sidecar/pcs_bridge.py`：6 个 `review.*` 方法、参数/权限边界、
  错误映射、`app.info` review 能力布尔值。
- `apps/desktop/src-tauri/src/**`：6 个 typed Tauri commands、session 校验。
- `apps/desktop/src/**`：Review 工作区、Work/候选列表、并排/上下差异、来源
  抽屉、批准/拒绝确认、已暂停/重新授权、WAIT/Legacy/冲突/未知面板。
- `apps/desktop/tests/**`、`tests/desktop/**`、`tests/collaboration/**`：新增
  bridge/authority/UI 状态测试。
- `docs/desktop/C12c-IMPLEMENTATION.md`、`tools/source-files.json`。

### 1.2 非目标

- 不实现 C13、Consult 候选、DSH Work/Memory/Experience-to-Skill、真实模型研究、
  Persona/Memory 扩展、语义检索或 ACL 平台。
- 不新增 PCS 契约、迁移、错误码；不修改 C06/C07/C12c-PRE 语义。
- 不扩展多版本审批、批量批准、取消、历史、feedback 或 UI 内部编辑候选。
- 不把 Spike 调试面板、测试夹具、私人数据或凭据放入生产/安装包。
- 不 push，不进入 main。

## 2. 编码前审计

### 2.1 跨 host_instance 读取与审批

审计用 host A 创建一个真实 candidate，再用 host B（同 namespace、隐私与
purpose 满足）调用 PRE `candidates` / `candidate_detail`，并对照 C07
`resolve` / `result`。

审计结果（`tests/collaboration/test_review_candidate_read.py`
`test_host_b_can_read_but_cannot_approve_or_recover`）：

| 调用 | host B 结果 |
|---|---|
| `candidates` | 可见 1 条候选 metadata |
| `candidate_detail` | `redacted=false`，proposal claim 可见，`can_approve=true` |
| `resolve(approve)` | `AuthorizationRequired` |
| `resolve(reject)` | `AuthorizationRequired` |
| `result` | `AuthorizationRequired` |

依据：

- C12c-PRE §6 明确记录：候选读取以 namespace + 隐私/purpose 为边界，不额外
  检查 interaction 的 `host_instance`；批准仍由 `resolve` 检查 host_instance。
- `docs/collaboration/FACADE.md` §4.4/§4.5：`resolve` 要求 interaction 属于
  `access.host_instance`；`result` 相同；读取方法没有该额外限制。
- 本次审计同样验证：只读 HostAccess（`work.scan + experience.read`）对同一
  candidate 得到 `can_approve=true`，但 `resolve` 仍 `AuthorizationRequired`。

结论：**C07 明确允许 namespace/privacy/purpose 级读取，审批/恢复仍要求
host_instance 与权限。** C12c 不自行新增/删除 host 检查；bridge 的
`review.candidate.detail` 也不添加 host 检查，`review.resolve` / `review.result`
依赖 C07 最终复验。

### 2.2 权限与许可

`HostAccess` 的实际职责（C07 `FACADE_PERMISSIONS`）与 C12c 的接线：

| 操作 | 权限 | C12c 行为 |
|---|---|---|
| Work 列表 | `work.scan` | `review.work.list`；managed/legacy 分离 |
| 候选列表/详情 | `work.scan + experience.read` | PRE 读取；purpose 必须含 `review` |
| 生成候选 | `review.generate` | `review.generate`；另要求 Work 是 managed 且当前无 pending/committed candidate |
| 批准 | `review.approve` | 按钮 + bridge 预检 + C07 |
| 拒绝 | `review.reject` | 按钮 + bridge 预检 + C07 |
| 结果/回执 | `result.read` | `review.result`；回执披露仍由 C07 |
| `can_approve`/`can_reject` | **不是权限** | 只做内容提示；绝不单独启用按钮 |

- 前端不能自行赋权；`app.info.capabilities.review` 只返回由宿主配置和启动
  就绪派生的 `read/generate/approve/reject/result` 布尔值，不返回 token、grant、
  access_ref 或权限表。
- 私有 `desktop.host.v1` 新增可选 `review.permissions`，取值必须是 C07 既有
  权限词汇；启用时要求 `host.purposes` 含 `review`。没有该段时 Review 保持
  不可用，C12b 配置仍可启动。
- Approve/Reject 同时满足：PRE `can_*`、实际宿主权限、candidate pending、exact
  artifact_version、Work generation 与当前 control 一致、candidate 未处于提交
  中；最终由 C07 `resolve` 权威复验。
- v1 限制保留：非 v1 候选可展示，`can_approve/can_reject=false`，bridge 返回
  `UNSUPPORTED_VERSION` 类型拒绝；不扩展多版本审批。
- Legacy 只读：不产生候选、无 Approve/Reject；`review.generate` 对 legacy 返回
  `LEGACY_WORK_READ_ONLY`，不会调用 C07/model。
- 模型路径：Consult 使用 `collab.v1`；Review 生成继续使用 C07 的 legacy review
  ModelPort（`pcs2-008` request/renderer）。两者共用同一 provider/model/
  manifest、同一模型授权与同一 C07/C06 事务路径；没有新增目的地、凭据或
  provider。Lab 的 `review_script` 只替换 `ScriptedHttpModel` 的传输，不绕过
  reservation、请求哈希、响应校验和 attempt 结算。

### 2.3 Rerun 与 feedback

- `rerun_authorization_ref` 存在 ≠ 已获得完整操作许可。`review.work.list` 只
  返回 `rerun_registered: bool`，不返回 reference 值；`review.generate` 仍由
  C07 `review_start` gate 判定，C05d 在 `register_candidate` 内再校验 grant
  equality、active 与 generation。
- 无 rerun ref：UI 只说明“需要宿主发放新许可”，按钮禁用；`review.generate`
  若被直接调用仍由 C07 返回 denied，不伪造成功。
- 有 ref：允许用户再次操作 `review.generate`；是否真的开始由 C07 决定。旧候选
  保持 rejected，不复活。
- feedback：C12c **不提供 feedback 输入框**；`ReviewFacade.review` 的
  `feedback` 参数未从 bridge 接线。`review.generate` 一旦收到 `feedback` 字段
  立即 `INVALID_PARAMS`。没有安全复用 C05b 正式来源登记/隐私/删除机制前，本卡
  不提供该功能。

### 2.4 未知结果

- 明确失败：结构化 PCS 异常（`AuthorizationRequired`、`REVISION_CONFLICT`、
  `CandidateTransitionDenied` 等）映射为公开错误码，UI 显示相应终态。
- 结果未知：Rust 超时/断帧/sidecar crash，或 Python 在 generate/resolve/result
  内发生未预期异常。bridge 返回 `INTERNAL_ERROR` + `result_unknown=true`，UI
  显示“结果未知”，不自动重发 generate/resolve。
- `review.result` 只对合法、属于本 host 的 interaction 恢复；没有
  interaction_id 或没有 `result.read` 时不恢复、不伪造成功/失败。
- `review.result` 自身失败也区分 unknown：未预期异常标 `result_unknown=true`；
  已知拒绝（权限、不存在、revision conflict）是明确失败。

## 3. Bridge 方法

所有方法都经可信 Rust/Python 宿主与 C07/PRE Facade；WebView 不能直读 DB。

| 方法 | 参数 | 权限/边界 | 返回 |
|---|---|---|---|
| `review.work.list` | `session_id` | `work.scan`；namespace scope | `managed[]`（work/event/generation/blocked/rerun_registered）、`legacy[]`（work/event/kind/status/created_at） |
| `review.candidates.list` | `session_id, work_id, generation?` | `work.scan + experience.read + review purpose`；不可见候选整行省略 | `candidates[]`（exact ref、status、时间、attempt_id/no） |
| `review.candidate.detail` | `session_id, candidate_id, artifact_version` | 同上；exact `(candidate_id, artifact_version)`；redacted detail 不返回正文 | proposal/current/current_state/diff/sources/can_*/eligibility/redacted |
| `review.generate` | `session_id, work_id, event_id, confirmed_destination_id?` | `review.generate`；managed Work；无 pending/committed candidate；外部目的地 session 确认 | ReviewView |
| `review.resolve` | `session_id, candidate_id, artifact_version, decision` | `review.approve/reject`；exact 预检；decision_id 由宿主生成 | ReviewView |
| `review.result` | `session_id, interaction_id` | `result.read`；C07 host_instance | ReviewView |

- 所有方法要求 session 匹配、宿主 ready、数据锁未被其他进程占用。
- `review.generate` 不接收 feedback；`review.resolve` 不接收 Approval/grant/
  decision_id/candidate body/model/provider；`review.result` 只接受合法
  interaction_id。
- 保留/未实现：`review.commit`、`review.cancel`、`review.history`；不存在批量
  批准、反馈或历史接口。
- `app.info` 新增：
  - `workspace.review`、`workspace.review_reason`；
  - `capabilities.review.{read,generate,approve,reject,result}`；
  - `methods`/`reserved` 更新；
  - `pcs.facade = "ConsultFacade + ReviewFacade"`。
- 不返回 token、grant、access_ref、policy_ref 之外的新内部引用或权限表。

### 3.1 错误映射

| 宿主失败 | 公开码 | 说明 |
|---|---|---|
| `AuthorizationRequired` | `AUTHORIZATION_REQUIRED` | 缺权限/许可；没有提交 |
| `StateRevisionConflict` / `RecordVersionConflict` | `REVISION_CONFLICT` | 目标版本移动；刷新，不重试 |
| bridge 预检 target moved/current 不可用 | `REVISION_CONFLICT` | 不调用 resolve |
| `CandidateTransitionDenied` / `ContextStale` | `CANDIDATE_STALE` | 候选/上下文已变化 |
| exact candidate/version 不存在或不一致 | `CANDIDATE_VERSION_MISMATCH` | 刷新候选 |
| 已有 pending candidate | `CANDIDATE_EXISTS` | 不重复生成 |
| 当前 generation 已有 committed candidate | `WORK_ALREADY_RESOLVED` | 不重复审查 |
| 无 control / 已不 pending | `WORK_NOT_PENDING` | 不提交 |
| Legacy Work | `LEGACY_WORK_READ_ONLY` | 只读 |
| 非 v1 candidate | `UNSUPPORTED_VERSION` | 不审批 |
| 未预期异常 | `INTERNAL_ERROR` + `result_unknown` | generate/resolve/result 为 true；只读为 false |

不新增 PCS 错误码；以上是 `desktop.v1` bridge 标签。

## 4. 精确提交绑定

`review.resolve` 的顺序：

1. 从 UI 只接收 `candidate_id` + `artifact_version` + `decision`；
2. 用 PRE `candidate_detail` 读取 exact `(candidate_id, artifact_version)`；
3. 核对：
   - summary candidate_id / artifact_version 与请求一致；
   - `redacted=false`；
   - status=`pending`；
   - artifact_version=`1`（C12c 支持范围）；
   - `eligibility_reason` 不是 `stale_generation`/`unsupported_version`；
   - approve 时 `can_approve=true`，current_state 不是 redacted/missing；
   - reject 时 `can_reject=true`；
   - Work 仍在 `managed_work` 中，且 control.generation = candidate.work_generation；
4. 通过后才调用 C07 `resolve`；`decision_id` 由宿主生成（`decision:<uuid>`）；
5. C07 在事务内再验证候选、Approval、Context、删除 barrier、current Belief 版本
   与回执；bridge 预检不是第二权威；
6. 版本/Work/current 不匹配返回结构化 stale/conflict，不调用 resolve、不写库；
7. committed candidate 的重复 approve 仍交给 C07 返回存储回执（幂等，不二次
   写）；其他终态返回 stale。

## 5. Review UI

### 5.1 工作区

- 固定顶栏导航进入 Review；Consult 不提供 Review 直达/候选入口（D7-B）。
- Review 隐藏 Consult 输入区；固定品牌、顶栏、目的地、设置入口不变。
- Work 列表分 `待审查`（managed）与 `旧工作项（无控制）`（legacy）。
- managed 行显示 work/event 短引用、待处理/已暂停、复查许可“已登记（待校验）”；
  legacy 行显示 kind/created_at 与“只读”，无候选入口。
- 选中 Work 后显示 candidate summary 列表；legacy 显示只读能力缺口。

### 5.2 候选差异（D5-A）

- 宽屏并排（字段/当前/候选/变化），窄屏按字段上下堆叠，变化标签为文本。
- 只显示 PRE 正式投影：claim、claim_type、scope、evidence/source、strength、
  valid_until；current status 放在“当前版本”区，候选 proposal 不伪造 status。
- 默认只展开 changed 字段；未变化字段折叠在 `<details>` 中；来源引用默认折叠。
- current `redacted`/`missing` 时明确标注，diff 为空，不显示空 Belief。
- source 三态 `available/redacted/unavailable`；正文只在正式可读时显示于来源
  抽屉；Event/Record 不读正文。
- 不显示 Prompt/HTTP body/DB 列/内部 revision/proposal_digest。

### 5.3 Approve/Reject

- 按钮启用 = PRE `can_*` + 宿主 `review.approve/reject` + exact
  version/Work generation + pending + 未提交中 + `v1`；`can_*` 单独不启用。
- 批准/拒绝都二次确认；拒绝确认文案明确“已暂停、旧候选不可再批准、需要新许可”。
- 提交中按钮禁用并显示“正在提交…”；UI 层 busy + bridge model lock + C07 幂等
  共同防双击。
- Approve 成功显示 committed + 权威回执；Reject 成功显示 rejected + 已暂停。
- 回执只来自 C07 `ReviewView.receipt`；`redacted=true` 时隐藏并显示“回执不可见”，
  不伪造。

### 5.4 冲突与结果

- `REVISION_CONFLICT` → “目标版本已变化，需要重新获取权威状态”；刷新
  candidate detail/current/work/result；不自动重试批准。
- `AUTHORIZATION_REQUIRED` → “缺少审批权限/许可”；不显示数据、不发放权限。
- `CANDIDATE_VERSION_MISMATCH`/`CANDIDATE_STALE` → 刷新候选，不提交。
- `result_unknown` → 固定“结果未知”卡；有 interaction_id 时可查询
  `review.result`，否则刷新 Work/candidate，不自动重发。
- 明确 failed/denied/stale 才显示相应终态；不虚报成功。

### 5.5 拒绝后重新授权（D6-A）

- 已暂停面板；旧候选只读、不能批准、不能复活。
- 无 rerun ref：只说明“需要宿主发放新许可”，`开始新的审查` 禁用。
- 有 ref：`开始新的审查（新候选）` 可操作，由 C07 gate 决定；失败显示 denied。
- “申请新的复查许可”始终禁用，说明没有公共发放入口；不在 UI 伪造授权/grant。

### 5.6 WAIT / Legacy / 删除

- WAIT：显示 C07 waiting 回执/结果，无 candidate，无 Approve/Reject。
- Legacy：只读 Work 元数据，无候选/审批；缺字段标缺口，不读 DB。
- 删除/隐私：candidate redacted → 无 proposal/diff/sources、两按钮禁用；
  current/source redacted → 只禁用依赖该内容的 Approve；Reject 仍按 PRE
  `can_reject`；不显示正文、不以空值替代。

### 5.7 窗口与质量

- 880×560、1280×800、1600×1000；高 DPI 200%、中文 IME。
- 固定主操作条；宽屏并排/窄屏上下差异；来源抽屉覆盖层。
- 沿用 C12b 品牌、顶栏、目的地、设置；Review 无 Consult 输入区。
- 不复制 Spike 调试面板；无 localStorage/IndexedDB/sessionStorage 正文。

## 6. 未知结果、断线与恢复

- double-click：UI `busy` 立即置位；bridge 对 generate/resolve 使用 model lock，
  第二个请求 `BUSY`；C07 对重复 approve 返回存储回执（单次写入）。
- 断线/超时：Rust 将 pending 请求失败为 `result_unknown=true` 并终止 sidecar；
  UI 不自动重发，显示未知。
- 重载：无本地缓存；Review 重新读取 work/candidate/detail，以宿主为准。
- 已知 interaction：`review.result` 恢复 committed/rejected/waiting/pending/
  stale/denied/failed/incomplete；恢复失败也区分 unknown。
- 拒绝后刷新仍 rejected；旧候选不可批准。
- rerun ref 存在但不等于许可；无 ref 只说明。

## 7. 安全边界

- WebView capability 仍只有 `core:default`；Rust 只新增 6 个 typed commands，
  没有 shell/fs/http/db 通用能力。
- Python bridge 只调用 `ReviewFacade`/PRE、`ConsultFacade` 与 Store 的正式读取；
  没有 UI 侧 SQL、payload 读取或 DB 路径。
- C06/C07/PRE 的 `resolve`/`commit`/`result`/迁移/错误码未改。
- `review.resolve` 拒绝前端伪造的 Approval/grant/decision_id/candidate body/
  model/provider；`review.generate` 拒绝 feedback 与 authority 字段。
- 回执过滤 `state_revision`；候选/来源读投影过滤 `origin_group_id`、
  `authorization_id`、`request_hash`、`decision_ref` 等内部字段。
- Lab 与正式私人实例分离：不读写仓库外正式私人实例目录或任何非实验目录。

## 8. 验证

### 8.1 自动化

| 套件 | 数量 | 结果 |
|---|---:|---|
| Python 全量（新增 C12c） | 951 | OK（skipped=3；packaged 测试默认跳过） |
| 其中 C12c bridge/authority/config | 20 | OK（含 1 个 packaged Review；完整 packaged class 3 个） |
| Rust host `--features host-tests` | 15 | OK（10 unit + 5 integration） |
| 前端 Node tests | 35 | OK（新增 12 个 Review 状态/权限/差异测试） |
| `pnpm build` | — | OK（JS 94.58 KiB / gzip 26.80；CSS 34.05 KiB / gzip 7.22） |
| packaged sidecar tests | 3 | OK（Review approve + Consult + data lock） |

覆盖：

- 跨 host_instance：host B 可读 candidate/detail，resolve/reject/result 均
  `AUTHORIZATION_REQUIRED`；只读 HostAccess 的 `can_approve=true` 仍不能
  resolve。
- `can_*` + 宿主权限：只读配置下按钮禁用并显示“缺少审批权限/拒绝权限”；
  bridge 直接 resolve 返回 `AUTHORIZATION_REQUIRED`，无写入。
- v1/非 v1、terminal、stale generation、exact candidate/version/Work/current。
- `review.generate` 生成 + 外部 D3 确认；feedback 拒绝；authority 字段拒绝；
  legacy 只读；WAIT 无候选；C07 denied；rerun ref 存在但 revoked 仍 denied；
  合法 rerun 生成新 generation。
- double-click：并发/重复 resolve 只产生 1 个 receipt；UI busy + bridge lock。
- `REVISION_CONFLICT`/`CANDIDATE_STALE`/`CANDIDATE_VERSION_MISMATCH` 映射；
  unknown 不自动重发；合法 interaction `review.result` 恢复。
- 删除 barrier：candidate 从列表消失，detail `redacted=true`，resolve 拒绝。
- UI 状态：pending/committed/rejected/reauth/waiting/legacy/readonly/confirm/
  sources；三尺寸；DPI 200% + IME；无 localStorage/IndexedDB/正文缓存。

### 8.2 打包、安装与 Review smoke（Lab）

- sidecar：PyInstaller 6.16.0 one-folder，30 文件（29 `_internal`、18 DLL/pyd、
  10 migrations），SHA256 见 Lab `sidecar-build.json`。
- app：Tauri release + NSIS，安装包 8.34 MiB；安装后 32 文件 / 24,246,370 字节；
  sidecar 30 文件。
- 安装版 Review smoke：app.info review 能力 true；work.list → generate →
  candidate.detail → resolve(approve) → committed/receipt；DB 中 1 个 committed
  candidate、1 个 review interaction。
- 单实例：第二个进程退出码 0，app 进程 1、sidecar 进程 1。
- 数据锁：packaged sidecar 第二个实例 `DATA_LOCKED`。
- L2 / WebView2 缺失 / 离线安装：仍为 UNVERIFIED（与 C12b 相同；本卡未改变
  WebView2 部署或签名策略）。
- 安装包未签名；SmartScreen/企业策略风险未在本卡消除。

### 8.3 截图矩阵（Lab，`evidence/final4/`）

| 状态 | 880×560 | 1280×800 | 1600×1000 | 备注 |
|---|---|---|---|---|
| 无候选 + 固定“开始审查” | ✓ | ✓ | ✓ | 主操作在固定操作条，首屏可达 |
| D3 外部发送确认 | ✓ | ✓ | ✓ | provider/model + “只生成候选或 WAIT” |
| pending 候选差异 | ✓ | ✓ | ✓ | changed 字段展开；来源默认折叠 |
| 来源抽屉 | ✓ | ✓ | ✓ | 三态来源；Event/Record 无正文 |
| committed + 回执 | ✓ | ✓ | ✓ | 回执字段过滤 state_revision |
| rejected + 已暂停 | ✓ | ✓ | ✓ | 旧候选只读；重新授权入口 |
| reauth | ✓ | ✓ | ✓ | 无 rerun ref 时开始审查禁用 |
| WAIT | ✓ | ✓ | ✓ | 无候选；工作项完成 |
| legacy | ✓ | ✓ | ✓ | 只读；无候选/审批 |
| readonly 权限矩阵 | ✓ | ✓ | ✓ | can_approve 提示 true 但按钮禁用 |
| pending（delta，用于 readonly/DPI） | ✓ | ✓ | ✓ | — |
| DPI 200% | ✓（1760×1120 物理 / 872×541 CSS / dpr=2，逻辑 880×560） | — | — | Review 详情 |
| 中文 IME | ✓（872×541 CSS / dpr=2） | — | — | 组合期间 Ctrl+Enter 不发送，`imeGuard=true` |

共 35 张 PNG。Review 本身没有文本输入，因此 IME 证据在共享外壳/Consult
输入区取得；Review 切换不改变输入法组合守卫。

### 8.4 资源（Review，7 次运行）

工具：PowerShell `Get-Process` 的 `WorkingSet64` / `PrivateMemorySize64`；按
app 进程树求和并补入本应用 WebView2 用户数据目录的进程；每次运行取 5 个
样本的中位数，再取 7 次运行的中位数。测量使用同一 Review 数据流与 sidecar；
最终 UI 在测量后只增加固定“开始审查”操作条按钮与品牌图标，未改变权限、
数据路径或 CSP。

| 档位 | Working set (MiB) | Private (MiB) | 备注 |
|---|---:|---:|---|
| Review idle（候选详情已加载） | 464.29 | 246.55 | 范围 WS 461.25–466.71；private 242.80–300.47 |
| Review interaction（1,500 ms 模型延迟） | 470.23 | 260.78 | 范围 WS 462.46–483.44；private 249.42–317.64 |
| C12b full idle 基线 | 454.26 | 232.33 | C12b 记录 |
| C12b interaction peak 基线 | 486.96 | 266.11 | C12b 记录 |

比较：

- Review idle WS +2.2%，private +6.1%（相对 C12b full idle）。
- Review interaction WS −3.4%，private −2.0%（相对 C12b interaction peak）。
- private 样本存在一次明显的 300/317 MiB 离群值；中位数仍在 C12b 记录的
  波动区间附近。未出现持续增长、崩溃或 OOM。
- 未为降低资源关闭 CSP、发送门、capability、沙箱、权限检查或移除 Review
  信息；没有引入 UI 框架。

### 8.5 仓库与隔离

- `python tools/check_repository.py`：246 files / 37 commits / 373 blobs，
  passed，errors=[]。
- `git status --porcelain`：提交后为空。
- ignored 扫描：仓库内 ignored 路径只有构建缓存、生成图标/品牌、Tauri gen 与
  `__pycache__`；无 SQLite/私钥/凭据/生产 DB；交付前已清理。
- `git remote -v` 有 origin，但本分支无 upstream；未 push、未合并 main。
- 未进入 C13；未修改 `src/quintessence_pcs`（C06/C07/PRE 语义），仅在
  `tests/collaboration/test_review_candidate_read.py` 增加一个 C12c 边界测试。

## 9. 实验隔离与清理

- 实验数据：仓库外 Lab 根目录的 `build/c12c/lab/...`；构建/安装/证据：
  同一 Lab 根目录的 `build/c12c/...` 与 `install/c12c`。
- 每个完整实验后清理临时 DB/日志/候选；保留非敏感 JSON/截图/命令输出。
- 不清理、不读写仓库外正式私人实例目录或正式 Lab 之外目录。
- 仓库内不保留 SQLite、私钥、node_modules/target/安装包/日志；构建产物在 Lab。

## 10. 明确未做的事

- 没有进入 C13；没有实现 Consult 候选、DSH Work/Memory/Skill、语义检索、ACL
  平台或真实模型研究。
- 没有修改 C06/C07/C12c-PRE 的 resolve/commit/result 语义、迁移或 PCS 错误码。
- 没有实现 feedback、批量批准、取消、历史、`review.commit`、多版本审批或 UI
  内编辑候选。
- 没有使用正式私人实例、真实 provider 或私人 Persona/Memory 正文。
- 没有 push，没有进入 main。

## 11. 提交信息

```text
feat(desktop): add the Review workspace and approval flow (C12c)
```
