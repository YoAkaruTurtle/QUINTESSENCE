# C12c-PRE — Review 候选读取、精确引用与审批前置契约

> 状态：C12c-PRE 已实施（仅公共读取接口、测试与契约）。
>
> 基线：`624aeb7`（C12b 已关闭；C12a 设计稿 APPROVED；C11 已关闭）。
>
> 日期：2026-10-24。
>
> 本文不授权、也不记录任何 C12c UI 编码。Review 工作区仍是占位；本卡只补齐
> “候选枚举、候选详情、证据来源”的 Python 读取契约，供 C12c 通过
> `desktop.v1` 接线。
>
> 所有候选、Belief、Context、Input 与来源均为合成 Lab 数据；未使用正式私人
> 实例，未调用外部 provider，未 push。

## 0. 一页结论

| 问题 | 结论 |
|---|---|
| 是否审计了真实 C07/C05 字段与授权？ | 是。事实表见 §1，全部来自当前基线代码，不按设计稿假定。 |
| candidate 身份是否带 exact version？ | 是。`ReviewCandidateRef` 与 `candidate_detail(artifact_version=...)` 均读取精确行；读取路径不硬编码 1。 |
| 生产候选是否仍是单版本？ | 是。C05d 对 `kind='candidate'` 建 `(namespace, artifact_id)` 唯一索引，`register_candidate` 拒绝非 1 版本。该索引不直接 CHECK 值为 1，因此新读取仍以行内存储值为准。 |
| 候选读取使用什么权限？ | `work.scan + experience.read`，并要求 `HostAccess.purposes` 含 `review`。不使用 `review.generate`。 |
| 枚举是否只读元数据？ | 是，只 SELECT `collab_artifacts` 元数据列，不读 `private_payloads`。 |
| 不可见候选是否会被列出？ | 否。隐私闭包不可满足或 deletion barrier 命中时整行不出现，不返回 `readable=false`。 |
| detail 是否返回原始 proposal dict？ | 否。`ReviewProposalView` 逐字段投影；`structural_validation` 是存储字符串 `passed`，不是对象。 |
| 三类 redaction 是否分离？ | 是。`redacted` 只表示 candidate 不可读；current 用 `current_state`，source 用 `availability`。 |
| `can_approve`/`can_reject` 是否替代 resolve？ | 否。它们只是状态提示，权限与最终校验仍由 `ReviewFacade.resolve` 和 C06/C07 事务完成。 |
| 现有 resolve 是否足够？ | 安全性足够：exact candidate、Work generation、Approval/grant、Context/删除、current Belief 版本都在写事务内复核。UI 可读性上，目标版本移动会以 `REVISION_CONFLICT` 异常而不是 `ReviewView` 返回；C12c UI 必须把它映射为 stale/冲突，本卡不改 resolve。 |
| WAIT 是否产生候选？ | 否。WAIT 走 `cognition.wait` 回执并完成 Work；候选枚举为空。 |
| Legacy 是否阻塞 PRE？ | 否。Legacy 没有 candidate/proposal/diff 的正式接口，属于能力缺口；UI 只读展示并标注，不读 DB 补字段。 |
| 是否新增 PCS 契约/迁移/错误码/ACL？ | 否。仅新增 Python DTO、Store 只读方法与 Facade 只读方法。 |
| 是否开始 C12c UI？ | 否。未修改 `apps/desktop/`，未接 `desktop.v1`，未进入 C13。 |

## 1. C07/C05 事实表

以下行号基于基线 `624aeb7` 加本卡改动后的工作树；路径均为仓库相对路径。

### 1.1 候选字段与身份

| 事实 | 核对结果与证据 |
|---|---|
| candidate ref/version | `collab_artifacts` 主键是 `(namespace, artifact_id, artifact_version)`；`artifact_version` 是逐行真实存储值（`src/quintessence_pcs/resources/migrations/0008_collaboration.sql` 199–245）。`load_artifact` 按三元组精确读取（`src/quintessence_pcs/store/collaboration.py:1374`）。 |
| 单版本索引 | `0010_candidate_request_key.sql:18` 建 `collab_candidates_single_version_idx ON collab_artifacts (namespace, artifact_id) WHERE kind='candidate'`。它保证同一 artifact_id 最多一个 candidate 行，但不直接约束该行版本等于 1。 |
| 生产写入约束 | `register_candidate` 显式拒绝 `artifact_version != 1`（`store/collaboration.py:2297`）；其内部 `register_artifact(..., artifact_version=1)`。低层 `register_artifact` 是 trusted primitive，不是生产候选链。 |
| 现有调用是否硬编码 1 | 是：Facade `_load_candidate`（`collaboration/facade.py:1667`）与 Service `_load_candidate`（`collaboration/service.py:973`）都用 `artifact_version=1`。这是“受支持生产候选恒为 v1”的读取假设，不是新读取路径可以继承的规则。 |
| 结论 | exact version 的权威来源是 `collab_artifacts.artifact_version` 行值。PRE 的枚举与详情按行值/调用方 exact version 读取，不假定 1；`resolve` 仍只支持 v1，因此 PRE 对非 1 版本把 `can_approve`/`can_reject` 置 false，避免 UI 承诺 resolve 无法执行的动作。 |
| status/decision | `status` 取值 `pending/committed/rejected/stale/failed`（`0010` 前的 0008 CHECK，`store/collaboration.py:88`）。`decided_at`、`decision_ref`、`work_id`、`work_generation` 均为 `collab_artifacts` 列。写入点：`register_candidate` → `pending`；`reject_candidate` → `rejected` 且把旧 generation 的 pending 置 `stale`；`_mark_candidate_committed_locked` → `committed`。删除复核路径也可能把 pending 置 `stale`。`failed` 是冻结枚举值，但当前生产写链没有主动写入点，读取不得把它推断为 committed。 |
| proposal | `proposal_payload_ref`、`proposal_digest`、`proposal_version` 是 candidate 非空列（0008 CHECK）。proposal 正文存于 `private_payloads`，`load_artifact` 读回并校验 `proposal_digest`。 |
| structural_validation | 是 **字符串**，不是对象：0008 约束 `structural_validation IS NULL OR structural_validation = 'passed'`；candidate 链写入字面量 `"passed"`（`store/collaboration.py` 候选插入分支）。候选行该列非空。PRE 只返回该字符串。 |
| attempt | `attempt_ref_json` 是可空 TEXT，0008 约束长度 2–16384；`load_artifact` 用 `_decode_json`（strict JSON）解码（`store/collaboration.py:1417`）。C07 review 生成候选时不传入 attempt，因此正常 review candidate 的 `attempt_ref` 是 `None`；PRE 的元数据读取同样只解码该列，不读 payload。 |
| committed 的权威 | 不能只看 `status='committed'`。`_mark_candidate_committed_locked` 要求 `processed_commands` 中存在 request_id、context_id、operation、digest 与 proposal 完全匹配且 outcome 为 `committed/noop` 的回执（`store/collaboration.py:2725`）。`commit_candidate` 在提交后重新读取 candidate 并要求 status 已变为 committed（`service.py:179`）；`get_result` 以 receipt 为唯一 committed 权威（`service.py:504`）。 |

### 1.2 审批路径

| 事实 | 核对结果与证据 |
|---|---|
| `_approval_for` | 从已存储 candidate 构造 `Approval(candidate_id, proposal_digest, context_id, request_id)`（`facade.py:1694`）。`_commit_decision` 逐项相等比较四项；任一项不同都不允许（`store/collaboration.py:3170–3195`）。 |
| `resolve_candidate` | approve 复用 `commit_candidate`，reject 复用 `store.reject_candidate`。`ReviewFacade.resolve` 先检查 `review.approve`/`review.reject`，再按 candidate_id 读取 exact candidate，并要求 interaction 的 `host_instance` 等于 access 的 host（`facade.py:739`，`_require_own_interaction`）。 |
| Work generation | `_commit_decision` 要求 candidate 是 `pending` 且 `candidate.work_generation == control.generation`；`_CandidateCommitGuard.before_apply` 在同一 Tx B 内再次调用 `require_current_candidate`（`service.py` guard + `store/collaboration.py:2814`）。 |
| 删除/权限 | Commit guard 在 Tx B 内重跑 `require_live_context` 与 `artifact_barred`；Facade 的 resolve 入口检查权限与 host instance。Reject 由 `reject_candidate` 在事务内重新读取 candidate/control 并证明 current。 |
| receipt | Commit 成功产生 `processed_commands` receipt；`_mark_candidate_committed_locked` 在同一事务内标记 candidate；Facade 的 `_disclose_receipt` 在返回前重新应用当前 `result.read`、Context/trigger 删除与 experience 可读性检查。 |
| `CommitAuthority` | `CommitAuthority` 的 approval 与 grant_id 互斥（`collaboration/policy.py`）。Facade 只接受前端提供的 `grant_id`；默认路径由 `_approval_for` 从存储 candidate 构造 Approval，前端不能提交 Approval 对象。 |
| 重复提交 | committed → `already_committed` 并返回存储 receipt；rejected → approve 返回 `rejected`（不写）；reject 在相同 decision_ref 下幂等，不同 decision_ref 拒绝。stale/failed → approve 返回该状态且不写；reject 对 stale/failed 抛 `CandidateTransitionDenied`。以上由 `resolve_candidate` 分支与 `reject_candidate` 实现。 |
| current target | C07 的 `_commit_decision` 本身不重读目标 Belief；真正的 current 校验在 `BeliefService._revise` 的 Tx B `apply` 内：`get_current` 后要求 `current.record_version == target.record_version`，否则 `StateRevisionConflict`（`domains/cognition/beliefs.py:459–470`）。retract 在 `submit_proposal` 和 `_revise` 两处做同样检查（`beliefs.py:314–322`）。propose 没有 target。 |
| 不满足时 | 目标版本移动会抛出 `REVISION_CONFLICT`，整个 Tx B 回滚，不产生 receipt/新 Belief。Facade 不捕获该异常，因此 C12c UI 必须把它显示为 stale/冲突，不能显示为“已提交”。PRE 不修改 resolve。 |

### 1.3 权限语义

`FACADE_PERMISSIONS` 是 C07 的封闭集合（`facade.py:95`）。实际职责：

| 操作 | 权限 | 证据/判断 |
|---|---|---|
| Work 列表（managed/legacy） | `work.scan` | `managed_work`/`legacy_work` 各自 `_require_permission`。 |
| 候选列表/详情 | **`work.scan + experience.read`** | 本卡新增读取组合。候选属于 Work，故需 `work.scan`；候选正文/来源与可见性由 C05c experience 读取语义治理，故需 `experience.read`。 |
| 来源正文（input/artifact） | `experience.read` | `read_experience`；同时要求 purpose `review` 在 `HostAccess.purposes` 中。 |
| 生成候选 | `review.generate` | `preview` 与 `review`。该权限不用于候选读取。 |
| 批准 | `review.approve` | `resolve(decision="approve")`。 |
| 拒绝 | `review.reject` | `resolve(decision="reject")`。 |
| 结果/回执 | `result.read` | `result` 与 `_disclose_receipt`。 |
| `can_approve`/`can_reject` | 不是权限 | 只表示内容/状态是否适合显示可执行按钮；真正权限仍由 `resolve` 检查。调用方若没有 `review.approve`/`review.reject`，必须在 UI 另行禁用。 |

结论：读取、生成、批准、拒绝保持四个不同授权含义。PRE 没有新增权限/ACL；没有用 `review.generate` 冒充读取权限。若现有权限无法表达候选读取，本卡应停止；审计确认 `work.scan + experience.read` 是现有合法组合，因此继续。

purpose 说明：`read_experience` 要求调用方声明 purpose，且 purpose 必须在 `access.purposes` 中。PRE 固定使用 `review`。若 `HostAccess.purposes` 不含 `review`，列表不显示候选，详情返回 redacted；这不是新增权限，而是 C05c 既有的 purpose 审计边界。C12c 接线必须构造含 `review` 的 HostAccess。

### 1.4 WAIT / Legacy / 来源

| 事实 | 核对结果 |
|---|---|
| `ReviewFacade.review` 返回 waiting 时是否有 candidate | 没有。`_review_wait` 只提交 `cognition.wait` 并完成 Work；不调用 `register_candidate`，不写 candidate 行（`facade.py` `_review_wait`）。 |
| WAIT 是否可普通批准 | 不可以。WAIT 没有 candidate_id，`resolve` 无法命名候选；`result` 只返回 waiting 回执。 |
| legacy_work 可安全读取的字段 | `PendingWork` 的 work_id/event_id/kind/status/created_at 由 `unfinished_work` 返回；`legacy_work` 只减去有 control 的 Work。没有 candidate/proposal/diff 的正式读取。 |
| legacy 审批 | 不能。没有 candidate id/control；即使伪造 candidate id，resolve 也会因 exact candidate 不存在或 interaction host 不匹配而失败。 |
| `read_experience` 对 candidate artifact | 通过 C05c 完整路径：namespace、purpose、隐私闭包、deletion barrier、payload digest 都检查；不可见返回 `ExperienceUnavailable`，barrier 情况返回 `ExperienceView(readability=redacted/unavailable, body=None)`。 |
| 当前 Belief 的 exact version/privacy/删除 | `store.get_version(ns, record_id, record_version)` 精确读取；`DeletionMixin` 在读取前抛 `Redacted`；privacy 是 `record_versions.privacy_class`，PRE 在 Facade 中显式比较 `access.privacy_classes`。`get_current` 只用于判断目标是否仍是 current，不替代精确读取。 |

安全判断：managed/legacy 可区分；legacy 无法被批准；候选与来源有 redaction；候选状态可确定。未发现触发整体停止的安全违规。Legacy 缺少 candidate count/额外时间戳等非关键展示字段，按能力缺口/只读降级处理，不停止 PRE。

## 2. C12c-PRE DTO 契约

以下 DTO 位于 `src/quintessence_pcs/collaboration/facade.py`，由包 `__init__` 导出。它们是 Python frozen dataclass，不是新的 PCS JSON 契约；C12c 的 `desktop.v1` 适配层可以在后续卡中逐字段映射。

### 2.1 ReviewCandidateRef

```python
@dataclass(frozen=True)
class ReviewCandidateRef:
    candidate_id: str
    artifact_version: int
    work_id: str
    work_generation: int
```

- 全部来自 `collab_artifacts` 元数据行，包括 exact `artifact_version`。
- 读取路径不写 `artifact_version=1`；调用方必须把该 ref 原样传回 `candidate_detail` 与后续审批绑定。
- PRE 只提供引用，不修改 `resolve`。

### 2.2 ReviewCandidateSummary

```python
@dataclass(frozen=True)
class ReviewCandidateSummary:
    candidate: ReviewCandidateRef
    interaction_id: str
    context_id: str
    status: str
    created_at: str
    decided_at: str | None
    decision_ref: str | None
    attempt_ref: dict | None
```

- 只读 metadata；不读 body/proposal payload。
- 不可见候选不出现在列表中，不以 `readable=false` 泄露存在性。
- 空结果不区分“无候选”和“无权候选”。

### 2.3 显式投影

```python
@dataclass(frozen=True)
class ReviewProposalView:
    operation: str | None
    target_ref: RecordRef | None
    claim: str | None
    claim_type: str | None
    scope: dict[str, Any] | None
    evidence_links: tuple[dict[str, Any], ...]
    strength: str | None
    strength_reason: str | None
    valid_until: str | None
```

- propose/revise 的 claim/claim_type/strength/strength_reason/valid_until/scope 来自已校验 body；target_ref 仅 revise/retract。
- retract 没有 belief body：claim/claim_type/scope/strength/valid_until 为 None；`evidence_links` 是 proposal 的 exact links；模型给出的 `reason` 放在 `strength_reason`（DTO 没有单独 reason 字段，避免新增字段/契约）。该映射在 UI 文案中必须标为“撤回理由”，不能伪装成强度理由。
- `evidence_links` 只在 proposal 可读时返回；candidate redacted 时为 None proposal。

```python
@dataclass(frozen=True)
class ReviewCurrentView:
    ref: RecordRef
    claim: str
    claim_type: str
    status: str
    strength: str
    scope: dict[str, Any]
    valid_until: str | None
    applicability: str
```

- `ref` 是候选 proposal 的目标 exact RecordRef。
- `applicability` 是存储 scope 的 UI 标签：空 scope 为 `general`，否则按 J.2 字段顺序渲染 `field=value`。
- current 不可读时 `current=None`，由 `current_state` 区分 `redacted`/`missing`；不可读不等于空 Belief。

```python
@dataclass(frozen=True)
class ReviewDiffField:
    field: str
    before: Any
    after: Any
    changed: bool
```

- revise：比较 claim、claim_type、scope、evidence_links、strength、valid_until。
- propose：before 为 None、after 为候选值、changed=True；这是“新增”而不是“与空 Belief 比较”。
- retract：只比较 evidence_links（其余字段不适用）。
- current 为 redacted/missing 时 diff 为空元组，绝不以空 Belief 代替。

```python
@dataclass(frozen=True)
class ReviewSourceView:
    ref: dict[str, Any]
    kind: str
    availability: str       # available/redacted/unavailable
    reason: str | None      # 公开原因标签
    body: str | None        # 仅 read_experience 可披露时
    ref_version: int | None
```

- `sources` 是 candidate artifact source links 与可读 proposal evidence refs 的并集，按 exact ref 去重。
- input/artifact 通过 `read_experience` 读取；event/record 只返回 ref/availability，不读 event body。
- event 的 `ref_version` 为 None；record/input/artifact 为 exact version。
- `reason` 只用公开标签：`deleted`/`privacy`/`not_found`/`unavailable`/`redacted`；不含正文、隐藏记录或内部计数。

```python
@dataclass(frozen=True)
class ReviewCandidateDetail:
    candidate: ReviewCandidateSummary
    proposal: ReviewProposalView | None
    structural_validation: str | None
    current: ReviewCurrentView | None
    current_state: str      # not_applicable/available/redacted/missing
    diff: tuple[ReviewDiffField, ...]
    sources: tuple[ReviewSourceView, ...]
    can_approve: bool
    can_reject: bool
    eligibility_reason: str | None
    redacted: bool
    detail: str | None
```

- candidate redacted：proposal=None、structural_validation=None、current=None、current_state=`not_applicable`（不声称目标不存在，只表示无法判断）、diff/sources 为空、两个 can 均 false、`redacted=True`、detail=`candidate_redacted`。
- `detail` 只放公开标签/错误码，不放正文、proposal、异常字符串。

## 3. 方法

### 3.1 candidates

```python
candidates(
    access: HostAccess, *, work_id: str, generation: int | None = None
) -> tuple[ReviewCandidateSummary, ...]
```

- 要求 `work.scan + experience.read`；不要求 `review.generate`。
- Work 必须在 `access.namespace`；跨 namespace 的 work_id 只得到空结果。
- 只读 `collab_artifacts` 元数据；稳定排序 `(created_at, artifact_id, artifact_version)`。
- `generation` 是 exact filter；`None` 表示该 Work 的所有 generation。
- 可见性复用 C05c 的隐私闭包与 deletion barrier；不可见候选整行不出现。
- 不写 audit 行、不写数据库。

### 3.2 candidate_detail

```python
candidate_detail(
    access: HostAccess, *, candidate_id: str, artifact_version: int
) -> ReviewCandidateDetail
```

- 要求 `work.scan + experience.read`；不要求 `review.generate`/`review.approve`/`review.reject`/`result.read`。
- 先按 exact `(candidate_id, artifact_version)` 读元数据；不存在 → 既有 `NOT_FOUND`。
- 再用 `read_experience` 判断 candidate 可读性；不可读 → redacted detail，不加载 proposal。
- 可读后才 `load_artifact` exact version；proposal/current/sources 全部按显式字段投影。
- 不改 `review`/`resolve`/`result` 的职责或语义。

### 3.3 Store 支持读

```python
candidate_summaries_for_work(namespace, work_id, generation=None)
candidate_summary_for_ref(namespace, artifact_id, artifact_version)
```

两者只 SELECT `collab_artifacts` 元数据列，不读 `private_payloads`；`candidate_summary_for_ref` 供详情在加载 payload 之前做 exact 元数据读取。它们不是 PCS JSON 契约。

## 4. Redaction 与可审批性

### 4.1 三类 redaction

| 类型 | 含义 | 对 detail | 对 can_approve | 对 can_reject |
|---|---|---|---|---|
| candidate redacted | candidate artifact 正文/proposal 不可读（隐私、deletion barrier、payload 不可读） | proposal/diff/sources 为空，redacted=true，detail=candidate_redacted | false | false |
| current redacted | 目标 current Belief 不可读/被删除 | current=None，current_state=redacted，diff=() | false | true |
| current missing | 目标 exact version 缺失/不存在/非 belief | current=None，current_state=missing，diff=() | false | true |
| source redacted | 某来源正文/可见性不可读 | 该 source availability=redacted，reason=deleted/privacy/redacted | false | true |
| source unavailable | 来源不存在或 read_experience 不可用 | 该 source availability=unavailable，reason=not_found/unavailable | false | true |
| target moved | 目标 exact version 仍可读，但不是 current pointer | current 仍显示 exact 目标版本；reason=target_moved | false | true |
| current terminal | 目标 Belief 已 retracted/contradicted | current 可读但 status 为终态；reason=current_terminal | false | true |
| stale generation | candidate.work_generation ≠ control.generation，或无 control | detail 可读但 reason=stale_generation | false | false |
| terminal | committed/rejected/stale/failed | 状态展示；reason=terminal | false | false |
| unsupported version | 候选行 artifact_version ≠ 1（现有 resolve 不支持） | 可展示；reason=unsupported_version | false | false |
| unsupported operation | proposal.operation 不在三种可提交操作内 | proposal 投影仍返回；reason=unsupported_operation | false | true |

- 不可读不等于空值；diff 不能把不可读 current 当成空 Belief。
- `can_*` 只做 UI 提示；最终由 C07 `resolve` 复核权限、current、generation、Context/删除与回执。
- PRE 的 `can_approve` 不修复、也不绕过 resolve 的授权/版本问题。

### 4.2 判定顺序

detail 的 can 判定按以下优先级给出一个公开 `eligibility_reason`：

1. candidate redacted → 两者 false，`candidate_redacted`；
2. 非 v1 候选 → 两者 false，`unsupported_version`；
3. status 非 pending → 两者 false，`terminal`；
4. Work control 缺失/generation 不一致 → 两者 false，`stale_generation`；
5. 否则 can_reject 保持 true，并按 approve 阻断原因设置 can_approve：`unsupported_operation`、`current_redacted`、`current_missing`、`target_moved`、`current_terminal`、`source_redacted`/`source_unavailable`。

`can_*` 不包含权限判断：读接口只要求 `work.scan + experience.read`，而批准/拒绝权限由 `resolve` 检查。C12c UI 必须同时检查 `review.approve`/`review.reject`。

## 5. WAIT 与 Legacy

### WAIT

- WAIT 是 Review 的正式结果，不形成 Belief 候选。
- `review()` 提交 `cognition.wait` 回执并完成 Work；不创建 candidate。
- candidate list 不出现 fake WAIT 候选；`resolve` 没有 WAIT 目标。
- 测试 `test_wait_creates_no_candidate_and_no_approval_target` 覆盖。

### Legacy Work

- `managed_work` 与 `legacy_work` 明确区分：有 `collab_work_control` 的 Work 只出现在 managed；无 control 的只在 legacy。
- Legacy 可经正式接口读取 Work 元数据并只读展示；没有正式 candidate/proposal/diff 接口。
- C12c UI 必须把 Legacy 标注为“无审查控制/无候选投影”的能力缺口，不得查 DB 补字段。
- Legacy 无法批准：没有 candidate_id；伪造 id 也会被 exact candidate/control 校验拒绝。
- 缺少 candidate count、额外时间戳等非关键字段属于只读降级，不停止 PRE。

## 6. 错误与缺口

- 不新增错误码。使用既有 `NOT_FOUND`、`SCOPE_VIOLATION`（`AuthorizationRequired` 的既有错误码）、`INVALID_SCHEMA`、`CORRUPT_STORE`、`REDACTED`。
- `candidate_detail` 对 exact 候选不存在 → `NOT_FOUND`；对存在但不可读的候选 → redacted detail（不是 `NOT_FOUND`，因为调用方已给出 ref）。
- 列表对不可见候选 → 整行省略；空结果不区分无候选/无权候选。
- 存储层发现候选列违反冻结约束（status/structural_validation/attempt_ref_json 形状）→ `CORRUPT_STORE`，不降级为普通视图。
- 已知能力缺口，不阻塞 PRE：
  1. Legacy 没有 candidate/proposal/diff 正式接口；
  2. current target 移动时 `resolve` 以 `REVISION_CONFLICT` 异常返回，而非 `ReviewView`；C12c UI 必须映射为 stale/冲突；
  3. 候选读取以 namespace + 隐私/purpose 为边界，不额外检查 interaction 的 host_instance；批准仍由 `resolve` 检查 host_instance；
  4. 没有 rerun grant 发放接口，C12c 仍不能从 UI “重新授权”；
  5. `desktop.v1` 尚未接线；本卡不修改 `apps/desktop/`。

## 7. 审计结论：现有 resolve 是否足够

**安全上足够，PRE 不修改 resolve。**

- exact candidate：`resolve` 使用 candidate_id 读取存储行，`_commit_decision` 要求 kind=candidate/status=pending/current generation；Approval 四项 exact 绑定。
- Work generation：`_commit_decision` 与 `require_current_candidate` 在 Tx B 内复核。
- 许可：`CommitAuthority` 的 approval 由 Facade 从存储 candidate 构造；grant 路径检查 grant kind/scope/active；前端不能提交 Approval。
- 当前目标：`BeliefService._revise` 在同一个 Tx B `apply` 内 `get_current` 并比较 exact version；retract 同样检查；不满足时整笔回滚并 `REVISION_CONFLICT`。
- 删除/权限/回执：commit guard 复核 live context + artifact barrier；Facade 复核 host instance 和 approve/reject 权限；receipt 由 `processed_commands` 产生并在披露前重新过滤。
- 重复决定：committed/rejected/stale/failed 的分支见 §1.2，没有复活旧候选的路径。

不足仅是可读性/UI 呈现：目标移动不会返回一个 `ReviewView(state="stale")`，而是异常。最小兼容方案是 C12c 适配层把 `REVISION_CONFLICT` 映射为 stale/冲突提示；这不需要修改 resolve，也不需要新增字段或错误码。若未来要求 resolve 本身返回稳定 stale 视图，应另开卡，不得在 PRE 内改语义。

另需如实记录：`ReviewFacade.resolve(access, candidate_id=..., decision=...)` 的公开签名仍不接收 `artifact_version`；它依赖 C05d 的单版本不变量，由唯一 candidate_id 定位 v1 行。C12c 应把 PRE ref 的 `artifact_version` 用于展示、审计与 exact 读取，但审批写入仍由 resolve 的既有 v1 绑定完成；若未来出现多版本候选或要求签名接收 exact ref，必须另开卡修改 resolve，不能在 PRE 内暗中扩展。

## 8. 变更文件

| 文件 | 变更 |
|---|---|
| `src/quintessence_pcs/store/collaboration.py` | `CandidateSummary` + 两个元数据只读方法；无表/迁移/错误码变化。 |
| `src/quintessence_pcs/collaboration/facade.py` | 6 个公开 DTO + `candidates`/`candidate_detail` + 投影/redaction 辅助；`resolve` 未改。 |
| `src/quintessence_pcs/collaboration/__init__.py` | 导出新 DTO。 |
| `tests/collaboration/test_review_candidate_read.py` | 22 个 C12c-PRE 测试。 |
| `docs/desktop/C12c-PRE-CONTRACT.md` | 本文。 |
| `tools/source-files.json` | 登记新增文件。 |

未修改：`apps/desktop/`、C06/C07 `resolve`、迁移、PCS JSON 契约、错误码、ACL。

## 9. 测试对照

| 卡片要求 | 测试 |
|---|---|
| 候选枚举空/多/status/generation/删除/跨 namespace | `test_candidates_empty_for_work_without_candidate`、`test_candidates_are_stable_metadata_only_rows`、`test_candidates_filter_exact_generation_and_keep_terminal_status`、`test_candidates_hide_invisible_and_deleted_candidates`、`test_candidates_are_scoped_to_the_access_namespace` |
| 枚举不含 body/proposal；不可见不出现 | `test_candidates_are_stable_metadata_only_rows`（patch payload loader）、`test_candidates_hide_invisible_and_deleted_candidates` |
| exact artifact_version/status/decision | `test_detail_binds_exact_candidate_version`、`test_terminal_candidates_disable_both_actions` |
| detail exact version/proposal/current/diff | `test_detail_binds_exact_candidate_version`、`test_detail_projects_proposal_and_propose_diff`、`test_detail_revise_has_current_and_field_diff`、`test_detail_retract_projects_reason_and_evidence_only` |
| propose 无 current | `test_detail_projects_proposal_and_propose_diff` |
| revise current 可用/redacted/missing | `test_detail_revise_has_current_and_field_diff`、`test_detail_revise_current_redacted_blocks_only_approve`、`test_detail_revise_current_missing_blocks_only_approve` |
| candidate redacted | `test_detail_candidate_redacted_has_no_projection_or_actions` |
| source redacted 三态 | `test_detail_source_availability_is_three_state`、`test_detail_input_source_body_only_when_read_experience_allows` |
| evidence links 授权来源；不可读不等于空 | `test_detail_source_availability_is_three_state` |
| stale generation/terminal | `test_stale_generation_disables_both_actions`、`test_terminal_candidates_disable_both_actions` |
| can 矩阵 | 上述各 detail 测试 + `test_detail_target_moved_blocks_only_approve` |
| 权限组合；不依赖 review.generate；approve/reject 仍由 resolve | `test_candidate_reads_require_work_scan_and_experience_read`、`test_candidate_reads_do_not_depend_on_review_generate`、`test_approve_and_reject_permissions_stay_with_resolve` |
| WAIT 无 candidate | `test_wait_creates_no_candidate_and_no_approval_target` |
| Legacy 只读/缺口 | `test_legacy_work_has_no_candidate_projection` |
| 不新增契约/迁移/错误码；C07 回归 | 本卡变更文件列表 + `test_review_facade`/`test_review_control`/`test_candidate_commit` 回归 |
| resolve 未被修改 | `service.py` 与 `facade.resolve` diff 为空；C07 回归通过 |

## 10. 验证

- `python -m unittest tests.collaboration.test_review_candidate_read`：22/22 OK。
- `python -m unittest tests.collaboration.test_review_candidate_read tests.collaboration.test_review_facade tests.collaboration.test_review_control tests.collaboration.test_candidate_commit`：95/95 OK。
- 全量测试、`tools/check_repository.py`、`git status --porcelain --ignored` 与未 push 状态见交付报告。
- 未开始 C12c 图形界面编码；`apps/desktop/` 无变更。
