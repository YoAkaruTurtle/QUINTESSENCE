"""RG08 C7: one PolicyBundle per turn, wired through both stages.

These tests drive a real v2 turn through the existing tool-loop harness and spy
on the three wiring points: the bundle is resolved once, the content and
expression tasks receive *that same object* (identity, not equality), an off
turn never reads the persona asset, and the event records which policy text was
sent without ever carrying the text itself.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
for _path in (
    HERE,
    ROOT / "src",
    ROOT / "tests" / "conversation",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import quintessence_pcs.conversation.orchestrator as orch_module  # noqa: E402
from quintessence_pcs.conversation.policy_bundle import (  # noqa: E402
    current_bundle,
    resolve_bundle,
)
from quintessence_pcs.conversation.sdrpe import SdrpePlan  # noqa: E402
from quintessence_pcs.domains.persona.policy import (  # noqa: E402
    CANDIDATE_KIND,
    POLICY_SCHEMA_VERSION,
    policy_record_id,
    policy_text_sha256,
)
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402
from quintessence_pcs.runtime.collab_protocol import (  # noqa: E402
    COLLAB_PROTOCOL_VERSION,
    COLLAB_V2_PROTOCOL_VERSION,
)
from test_tool_loop import HOST, NS, V2ToolLoopTests  # noqa: E402

CANDIDATE_TEXT = "这是某个合成人格的表达政策。"


class TurnBundleWiringTests(V2ToolLoopTests):
    def _profile_pointers(self):
        record = self.store.get_version(
            NS,
            self.profile_ref["record_id"],
            int(self.profile_ref["record_version"]),
        )
        return record.body["profile_id"], str(record.body["profile_version"])

    def _append_candidate(self, text: str = CANDIDATE_TEXT) -> str:
        profile_id, profile_version = self._profile_pointers()
        record_id = policy_record_id(NS, CANDIDATE_KIND, profile_id, profile_version)
        self.store.append_record_version(
            namespace=NS,
            record_id=record_id,
            record_version=1,
            kind=CANDIDATE_KIND,
            body={
                "schema_version": POLICY_SCHEMA_VERSION,
                "status": "lab_candidate",
                "profile_id": profile_id,
                "profile_version": profile_version,
                "text": text,
                "text_sha256": policy_text_sha256(text),
                "review_ref": None,
            },
            schema_version=POLICY_SCHEMA_VERSION,
        )
        return record_id

    def test_one_bundle_instance_reaches_both_stages(self) -> None:
        seen: dict[str, object] = {}
        real_resolve = orch_module.resolve_bundle
        real_content = orch_module.build_content_task
        real_expression = orch_module.build_expression_task

        def spy_resolve(*args, **kwargs):
            bundle = real_resolve(*args, **kwargs)
            seen["bundle"] = bundle
            seen["include_persona"] = kwargs.get("include_persona")
            return bundle

        def spy_content(*, sdrpe_mode, bundle):
            seen.setdefault("content", []).append(bundle)
            return real_content(sdrpe_mode=sdrpe_mode, bundle=bundle)

        def spy_expression(*, pending_cognition_changes, bundle):
            seen["expression"] = bundle
            return real_expression(
                pending_cognition_changes=pending_cognition_changes, bundle=bundle
            )

        with mock.patch.object(orch_module, "resolve_bundle", spy_resolve), \
                mock.patch.object(orch_module, "build_content_task", spy_content), \
                mock.patch.object(orch_module, "build_expression_task", spy_expression):
            self.enqueue({"action": "final", "reply": "草稿。"})
            self.enqueue({"action": "final", "reply": "最终。"})
            _creation, execution = self.send("写一句。", sdrpe="on")

        self.assertEqual("completed", execution.turn.state)
        self.assertIs(True, seen["include_persona"])
        bundle = seen["bundle"]
        self.assertEqual(1, len(seen["content"]))
        self.assertIs(bundle, seen["content"][0])
        self.assertIs(bundle, seen["expression"])

    def test_off_turn_never_resolves_the_persona_asset(self) -> None:
        seen: dict[str, object] = {}
        real_resolve = orch_module.resolve_bundle

        def spy_resolve(*args, **kwargs):
            seen.update(kwargs)
            return real_resolve(*args, **kwargs)

        with mock.patch.object(orch_module, "resolve_bundle", spy_resolve):
            self.enqueue({"action": "final", "reply": "平实回答。"})
            _creation, execution = self.send("说一句。")

        self.assertEqual("completed", execution.turn.state)
        self.assertIs(False, seen["include_persona"])

    def test_v1_never_builds_a_content_task(self) -> None:
        orchestrator = orch_module.ConversationOrchestrator(
            self.store,
            context_service=self.contexts,
            model=self.model,
            protocol_version=COLLAB_PROTOCOL_VERSION,
        )
        plan = SdrpePlan.resolve("on", protocol_version=COLLAB_PROTOCOL_VERSION)
        self.assertIsNone(orchestrator._content_task_for(plan, current_bundle()))
        self.assertIsNone(orchestrator._policy_record_ref(current_bundle()))

    def test_host_is_lab_defaults_to_false(self) -> None:
        self.assertIs(False, self.orchestrator._host_is_lab)
        lab = orch_module.ConversationOrchestrator(
            self.store,
            context_service=self.contexts,
            model=self.model,
            protocol_version=COLLAB_V2_PROTOCOL_VERSION,
            host_is_lab=True,
        )
        self.assertIs(True, lab._host_is_lab)

    def test_candidate_asset_is_visible_only_to_a_lab_host(self) -> None:
        self._append_candidate()
        ref = RecordRef.from_value(self.profile_ref, field="profile_ref")

        lab = resolve_bundle(
            self.store, NS, ref, host_is_lab=True, include_persona=True
        )
        self.assertEqual("present", lab.persona.state)
        self.assertEqual(CANDIDATE_TEXT, lab.persona.text)
        self.assertEqual("unreviewed", lab.persona.review_state)

        production = resolve_bundle(
            self.store, NS, ref, host_is_lab=False, include_persona=True
        )
        self.assertEqual("absent", production.persona.state)
        self.assertEqual("candidate_ignored", production.persona.reason)

    def test_event_payload_reports_policy_provenance_without_the_text(self) -> None:
        self._append_candidate()
        lab = orch_module.ConversationOrchestrator(
            self.store,
            context_service=self.contexts,
            model=self.model,
            protocol_version=COLLAB_V2_PROTOCOL_VERSION,
            tool_runtime_factory=self.factory,
            host_is_lab=True,
        )
        self.enqueue({"action": "final", "reply": "草稿。"})
        self.enqueue({"action": "final", "reply": "最终。"})
        _creation, execution = self.send(
            "写一句。", orchestrator=lab, sdrpe="on"
        )
        self.assertEqual("completed", execution.turn.state)

        events = [row for row in self.events(execution.turn.turn_id)
                  if row.kind == "expression_checked"]
        self.assertEqual(1, len(events))
        payload = events[0].payload
        for key in (
            "policy_version",
            "policy_digest",
            "shared_sha256",
            "content_sha256",
            "host_expression_sha256",
            "persona_policy_state",
            "persona_policy_review_state",
            "persona_policy_ref",
            "persona_policy_sha256",
            "persona_profile_id",
            "persona_profile_version",
            "expression_sha256",
        ):
            self.assertIn(key, payload)
        self.assertEqual("present", payload["persona_policy_state"])
        self.assertEqual("unreviewed", payload["persona_policy_review_state"])
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn(CANDIDATE_TEXT, serialized)
        self.assertNotIn("草稿", serialized)


if __name__ == "__main__":
    unittest.main()
