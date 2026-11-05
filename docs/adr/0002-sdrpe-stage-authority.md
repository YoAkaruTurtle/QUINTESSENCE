# ADR 0002 — SDRPE 的阶段职责与人格表达政策归属

- 状态：Accepted（RG08，2026-11-03）
- 相关：`docs/adr/0001-agent-mode.md`、`RG08-SDRPE-EXPRESSION-REPAIR-IMPLEMENTATION-PLAN-V1.5.1.md`
- 决策者：用户
- 实施范围：PCS 通用层；不改 Persona Schema，不迁移私人资产

## 1. 背景

SDRPE（Content → Expression）原本的意图是：第一阶段把事情想明白，第二阶段决定这个人最终怎么说。

实际运行中两个职责重叠了：

1. Content 被宿主政策要求产出「可直接发布的成品」，于是它同时决定了内容与交流方式；
2. Expression 拿到成文草稿，名义上有改写权，但缺少「哪些可以整段删」的依据；
3. 表达政策以源码常量的形式存在，且写的是某一个人的具体风格。

结果：一是 Expression 沦为润色器；二是通用源码里混进了私有人格资产。

## 2. 决策

### 2.1 三层边界

| 层 | 拥有 |
|---|---|
| PCS 通用层 | 双阶段机制、阶段任务、PolicyBundle、Checker、审计、政策加载与隔离 |
| Persona 资产层 | 身份、原则、风格、示例、**Persona Expression Policy** |
| 私人实例层 | 某个具体角色专属的记忆、认知、关系、经历（RG08 不涉及） |

### 2.2 政策的三个来源

- `shared_style_policy`（system 声明）：两阶段共享的事实/安全/工具/协议边界，**不规定谁定稿**；
- `content_task`（Context 段，宿主）：第一阶段职责；
- 表达政策 = **Persona 资产（记录）** + **通用宿主层（源码）**，拼接后发送。

宿主层只写机制：最终表达权、三类必留例外、提问不是默认动作、不是第二个内容阶段。它不写任何人格风格。

### 2.3 优先级

```
输出协议/Schema/动作权限
  > 事实、安全、隐私、授权、工具真实结果
  > 阶段职责（Content 想明白 / Expression 定表达）
  > Persona 的表达风格
```

冲突时：事实与安全以 Shared 为准，写法以阶段任务为准。

### 2.4 人格政策是资产，不是常量

- 载体：Record，`kind ∈ {persona_expression_policy, persona_expression_policy_candidate}`；
- 唯一键：`(namespace, kind, profile_id, profile_version)`，id 由 uuid5 推导；
- 正式资产必须有 `review_ref`；候选资产必须没有；
- 只有可信 Lab host（`HostConfig.is_lab`）能读候选；
- 解析三态 `present/absent/invalid`，禁止跨版本、跨人格、跨 kind 回退；
- store 异常使轮次失败，不伪装成 absent。

## 3. 为什么不用其他方案

| 方案 | 否决理由 |
|---|---|
| 政策写进 profile body | `PROFILE_BODY_KEYS` 封闭，加键要升 schema 4 + 改发布与 review 摘要语义 |
| 政策放侧车文件 | 运行时不能读任意文件；无审计、无版本、无隔离 |
| 政策放环境变量/宿主配置 | 不随人格走，切换人格会串 |
| 只给 Expression 加权限、不改政策归属 | 通用源码仍含私有人格文本，其他使用者会继承那个角色的风格 |

## 4. 后果

正面：

- 通用层可服务任意人格；新人格只发布资产，不改代码；
- 「这一轮用了谁的政策、哪一版、有没有降级」可审计；
- 表达决策权与事实保护分层，互不越界。

代价：

- 每轮多一次记录读取（微秒级）；
- 发布流程多一个可选参数与一个 review 字段；
- 需要一次性的 Lab 兼容迁移（本期已延期，见 D25）。

## 5. 不做什么

- 不新增第三阶段、不新增模型调用、不新增数据库表、不改 Persona Schema；
- 不建 Persona 管理 UI、不建资产管理平台；
- 不迁移任何私人实例的资产（J 系列负责）。
