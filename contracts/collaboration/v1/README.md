# collab.v1 协作契约（C05a / REV-C）

本目录在**建表之前**冻结协作层的数据契约：对象、引用、授权、删除与版本语义。
C05b（存储）与 C08（consult Context）必须按同一套语义实现，不允许各自定义
字段或判定。

- 状态：仅契约设计与静态校验；**不建表、不写迁移、不实现业务逻辑、不调用
  真实模型、不访问网络、不携带私人数据**。
- 基线：C04 提交 `1034689`（二级验收后派发）；分支 `feat/llm-pcs-collaboration`。
- 校验器：`../../tests/contract/contract_checker.py`（仅标准库、fail-closed）。
  只使用该文件实际实现的关键字；本目录没有 `allOf`/`if`/`then`/`format`。
- 测试：`tests/contract/test_collaboration_contract.py`（8 个用例：Schema 文件、
  公共定义、对象字段、权限分离、Schema 无效样例、语义无效样例、两条静态链）。

## 文件

| 文件 | 作用 |
|---|---|
| `_common.json` | 公共 `$defs`：UUID、namespace、时间、摘要、引用、`attempt_ref` |
| `source_ref.json` | `SourceRef` 薄封装，形状在 `_common.json`，不重复实现 |
| `interaction.json` | 一次 consult / review 交互 |
| `input.json` | 不可变的协作输入版本 |
| `artifact.json` | 普通产物（reply / query_result）的存储态 |
| `candidate.json` | review 候选的存储态（含生成与提交两条授权依据） |
| `work_control.json` | 每个 work 的 generation、拒绝阻断与重跑授权 |
| `context_binding.json` | `context_snapshots` 行的 collab.v1 规范表示 |
| `source_link.json` | Context / artifact 到来源的持久派生边 |
| `grant.json` | 五分支授权：access / egress / commit(policy) / commit(work_generation) / rerun |
| `experience_view.json` | 唯一完整读取视图（readability 只存在于读取侧） |
| `experience_unavailable.json` | 无存在性泄漏的不可用读取结果（只有调用方 ref 与固定状态） |
| `examples/consult-chain.json` | consult 静态链（含删除与空读集变体） |
| `examples/review-chain.json` | review 静态链（拒绝、重跑、人工批准） |
| `examples/invalid.json` | Schema 层与语义层无效样例 |

## 版本与命名

- 所有 DTO 的 `schema_version` 固定为 `"collab.v1"`。改形状必须新建目录，
  不允许原地修改本目录的已冻结文件。
- 引用一律是**类型化对象**，裸 ID 不可表示。`ref_type` 的取值：
  `event`、`record`（pcs.v2）与 `collab_input`、`collab_artifact`（本契约）。
- `namespace` 与 pcs.v2 相同；所有引用都必须落在 owner 的 namespace 内。
- 时间戳是 UTC RFC 3339（`...Z`）；未知或尚未到达的时间是 `null`，不编造。
- `sha256_digest` 是带前缀的 `sha256:<64 hex>`；`hex64` 是裸 64 位小写十六进制。
  两个字段不混用：`request_hash`、`content_digest`、`proposal_digest`、
  `content_hash` 用前者；`candidate.command_digest` 用后者，因为它的值必须
  逐字等于 Tx B 已经存储的 `_request_digest`。
- `retention` 是**有序闭集** `session < standard < durable`（最严格 → 最宽松）：
  `session` 会话结束即不可读，`standard` 由存储层策略表解析时长，`durable`
  长期保留直到显式删除。具体天数不写进契约；过期在每次读取时重判，不缓存。
- `readability` 只出现在读取侧（`experience_view.json` 与
  `experience_unavailable.json`）。存储对象没有可读状态字段。

## 对象语义

### interaction

`mode` 为 `consult` 或 `review`。review 必须同时携带 `trigger_ref`、
`work_id`、`work_generation`；consult 三者必须为 `null`（R12）。
`result = null` 表示交互仍打开；其余九个值是终态。
WAIT 的终态是 `result = "waiting"`、`receipt_request_id` 非 `null`、且没有
candidate。`completed_at` 在打开时为 `null`。
`input_refs` 允许为空：review 可以不新增输入。`access_ref` 与 `policy_ref`
是不透明授权/策略引用，不参与结构推导。

### input

正文创建后不可原地修改（R3）。改写正文 = 新 `input` 版本；`supersedes`
指向被替代的旧版本。`content_digest` 是 `body` 的 UTF-8 字节摘要。

### artifact / candidate

`artifact` 是普通产物的存储态；`candidate` 是独立 Schema，字段显式重复，
并额外携带 proposal、work generation 与两条授权依据。candidate 的
`artifact_id` 就是 candidate id，`artifact_version` v1 恒为 1。

- `proposal` 是解析并领域规范化后的提交依据；其规范 JSON 就是 C05d 传给
  `commit_command` 的 `request_payload`（不含 `request_id`/digest）。
- `proposal_digest` 是 `canonical(proposal)` 的 UTF-8 摘要。人工批准必须精确
  绑定 `candidate_id + proposal_digest + context_id + request_id`。
- `command_digest` 必须等于 Tx B 的
  `_request_digest(namespace, operation, context_id, request_payload)`。
- `structural_validation` 恒为 `"passed"`。结构校验失败的输出不是 candidate。
- `retention` 是创建时赋予的有序保留标签；artifact/candidate 的保留不得宽于
  其协作来源（R15），没有协作来源时由宿主按策略默认值赋值。
- **`rerun_authorization_ref`**：生成本候选所依据的 rerun grant；generation 0
  且从未被拒绝时为 `null`。
- **`commit_authorization_ref`**：提交本候选所依据的 commit grant；人工批准
  路径为 `null`。两个字段互不代替，也互不推导。
- 只有 `status`、`decided_at`、`decision_ref` 在创建后变化。

存储 DTO 冻结的是**创建形状**；删除清理 payload 后，存活行只通过
`experience_view` 读取，不会被重新当作 DTO 校验，也不会暴露正文。

### work_control

唯一键 `(namespace, work_id)`。`generation` 只在拒绝事务里 +1（R4），这是
全系统唯一的递增点。拒绝同一事务内还要：candidate → `rejected`、
`blocked_after_rejection = true`、`control_version += 1`、
`rerun_authorization_ref = null`。

`rerun_authorization_ref` **只存 rerun grant**。本表不保存、也不推断任何提交
许可。重新授权（R6）只写该字段并 `control_version += 1`：不改 generation、
不清 blocked、不产生提交许可。

### context_binding

`context_snapshots` 一行的规范表示。`context_kind = review` 时
`envelope_version = "pcs.v2"`，其 `read_set`、三个水位、`profile_ref`、
`content_hash` 必须与同一 `context_id` 的 pcs.v2 envelope 完全一致；
`context_kind = "consult"` 时 `envelope_version = "collab.v1"` 且
`trigger_ref = null`。consult 行不能被 pcs.v2 读取器解析，review 行必须与
envelope 一致（R12）。

`read_set` 只含 `RecordRef`（`read_set_entry.ref` 的类型即约束）；协作来源
单独放在 `collab_source_refs`，不进入 read_set。`profile_ref` 在 v1 装配中
必须非 `null`；`null` 不得用于掩盖缺失，只允许与空 read_set 同时出现（该
组合结构合法，但 v1 装配不会产生）。

### source_link

持久派生边。`owner_kind` 为 `context` 或 `artifact`；context 的
`owner_version` 为 `null`，artifact 的必须等于该 artifact 的
`artifact_version`。owner 与 source 同 namespace。artifact 的持久 links 的
source 集合必须等于其 `source_refs`。

### grant

公共字段：`grant_id`、`namespace`、`schema_version`、`kind`、`grant_version`、
`subject`（host instance）、`authorization_ref`、`issued_at`、`expires_at`、
`revoked_at`。五个分支由 `oneOf` 互斥：

| 分支 | 额外字段 | 允许 | 绝不隐含 |
|---|---|---|---|
| `access` | `purposes`、`data_classes` | 按 purpose 读取所列隐私类 | 外发、提交、重跑 |
| `egress` | `provider`、`endpoint`、`model`、`manifest_hash`、`privacy_classes`、`purposes`、`model_authorization_ref` | 把所列隐私类发给该 provider/model | 提交、重跑、提高账本上限 |
| `commit` + `scope=policy` | `operations`、`trigger_kinds` | 覆盖范围内免人工批准提交 | 重新执行 |
| `commit` + `scope=work_generation` | `work_id`、`generation` | 该 generation 免人工批准提交 | 重新执行 |
| `rerun` | `work_id`、`generation` | 在该 generation 开始新一轮生成 | **提交、外发、读取** |

- `revoked_at` 非 `null` 即失效；每次使用按当前时间重判 `expires_at`，不缓存。
- egress 与 `model_authorizations` 是**两道独立闸门**：账本管 calls/预算/
  manifest，egress 管“该类私人内容能否发出”。两者必须同时有效，egress
  不能抬高账本上限。

## 权限分离（R13）与单一检查点（R14）

四种许可是独立对象，**互不推导**：

```
access  ≠  egress  ≠  commit  ≠  rerun
```

- rerun 只允许开始新一轮生成，绝不隐含提交；
- commit 只允许在满足其余条件时免人工批准提交，绝不隐含重新执行；
- egress 绝不隐含提交或重跑；
- 未来记忆整合许可不属于本期，也不得由以上任一许可推导。

### 开始 gate

```
review_start_decision(namespace, work_id) -> Decision(allowed, reason, generation)
allowed = !blocked_after_rejection
          OR 存在有效 rerun grant 且 (work_id, generation) 与 control 完全一致
```

必须在任何模型调用之前求值；拒绝时零 HTTP、零模型调用。

### 提交 gate

```
commit_decision(namespace, candidate_id, approval_ref=None) -> Decision(allowed, reason, generation)
allowed = candidate.status == "pending"
          AND candidate.work_generation == control.generation
          AND ( 人工批准精确绑定 candidate_id + proposal_digest + context_id + request_id
                OR 存在有效 commit grant，覆盖 (work_id, generation) 或
                   policy 的 operation/trigger_kind )
```

必须在 Tx B 写事务内、`apply` 之前求值。

两个判定**不看对方许可**：rerun grant 不能出现在提交判定，commit grant 不能
出现在开始判定，egress/access 两者都不能。C05b 提供且只提供这两个共享判定；
`run_step`（旧单步）、Review Facade、宿主 pending-work 扫描、候选决议四个
入口一律调用，禁止内联复制。

## 规则 R1–R15

- **R1 协作引用不是证据**：协作引用不得出现在 `evidence_links`、
  `RecordVersion.source_refs`、`read_set`。
- **R2 结构合法 ≠ 证据有效 ≠ 已提交**：合法 JSON 不代表来源有效或命令已提交；
  引用必须同 namespace、外部 ref 必须在 `synthetic_externals` 中声明，
  `source_refs` 与持久 links 必须一致。
- **R3 内容不可变**：input/artifact 正文不原地改；改写 = 新 input，修复/
  重生成 = 新 candidate；只有 candidate 的 `status`/`decided_at`/`decision_ref`
  可变。
- **R4 拒绝终态与唯一递增点**：同一事务内 candidate→`rejected`、
  `generation += 1`、blocked=true、`control_version += 1`、
  `rerun_authorization_ref = null`；generation 不在别处递增；旧候选永久
  `rejected`。
- **R5 阻断对所有入口有效**：换 interaction/session/request_id/重启都不能
  绕过；开始 gate 只在起始处读取当前 control。
- **R6 重新授权只解锁重跑**：用户明确要求重新考虑后，可信宿主创建 rerun
  grant（work_id + 当前 generation），在受信事务内写
  `control.rerun_authorization_ref` 并 `control_version += 1`；不改
  generation、不清 blocked、**不产生任何提交许可**。
- **R7 删除与闭包**：`delete_experience(ref)` 只接受 collaboration_ref；
  barrier 先行（立即读拒绝，`privacy_epoch`/`state_revision` 各 +1），
  cleanup 可重试；闭包 = 该 ref 在 `collab_source_links` 中的全部 owner
  递归；清理范围含 input 正文、派生 artifact/candidate 正文、Context 渲染
  正文、相关 attempt 的 request/response payload；保留 id/版本/时间/digest/
  状态/授权引用/namespace；artifact→artifact 链深度上限 8，超限硬失败；
  pcs.v2 的 `request_deletion` 不变。
- **R8 协作元数据不推进认知 `state_revision`**：删除仍按既有规则推进两个
  水位；consult 的读写不产生认知提交。
- **R9 请求与账本**：`attempt_ref.request_hash` 必须等于
  `model_attempts.request_hash`；attempt 的 `context_id` 必须等于
  `binding.context_id`；渲染输入必须逐字出现在请求正文中（C09 验证）。
- **R10 Context 失效**：任一水位变化、来源被删、profile 变化后，旧 Context
  不可继续发送或提交；历史回执仍可查。
- **R11 保留期限**：retention 生效；删除/过期后 ref 解释为
  `redacted`/`unavailable`；`body` 非 `null` 当且仅当
  `readability = "available"`。`experience_view.retention` 必须等于其所
  引用对象的 retention（视图不重新解释策略）。
- **R12 读取边界**：consult 的 binding 不能被 pcs.v2 读取器解析；review 的
  binding 必须与 envelope 一致。
- **R13 权限不互相推导**：见上一节。
- **R14 单一检查点**：见上一节。
- **R15 派生保留不得宽于上游来源**：对每个 artifact/candidate，其 retention
  的顺序必须**不宽于**它全部 collaboration source 的 retention（多来源取
  最严格者）；没有 collaboration source 时（例如 review 无新增输入）不施加
  上游约束，由宿主按策略默认值赋值。该规则只约束协作来源；pcs.v2 的
  event/record 没有保留标签，它们始终由 R7 的删除屏障覆盖。retention 永远
  不能覆盖删除：删除优先，删除后按 R7 清理。

## 删除闭包

```
delete_experience(collaboration_ref)
  Tx1 barrier:  deletion_targets 行 + privacy_epoch +1 + state_revision +1
  Tx2 cleanup:  清 input body
                清派生 artifact/candidate body
                清 Context rendered_input
                清相关 attempt request/response payload
  status:       pending_cleanup -> cleaned（可重试、可跨进程恢复）
```

保留：id、版本、时间、digest、状态、授权引用、namespace。删除后
`experience_view` 对同一 ref 返回 `redacted`/`unavailable` 且 `body = null`；
不返回摘要、片段或计数。`collab_source_links` 的 owner 递归就是闭包；
深度按 artifact→artifact 边计算，超过 8 硬失败，不做静默截断。

## 读取接口与结果

```python
list_experience_refs(access, *, namespace, after_cursor=None, limit=20)
read_experience(access, *, ref, purpose)
    -> experience_view | experience_unavailable
```

`read_experience` 返回两个 DTO 的联合：

- 对象存在且调用者有权访问 → `experience_view`。其 `readability` 为
  `available` / `redacted` / `unavailable`，元数据真实；`unavailable` 表示
  对象自身已被删除，`redacted` 表示派生对象仍有元数据但正文不可读。
- 对象不存在、调用者无权访问、或来源隐私无法解析 →
  `experience_unavailable`。该 DTO 只有调用方自己提供的 `ref` 与固定状态
  `"unavailable"`，与“不存在”的返回完全同形，不泄漏存在性。

`experience_unavailable` 是读取结果，不是存储对象；存储表没有可读状态列。

未经任何 interaction 关联的 input 不是 experience：`list_experience_refs`
不列举它，`read_experience` 对它返回 `experience_unavailable`。

input 的 `interaction_id` 是**代表性关联（representative association）**：
在全部链接该 input 的 interaction 中，按
`(interaction.created_at, interaction_id)` 字典序最小者。它不是该 input 的
永久来源身份，也不是唯一来源；正常追加更晚的链接不会改变该归属。两个字段
都是持久列，重开数据库后结果不变。

`list_experience_refs` 先按可见性过滤再应用 `limit`：不可见对象不占页位、
不影响 `next_cursor`，页大小不暗示其存在；游标指向最后一条已返回记录。
`purpose` 必填并进入审计；不做语义搜索、打分、摘要或 Episode。
未授权统一 `unavailable`，不因“部分可读”而返回片段。

## 两层静态校验

### 层 S（Schema）

只用 `ContractChecker`。`examples/invalid.json` 的 `layer = "schema"` 样例声明
`schema`、`instance`、`expect_path`、`expect_keyword`，测试断言该
`(path, keyword)` 确实出现在失败集合中——不是“有错即可”。当前 16 条。

### 层 D（语义）

`check_semantics(objects)` 是测试内的纯函数，`objects` 是
`[{"type": ..., "value": ...}]` 列表，返回 `Violation(rule, path, message)`。
它不读文件、不连数据库。当前 19 条，覆盖：

- 自引用 / 成环 / artifact 链深度 > 8（R7）
- consult trigger 必须 null、review trigger 必须非 null（R12）
- review 的 trigger/work 字段完整性（R12）
- `readability` 与 `body` 的耦合、删除后仍可读、视图 retention 与对象一致（R11）
- `read_set` 只含 `RecordRef`（R1）
- binding 与 `context_snapshots` 一致（R12）
- `source_refs` 与持久 links 一致、artifact owner_version 一致（R2）
- 跨 namespace（R2）
- 外部 EventRef/RecordRef 未在 `synthetic_externals` 声明（R2）
- generation 单调性与候选一致（R4）
- attempt hash/context 与账本一致（R9）
- 派生保留不得宽于协作来源（R15）
- **rerun 冒充提交、commit 冒充重新执行、egress 冒充提交（R13）**
- 提交 gate 与冻结判定不一致（R14）

语义样例只声明 `rule`，不伪造 Schema 错误。

## 静态链

`examples/consult-chain.json`（12 步）：interaction → input → binding →
attempt → artifact → 两条 source_link → 两个 available 视图 → 删除 →
两个 redacted 视图。删除步骤重算闭包（context + artifact 两个 owner），
断言 `privacy_epoch`/`state_revision` 各 +1、清理五类正文。`alt` 的空读集
binding 只证明 Schema 不强制非空，并标注 v1 装配不会产生。

`examples/review-chain.json`（17 步）：interaction → pcs.v2 binding →
control(gen 0) → candidate(gen 0) → 拒绝 → control(gen 1, blocked) →
无 rerun 时 denied → rerun grant(gen 1) → control 写 rerun ref、gen 仍为 1 →
allowed → 新 candidate(gen 1) → 人工批准 → 提交 → interaction committed。
`alt` 覆盖：WAIT（result=waiting、receipt 非 null、无 candidate）、二次拒绝
（generation 1→2，旧 rerun grant 因 generation 不匹配失效，判 R5）、以及
“用 rerun grant 请求提交”（判 R13，commit denied）。

链中的每个摘要都由测试重新计算：`content_digest`、`proposal_digest`、
`command_digest` 与正文/规范 JSON 逐字对应。

## C05b / C08 接口边界

C05b（存储）必须：

- 新增 `collab_interactions` / `collab_inputs` / `collab_artifacts` /
  `collab_work_control` / `collab_source_links` / `collab_grants`；
- `context_snapshots` 增加 `context_kind`（默认 `'review'`）；
- `deletion_targets` 重建以容纳内部 `collab_input` / `collab_artifact`，
  公开 `request_deletion` 仍只接受 record/event；
- `commit_command` 加载快照后做 kind 检查：consult → `INVALID_TRANSITION`；
- `delete_experience` 走内部 target 校验 + 两阶段屏障；
- 反查合并：record/event 走 `context_sources` 与 `collab_source_links`，
  协作源走 `collab_source_links`；attempt payload 一并清理；
- 提供 `review_start_decision` 与 `commit_decision` 两个共享判定
  （名字可调，语义固定；`Decision` 至少含 `allowed`、`reason`、`generation`）；
- 不得修改 `context_sources`。

C06b/C07（执行入口）：`run_step`、Review Facade、宿主扫描、候选决议四处只
调用上述判定；开始 gate 在零模型调用处，提交 gate 在 Tx B 事务内。

C08（consult）：落 `context_snapshots(context_kind='consult')`、
`trigger_ref_json = NULL`；不建 work、不写 Belief、不刷新 SelfView；新增
collab.v1 读取入口；旧 `load_context` 对 consult 行拒绝；协作来源写
`collab_source_links(owner=context)`；`read_set` 只含 RecordRef。

## 本卡显式结构取舍（卡未逐字规定处）

1. 所有 DTO 的 `schema_version` 取常量 `"collab.v1"`，作为目录级版本锚点。
2. `retention` 是有序闭集 `session < standard < durable`（最严格 → 最宽松）；
   只冻结标签与顺序，时长由 C05b 策略表解析，契约不编码天数。
3. `interaction.completed_at`、`candidate.decided_at`、`candidate.decision_ref`
   允许 `null`：打开/pending 状态不编造完成时间或决定引用。
4. artifact/candidate 的 `body` 上限取 262144 字符，与 input 正文同一上界；
   删除后的 payload 清理不改变 DTO 的创建形状。
5. `access_ref`/`policy_ref` 是不透明字符串（1–256）；本契约不对其内部形态
   作断言。
6. `attempt_ref.request_hash` 用 `sha256:<hex>`，与 `model_attempts` 的存储
   形式一致；`candidate.command_digest` 用裸 hex，与 `_request_digest` 一致。
7. `rerun_authorization_ref`/`commit_authorization_ref` 存对应 grant 的
   `grant_id` 文本（字符串，允许 `null`）；两个字段名与语义都独立。
8. `work_control.last_candidate_ref` 是最近候选的 `artifact_id`（或 `null`）。
9. `experience_view.context_ref` 是 `context_id`；`ref` 决定读取的是 input
   还是 artifact。
10. `grant.expires_at = null` 表示无到期时间；`revoked_at` 非 null 立即失效，
    优先于任何时间判断。

## 边界

本目录不建表、不写迁移、不改 `contracts/v2`、不改 `src/`、不改既有测试；
不实现 C05b/C06b/C07/C08 的业务；不调用真实模型或网络；不携带私人数据。
`MANIFEST.in` 已递归包含 `contracts/**`，无需修改。

## 运行

```powershell
python -m unittest tests.contract.test_collaboration_contract -v
python tools/run_tests.py
```
