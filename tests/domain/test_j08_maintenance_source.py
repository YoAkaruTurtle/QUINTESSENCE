"""J08 WP2/WP3: bounded material verification and bounded proposal.

The point of both modules is the *bound*: a page larger than the limit is
refused rather than truncated, a vanished source is reported as gone rather
than dropped, and a builder that declines is ``no_change`` rather than a
failure.  None of these tests may commit anything.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.maintenance_review import (  # noqa: E402
    propose_from_material,
)
from quintessence_pcs.domains.memory.maintenance_source import (  # noqa: E402
    MAX_MATERIAL_PAGE,
    usable_material,
    verify_source_material,
)
from quintessence_pcs.foundation.errors import InvalidInput  # noqa: E402


class SourceMaterialTests(CognitionTestCase):
    def test_live_event_is_usable_and_digested(self) -> None:
        event = self.ingest(payload={"text": "synthetic"})
        material = verify_source_material(
            self.store, namespace=NS, event_ids=[event.event_id]
        )
        self.assertEqual(len(material), 1)
        self.assertTrue(material[0].usable)
        self.assertTrue(material[0].payload_digest.startswith("sha256:"))

    def test_missing_source_is_reported_not_dropped(self) -> None:
        material = verify_source_material(
            self.store,
            namespace=NS,
            event_ids=["00000000-0000-4000-8000-000000000000"],
        )
        self.assertEqual(len(material), 1)
        self.assertFalse(material[0].usable)
        self.assertEqual(material[0].reason, "source_gone")
        self.assertEqual(usable_material(material), ())

    def test_page_larger_than_the_limit_is_refused(self) -> None:
        with self.assertRaises(InvalidInput):
            verify_source_material(
                self.store,
                namespace=NS,
                event_ids=[f"id-{index}" for index in range(MAX_MATERIAL_PAGE + 1)],
            )

    def test_a_revision_changes_the_digest(self) -> None:
        first = self.ingest(payload={"text": "one"})
        second = self.ingest(payload={"text": "two"})
        material = verify_source_material(
            self.store, namespace=NS, event_ids=[first.event_id, second.event_id]
        )
        self.assertNotEqual(
            material[0].payload_digest, material[1].payload_digest
        )


class ReviewTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        from quintessence_pcs.collaboration.change_service import (
            ChangeAccess,
            CognitionChangeService,
        )

        self.ChangeAccess = ChangeAccess
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: "2026-10-27T12:00:00Z"
        )
        self.context = self.build_context()

    def _access(self):
        return self.ChangeAccess(
            namespace=NS,
            host_instance="synthetic-host",
            operator="operator-a",
            access_ref="access:synthetic",
            permissions=frozenset(
                {"change.propose", "change.review", "change.commit", "change.result"}
            ),
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"cognition.change"}),
        )

    def _material(self):
        event = self.ingest(payload={"text": "synthetic"})
        return verify_source_material(
            self.store, namespace=NS, event_ids=[event.event_id]
        )

    def test_declining_builder_is_no_change_not_failure(self) -> None:
        import uuid

        outcomes = propose_from_material(
            self.changes,
            self._access(),
            context_id=self.context.context_id,
            material=self._material(),
            build_bundle=lambda item: None,
            request_ids=[str(uuid.uuid4())],
        )
        self.assertEqual(outcomes[0].outcome, "no_change")
        self.assertIsNone(outcomes[0].candidate_id)

    def test_each_item_needs_its_own_request_id(self) -> None:
        with self.assertRaises(InvalidInput):
            propose_from_material(
                self.changes,
                self._access(),
                context_id=self.context.context_id,
                material=self._material(),
                build_bundle=lambda item: None,
                request_ids=[],
            )

    def test_proposal_is_bounded_by_the_limit(self) -> None:
        events = [self.ingest(payload={"text": f"e{index}"}) for index in range(3)]
        material = verify_source_material(
            self.store, namespace=NS, event_ids=[e.event_id for e in events]
        )
        import uuid

        with self.assertRaises(InvalidInput):
            propose_from_material(
                self.changes,
                self._access(),
                context_id=self.context.context_id,
                material=material,
                build_bundle=lambda item: None,
                request_ids=[str(uuid.uuid4())] * 3,
                limit=2,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
