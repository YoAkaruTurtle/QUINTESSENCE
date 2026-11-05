"""UI-F01: the repair marker is written by the real loop, once per repair.

A format repair retries the same logical request with feedback.  When that
repair request itself hits a transport failure, the adapter resends the exact
same body: two HTTP attempts for one logical repair.  The persisted marker must
name only the attempt that settled the repair, and the transport retry must
stay a transport retry.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (
    REPO_ROOT / "tests" / "domain",
    REPO_ROOT / "tests" / "store",
    REPO_ROOT / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS  # noqa: E402
from model_support import ModelPortTestCase, make_manifest  # noqa: E402

from quintessence_pcs.conversation import dto as conversation_dto  # noqa: E402
from quintessence_pcs.conversation.orchestrator import (  # noqa: E402
    ConversationOrchestrator,
    resolve_turn_profile,
)
from quintessence_pcs.runtime import (  # noqa: E402
    ConsultSendPolicy,
    DirectHttpModel,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:ui-f01-trace"
ACCESS_REF = "authorization:ui-f01-trace"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"
REPAIR_PAYLOAD_KEYS = {"stage", "reason", "attempt_id", "attempt_no"}


class UsageTraceLoopTests(ModelPortTestCase):
    """The real DirectHttpModel against the loopback scripted provider."""

    def setUp(self) -> None:
        super().setUp()
        for existing in self.store.model_authorizations(
            namespace=NS, status="active"
        ):
            self.store.revoke_model_authorization(existing.authorization_id)
        self.manifest = make_manifest(
            self.provider.url,
            api_key_env=self.ENV_NAME,
            max_calls=64,
            max_wall_seconds=30,
        )
        self.grant_authorization(
            manifest=self.manifest, max_calls=64, max_wall_seconds=30
        )
        self.access = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"consult"}),
        )
        self.policy = ConsultSendPolicy(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY}),
            purposes=frozenset({"consult"}),
            disclose=True,
        )
        self.model = DirectHttpModel(
            self.store,
            self.manifest,
            protocol="collab.v1",
            consult_policy=self.policy,
            request_budget_units=200_000,
            output_reserve_units=4096,
        )
        self.orchestrator = ConversationOrchestrator(
            self.store,
            context_service=self.contexts,
            model=self.model,
        )
        self.profile_ref = resolve_turn_profile(self.profiles, NS)
        self.conversation = self.store.create_conversation(
            namespace=NS, host_instance=HOST, mode="chat"
        )
        self._request = 0

    # ------------------------------------------------------------- helpers

    def send(self, text: str):
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"ui-f01-trace-{self._request}",
            text=text,
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        return creation, execution

    def attempts(self, trace: dict) -> list[dict]:
        return [
            attempt
            for stage in trace["stages"]
            for context in stage["contexts"]
            for attempt in context["attempts"]
        ]

    def repair_events(self, turn_id: str):
        page = self.store.read_turn_events(
            namespace=NS, host_instance=HOST, turn_id=turn_id
        )
        return [row for row in page.events if row.kind == "repair_attempt"]

    # --------------------------------------------------------------- tests

    def test_repair_with_internal_transport_retry_marks_one_attempt(self) -> None:
        # 1) a real format error (valid envelope, unusable content) => repair
        self.enqueue_content("{not json")
        # 2) the repair request itself hits a retryable transport failure...
        self.provider.enqueue(
            self.response({"error": "rate limited"}, status=429)
        )
        # 3) ... and the adapter resends the exact same body successfully.
        self.enqueue_content('{"action":"none","reply":"修复后的回答"}')

        creation, execution = self.send("格式修复")
        self.assertEqual("completed", execution.turn.state)
        self.assertEqual(
            "修复后的回答", self.store.turn_result(
                namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id,
                access=self.access,
            ).final.text
        )

        trace = self.store.turn_trace(
            namespace=NS, host_instance=HOST, turn_id=creation.turn.turn_id
        )
        attempts = self.attempts(trace)
        self.assertEqual(3, len(attempts))
        self.assertEqual(
            ["primary", "transport_retry", "repair"],
            [item["kind"] for item in attempts],
        )
        self.assertEqual(1, trace["counts"]["repairs"])
        self.assertEqual(0, trace["counts"]["repairs_unbound"])
        transport = attempts[1]
        self.assertFalse(transport["repair"])
        self.assertTrue(transport["resend_same_body"])
        repair = attempts[2]
        self.assertTrue(repair["repair"])
        self.assertEqual("format", repair["repair_reason"])
        self.assertEqual(3, trace["attempts_total"])
        self.assertEqual(2, trace["attempts_known"])
        self.assertEqual(1, trace["attempts_unknown"])
        self.assertFalse(trace["tokens"]["complete"])

        events = self.repair_events(creation.turn.turn_id)
        self.assertEqual(1, len(events))
        payload = dict(events[0].payload)
        self.assertEqual(REPAIR_PAYLOAD_KEYS, set(payload))
        self.assertEqual("content", payload["stage"])
        self.assertEqual("format", payload["reason"])
        self.assertEqual(repair["attempt_id"], payload["attempt_id"])
        self.assertEqual(3, payload["attempt_no"])
        rendered = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("authorization_id", rendered)
        self.assertNotIn("request_hash", rendered)
        self.assertNotIn("sha256:", rendered)
        self.assertNotIn(self.auth.authorization_id, rendered)

        public = conversation_dto.turn_event_dict(events[0])
        public_text = json.dumps(public, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("authorization_id", public_text)
        self.assertNotIn("request_hash", public_text)
        self.assertNotIn("sha256:", public_text)
        self.assertIsNone(public.get("attempt_ref"))

        usage = self.store.conversation_usage(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            turn_id=creation.turn.turn_id,
        )
        self.assertEqual(1, usage["counts"]["repairs"])
        self.assertEqual(0, usage["counts"]["repairs_unbound"])
        self.assertEqual(3, usage["attempts_total"])
        self.assertEqual(1, usage["turn"]["counts"]["repairs"])


if __name__ == "__main__":
    unittest.main()
