"""J11: the conversation-scoped tentative expression preference is retired.

RG05-F02 used to keep a unified Record (``kind=expression_preference``) for an
ordinary remark such as "我其实更喜欢简短一点的回答", so the next Context could
apply it.  J11 removed the *active* mechanism: the host no longer creates,
supersedes or renders these records, because a keyword classifier cannot tell a
description ("感觉轻松一些") from a directive, and mis-reading either drops the
whole turn from memory or silently changes the reply tone.

What must still hold (and is pinned here):

* the service no longer exposes a write path (``record_tentative``/``supersede``);
* **legacy rows stay readable and revocable** — they are ordinary unified
  Records, so no migration is needed and no data is deleted;
* a legacy row is never an experience/memory source;
* a legacy row whose cited input was deleted reads as inactive.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.persona.expression_preferences import (  # noqa: E402
    EXPRESSION_PREFERENCE_KIND,
    EXPRESSION_PREFERENCE_SCHEMA,
    ExpressionPreferenceService,
)
from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402

CONVERSATION = "11111111-1111-4111-8111-111111111111"
OTHER_CONVERSATION = "22222222-2222-4222-8222-222222222222"
QUOTE = "我其实更喜欢简短一点的回答。"


class ExpressionPreferenceLegacyTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.prefs = ExpressionPreferenceService(self.store)
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=QUOTE,
            privacy_class="private",
            retention="standard",
            content_version=1,
        )
        self.input_id = row.input_id
        self.ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": 1,
        }

    # ------------------------------------------------------------------ shape
    def legacy(self, **overrides):
        """Write a *legacy* row the way the retired host path used to."""

        body = {
            "dimension": "length",
            "value": "concise",
            "quote": QUOTE,
            "source_ref": dict(self.ref),
            "applies_to": "conversation",
            "state": "tentative",
        }
        body.update(overrides.pop("body", {}))
        values = {
            "namespace": NS,
            "record_id": self.prefs.record_id(NS, CONVERSATION, "length"),
            "record_version": 1,
            "kind": EXPRESSION_PREFERENCE_KIND,
            "body": body,
            "status": "tentative",
            "schema_version": EXPRESSION_PREFERENCE_SCHEMA,
            "privacy_class": "private",
            "recorded_at": "2026-11-05T00:00:00Z",
            "scope": {"session_id": CONVERSATION},
        }
        values.update(overrides)
        return self.store.append_record_version(**values)

    def test_the_active_write_path_is_gone(self) -> None:
        self.assertFalse(hasattr(self.prefs, "record_tentative"))
        self.assertFalse(hasattr(self.prefs, "supersede"))

    def test_legacy_row_is_still_readable_and_keeps_its_scope(self) -> None:
        version = self.legacy()
        self.assertEqual("tentative", version.status)
        self.assertEqual({"session_id": CONVERSATION}, version.scope)

        active = self.prefs.active(
            namespace=NS,
            conversation_id=CONVERSATION,
            privacy_classes=frozenset({"private"}),
        )
        self.assertEqual(1, len(active))
        self.assertEqual("length", active[0].dimension)
        self.assertEqual("concise", active[0].value)

        other = self.prefs.active(
            namespace=NS,
            conversation_id=OTHER_CONVERSATION,
            privacy_classes=frozenset({"private"}),
        )
        self.assertEqual((), other)

    def test_legacy_row_can_still_be_revoked(self) -> None:
        self.legacy()
        self.prefs.revoke(
            namespace=NS,
            conversation_id=CONVERSATION,
            dimension="length",
            reason="user asked to forget it",
            revoked_by_ref=self.ref,
        )
        active = self.prefs.active(
            namespace=NS,
            conversation_id=CONVERSATION,
            privacy_classes=frozenset({"private"}),
        )
        self.assertEqual((), active)

    def test_revoking_an_unknown_dimension_is_refused(self) -> None:
        with self.assertRaises(InvalidInput):
            self.prefs.revoke(
                namespace=NS,
                conversation_id=CONVERSATION,
                dimension="colour",
                reason="not a dimension",
                revoked_by_ref=self.ref,
            )

    def test_a_legacy_row_is_not_an_experience_or_memory_source(self) -> None:
        self.legacy()
        kinds = {
            row["kind"]
            for row in self.store.connection.execute(
                "SELECT DISTINCT kind FROM record_versions WHERE namespace = ?",
                (NS,),
            )
        }
        self.assertIn(EXPRESSION_PREFERENCE_KIND, kinds)
        outbox = self.store.connection.execute(
            "SELECT COUNT(*) FROM memory_delivery"
        ).fetchone()[0]
        self.assertEqual(0, outbox)


if __name__ == "__main__":
    unittest.main()
