# J11 — PS06 部署与恢复说明（删除安全三件套）

**状态：** 已实现（J10 W1 + J11 adopt），本文只描述**如何部署、备份与恢复**，不重新定义机制。
**适用代码：** 合并 J11 后的实例文件夹管理；采用实例完整性与可迁移性优先的用户裁决。

---

## 1. 为什么是三件套

删除安全依赖三个**必须同时成立**的东西：

| 件 | 默认位置 | 作用 | 丢了会怎样 |
|---|---|---|---|
| 业务库（PCS DB） | `store.root/pcs.sqlite` | 业务状态、屏障行、`privacy_epoch` | 库丢 = 数据没了 |
| **删除账本** | `store.deletion_ledger_dir`，默认 `<store.root>/ledger`（可选独立备份单元，见 §2.1） | 预写+确认的删除记录、哈希链、每 namespace 水位 | 库在、账本丢 → **拒绝打开**（只读诊断） |
| **anchor** | `store.deletion_anchor_dir`；桌面默认 `<store.root>/anchor/<sha256(db 绝对路径)[:16]>.json` | 实例内的独立见证；仍存活且比数据库、账本更新时可识别局部回退 | anchor 丢 → **拒绝打开**（只读诊断） |

恢复闸门 `restore-gate.json`（默认在实例根）记录 `ledger_head_hash` / `ledger_max_epoch` / `baseline_epoch` / `reject_reason`，它**不是第四份数据**：可以重建，重建后仍要过同一套判定。

### 关键约束

> **默认三者都属于实例文件夹，迁移与冷备份应包含完整三件套。**

如果数据库、账本和 Anchor 一起恢复到同一个旧快照，系统无法可靠区分这与从未发生后续删除的状态。现阶段明确接受此风险，不再宣称完整防共同回滚。保留较新的见证可以检测部分回退，但不能把这种条件性保证说成完整保证。

---

## 2. 部署

```jsonc
"store": {
  "root": "<实例根>/instance",
  "database": "<实例根>/instance/pcs.sqlite",
  "deletion_ledger_dir": "<实例根>/instance/ledger",
  "deletion_anchor_dir": "<实例根>/instance/anchor",
  // 仅"存量实例首次接入"时加一次，随后必须删除：
  "adoption": { "by": "operator-name", "reason": "why this instance predates the ledger" }
}
```

* 停止所有写入进程后备份完整实例。可以额外保存较新的见证副本以加强恢复判断；实例本身不依赖文件夹外不可再生的私人状态。
* 账本是**追加写**的 JSONL（含前置哈希链）。备份只需复制文件；不要"只保留最新一行"。
* anchor 是小文件，覆盖写；备份保留**最近若干份历史**（否则同点回退又无法识别）。

### 2.1 没有账本时会发生什么（重要）

账本的默认值是 `<store.root>/ledger`，所以**新实例默认受保护**。但默认仍在实例目录内：它能挡住"只恢复数据库文件"，挡不住"整目录回退"。现阶段默认将账本和 Anchor 保存在实例文件夹内，以保证实例完整性与可迁移性。**数据库、账本和 Anchor 一起回退时，无法可靠检测共同回滚**；`enforced` 仅表示现有账本与恢复检查生效，不代表完整防共同回滚。额外保留较新的见证副本可加强判断；仅选择实例文件夹即可运行的产品路径仍要求必要私人资产位于实例内。

账本不可用时（未配置、目录被删、文件损坏）：

| 情形 | 行为 | `app.info.deletion_safety` |
|---|---|---|
| 默认（未声明 `store.unprotected`） | **一律拒绝打开**（`restore_ledger_missing`，只读诊断） | `not_configured` |
| 库里已有删除水位（`privacy_epoch > 0`） | **拒绝打开**（同上） | `not_configured` |
| 显式声明 `store.unprotected = {by, reason}` | 打开，但删除**被拒绝**（见 §2.3） | `unprotected` |

**关键：数据库里"没有删除记录"不能当作可以无账本运行的证据。**

```
周一：库里有私人信息，还没删除
周二：启用 PS06，删掉它；账本 + anchor 记录了这次删除
周三：错误恢复——周一那个库 + 周一那份"没有账本路径"的旧配置
       系统看到：账本未配置、库里没有删除记录
       若此时 OPEN，周二删掉的私人信息就复活了
```

恢复旧快照后，**数据库自己已经忘了发生过删除**。能识别这件事的只有 anchor——它必须比被恢复的库和账本更新，才能提供额外证据。默认随整个实例回滚时，此保证不成立。所以：

* **有 anchor + 账本不可用 → 拒绝打开**（即使库里没有删除记录）；
* **无 anchor + 账本不可用 → 同样拒绝打开**（无法区分"新实例"与"被恢复的旧实例"）；
* 只有显式 `store.unprotected` 才允许打开，且该模式**禁止删除**。

### 2.2 冷恢复步骤（库 + 账本 + anchor）

1. 停掉所有宿主进程（单写者；`DATA_LOCKED` 时不要强开第二份）。
2. 恢复业务库到实例根（`store.root/pcs.sqlite`）。
3. 恢复账本目录（含 `ledger.jsonl` 与 `baseline.json`）到 `store.deletion_ledger_dir`。
4. 恢复该库对应的 anchor 文件（默认实例内 `anchor/<sha16>.json` 或显式配置目录）。
5. 启动宿主：闸门按 §3 判定；失败时**只有只读诊断**，不得直接进入正常对话。
6. 核对 `app.info.deletion_safety == "enforced"` 且 `restore-gate.json` 的 `status == "open"`。

### 2.3 不受保护模式（仅实验）

```jsonc
"store": {
  "deletion_ledger_dir": false,         // 显式关闭账本（需与下面的声明同时出现）
  "unprotected": { "by": "operator-name", "reason": "why the ledger cannot be used" }
}
```

* 需要同时给出 `by` 与 `reason`；缺任一 → 拒绝打开（`ps06_unprotected_invalid`）。
* 打开后 `app.info.deletion_safety == "unprotected"`，`restore-gate.json` 同样记录。
* **删除被拒绝**（`request_deletion` 报错）：不受保护的删除会积累永远无法证明的历史，比不删更危险。
* 该模式不能用于库、仍在的账本或 Anchor 记有删除水位的实例——直接拒绝打开；恢复旧库也不能隐藏存活的删除见证。账本或 Anchor 损坏同样不能进入实验模式。

### 首次部署 / 存量实例接入（adopt）

* **全新实例**：首次打开时建立基线（每 namespace 的 `baseline_epoch` = 当前水位）。基线**不追溯**更早的历史删除。
* **已存在且有删除历史的实例**（`privacy_epoch > 0`、无账本）：默认**拒绝打开**。需要一次性、显式、可审计的 adopt——在 `store.adoption` 写 `{by, reason}`（Memory 的目录重绑定是另一种审计操作，两者不能混用；统一连接入口见 [实例操作说明](INSTANCE-FOLDERS.md)）。

adopt 写入的审计字段（账本首条）：

```
adopted=true, adopted_at, adopted_by, adopt_reason,
baseline_epoch=<每 namespace 当前水位>,
pre_baseline_deletions="unprovable"
```

**口径：** adopt 只声明"从此可追踪"，**不声明历史删除已被证明完整**。任何早于 `baseline_epoch` 的快照一律拒绝恢复（`restore_before_ledger_baseline`）。

---

## 3. 冷恢复步骤

1. **停进程**（单写者：确认没有第二个实例持锁）。
2. 恢复业务库到目标时点（文件级复制）。
3. 恢复**账本**到"不早于该库"的位置：
   - 账本必须覆盖库中出现的所有 `privacy_epoch`；账本落后 → `restore_ledger_stale`，拒绝。
4. 恢复 **anchor**（保留历史副本）。
5. 启动应用。启动时的判定（无需人工"放行"）：

| 情形 | 结果 |
|---|---|
| 库水位 < 账本水位 | 重放缺失删除 → 重做清理 → 校验 `pending=0` → 开放 |
| 库水位 = 账本水位 | 校验通过 → 开放 |
| 库水位 > 账本水位 | `ledger_degraded`（库里有账本证明不了的删除）→ **拒绝开放** |
| 账本/ Anchor 缺失、损坏、早于基线，或存活见证能证明局部回退 | **拒绝开放**，只读诊断 |
| 库、账本、Anchor 及恢复记录一起回到一致的旧快照 | 可能开放；不能可靠检测共同回滚，旧数据可能重新出现 |
| 闸门文件被旧快照带回（head 落后） | `restore_gate_stale` → 按当前账本重新判定 |

6. 开放后确认：`restore-gate.json` 的 `ledger_head_hash` 与账本当前 head 一致；`pending` 清理完成。

---

## 4. 失败处理

| 拒绝原因 | 含义 | 处理 |
|---|---|---|
| `restore_ledger_missing` | 库有删除、账本不在 | 找回账本；找不到 → 该实例不可恢复（只读诊断） |
| `restore_ledger_corrupt` | 哈希链断裂/截断 | 用备份替换；**不要手工补行** |
| `restore_ledger_stale` | 账本落后于库 | 找回更新的账本 |
| `ledger_degraded` | 库领先账本 | 说明有删除未被账本记录；**不得继续** |
| `restore_anchor_missing` | 无法判断是否同点回退 | 找回 anchor 历史副本 |
| `restore_same_point_rollback` | 库与账本回退，较新的 Anchor 仍存活 | 拒绝；找回正确的完整恢复资产。三方共同回退不保证触发此错误 |
| `restore_replay_failed` / `restore_cleanup_incomplete` | 重放或清理未完成 | 修复后重试；闸门保持关闭 |

**失败时的产品行为：** 只允许**只读诊断**；不允许正常聊天、检索、认知、维护、记忆投递、工具结果注入。这是 v1.1 §3.7 的 fail-closed 口径——**停机是设计，不是缺陷**。

---

## 5. 明确边界

* **PCS 的恢复安全 ≠ 外部存储的物理删除保证。** Scope Recall 等外部记忆存储有自己的删除/镜像机制；PCS 不宣称替它们完成物理删除。
* 账本与 anchor 的**可用性 = 产品的可用性**：任一缺失即停机。备份与监控是运维要求，不是可选优化。
* 本说明不改变任何已冻结契约；零迁移。

---

## 6. 验收清单（部署方）

- [ ] 实例内包含数据库、账本、Anchor 和真实 Memory 数据，关闭写入后整体备份。
- [ ] 正常删除后账本水位前进，重启仍能加载，已删除的 PCS 内容不可读。
- [ ] 旧数据库配合较新的完整账本、Anchor 能重放删除后开放；不能要求这条安全恢复路径一律拒绝。
- [ ] 账本缺失、损坏或落后时拒绝连接，不自动用空记录替代。
- [ ] 数据库和账本回退而较新的 Anchor 仍在时，按存活见证拒绝。
- [ ] 数据库、账本、Anchor 及恢复记录共同回退可能开放；报告明确说明无法可靠检测共同回滚。
- [ ] 无安全记录的旧实例只能显式登记基线，记录此前删除历史不可证明。
