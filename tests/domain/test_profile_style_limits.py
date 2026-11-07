"""The style-text limits a private persona may publish under.

History: ``style.tone`` was 128 and ``style.verbosity_by_context.casual`` was
64; the V16 author text needed 132 and 76, so they became 256 / 128 while
``technical`` kept its old bound of 64.  The V17 author text needs 726 / 412 /
204, so the J11 ceiling is 1024 / 512 / 256.

The tests pin both ends for every field: the approved lengths pass, the exact
ceiling passes, one character more is rejected, and an empty string is still
rejected — a wider bound is not "no bound".
"""

from __future__ import annotations

import unittest

from quintessence_pcs.domains.persona.profiles import validate_profile_body
from quintessence_pcs.domains.refs import EventRef
from quintessence_pcs.foundation.errors import InvalidInput

NS = "example.persona.test"
PROFILE_ID = "example.persona.p0"
EVENT_ID = "a5ae43a5-ac4e-488f-a517-ce61b2516ef1"

TONE_CEILING = 1024
CASUAL_CEILING = 512
TECHNICAL_CEILING = 256


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

    def test_the_approved_texts_are_accepted(self) -> None:
        self.validate(tone=132, casual=76, technical=32)  # V16
        self.validate(tone=726, casual=412, technical=204)  # V17

    def test_the_exact_ceilings_are_accepted(self) -> None:
        self.validate(tone=TONE_CEILING, casual=CASUAL_CEILING, technical=TECHNICAL_CEILING)

    def test_one_character_past_each_ceiling_is_rejected(self) -> None:
        with self.assertRaises(InvalidInput):
            self.validate(tone=TONE_CEILING + 1, casual=76)
        with self.assertRaises(InvalidInput):
            self.validate(tone=132, casual=CASUAL_CEILING + 1)
        with self.assertRaises(InvalidInput):
            self.validate(tone=132, casual=76, technical=TECHNICAL_CEILING + 1)

    def test_an_empty_style_text_is_still_rejected(self) -> None:
        with self.assertRaises(InvalidInput):
            self.validate(tone=0, casual=76)
        with self.assertRaises(InvalidInput):
            self.validate(tone=132, casual=0)
        with self.assertRaises(InvalidInput):
            self.validate(tone=132, casual=76, technical=0)

    def test_the_other_style_fields_keep_their_bounds(self) -> None:
        body = _body(tone=132, casual=76)
        body["style"]["language"] = "l" * 129
        with self.assertRaises(InvalidInput):
            validate_profile_body(
                body, profile_id=PROFILE_ID, review_ref=_value(), schema_version="3"
            )


if __name__ == "__main__":
    unittest.main()
