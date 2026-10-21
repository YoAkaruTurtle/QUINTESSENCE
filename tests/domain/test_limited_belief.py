"""PCS2-010 Phase 0 diagnostic: can the existing schema express limited knowledge?

These tests are not a model run.  They prove the *capacity* side of the
question: a narrow, observation-level claim with ``strength=limited``,
explicit scope, ``valid_until`` and three evidence links is accepted by the
existing validator, persisted, and visible in a later Context.  The real
Round-3 model did not emit such a claim; that is a model/prompt finding, not a
schema rejection.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402


class LimitedBeliefCapacityTests(CognitionTestCase):
    def _evidence_events(self):
        return (
            self.ingest(
                source_kind="host_observation",
                event_type="tool.path_probe_dev.scan",
                payload={
                    "summary": "长中文路径扫描失败，原因未定位",
                    "environment": "lab-dev",
                    "fixture_id": "DIAG-D1",
                },
            ),
            self.ingest(
                source_kind="host_observation",
                event_type="tool.path_probe_dev.review",
                payload={
                    "finding": "权限拒绝",
                    "summary": "复核指出失败原因为权限拒绝",
                    "environment": "lab-dev",
                    "fixture_id": "DIAG-D2",
                },
            ),
            self.ingest(
                source_kind="host_observation",
                event_type="tool.path_probe_dev.scan",
                payload={
                    "summary": "权限具备后同版本同环境同条件扫描成功",
                    "environment": "lab-dev",
                    "fixture_id": "DIAG-D3",
                },
            ),
        )

    def _local_body(self, events) -> dict:
        return self.belief_body(
            claim=(
                "此次 lab-dev 观察中，path_probe_dev 的扫描失败与权限拒绝有关；"
                "权限具备后，同版本、同环境、同条件扫描成功。"
            ),
            claim_type="environment_state",
            evidence_links=[
                self.evidence_link(self.event_ref(event.event_id)) for event in events
            ],
            strength="limited",
            strength_reason=(
                "三次合成观察支持这次局部关系；不足以支持长期可靠性"
            ),
            inference_basis="observation",
            valid_until="2026-12-31T00:00:00Z",
            review_condition=(
                "需要更多同条件重复观察后再评估长期可靠性；"
                "若出现反例则重新评估。"
            ),
        )

    def test_limited_local_environment_state_is_accepted_and_persisted(self) -> None:
        events = self._evidence_events()
        context = self.build_context(events[0].event_id)
        scope = {"scene": "example.scene", "entity_ids": ["path_probe_dev"]}
        version = self.beliefs.propose_belief(
            namespace=NS,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            body=self._local_body(events),
            scope=scope,
        )
        self.assertEqual("supported", version.status)
        self.assertEqual("environment_state", version.body["claim_type"])
        self.assertEqual("limited", version.body["strength"])
        self.assertEqual("2026-12-31T00:00:00Z", version.body["valid_until"])
        self.assertEqual(scope, version.scope)
        self.assertEqual(3, len(version.body["evidence_links"]))
        self.assertIn("同版本、同环境、同条件", version.body["claim"])
        self.assertIn("长期可靠性", version.body["review_condition"])

    def test_local_belief_is_visible_in_a_later_context(self) -> None:
        events = self._evidence_events()
        context = self.build_context(events[0].event_id)
        scope = {"scene": "example.scene", "entity_ids": ["path_probe_dev"]}
        version = self.beliefs.propose_belief(
            namespace=NS,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            body=self._local_body(events),
            scope=scope,
        )
        later_question = self.ingest(
            source_kind="user_report",
            event_type="exp01.question",
            payload={
                "question": "基于这次局部认识，能否进一步判断长期可靠性？",
                "scenario": "DIAG",
                "synthetic_fixture": True,
            },
        )
        later = self.build_context(later_question.event_id)
        self.assertTrue(
            any(
                ref.record_id == version.record_id
                for ref in later.belief_refs
            ),
            "the newly persisted local belief must be available to a later Context",
        )
        rendered = json.loads(later.rendered_input)
        claims = [
            item.get("claim")
            for item in rendered.get("beliefs", [])
            if isinstance(item, dict)
        ]
        self.assertTrue(
            any("权限拒绝" in (claim or "") for claim in claims),
            "the later Context must render the local belief body",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
