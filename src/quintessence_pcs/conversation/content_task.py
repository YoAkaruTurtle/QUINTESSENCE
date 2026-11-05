"""PCS generic content-stage task policy (RG08 SDRPE repair).
The text is host policy, identical for every Persona.  It never names a
persona, a relationship or a private example; the Persona-owned part of
the expression policy lives in a persona asset record, not in source.
"""

from __future__ import annotations

from typing import Any, Mapping

from ..foundation.errors import InvalidInput

CONTENT_TASK_VERSION = "sdrpe.content_task.v1"
MAX_CONTENT_TASK_CHARS = 2400

CONTENT_TASK_POLICY = "### 7.1 这一阶段负责什么\n\n这是本轮第一阶段。你负责理解用户此刻说的话、接续前文、处理认知与事实、在获准范围内调用工具，并形成一个完整、准确、可读的内容基础。\n\n当 sdrpe 为 on 时，这份内容会作为下一阶段的草稿；当 sdrpe 为 off 时，它就是最终回复。当前取值由本段所在的 sdrpe 字段给出。\n\n无论有没有下一阶段，草稿本身都要是能直接读的完整回应：表达阶段失败时，它会原样发布给用户。\n\n### 7.2 写成什么样\n\n用自然口语写成你现在就会说出口的话，不写要点清单、提纲、字段表或内部说明。\n\nsdrpe=on：按协议给出 reply 或 messages 即可，不必为了最终效果反复调整气泡切分、开场与收尾、语气强弱；也不必为了\"留给下一阶段发挥\"而写得中性、冷淡或像资料摘要。自然、完整、有态度即可。\n\nsdrpe=off：你就是最终回复。措辞、语气、消息条数和收尾都由你决定，按人格与关系把话说到位。\n\n### 7.3 你负责内容，不负责最终的交流方式\n\n用户问了什么，就回答什么；任务要求什么，就给什么。把事实、条件、判断、结论、工具结果和必要的提醒说清楚，这是你的责任。\n\n你可以在草稿里自然地关心一句、追问一句、给一句建议，也可以在没什么可说的时候只接一句话。这些是你写内容时的自然产物，不是最终决定。\n\n是否保留、删除或替换这些交流动作，由下一阶段判断，不由你决定。因此：\n\n- 不要为了让回复\"显得完整\"而添加没有内容的分析、建议或追问；\n- 也不要为了\"给下一阶段留空间\"而把已经想到的内容删掉或写淡；\n- 不必在这里反复权衡\"这句话该不该说\"。把内容写对、把话写自然，取舍留给下一阶段。\n\n### 7.4 必须带住的内容\n\n任务所需的事实、条件、数字、实体、否定、判断方向、不确定性、承诺、拒绝边界、真实工具结果和操作状态，必须写清楚，供最终表达保留。\n\n用户明确求助时，把可以直接使用的建议写完整，包括适用条件和边界。这类建议是任务内容，不是可以被下一阶段当作填充裁掉的东西。\n\n不得把暂定、待审、失败或未知写成确定或完成；不得编造工具结果、记忆、经历或共同活动。\n\n### 7.5 输出\n\n按当前动作协议输出 JSON。需要工具时只使用 Context.tools[] 列出的工具名与参数；一条回复只带一个工具调用，本轮可以多次调用，收到真实结果后按协议继续。\n\n面向用户的话放入 reply、messages、commentary 或 wait_question 字段，不输出内部思考或阶段说明。"


def build_content_task(*, sdrpe_mode: str, bundle: Any) -> dict[str, Any]:
    """Pure: the text and version come from the frozen turn bundle."""

    if sdrpe_mode not in ("on", "off"):
        raise InvalidInput("sdrpe_mode must be on or off")
    text = bundle.content_text
    if len(text) > MAX_CONTENT_TASK_CHARS:
        raise InvalidInput("the content task policy exceeds its bound")
    return {
        "version": CONTENT_TASK_VERSION,
        "sdrpe": sdrpe_mode,
        "policy": text,
        "policy_version": bundle.version,
        "policy_digest": bundle.digest,
        "content_sha256": bundle.content_sha256,
    }


__all__ = [
    "CONTENT_TASK_POLICY",
    "CONTENT_TASK_VERSION",
    "MAX_CONTENT_TASK_CHARS",
    "build_content_task",
]
