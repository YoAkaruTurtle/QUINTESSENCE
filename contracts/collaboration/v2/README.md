# collab.v2 模型输出契约（RG03）

本目录是 `contracts/collaboration/v1/` 的**增量版本目录**：v1 目录冻结、一字不改；
v2 只描述"聊天里模型可以返回什么"。它不建表、不写迁移、不定义存储对象。

- 状态：契约 + 静态校验；实现见 `src/quintessence_pcs/runtime/collab_protocol.py`
  的 `parse_collab_output(..., protocol_version="collab.v2")`（**一份解析器按版本分派**）。
- 基线：RG01 v5 §5.3.6（Q1 裁定方案 A，结构化 action）、RG03 实施说明 v3 §2。
- 校验器：`tests/contract/contract_checker.py`（仅标准库、fail-closed）；测试
  `tests/contract/test_collaboration_contract_v2.py`。

## 文件

| 文件 | 作用 |
|---|---|
| `model_output.json` | v2 输出形状：`final` / `call_tool` / `wait` + 可选 `commentary`，`final` 另可带有序 `messages` |
| `_common.json` | **v1 `_common.json` 的逐字节冻结副本**（8,315 B）。`contract_checker` 拒绝指向目录外的 `$ref`，版本目录必须自包含。`_common` 级改动同样要新建目录 |

**不新增** `context_binding.json`：`context_snapshots` 行形状未变（`context_kind='consult'`），
v2 的渲染器身份记录在既有可空列 `renderer_version`（RG06 起为 `collab.v2.renderer.2`，RG05 起为 `collab.v2.renderer.3`，RG05-F02 起为 `collab.v2.renderer.4`，RG07 起为 `collab.v2.renderer.5`：新增宿主构造的 `content_handoff` / `expression_task` 段；RG08 起为 `collab.v2.renderer.6`：新增可选有序 `messages` 气泡；RG08-SDRPE 起为 `collab.v2.renderer.7`：声明键 `chat_style_policy` 改名为 `shared_style_policy`，Context 新增宿主构造的 `content_task` 段；RG08 时间锚起为 `collab.v2.renderer.8`：Context 新增受信任的 `now` 段，history 条目可带 `message_time` / `time_basis` / `age_seconds`），
binding 继续按 v1 契约消费。理由见 RG03 实施说明 §2.1；RG06/RG08 只升 v2 常量，
v1 常量/声明逐字节不变；旧 `renderer.5` Context 仍可读，不做迁移。

## 与 v1 的差异

| 项 | v1 | v2 |
|---|---|---|
| 动作集 | `none` / `query` / `propose` / `wait` | `final` / `call_tool` / `wait` |
| 工具 | 宿主固定 `query_plan`，模型不能命名工具 | 模型显式命名 `tool_call.name`，参数在 `tool_call.arguments`；**工具清单来自 Context 的 `tools[]` 段，不在本文件里** |
| 过渡短句 | 无（`brief_reason` 是诊断字段，永不展示） | `commentary`（可随 `call_tool`/`final`/`wait`；长度/模式/去重是**宿主策略**，schema 只保证 1..4096 字符串） |
| 澄清 | `wait` + `reply` | `wait` + `wait_question` |
| 分气泡（RG08） | 无；`reply` 恒为单条 | `final` 可选 `messages`：1..4 条有序字符串，每条 1..4096 字符；模型显式给出，宿主绝不按标点/换行切分 |
| 禁止字段 | `request_id/read_set/state_revision/authorization/context_id` | 再加 `turn_id/conversation_id/stage_id/attempt_id` |
| 人格/交谈者（RG06） | 无声明字段；`profile` 段只含身份/原则/风格/关注偏好 | 声明新增固定 `persona_policy`/`chat_style_policy`；Context `profile` 可含 `asset_hash`/`examples`/`examples_omitted`，顶层可含宿主提供的 `speaker`；示例可带可选 `pre_tool_commentary`（先短句、后 final，绝不拼接）；profile schema 3 的示例可用 `assistant_messages` 表示一条回复的 2-4 个有序气泡（与 `assistant` 二选一，上下文原样按序渲染，绝不拼接） |

**动作不变量**（schema 只做结构，语义在解析器）：

- `final`：必须有 `reply` 或 `messages`（两者都有时 `reply` 必须等于 `messages` 以双换行连接）；不得有 `tool_call`/`wait_question`；`commentary` 可选（宿主在 final 前发布为公开短句）。
- `call_tool`：必须有 `tool_call`；不得有 `reply`/`wait_question`/`messages`/`references`；
  `commentary` 可选。
- `wait`：必须有 `wait_question`；不得有 `reply`/`tool_call`/`messages`；`commentary` 可选（宿主在 wait 前发布为公开短句）。
- 任意动作都可带 `brief_reason`（诊断，从不展示）。

## 边界

本目录不建表、不写迁移、不改 `contracts/collaboration/v1`、不改 `src/`；
不实现工具执行、不调用模型、不携带私人数据。
