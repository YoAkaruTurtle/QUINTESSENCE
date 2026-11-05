"""RG08: the style-text limits a private persona may publish under.

``style.tone`` was 128 and ``style.verbosity_by_context.casual`` was 64; the
V16 author text needs 132 and 76.  The limits are now 256 / 128 while the
other fields keep their old bounds, so the test pins both ends: the new values
pass, one character more still fails, and ``technical`` is unchanged.
"""

from __future__ import annotations

import unittest

from quintessence_pcs.domains.persona.profiles import validate_profile_body
from quintessence_pcs.domains.refs import EventRef
from quintessence_pcs.foundation.errors import InvalidInput

NS = "example.persona.test"
PROFILE_ID = "example.persona.p0"
EVENT_ID = "a5ae43a5-ac4e-488f-a517-ce61b2516ef1"


def _body(*, tone: int, casual: int, technical: int = 10) -> dict:
    ref = {"ref_type": "event", "namespace": NS, "event_id": EVENT_ID}
    return {
        "profile_id": PROFILE_ID,
        "profile_version": "16",
        "identity_summary": "合成身份说明",
        "principles": ["合成条目"],
        "style": {
            "language": "zh-CN",
            "tone": "t" * tone,
            "verbosity_by_context": {"casual": "c" * casual, "technical": "x" * technical},
        },
        "attention_preferences": [{"topic": "合成", "preference": "合成"}],
        "examples": [
            {
                "example_id": "ex01",
                "scene": "owner-tired",
                "user": "合成问",
                "assistant": "合成答",
                "synthetic": True,
            }
        ],
        "asset_hash": "0" * 64,
        "renderer_compatibility": "pcs2-008.renderer.v1",
        "review_ref": ref,
    }


def _value():
    return EventRef(namespace=NS, event_id=EVENT_ID)


class StyleLimitTests(unittest.TestCase):
    def validate(self, **kwargs) -> None:
        validate_profile_body(
            _body(**kwargs), profile_id=PROFILE_ID, review_ref=_value(), schema_version="3"
        )

    def test_the_v16_lengths_are_accepted(self) -> None:
        self.validate(tone=132, casual=76)  # actual V16 values
        self.validate(tone=256, casual=128)  # exact new ceilings

    def test_one_character_past_the_ceiling_is_rejected(self) -> None:
        with self.assertRaises(InvalidInput):
            self.validate(tone=257, casual=76)
        with self.assertRaises(InvalidInput):
            self.validate(tone=132, casual=129)

    def test_technical_keeps_its_old_bound(self) -> None:
        self.validate(tone=132, casual=76, technical=64)
        with self.assertRaises(InvalidInput):
            self.validate(tone=132, casual=76, technical=65)


if __name__ == "__main__":
    unittest.main()
