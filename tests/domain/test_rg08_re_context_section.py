"""RG08 R-E: the maintenance Context section is explicit and opt-in."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402


class MaintenanceContextSectionTests(CognitionTestCase):
    def _access(self) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=NS,
            host_instance="host:rg08-r-e-context",
            access_ref="authorization:rg08-r-e-context",
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult", "review"}),
        )

    def test_none_keeps_the_existing_document_shape(self) -> None:
        envelope = self.contexts.build_consult_context(
            namespace=NS,
            access=self._access(),
            input_refs=[],
            adapter_kind="direct-http",
        )
        document = json.loads(envelope.rendered_input)
        self.assertNotIn("maintenance_task", document)
        self.assertNotIn("memory_recall", document)

    def test_maintenance_task_is_rendered_without_a_memory_recall_duplicate(self) -> None:
        envelope = self.contexts.build_consult_context(
            namespace=NS,
            access=self._access(),
            input_refs=[],
            adapter_kind="direct-http",
            maintenance_task={
                "schema": "maintenance.review.v1",
                "task_id": "task-1",
                "trigger_key": "turn:1",
                "mode": "cognition.review",
                "instructions": "synthetic",
                "allowed_claim_types": ["general_hypothesis"],
            },
        )
        document = json.loads(envelope.rendered_input)
        self.assertIn("maintenance_task", document)
        self.assertEqual(document["maintenance_task"]["material"], [])
        self.assertNotIn("memory_recall", document)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
