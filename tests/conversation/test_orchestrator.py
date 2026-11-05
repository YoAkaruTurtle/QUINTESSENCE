"""RG02 conversation orchestration: C01/C02/C03, 12 turns, repair, query, wait.

The model is the real ``DirectHttpModel`` pointed at the loopback scripted
provider from ``tests/domain/model_support.py``: every reply artifact cites a
real settled attempt, exactly like production.  No network or private data.
"""

from __future__ import annotations

import json
import sys
import tempfile
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

from quintessence_pcs.conversation.orchestrator import (  # noqa: E402
    ConversationOrchestrator,
    PersonaNotReady,
    resolve_turn_profile,
)
from quintessence_pcs.domains.persona.profiles import (  # noqa: E402
    PersonaProfileService,
)
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime import (  # noqa: E402
    ConsultSendPolicy,
    DirectHttpModel,
)
from quintessence_pcs.runtime.model_port import ModelAdapterError  # noqa: E402
from quintessence_pcs.store.conversation import (  # noqa: E402
    HistoryBundle,
    HistoryMessage,
)
from quintessence_pcs.store.cognition import (  # noqa: E402
    MAX_CONTEXT_BYTES,
    CognitionStore,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg02-orchestrator"
ACCESS_REF = "authorization:rg02-orchestrator"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class OrchestratorTests(ModelPortTestCase):
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
        self.chat_model = DirectHttpModel(
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
            model=self.chat_model,
        )
        self.profile_ref = resolve_turn_profile(self.profiles, NS)
        self.conversation = self.store.create_conversation(
            namespace=NS, host_instance=HOST, mode="chat"
        )
        self._request = 0

    # ------------------------------------------------------------- helpers

    def enqueue(self, payload: dict) -> None:
        self.enqueue_content(canonical_json_text(payload))

    def send(self, text: str, *, orchestrator=None):
        self._request += 1
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id=f"req-{self._request}",
            text=text,
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=self.profile_ref,
            strategy={"mode": "chat"},
        )
        return creation, (orchestrator or self.orchestrator).execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=self.profile_ref,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )

    def answer(self, text: str, reply: str):
        self.enqueue({"action": "none", "reply": reply})
        return self.send(text)

    def context_of(self, turn_id: str):
        row = self.store.connection.execute(
            "SELECT sc.context_id FROM collab_stage_contexts sc "
            "JOIN collab_turn_stages st ON st.namespace = sc.namespace "
            "AND st.stage_id = sc.stage_id WHERE st.turn_id = ?",
            (turn_id,),
        ).fetchone()
        return self.store.load_context_snapshot(row["context_id"])

    def result(self, turn_id: str):
        return self.store.turn_result(
            namespace=NS, host_instance=HOST, turn_id=turn_id, access=self.access
        )

    # ---------------------------------------------------------------- C01

    def test_two_options_are_reachable_from_the_next_turn(self) -> None:
        self.enqueue(
            {
                "action": "none",
                "reply": "安排 A：周三晚上；安排 B：周六下午。",
            }
        )
        first, execution = self.send("我们准备办读书会，我更喜欢详细的解释。")
        self.assertEqual("completed", execution.turn.state)
        second, execution = self.answer("选第二个，再简短点。", "好，周六下午。")
        self.assertEqual("completed", execution.turn.state)
        context = self.context_of(second.turn.turn_id)
        document = json.loads(context.rendered_input)
        self.assertEqual(2, len(document["history"]))
        self.assertEqual("user", document["history"][0]["role"])
        self.assertIn("安排 A", document["history"][1]["text"])
        self.assertIn("安排 B", document["history"][1]["text"])
        self.assertEqual("选第二个，再简短点。", document["inputs"][0]["content"])
        # The actual request body carries the same persisted text.
        body = json.loads(self.provider.requests[-1]["body"].decode("utf-8"))
        sent = body["messages"][1]["content"]
        self.assertEqual(context.rendered_input, sent)
        self.assertLess(sent.index("安排 A"), sent.index("选第二个"))
        rows = self.store.context_history_rows(
            namespace=NS, context_id=context.context_id
        )
        self.assertEqual(
            ["collab_input", "collab_artifact"],
            [row["ref_kind"] for row in rows],
        )

    def test_empty_history_document_is_byte_compatible(self) -> None:
        first, execution = self.answer("第一轮", "第一答")
        context = self.context_of(first.turn.turn_id)
        document = json.loads(context.rendered_input)
        self.assertNotIn("history", document)
        self.assertEqual("collab.v1.renderer.3", context.renderer_version)
        self.assertEqual(
            {"context_kind", "run_id", "namespace", "profile", "self_view",
             "inputs", "omissions"},
            set(document),
        )
        self.assertEqual(
            0,
            len(
                self.store.context_history_rows(
                    namespace=NS, context_id=context.context_id
                )
            ),
        )

    # ---------------------------------------------------------------- C02

    def test_history_budget_keeps_the_newest_turn_and_the_correction(self) -> None:
        for index in range(1, 6):
            self.enqueue({"action": "none", "reply": "长回答" + "内容" * 1500})
            self.send(f"问题{index}")
        tight = ConversationOrchestrator(
            self.store,
            context_service=self.contexts,
            model=self.chat_model,
            max_history_turns=8,
            max_history_bytes=1024,
        )
        self.enqueue({"action": "none", "reply": "好。"})
        _, execution = self.send("改一下：以后简短点。", orchestrator=tight)
        self.assertEqual("completed", execution.turn.state)
        context = self.context_of(execution.turn.turn_id)
        self.assertLessEqual(
            len(context.rendered_input.encode("utf-8")), MAX_CONTEXT_BYTES
        )
        document = json.loads(context.rendered_input)
        self.assertEqual("改一下：以后简短点。", document["inputs"][0]["content"])
        reasons = {item.get("reason") for item in document["omissions"]}
        self.assertIn("history_byte_limit", reasons)

    # ---------------------------------------------------------------- C03

    def test_two_conversations_do_not_mix_pronouns(self) -> None:
        other = self.store.create_conversation(
            namespace=NS, host_instance=HOST, mode="chat"
        )

        def send_to(conversation, text, request_id):
            creation = self.store.register_turn(
                namespace=NS,
                host_instance=HOST,
                conversation_id=conversation.conversation_id,
                client_request_id=request_id,
                text=text,
                privacy_class=PRIVACY,
                retention="standard",
                profile_ref=self.profile_ref,
                strategy={"mode": "chat"},
            )
            return creation, self.orchestrator.execute(
                conversation=conversation,
                turn=creation.turn,
                input_ref=creation.input_ref,
                profile_ref=self.profile_ref,
                access=self.access,
                retention="standard",
                policy_ref=POLICY_REF,
            )

        self.enqueue({"action": "none", "reply": "A 的他说的是甲。"})
        send_to(self.conversation, "A 会话里的那个他", "a-1")
        self.enqueue({"action": "none", "reply": "A 会话第二答。"})
        _, a2 = send_to(self.conversation, "继续 A", "a-2")
        self.enqueue({"action": "none", "reply": "B 的他说的是乙。"})
        send_to(other, "B 会话里的那个他", "b-1")
        self.enqueue({"action": "none", "reply": "B 会话第二答。"})
        _, b2 = send_to(other, "继续 B", "b-2")
        a_doc = json.loads(self.context_of(a2.turn.turn_id).rendered_input)
        b_doc = json.loads(self.context_of(b2.turn.turn_id).rendered_input)
        self.assertIn("A 的他说的是甲。", [m["text"] for m in a_doc["history"]])
        self.assertNotIn("B 的他说的是乙。", [m["text"] for m in a_doc["history"]])
        self.assertIn("B 的他说的是乙。", [m["text"] for m in b_doc["history"]])
        self.assertNotIn("A 的他说的是甲。", [m["text"] for m in b_doc["history"]])

    # --------------------------------------------------------------- TWELVE

    def test_twelve_turns_register_without_a_closure_depth_error(self) -> None:
        for index in range(1, 13):
            self.enqueue(
                {"action": "none", "reply": f"第{index}轮回答：" + "答" * 40}
            )
            _, execution = self.send(f"第{index}轮问题")
            self.assertEqual(
                "completed", execution.turn.state,
                f"turn {index}: {self.result(execution.turn.turn_id).failure_reason}",
            )
        turns = self.store.read_turns(
            namespace=NS, host_instance=HOST,
            conversation_id=self.conversation.conversation_id, limit=50,
        )
        self.assertEqual(12, len(turns))
        self.assertEqual(
            12, sum(1 for item in turns if item.turn.state == "completed")
        )
        finals = self.store.connection.execute(
            "SELECT COUNT(*) FROM collab_turn_messages WHERE namespace = ? "
            "AND kind = 'final' AND status = 'published'",
            (NS,),
        ).fetchone()[0]
        self.assertEqual(12, int(finals))
        newest = self.context_of(turns[-1].turn.turn_id)
        document = json.loads(newest.rendered_input)
        self.assertLessEqual(len(document["history"]), 16)
        self.assertEqual(
            16,
            len(
                self.store.context_history_rows(
                    namespace=NS, context_id=newest.context_id
                )
            ),
        )
        # The reply chain never exceeds the frozen depth: every reply cites
        # only its own material sources, never the previous reply.
        reply = self.store.connection.execute(
            "SELECT artifact_id FROM collab_turn_messages WHERE turn_id = ? "
            "AND kind = 'final'",
            (turns[-1].turn.turn_id,),
        ).fetchone()[0]
        sources = self.store.connection.execute(
            "SELECT source_kind FROM collab_source_links WHERE namespace = ? "
            "AND owner_kind = 'artifact' AND owner_id = ?",
            (NS, reply),
        ).fetchall()
        self.assertNotIn("collab_artifact", [row[0] for row in sources])

    # ------------------------------------------------------------- failures

    def test_query_action_fails_without_a_second_context(self) -> None:
        self.enqueue({"action": "query", "brief_reason": "needs facts"})
        creation, execution = self.send("需要事实的问题")
        self.assertEqual("failed", execution.turn.state)
        result = self.result(creation.turn.turn_id)
        self.assertEqual("query_unavailable", result.failure_reason)
        self.assertIsNone(result.final)
        self.assertEqual(
            1,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM context_snapshots"
                ).fetchone()[0]
            ),
        )

    def test_wait_action_becomes_a_wait_question(self) -> None:
        self.enqueue({"action": "wait", "reply": "你指的是哪一个？"})
        creation, execution = self.send("需要澄清")
        self.assertEqual("waiting", execution.turn.state)
        result = self.result(creation.turn.turn_id)
        self.assertEqual("你指的是哪一个？", result.wait_question.text)
        self.assertIsNone(result.final)

    def test_restart_after_waiting_turn_does_not_refuse_the_host(self) -> None:
        """RG06: a waiting interaction is already frozen; restart must not
        downgrade it to 'incomplete' (that used to raise CORRUPT_STORE and make
        the whole host refuse to start)."""

        self.enqueue({"action": "wait", "reply": "你指的是哪一个？"})
        creation, execution = self.send("需要澄清")
        self.assertEqual("waiting", execution.turn.state)
        interrupted = self.store.interrupt_stale_turns(namespace=NS)
        self.assertEqual(1, interrupted)
        state = self.store.connection.execute(
            "SELECT state FROM collab_turns WHERE namespace = ? AND turn_id = ?",
            (NS, creation.turn.turn_id),
        ).fetchone()[0]
        self.assertEqual("interrupted", state)
        self.assertEqual(0, self.store.interrupt_stale_turns(namespace=NS))

    def test_format_repair_uses_the_same_context(self) -> None:
        self.enqueue_content("{not json")
        self.enqueue({"action": "none", "reply": "修复后的回答"})
        creation, execution = self.send("格式修复")
        self.assertEqual("completed", execution.turn.state)
        self.assertEqual("修复后的回答", self.result(creation.turn.turn_id).final.text)
        self.assertEqual(
            1,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM context_snapshots"
                ).fetchone()[0]
            ),
        )

    def test_pinned_profile_survives_an_activation(self) -> None:
        old = resolve_turn_profile(self.profiles, NS)
        review_ref = self.approve_review()
        body = self.profile_body(
            review_ref=review_ref,
            profile_version="0.0.2",
            identity_summary="synthetic second version",
        )
        version = self.profiles.publish_profile(
            namespace=NS,
            profile_id=body["profile_id"],
            body=body,
            review_ref=review_ref,
            authorization_ref="authorization:test",
        )
        self.profiles.activate_profile(
            namespace=NS,
            record_id=version.record_id,
            record_version=version.record_version,
            authorization_ref="authorization:test",
        )
        self.enqueue({"action": "none", "reply": "仍然用旧人格"})
        creation = self.store.register_turn(
            namespace=NS,
            host_instance=HOST,
            conversation_id=self.conversation.conversation_id,
            client_request_id="req-pin",
            text="人格固定",
            privacy_class=PRIVACY,
            retention="standard",
            profile_ref=old,
            strategy={"mode": "chat"},
        )
        execution = self.orchestrator.execute(
            conversation=self.conversation,
            turn=creation.turn,
            input_ref=creation.input_ref,
            profile_ref=old,
            access=self.access,
            retention="standard",
            policy_ref=POLICY_REF,
        )
        self.assertEqual("completed", execution.turn.state)
        context = self.context_of(creation.turn.turn_id)
        self.assertEqual(old, context.profile_ref)
        self.assertEqual(
            old,
            self.store.read_turns(
                namespace=NS,
                host_instance=HOST,
                conversation_id=self.conversation.conversation_id,
            )[-1].turn.profile_ref,
        )

    def test_history_privacy_is_rechecked_before_the_request(self) -> None:
        restricted = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="受限的历史输入",
            privacy_class="restricted",
            retention="standard",
        )
        current = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body="当前输入",
            privacy_class="private",
            retention="standard",
        )
        history = HistoryBundle(
            messages=(
                HistoryMessage(
                    role="user",
                    text="受限的历史输入",
                    ref_kind="collab_input",
                    ref_id=restricted.input_id,
                    ref_version=1,
                    turn_no=1,
                    seq=1,
                ),
            ),
            omissions=(),
            stats={},
        )
        current_ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": current.input_id,
            "content_version": 1,
        }
        context = self.contexts.build_consult_context(
            namespace=NS,
            access=self.access,
            input_refs=[current_ref],
            adapter_kind="direct-http",
            history=history,
        )
        # The current input must be linked to an interaction before the send
        # gate re-reads material sources (the orchestrator does this after the
        # Context and before generation).
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref=POLICY_REF,
            input_refs=[current_ref],
            context_id=context.context_id,
        )
        attempts = int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM model_attempts"
            ).fetchone()[0]
        )
        # `self.chat_model` allows only `private`: the restricted history ref
        # must be refused before any reservation or HTTP try.
        with self.assertRaises(ModelAdapterError):
            self.chat_model._require_collab_send_basis(NS, context)
        self.assertEqual(
            attempts,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM model_attempts"
                ).fetchone()[0]
            ),
        )
        # A policy that allows both classes passes the same gate.
        permissive_policy = ConsultSendPolicy(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({PRIVACY, "restricted"}),
            purposes=frozenset({"consult"}),
            disclose=True,
        )
        permissive = DirectHttpModel(
            self.store,
            self.manifest,
            protocol="collab.v1",
            consult_policy=permissive_policy,
            request_budget_units=200_000,
            output_reserve_units=4096,
        )
        permissive._require_collab_send_basis(NS, context)
        self.assertEqual(
            attempts,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM model_attempts"
                ).fetchone()[0]
            ),
        )

    def test_no_active_profile_raises_before_any_turn(self) -> None:
        with tempfile.TemporaryDirectory(prefix="rg02-no-profile-") as raw:
            store = CognitionStore.open(Path(raw) / "pcs.sqlite")
            try:
                profiles = PersonaProfileService(store)
                with self.assertRaises(PersonaNotReady) as caught:
                    resolve_turn_profile(profiles, NS)
                self.assertEqual("PERSONA_NOT_READY", caught.exception.reason)
                for table in (
                    "collab_turns",
                    "collab_inputs",
                    "collab_interactions",
                    "context_snapshots",
                    "model_attempts",
                ):
                    self.assertEqual(
                        0,
                        int(
                            store.connection.execute(
                                f"SELECT COUNT(*) FROM {table}"
                            ).fetchone()[0]
                        ),
                        table,
                    )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
