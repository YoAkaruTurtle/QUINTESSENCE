"""RG04 M06: turn-level experience eligibility and the legacy regression.

The negative assertion is that a turn with one published commentary, two tool
results and one final produces only the user input and the final as independent
experience refs (no per-commentary/per-tool mapping).  The positive assertion
is that those two refs really are projectable, and that a legacy 0016 database
keeps exactly the three refs it had before the upgrade.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOMAIN = HERE.parent / "domain"
SRC = HERE.parents[1] / "src"
for _path in (HERE, DOMAIN, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import CognitionTestCase, NS  # noqa: E402

from quintessence_pcs.collaboration.consult import _artifact_sources  # noqa: E402
from quintessence_pcs.domains.memory.experience_bridge import (  # noqa: E402
    BridgeConfig,
    ExperienceBridge,
)
from quintessence_pcs.foundation.migrations import (  # noqa: E402
    default_migrations_dir,
    discover_migrations,
)
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:rg04-projection"
ACCESS_REF = "authorization:rg04-projection"
POLICY_REF = "policy:collab.v1"
PRIVACY = "private"


class FakeAdapter:
    def __init__(self, disposition: str = "inserted"):
        self.calls: list[dict] = []
        self.disposition = disposition

    def capture(self, event, *, actor_origin, timeout_seconds):
        self.calls.append(dict(event))
        return {
            "kind": "ok",
            "receipt": {
                "disposition": self.disposition,
                "durability": "persisted",
                "event_refs": [{"ref": "external-rg04", "revision": 1}],
                "gaps": [],
                "error_code": None,
            },
        }


class TurnProjectionTests(CognitionTestCase):
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

    # ------------------------------------------------------------- helpers

    def _start_turn(self, text: str):
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

    def _commentary(self, creation, context, interaction_id, stage):
        message = self.store.publish_commentary(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text="我翻一下之前的记录。",
            retention="standard",
            sources=_artifact_sources(context),
        )
        return message

    def _tool(self, creation, context, interaction_id, stage, step_no: int):
        step = self.store.plan_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            step_no=step_no,
            tool_name="cognition.belief.read",
            arguments_digest="a" * 64,
            operation_key=f"tool:{step_no}",
        )
        step = self.store.start_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=step.step_id,
        )
        body = json.dumps(
            {
                "tool": "cognition.belief.read",
                "arguments": {},
                "status": "succeeded",
                "result": {"items": []},
            },
            ensure_ascii=False,
        )
        return self.store.finish_tool_step(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            step_id=step.step_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            status="succeeded",
            duration_ms=1,
            body=body,
            sources=_artifact_sources(context),
            retention="standard",
        )

    def _publish_final(self, text: str, reply: str, *, with_noise: bool = True):
        creation, context, interaction_id, stage = self._start_turn(text)
        sources = _artifact_sources(context)
        commentary_id = None
        if with_noise:
            message = self._commentary(creation, context, interaction_id, stage)
            commentary_id = message.artifact_id
            sources = sources + [
                {
                    "ref_type": "collab_artifact",
                    "namespace": NS,
                    "artifact_id": message.artifact_id,
                    "artifact_version": message.artifact_version,
                }
            ]
        self._tool(creation, context, interaction_id, stage, 1)
        self._tool(creation, context, interaction_id, stage, 2)
        turn = self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text=reply,
            retention="standard",
            sources=sources,
            attempt_ref=None,
        )
        final_artifact_id = self.store.connection.execute(
            "SELECT artifact_id FROM collab_turn_messages WHERE turn_id = ? "
            "AND kind = 'final'",
            (turn.turn_id,),
        ).fetchone()[0]
        return creation, context, turn, final_artifact_id, commentary_id

    def _bridge(self):
        return ExperienceBridge(
            self.store, FakeAdapter(), BridgeConfig(scope_id="scope-rg04")
        )

    # --------------------------------------------------------------- tests

    def test_turn_mapping_has_only_user_and_final(self) -> None:
        creation, _context, _turn, final_artifact_id, commentary_id = (
            self._publish_final("第一问", "最终回答")
        )
        refs = self.store.turn_experience_refs(
            self.access, namespace=NS, turn_id=_turn.turn_id
        )
        ref_ids = {
            ref["input_id"] if ref["ref_type"] == "collab_input" else ref["artifact_id"]
            for ref in refs
        }
        self.assertEqual(2, len(refs))
        self.assertEqual(
            {creation.input_ref["input_id"], final_artifact_id}, ref_ids
        )
        self.assertNotIn(commentary_id, ref_ids)

        result = self._bridge().sync_refs(
            self.access, namespace=NS, refs=refs, limit=10
        )
        self.assertEqual(2, result.projected)
        rows = self.store.connection.execute(
            "SELECT ref_type, ref_id FROM experience_source_mapping "
            "ORDER BY ref_type, ref_id"
        ).fetchall()
        mapped = {(row["ref_type"], row["ref_id"]) for row in rows}
        self.assertEqual(
            {
                ("collab_input", creation.input_ref["input_id"]),
                ("collab_artifact", final_artifact_id),
            },
            mapped,
        )
        self.assertNotIn(("collab_artifact", commentary_id), mapped)

    def test_running_turn_is_not_eligible_until_terminal(self) -> None:
        creation, _context, _interaction, _stage = self._start_turn("进行中")
        self.assertEqual(
            (),
            self.store.turn_experience_refs(
                self.access, namespace=NS, turn_id=creation.turn.turn_id
            ),
        )
        page = self.store.list_experience_refs(
            self.access, namespace=NS, limit=100
        )
        ids = {
            ref["input_id"] if ref["ref_type"] == "collab_input" else ref["artifact_id"]
            for ref in page.refs
        }
        self.assertNotIn(creation.input_ref["input_id"], ids)

    def test_failed_and_cancelled_turns_project_user_input_only(self) -> None:
        for terminal in ("failed", "cancelled"):
            creation, _context, interaction_id, stage = self._start_turn(terminal)
            if terminal == "failed":
                self.store.fail_turn(
                    namespace=NS,
                    turn_id=creation.turn.turn_id,
                    reason="synthetic",
                    stage_id=stage.stage_id,
                    interaction_id=interaction_id,
                )
            else:
                self.store.cancel_turn(
                    namespace=NS,
                    turn_id=creation.turn.turn_id,
                    stage_id=stage.stage_id,
                    interaction_id=interaction_id,
                )
            refs = self.store.turn_experience_refs(
                self.access, namespace=NS, turn_id=creation.turn.turn_id
            )
            self.assertEqual(1, len(refs))
            self.assertEqual("collab_input", refs[0]["ref_type"])
            self.assertEqual(creation.input_ref["input_id"], refs[0]["input_id"])

    def test_waiting_turn_projects_wait_question_but_not_commentary(self) -> None:
        creation, context, interaction_id, stage = self._start_turn("等待澄清")
        message = self._commentary(creation, context, interaction_id, stage)
        wait = self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text="你是指哪一个安排？",
            retention="standard",
            sources=_artifact_sources(context)
            + [
                {
                    "ref_type": "collab_artifact",
                    "namespace": NS,
                    "artifact_id": message.artifact_id,
                    "artifact_version": message.artifact_version,
                }
            ],
            attempt_ref=None,
            kind="wait_question",
        )
        wait_artifact_id = self.store.connection.execute(
            "SELECT artifact_id FROM collab_turn_messages WHERE turn_id = ? "
            "AND kind = 'wait_question'",
            (wait.turn_id,),
        ).fetchone()[0]
        refs = self.store.turn_experience_refs(
            self.access, namespace=NS, turn_id=wait.turn_id
        )
        self.assertEqual(
            {creation.input_ref["input_id"], wait_artifact_id},
            {
                ref["input_id"]
                if ref["ref_type"] == "collab_input"
                else ref["artifact_id"]
                for ref in refs
            },
        )
        self.assertNotIn(
            message.artifact_id,
            {ref.get("artifact_id") for ref in refs},
        )

    def test_interrupted_turn_projects_user_input_only(self) -> None:
        creation, _context, _interaction, _stage = self._start_turn("崩溃")
        interrupted = self.store.interrupt_stale_turns(namespace=NS)
        self.assertEqual(1, interrupted)
        refs = self.store.turn_experience_refs(
            self.access, namespace=NS, turn_id=creation.turn.turn_id
        )
        self.assertEqual(1, len(refs))
        self.assertEqual(creation.input_ref["input_id"], refs[0]["input_id"])

    def test_multi_privacy_final_is_a_gap_without_mapping(self) -> None:
        creation, context, interaction_id, stage = self._start_turn("多类")
        restricted = self.store.register_input(
            namespace=NS,
            origin_kind="host_supplied",
            body="synthetic restricted material",
            privacy_class="restricted",
            retention="standard",
        )
        wait = self.store.publish_assistant_message(
            namespace=NS,
            turn_id=creation.turn.turn_id,
            stage_id=stage.stage_id,
            interaction_id=interaction_id,
            context_id=context.context_id,
            text="混合来源回答",
            retention="standard",
            sources=_artifact_sources(context)
            + [
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": restricted.input_id,
                    "content_version": restricted.content_version,
                }
            ],
            attempt_ref=None,
        )
        final_artifact_id = self.store.connection.execute(
            "SELECT artifact_id FROM collab_turn_messages WHERE turn_id = ? "
            "AND kind = 'final'",
            (wait.turn_id,),
        ).fetchone()[0]
        wide = ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset({"private", "restricted"}),
            purposes=frozenset({"memory.bridge"}),
        )
        refs = self.store.turn_experience_refs(
            wide, namespace=NS, turn_id=creation.turn.turn_id
        )
        self.assertEqual(2, len(refs))
        result = self._bridge().sync_refs(wide, namespace=NS, refs=refs, limit=10)
        self.assertEqual(1, result.blocked)
        self.assertTrue(
            any(gap["reason"] == "multi_or_empty_privacy" for gap in result.gaps)
        )
        rows = self.store.connection.execute(
            "SELECT ref_type, ref_id FROM experience_source_mapping"
        ).fetchall()
        self.assertEqual(1, len(rows))  # the user input only
        self.assertNotIn(
            ("collab_artifact", final_artifact_id),
            {(row["ref_type"], row["ref_id"]) for row in rows},
        )


class LegacyProjectionRegressionTests(unittest.TestCase):
    """RG04 M06 legacy half: a 0016 database keeps its exact three refs."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="rg04-legacy-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "pcs.sqlite"
        self.namespace = "example.test.rg04.legacy"
        self.host = "host:rg04-legacy"
        self.access_ref = "authorization:rg04-legacy"

    def _subset_migrations(self, upto: int) -> Path:
        target = self.tmp_path / f"migrations-{upto:04d}"
        target.mkdir()
        for migration in discover_migrations(default_migrations_dir()):
            if migration.version <= upto:
                shutil.copy2(migration.path, target / migration.path.name)
        return target

    def _old_sql_refs(self, store) -> list[tuple]:
        query = (
            "SELECT ref_type, ref_id, ref_version FROM ("
            "SELECT 'collab_input' AS ref_type, input_id AS ref_id, "
            "content_version AS ref_version FROM collab_inputs AS i "
            "WHERE namespace = ? AND EXISTS (SELECT 1 FROM "
            "collab_interaction_inputs AS l WHERE l.namespace = i.namespace "
            "AND l.input_id = i.input_id AND l.content_version = i.content_version) "
            "UNION ALL "
            "SELECT 'collab_artifact' AS ref_type, a.artifact_id AS ref_id, "
            "a.artifact_version AS ref_version FROM collab_artifacts AS a "
            "WHERE a.namespace = ?) "
            "ORDER BY created_at, ref_type, ref_id, ref_version"
        )
        # The old query ordered by created_at but did not select it; the
        # comparison below uses the same ordering columns explicitly.
        rows = store.connection.execute(
            "SELECT ref_type, ref_id, ref_version FROM ("
            "SELECT 'collab_input' AS ref_type, input_id AS ref_id, "
            "content_version AS ref_version, recorded_at AS created_at "
            "FROM collab_inputs AS i WHERE namespace = ? AND EXISTS ("
            "SELECT 1 FROM collab_interaction_inputs AS l "
            "WHERE l.namespace = i.namespace AND l.input_id = i.input_id "
            "AND l.content_version = i.content_version) UNION ALL "
            "SELECT 'collab_artifact', a.artifact_id, a.artifact_version, "
            "a.created_at FROM collab_artifacts AS a WHERE a.namespace = ?) "
            "ORDER BY created_at, ref_type, ref_id, ref_version",
            (self.namespace, self.namespace),
        ).fetchall()
        return [
            (str(row["ref_type"]), str(row["ref_id"]), int(row["ref_version"]))
            for row in rows
        ]

    def test_legacy_3_refs_survive_upgrade_from_0016(self) -> None:
        store = CognitionStore.open(
            self.db_path, migrations_dir=self._subset_migrations(16)
        )
        try:
            self.assertEqual(
                16,
                int(
                    store.connection.execute(
                        "SELECT MAX(migration_id) FROM schema_migrations"
                    ).fetchone()[0]
                ),
            )
            input_row = store.register_input(
                namespace=self.namespace,
                origin_kind="user_statement",
                body="legacy question",
                privacy_class="private",
                retention="standard",
            )
            interaction = store.register_interaction(
                namespace=self.namespace,
                mode="consult",
                activation_kind="user_input",
                host_instance=self.host,
                access_ref=self.access_ref,
                policy_ref=POLICY_REF,
                input_refs=[
                    {
                        "ref_type": "collab_input",
                        "namespace": self.namespace,
                        "input_id": input_row.input_id,
                        "content_version": input_row.content_version,
                    }
                ],
                result="answered",
            )
            context_id = store.capture_snapshot(self.namespace).context_id
            query_result = store.register_artifact(
                namespace=self.namespace,
                kind="query_result",
                interaction_id=interaction,
                context_id=context_id,
                origin_kind="deterministic_query",
                body="legacy query material",
                retention="standard",
            )
            reply = store.register_artifact(
                namespace=self.namespace,
                kind="reply",
                interaction_id=interaction,
                context_id=context_id,
                origin_kind="model_output",
                body="legacy reply",
                retention="standard",
            )
            before = self._old_sql_refs(store)
            self.assertEqual(3, len(before))
        finally:
            store.close()

        upgraded = CognitionStore.open(self.db_path)
        try:
            self.assertEqual(
                17,
                int(
                    upgraded.connection.execute(
                        "SELECT MAX(migration_id) FROM schema_migrations"
                    ).fetchone()[0]
                ),
            )
            access = ExperienceAccess(
                namespace=self.namespace,
                host_instance=self.host,
                access_ref=self.access_ref,
                privacy_classes=frozenset({"private"}),
                purposes=frozenset({"memory.bridge"}),
            )
            page = upgraded.list_experience_refs(access, namespace=self.namespace, limit=100)
            after = [
                (
                    ref["ref_type"],
                    ref["input_id"]
                    if ref["ref_type"] == "collab_input"
                    else ref["artifact_id"],
                    ref["content_version"]
                    if ref["ref_type"] == "collab_input"
                    else ref["artifact_version"],
                )
                for ref in page.refs
            ]
            self.assertEqual(before, after)
            self.assertEqual(
                {
                    ("collab_input", input_row.input_id, 1),
                    ("collab_artifact", query_result.artifact_id, 1),
                    ("collab_artifact", reply.artifact_id, 1),
                },
                set(after),
            )
        finally:
            upgraded.close()


if __name__ == "__main__":
    unittest.main()
