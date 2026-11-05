"""RG04 04.2/04.3: durable postprocess status and the bounded sweep."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
STORE_TESTS = HERE.parent / "store"
SRC = HERE.parents[1] / "src"
for _path in (HERE, STORE_TESTS, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import CognitionTestCase, NS  # noqa: E402

from quintessence_pcs.collaboration.consult import _artifact_sources  # noqa: E402
from quintessence_pcs.domains.memory.experience_bridge import (  # noqa: E402
    BridgeConfig,
    ExperienceBridge,
)
from quintessence_pcs.domains.memory.postprocess import (  # noqa: E402
    MEMORY_DELIVERED,
    MEMORY_NEEDS_REVIEW,
    MEMORY_NOT_CONFIGURED,
    MEMORY_NOT_ELIGIBLE,
    MEMORY_OUTCOME_UNKNOWN,
    MEMORY_PENDING,
    run_postprocess_sweep,
    turn_postprocess_status,
)
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg04-postprocess"
ACCESS_REF = "authorization:rg04-postprocess"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class UnknownAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def capture(self, event, *, actor_origin, timeout_seconds):
        self.calls += 1
        return {"kind": "unknown", "error": "timeout"}


class OkAdapter:
    def __init__(self) -> None:
        self.calls = 0

    def capture(self, event, *, actor_origin, timeout_seconds):
        self.calls += 1
        return {
            "kind": "ok",
            "receipt": {
                "disposition": "inserted",
                "durability": "persisted",
                "event_refs": [{"ref": "external-rg04-post", "revision": 1}],
                "gaps": [],
                "error_code": None,
            },
        }


class PostprocessTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.access = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"memory.bridge", "consult"}),
        )
        self.profile_ref = self.profiles.get_active_profile(NS).ref.to_dict()
        self.conversation = self.store.create_conversation(
            namespace=NS, host_instance=HOST, mode="chat"
        )

    def _turn(self, text: str = "第一问"):
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"req-{text}",
            text=text,
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access,
            input_refs=[creation.input_ref],
            adapter_kind="direct-http",
        )
        interaction_id = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref=POLICY_REF,
            input_refs=[creation.input_ref],
            context_id=context.context_id,
        )
        stage = self.store.begin_turn(namespace=NS, turn_id=creation.turn.turn_id)
        self.store.bind_stage_context(
            namespace=NS,
            stage_id=stage.stage_id,
            context_id=context.context_id,
        )
        return creation, context, interaction_id, stage

    def _complete(self, text: str = "第一问", reply: str = "回答"):
        creation, context, interaction_id, stage = self._turn(text)
        turn = self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text=reply,
            retention="standard",
            sources=_artifact_sources(context),
            attempt_ref=None,
        )
        return creation, turn

    def _status(self, turn_id: str, *, configured: bool = True, store=None):
        return turn_postprocess_status(
            store or self.store,
            namespace=NS,
            host_instance=HOST,
            turn_id=turn_id,
            access=self.access,
            configured=configured,
        )

    def _bridge(self, adapter):
        return ExperienceBridge(
            self.store, adapter, BridgeConfig(scope_id="scope-rg04-post")
        )

    def _sweep(self, bridge, store=None):
        return run_postprocess_sweep(
            store or self.store,
            bridge,
            access=self.access,
            namespace=NS,
            host_instance=HOST,
        )

    def test_status_distinguishes_saved_from_memory(self) -> None:
        creation, turn = self._complete()
        status = self._status(turn.turn_id)
        self.assertTrue(status.transcript_saved)
        self.assertEqual(MEMORY_PENDING, status.memory_state)
        self.assertEqual("not_projected", status.memory_detail)
        self.assertEqual(2, status.pending)
        self.assertEqual(0, status.delivered)

        report = self._sweep(self._bridge(OkAdapter()))
        self.assertEqual(2, report.projected_refs)
        self.assertEqual(1, report.delivered)  # one due delivery per slice
        status = self._status(turn.turn_id)
        self.assertEqual(MEMORY_PENDING, status.memory_state)  # final still pending
        self.assertEqual(1, status.delivered)
        self.assertEqual(1, status.pending)
        self.assertIsNotNone(self.store.delivery_watermark(NS))

        self._sweep(self._bridge(OkAdapter()))
        status = self._status(turn.turn_id)
        self.assertEqual(MEMORY_DELIVERED, status.memory_state)
        self.assertEqual(2, status.delivered)

    def test_not_configured_is_not_a_failure(self) -> None:
        _creation, turn = self._complete()
        status = self._status(turn.turn_id, configured=False)
        self.assertTrue(status.transcript_saved)
        self.assertEqual(MEMORY_NOT_CONFIGURED, status.memory_state)

    def test_running_turn_is_pending_not_memory(self) -> None:
        creation, _context, _interaction, _stage = self._turn("进行中")
        status = self._status(creation.turn.turn_id)
        self.assertFalse(status.transcript_saved)
        self.assertEqual(MEMORY_PENDING, status.memory_state)
        self.assertEqual("turn_running", status.memory_detail)

    def test_interrupted_before_interaction_is_not_eligible(self) -> None:
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id="req-no-interaction",
            text="T1 only",
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        self.store.interrupt_stale_turns(namespace=NS)
        status = self._status(creation.turn.turn_id)
        self.assertTrue(status.transcript_saved)
        self.assertEqual(MEMORY_NOT_ELIGIBLE, status.memory_state)
        self.assertEqual("no_eligible_refs", status.memory_detail)

    def test_unknown_capture_is_visible_and_not_delivered(self) -> None:
        _creation, turn = self._complete()
        report = self._sweep(self._bridge(UnknownAdapter()))
        self.assertEqual(0, report.delivered)
        status = self._status(turn.turn_id)
        self.assertEqual(MEMORY_OUTCOME_UNKNOWN, status.memory_state)
        self.assertGreaterEqual(status.pending, 1)

    def test_crash_after_terminal_commit_is_recoverable(self) -> None:
        _creation, turn = self._complete("崩溃前", "已保存回答")
        self.store.close()
        reopened = CognitionStore.open(self.db_path)
        self.addCleanup(reopened.close)
        status = self._status(turn.turn_id, store=reopened)
        self.assertTrue(status.transcript_saved)
        self.assertEqual(MEMORY_PENDING, status.memory_state)
        bridge = ExperienceBridge(
            reopened, OkAdapter(), BridgeConfig(scope_id="scope-rg04-post")
        )
        report = run_postprocess_sweep(
            reopened,
            bridge,
            access=self.access,
            namespace=NS,
            host_instance=HOST,
        )
        self.assertEqual(2, report.projected_refs)
        status = self._status(turn.turn_id, store=reopened)
        self.assertIn(status.memory_state, (MEMORY_PENDING, MEMORY_DELIVERED))
        self.assertGreaterEqual(status.pending + status.delivered, 1)

    def test_needs_review_is_a_terminal_ui_state(self) -> None:
        class ConflictAdapter:
            def capture(self, event, *, actor_origin, timeout_seconds):
                return {
                    "kind": "ok",
                    "receipt": {
                        "disposition": "conflict",
                        "durability": "persisted",
                        "event_refs": [],
                        "gaps": [],
                        "error_code": "CONFLICT",
                    },
                }

        _creation, turn = self._complete()
        self._sweep(self._bridge(ConflictAdapter()))
        status = self._status(turn.turn_id)
        self.assertEqual(MEMORY_NEEDS_REVIEW, status.memory_state)


if __name__ == "__main__":
    unittest.main()
