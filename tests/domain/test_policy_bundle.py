"""RG08: the immutable per-turn PolicyBundle.

Pure unit tests: they pin the composition rule (persona text, blank line, host
rules), the digest contract (the bundle digest covers exactly the texts it will
send) and the stage gating (a turn that does not use the expression asset must
not read the store at all).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
for _path in (HERE, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.conversation.content_task import (  # noqa: E402
    CONTENT_TASK_POLICY,
    build_content_task,
)
from quintessence_pcs.conversation.expression_host_policy import (  # noqa: E402
    HOST_EXPRESSION_STAGE_POLICY,
)
from quintessence_pcs.conversation.policy_bundle import (  # noqa: E402
    PolicyBundle,
    current_bundle,
    resolve_bundle,
)
from quintessence_pcs.conversation.sdrpe import SDRPE_POLICY_VERSION  # noqa: E402
from quintessence_pcs.domains.persona.policy import (  # noqa: E402
    PersonaPolicyAsset,
    policy_text_sha256,
)
from quintessence_pcs.foundation.jsonutil import sha256_hex  # noqa: E402
from quintessence_pcs.runtime.collab_protocol import SHARED_STYLE_POLICY  # noqa: E402

PERSONA_TEXT = "这是某个人格的表达政策。"


def asset(state="present", text=PERSONA_TEXT):
    return PersonaPolicyAsset(
        state=state,
        profile_id="lab.persona.p0",
        profile_version="14",
        text=text if state == "present" else "",
        sha256=policy_text_sha256(text) if state == "present" else None,
        review_state="unreviewed" if state == "present" else None,
    )


def bundle(persona=None, host=HOST_EXPRESSION_STAGE_POLICY):
    return PolicyBundle(
        version=SDRPE_POLICY_VERSION,
        shared_text=SHARED_STYLE_POLICY,
        content_text=CONTENT_TASK_POLICY,
        host_expression_text=host,
        persona=persona,
    )


class CompositionTests(unittest.TestCase):
    def test_present_persona_precedes_host_rules(self) -> None:
        text = bundle(persona=asset()).expression_text
        self.assertEqual(PERSONA_TEXT + "\n\n" + HOST_EXPRESSION_STAGE_POLICY, text)

    def test_absent_persona_sends_host_rules_only(self) -> None:
        self.assertEqual(
            HOST_EXPRESSION_STAGE_POLICY, bundle(persona=asset("absent")).expression_text
        )

    def test_invalid_persona_sends_host_rules_only(self) -> None:
        self.assertEqual(
            HOST_EXPRESSION_STAGE_POLICY, bundle(persona=asset("invalid")).expression_text
        )

    def test_a_arm_without_host_rules_is_the_persona_text_alone(self) -> None:
        text = bundle(persona=asset(), host="").expression_text
        self.assertEqual(PERSONA_TEXT, text)
        self.assertFalse(text.endswith("\n"))


class DigestTests(unittest.TestCase):
    def test_expression_digest_matches_the_sent_text(self) -> None:
        self.assertEqual(
            sha256_hex(bundle(persona=asset()).expression_text),
            bundle(persona=asset()).expression_sha256,
        )

    def test_digest_is_stable_for_equal_texts(self) -> None:
        self.assertEqual(bundle(persona=asset()).digest, bundle(persona=asset()).digest)

    def test_digest_changes_when_any_part_changes(self) -> None:
        base = bundle(persona=asset())
        other_host = bundle(persona=asset(), host=HOST_EXPRESSION_STAGE_POLICY + "x")
        other_persona = bundle(persona=asset(text=PERSONA_TEXT + "x"))
        self.assertNotEqual(base.digest, other_host.digest)
        self.assertNotEqual(base.digest, other_persona.digest)

    def test_persona_digest_is_none_without_a_present_asset(self) -> None:
        self.assertIsNone(bundle(persona=None).persona_sha256)
        self.assertIsNone(bundle(persona=asset("absent")).persona_sha256)


class StageGatingTests(unittest.TestCase):
    def test_generic_bundle_carries_no_persona(self) -> None:
        one = current_bundle()
        self.assertIsNone(one.persona)
        self.assertEqual(HOST_EXPRESSION_STAGE_POLICY, one.expression_text)

    def test_include_persona_false_never_touches_the_store(self) -> None:
        class ExplodingStore:
            def __getattr__(self, name):
                raise AssertionError("the store must not be read on this path")

        one = resolve_bundle(
            ExplodingStore(),
            "example.rg08.policy",
            object(),
            host_is_lab=False,
            include_persona=False,
        )
        self.assertIsNone(one.persona)


class ContentTaskTests(unittest.TestCase):
    def test_task_uses_the_bundle_text_and_version(self) -> None:
        one = bundle(persona=asset())
        task = build_content_task(sdrpe_mode="on", bundle=one)
        self.assertEqual(CONTENT_TASK_POLICY, task["policy"])
        self.assertEqual(one.version, task["policy_version"])
        self.assertEqual(one.digest, task["policy_digest"])
        self.assertEqual(one.content_sha256, task["content_sha256"])

    def test_mode_is_validated(self) -> None:
        from quintessence_pcs.foundation.errors import InvalidInput

        with self.assertRaises(InvalidInput):
            build_content_task(sdrpe_mode="auto", bundle=bundle())


if __name__ == "__main__":
    unittest.main()
