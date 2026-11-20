"""Synthetic continuity checks against the PCS collaboration store."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from quintessence_pcs.domains.cognition.questions import QuestionView
from quintessence_pcs.domains.refs import RecordRef
from quintessence_pcs.foundation.errors import CorruptStore, InvalidInput
from quintessence_pcs.foundation.jsonutil import canonical_json_text
from quintessence_pcs.runtime.dreaming_history import DreamingHistory, plan_deep_batch
from quintessence_pcs.store.cognition import CognitionStore
from quintessence_pcs.store.conversation import HistoryMessage
from quintessence_pcs.store.experience import ExperienceAccess, ExperienceUnavailable


NS = "example.test.dreaming"
HOST = "host:synthetic"
ACCESS = "access:synthetic"
INSTANCE = "11001100-0000-4000-8000-000011001100"
NOW = datetime(2026, 8, 12, 14, tzinfo=UTC)


class DreamingHistoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="dreaming-history-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.database = self.root / "legacy.sqlite"
        self.dreaming = self.root / "dreaming"
        self.dreaming.mkdir()
        self.store = CognitionStore.open(self.database)
        self.addCleanup(lambda: self.store.close())
        self.access = ExperienceAccess(
            NS, HOST, ACCESS, frozenset({"private"}), frozenset({"consult", "dreaming"}),
        )
        self.history = self.reopen_history()

    def reopen_history(self):
        return DreamingHistory(self.dreaming, instance_id=INSTANCE, namespace=NS)

    def input(self, body, *, observed=None, recorded=NOW, retention="standard",
              privacy="private", host=HOST, access_ref=ACCESS):
        input_row = self.store.register_input(
            namespace=NS, origin_kind="user_statement", body=body,
            privacy_class=privacy, retention=retention,
            observed_at=None if observed is None else observed.isoformat().replace("+00:00", "Z"),
            recorded_at=recorded.isoformat().replace("+00:00", "Z"),
            input_id=str(uuid4()), content_version=1,
        )
        ref = {"ref_type": "collab_input", "namespace": NS, "input_id": input_row.input_id,
               "content_version": input_row.content_version}
        self.store.register_interaction(
            namespace=NS, mode="consult", activation_kind="user_input",
            host_instance=host, access_ref=access_ref, policy_ref="collab.v1",
            input_refs=[ref], result="answered",
        )
        return ref

    def tool_turn(self, *, retention):
        conversation = self.store.create_conversation(namespace=NS, host_instance=HOST, mode="chat")
        creation = self.store.register_turn(
            namespace=NS, host_instance=HOST, conversation_id=conversation.conversation_id,
            client_request_id="synthetic-tool-test", text="Check the report", privacy_class="private",
            retention=retention, profile_ref=RecordRef(NS, str(uuid4()), 1).to_dict(),
            strategy={"mode": "chat"},
        )
        context_id = self.store.capture_snapshot(NS).context_id
        interaction_id = self.store.register_interaction(
            namespace=NS, mode="consult", activation_kind="user_input",
            host_instance=HOST, access_ref=ACCESS, policy_ref="collab.v1",
            input_refs=[creation.input_ref], context_id=context_id,
        )
        turn_id = creation.turn.turn_id
        stage = self.store.begin_turn(namespace=NS, turn_id=turn_id)
        self.store.bind_stage_context(namespace=NS, stage_id=stage.stage_id, context_id=context_id)
        step = self.store.plan_tool_step(
            namespace=NS, turn_id=turn_id, stage_id=stage.stage_id, step_no=1,
            tool_name="cognition.belief.read", arguments_digest="b" * 64, operation_key="synthetic",
        )
        self.store.start_tool_step(namespace=NS, turn_id=turn_id, step_id=step.step_id)
        result = self.store.finish_tool_step(
            namespace=NS, turn_id=turn_id, step_id=step.step_id,
            interaction_id=interaction_id, context_id=context_id,
            status="succeeded", duration_ms=5, retention=retention, sources=[creation.input_ref],
            body=canonical_json_text({
                "arguments": {"filter": "synthetic argument"}, "result": {"status": "ready"},
            }),
        )
        ref = {"ref_type": "collab_artifact", "namespace": NS, "artifact_id": result.result_artifact_id,
               "artifact_version": result.result_artifact_version}
        return creation.input_ref, ref, (turn_id, stage.stage_id, interaction_id, context_id)

    def finish_turn(self, turn, input_ref, *, retention):
        turn_id, stage_id, interaction_id, context_id = turn
        self.store.publish_assistant_message(
            namespace=NS, turn_id=turn_id, stage_id=stage_id, interaction_id=interaction_id,
            context_id=context_id, text="The report is ready to review", retention=retention,
            sources=[input_ref], attempt_ref=None,
        )

    def delete(self, ref):
        self.store.delete_experience(
            namespace=NS, ref_type=ref["ref_type"], ref_id=ref["input_id"],
            ref_version=ref["content_version"], authorization_ref="authorization:synthetic-delete",
        )

    def test_window_fallback_occurrence_restart_and_no_legacy_writes(self):
        floor = NOW - timedelta(hours=24)
        beginning = self.input("beginning", observed=floor, recorded=NOW)
        fallback = self.input("recorded fallback", recorded=NOW - timedelta(microseconds=1))
        self.input("too old", observed=floor - timedelta(microseconds=1))
        self.input("outside at end", observed=NOW)
        self.input("session only", observed=NOW - timedelta(seconds=1), retention="session")
        self.input("restricted", observed=NOW - timedelta(seconds=2), privacy="restricted")
        self.input("other host", observed=NOW - timedelta(seconds=3), host="host:other")
        self.input("other grant", observed=NOW - timedelta(seconds=4), access_ref="access:other")
        self.store.close()
        original = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.store = CognitionStore.open_read_only(self.database)
        raw = self.history.recent(self.store, self.access, now=NOW)
        self.assertEqual([fallback, beginning], [item.source.ref for item in raw.items])
        self.assertEqual(["recorded_at_fallback", "observed_at"],
                         [item.source.time_basis for item in raw.items])
        self.assertEqual(floor, raw.items[1].source.occurred_at)
        self.assertEqual(2, self.history.advance_recent(self.store, self.access, now=NOW).added)
        self.assertEqual(0, self.reopen_history().advance_recent(self.store, self.access, now=NOW).added)
        self.assertEqual(raw, self.reopen_history().recent(self.store, self.access, now=NOW))
        self.store.close()
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()})
        with closing(sqlite3.connect(self.dreaming / "wake.sqlite3")) as connection:
            self.assertEqual(4, connection.execute("SELECT count(*) FROM dreaming_history_reads").fetchone()[0])
            self.assertEqual(2, connection.execute("SELECT count(*) FROM dreaming_recent_refs").fetchone()[0])
        self.assertNotIn(b"recorded fallback", (self.dreaming / "wake.sqlite3").read_bytes())

    def test_checkpoint_advances_one_page_and_live_privacy_and_deletion_win(self):
        refs = [self.input(f"experience {index}", observed=NOW - timedelta(minutes=index + 1))
                for index in range(25)]
        first = self.history.advance_recent(self.store, self.access, now=NOW)
        self.assertEqual((20, True), (first.added, first.has_more))
        second = self.reopen_history().advance_recent(self.store, self.access, now=NOW)
        self.assertEqual((5, False), (second.added, second.has_more))
        blocked = self.history.recent(self.store, replace(self.access, privacy_classes=frozenset()), now=NOW)
        self.assertEqual((), blocked.items)
        blocked = self.history.recent(self.store, replace(self.access, purposes=frozenset()), now=NOW)
        self.assertEqual((), blocked.items)
        self.delete(refs[0])
        raw = self.history.recent(self.store, self.access, now=NOW, limit=2)
        self.assertEqual(refs[1:3], [item.source.ref for item in raw.items])
        with self.assertRaises(InvalidInput):
            self.store.read_experience_for_dreaming(self.access, ref=refs[1], purpose="consult", audit=None)
        with self.assertRaisesRegex(OSError, "synthetic audit failure"):
            def fail(*_):
                raise OSError("synthetic audit failure")

            self.store.read_experience_for_dreaming(self.access, ref=refs[1], purpose="consult", audit=fail)

    def test_tool_progress_reuses_transcript_excerpt_and_barrier(self):
        user, tool, turn = self.tool_turn(retention="standard")
        self.assertIsNone(self.store.dreaming_experience_ref(self.access, ref=tool))
        self.finish_turn(turn, user, retention="standard")
        session_user, session_tool, session_turn = self.tool_turn(retention="session")
        self.finish_turn(session_turn, session_user, retention="session")
        now = datetime.now(UTC) + timedelta(seconds=1)
        items = self.history.recent(self.store, self.access, now=now).items
        indexed = {canonical_json_text(item.source.ref): item for item in items}
        result = indexed[canonical_json_text(tool)]
        self.assertEqual('{"status":"ready"}', result.excerpt)
        self.assertEqual("artifact_created_at", result.source.time_basis)
        self.assertEqual("deterministic_query", result.origin_kind)
        self.assertNotIn(canonical_json_text(session_tool), indexed)
        self.assertTrue(any(item.excerpt == "The report is ready to review" for item in items))
        self.delete(user)
        self.assertEqual((), self.history.refresh(self.store, self.access, [result.source],
                                                  now=now, purpose="consult"))
        audits = []
        self.assertIsInstance(self.store.read_experience_for_dreaming(
            self.access, ref=tool, purpose="consult", audit=lambda *row: audits.append(row),
        ), ExperienceUnavailable)
        self.assertEqual("unavailable", audits[0][-1])

    def test_deep_checkpoint_moves_only_after_handled_batch(self):
        first = self.input("older report", observed=NOW - timedelta(days=3))
        second = self.input("second report", observed=NOW - timedelta(days=2))
        self.input("recent", observed=NOW - timedelta(hours=1))
        batch = self.history.deep(self.store, self.access, now=NOW, limit=1)
        self.assertEqual([first], [item.source.ref for item in batch.items])
        self.assertTrue(batch.has_more)
        restarted = self.reopen_history()
        self.assertEqual(batch, restarted.deep(self.store, self.access, now=NOW, limit=1))
        self.assertTrue(restarted.complete_deep(self.access, batch))
        self.assertFalse(self.history.complete_deep(self.access, batch))
        batch = self.history.deep(self.store, self.access, now=NOW, limit=1)
        self.assertEqual([second], [item.source.ref for item in batch.items])
        self.assertTrue(self.history.complete_deep(self.access, batch))
        self.assertEqual((), self.history.deep(self.store, self.access, now=NOW).items)

    def test_indirect_history_cannot_reintroduce_deleted_or_session_sources(self):
        def reply_from(history_ref):
            context = self.store.capture_snapshot(NS).context_id
            key, version = ("input_id", "content_version") if history_ref["ref_type"] == "collab_input" else (
                "artifact_id", "artifact_version",
            )
            with self.store.transaction():
                self.store.register_context_history(namespace=NS, context_id=context, messages=[
                    HistoryMessage("user", "synthetic excerpt", history_ref["ref_type"],
                                   history_ref[key], history_ref[version], 1, 1),
                ])
            interaction = self.store.register_interaction(
                namespace=NS, mode="consult", activation_kind="user_input", host_instance=HOST,
                access_ref=ACCESS, policy_ref="collab.v1", context_id=context, result="answered",
            )
            artifact = self.store.register_artifact(
                namespace=NS, kind="reply", interaction_id=interaction, context_id=context,
                origin_kind="model_output", body="Synthetic reply", retention="standard",
            )
            return {"ref_type": "collab_artifact", "namespace": NS, "artifact_id": artifact.artifact_id,
                    "artifact_version": artifact.artifact_version}

        source = self.input("synthetic retained source", observed=NOW - timedelta(seconds=1))
        echo = reply_from(reply_from(source))
        self.assertIsNotNone(self.store.dreaming_experience_ref(self.access, ref=echo))
        self.delete(source)
        self.assertIsNone(self.store.dreaming_experience_ref(self.access, ref=echo))
        session = self.input("synthetic session material", retention="session")
        self.assertIsNone(self.store.dreaming_experience_ref(self.access, ref=reply_from(session)))

    def test_deep_uses_existing_waiting_question_trigger(self):
        view = QuestionView(
            ref=RecordRef(NS, str(uuid4()), 1), status="waiting", question="Compare the reports?",
            origin="user", priority="normal", paused=False, pause_reason=None, waiting_for=None,
            review_after="2026-08-12T13:59:59Z", last_evaluated_at=None,
            last_evaluated_revision=None, scope={}, recorded_at="2026-08-12T13:00:00Z",
            evidence_refs=(), trigger_spec={}, consumed_trigger_refs=(), resolution_refs=(),
            related_question_refs=(),
        )
        batch = self.history.deep(self.store, self.access, views=[view], now=NOW)
        self.assertEqual(view, batch.question)
        self.assertEqual("review_due", batch.trigger.reason)
        self.assertTrue(self.history.complete_deep(self.access, batch))
        self.assertFalse(self.history.complete_deep(self.access, batch))
        self.assertIsNone(self.reopen_history().deep(self.store, self.access, views=[view], now=NOW).question)
        for unavailable in (replace(view, paused=True), replace(view, status="resolved"),
                            replace(view, review_after=None, waiting_for="When it seems relevant")):
            self.assertIsNone(plan_deep_batch([unavailable], (), now=NOW).question)

    def test_deep_prioritizes_exact_manual_and_live_source_revision_then_due(self):
        record_id = str(uuid4())
        for version in (1, 2):
            self.store.append_record_version(
                namespace=NS, record_id=record_id, record_version=version,
                kind="Observation", body={"report_version": version}, status="active",
                schema_version="1", privacy_class="private", recorded_at="2026-08-12T12:00:00Z",
            )
        due = QuestionView(
            ref=RecordRef(NS, str(uuid4()), 1), status="open", question="Compare the reports?",
            origin="user", priority="normal", paused=False, pause_reason=None, waiting_for=None,
            review_after="2026-08-12T14:00:00Z", last_evaluated_at="2026-08-12T13:59:59Z",
            last_evaluated_revision=1, scope={}, recorded_at="2026-08-12T13:00:00Z",
            evidence_refs=(), trigger_spec={}, consumed_trigger_refs=(), resolution_refs=(),
            related_question_refs=(),
        )
        changed = replace(due, ref=RecordRef(NS, str(uuid4()), 1), review_after=None,
                          trigger_spec={"source_refs": [RecordRef(NS, record_id, 1).to_dict()]})
        manual = replace(due, ref=RecordRef(NS, str(uuid4()), 1), review_after=None)
        views = [due, changed, manual]
        foreign = RecordRef("other.test", manual.ref.record_id, 1).to_dict()
        batch = self.history.deep(self.store, self.access, views=views, now=NOW, manual_refs=[foreign])
        self.assertEqual(changed, batch.question)
        self.assertEqual((RecordRef(NS, record_id, 2),), batch.trigger_sources)
        self.assertEqual("source_revision", batch.trigger.reason)
        requested = [manual.ref.to_dict()]
        batch = self.history.deep(self.store, self.access, views=views, now=NOW, manual_refs=requested)
        self.assertEqual(manual, batch.question)
        self.assertEqual("manual_review", batch.trigger.reason)
        self.assertTrue(self.history.complete_deep(self.access, batch))
        batch = self.reopen_history().deep(self.store, self.access, views=views, now=NOW, manual_refs=requested)
        self.assertEqual(changed, batch.question)
        self.assertTrue(self.history.complete_deep(self.access, batch))
        batch = self.history.deep(self.store, self.access, views=views, now=NOW, manual_refs=requested)
        self.assertEqual(due, batch.question)
        self.assertEqual("review_due", batch.trigger.reason)
        self.assertTrue(self.history.complete_deep(self.access, batch))
        self.assertIsNone(self.history.deep(self.store, self.access, views=views, now=NOW, manual_refs=requested).question)

    def test_dreaming_database_collision_preserves_original_bytes(self):
        occupied = self.root / "occupied"
        occupied.mkdir()
        collision = occupied / "wake.sqlite3"
        collision.write_bytes(b"synthetic private file")
        with self.assertRaises(CorruptStore):
            DreamingHistory(occupied, instance_id=INSTANCE, namespace=NS)
        self.assertEqual(b"synthetic private file", collision.read_bytes())


if __name__ == "__main__":
    unittest.main()
