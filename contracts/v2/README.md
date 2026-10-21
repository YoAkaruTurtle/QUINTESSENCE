# pcs.v2 契约（PCS2-002）

本目录是 `pcs.v2` **最小 DTO 的公开真源**：`*.json` 是 JSON Schema（2020-12），测试只读取这些文件，不维护第二份字段清单。

- 规范依据：历史设计附录 J.1–J.5（基线 `PCS-2026-10-17-r3`）；字段名、必填性、可空性以附录为准。
- 校验器：`../../tests/contract/contract_checker.py`（仅标准库，fail-closed）。
- golden vectors：`../../tests/contract/vectors/`。

## 文件

| 文件 | DTO | 依据 |
|---|---|---|
| `_common.json` | 公共 `$defs`（UUID、时间戳、枚举、两种引用） | J.1、J.2 |
| `event_ref.json` | `EventRef` | J.1 |
| `record_ref.json` | `RecordRef`（必须带 `record_version`） | J.1 |
| `scope.json` | `Scope`（五个可选字段，封闭集合） | J.2 |
| `event.json` | `Event` 信封 | J.2 |
| `record_version.json` | `RecordVersion` | J.2 |
| `request.json` | 机器请求信封 | J.4 |
| `response.json` | 公共响应信封 | J.4 |
| `error.json` | 受控错误对象与错误码枚举 | J.4 |
| `context_envelope.json` | `ContextEnvelope` 最小字段集 | J.5 |

## 本卡固定的结构取舍（J 未逐字规定处）

附录只固定字段集合与语义，以下结构细节是本卡**显式选择**，不是对 J 的改写：

1. **可空性**：只有 J 明确标为可空的字段允许 `null`（`Event.source_position`、`Event.observed_at`、`Request.expected_state_revision`）；缺字段仍然算缺失。
2. **UUID**：`event_id`/`record_id`/`request_id` 使用规范小写 UUID 文本（8-4-4-4-12），不限制版本号（为将来的 UUIDv7 留出空间）。scope 的 `*_id` 是 opaque string，J 未要求它们是 UUID。
3. **时间**：`recorded_at`/`observed_at` 使用 UTC RFC 3339（`...Z`），`observed_at` 未知时为 `null`。pattern 只校验语法形状，日历合法性不在结构层。
4. **namespace**：`^[a-z][a-z0-9._-]{0,127}$`（如 `example.private`）。
5. **`kind`/`status`/`schema_version`/`event_type`**：J.2 没有给出枚举，因此本卡只要求非空字符串；未知值不是结构错误。按 kind 的正文校验属于 007 起的工作。
6. **`body`**：`object`，不细分 kind；`source_refs`、`causation_refs` 的元素是 `EventRef`/`RecordRef` 联合，拒绝裸 ID。
7. **`payload_ref`/`permission_view_ref`/`run_id`/`context_id`**：J 未规定内部形态，按非空字符串处理；`context_id` 与 `run_id` 是运行账目字段，不是 RecordRef。
8. **J.5 的欠定字段**：`budget_view`、`index_coverage` 先取 `object`；`memory_hits` 取 `RecordRef` 数组；`omissions` 为 `{ref?, reason}` 对象数组。这些会在 007/008/011 收窄，本卡不假装已定稿。
9. **J.5 字段名的两处补齐**：J.5 允许“另带 `profile_version` 便于人读”，并在 read_set 段落要求“EventRef 分列 `source_refs`”。为不与 `additionalProperties: false` 冲突，信封把 `profile_version` 与 `source_refs` 列为**可选**字段；22 个必含字段保持原样。
10. **唯一性**：`entity_ids`、`causation_refs`、`source_refs`、`read_set`、`memory_hits`、`belief_refs`、`question_refs`、`focus_refs`、`unknown_execution_ids`、`pending_source_event_refs`、`omissions` 使用 `uniqueItems: true`（重复项是结构噪声）。
11. **`Response.data`**：操作相关对象；`outcome` 若出现必须是非空字符串。J.4 的 `committed/noop/accepted/duplicate` 是示例（原文有“等”），因此不设为封闭枚举；`ok` 与 `error` 的耦合关系 J 未明说，本卡不编码，留给命令层。
12. **`Response.warnings`**：非空字符串数组。

## 错误码映射（结构层）

`error.json` 固定 J.4 的 22 个错误码枚举；**何时产生哪个码**属于状态层，本卡不规定。结构校验失败按以下顺序映射（`tests/contract/contract_errors.py`）：

1. `scope.json` 的任何违规，或实例中位于 `scope` 对象内部的违规 → `SCOPE_VIOLATION`；
2. `protocol_version` 的 `const` 不匹配 → `UNSUPPORTED_VERSION`；
3. 其余结构违规、以及 NaN/Infinity/非法 JSON 等解析层拒绝 → `INVALID_SCHEMA`。

映射是全序、确定性的；每条非法 golden vector 都预先声明自己的码，测试断言实际映射等于声明，而不是只断言“有错”。

## 校验器语义（fail-closed）

- 只实现实际用到的关键字：`$ref`、`$defs`、`type`、`required`、`properties`、`additionalProperties`、`enum`、`const`、`pattern`、`minLength`/`maxLength`、`minimum`/`maximum`、`items`、`minItems`/`maxItems`、`uniqueItems`、`oneOf`。
- 出现任何未实现的关键字（包括拼错的键、`format`、`if`/`then`、tuple-form `items`）立即抛错，不跳过。
- `type: integer` 用 `type(v) is int`，布尔与 `1.0` 都不算整数。
- `pattern` 用 `re.fullmatch`，并要求 schema 里的 pattern 以 `^...$` 锚定（避开 Python `$` 匹配行尾换行的陷阱）。
- 实例必须是真正的 JSON 值：`strict_loads` 用 `parse_constant` 拒绝 NaN/Infinity，并二次扫描拒绝 `1e999` 这类溢出为 inf 的数字。
- `enum`/`const`/`uniqueItems` 用类型敏感的规范化：`1` 与 `true` 不同，`1` 与 `1.0` 相同。

## 边界（本卡不做）

引用指向的记录是否存在、版本链是否连续、`request_id` 幂等、`state_revision` 与 `record_version` 的数值混用（两者都是合法正整数时 schema 无法区分）、Tx A/Tx B 水位、SQLite 建表与迁移——全部属于 004/005/006 及状态层测试。本卡只负责结构与错误码清单。

## 运行

```powershell
python tools/run_tests.py
```

不需要安装任何第三方包；`tests/contract` 的模块通过标准库 `unittest` 发现。
