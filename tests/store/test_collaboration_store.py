"""C05b: collaboration storage, registration atomicity and the decision gates.

Every test uses a fresh migrated database in a temporary directory.  The
fixtures are synthetic; no model, network or private content is involved.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import RECORDED_AT, StoreTestCase, append  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    InvalidDeletionTarget,
    InvalidInput,
    RecordNotFound,
    Redacted,
    ScopeViolation,
    SourceRedacted,
)
from quintessence_pcs.foundation.jsonutil import (  # noqa: E402
    canonical_json_text,
    sha256_hex,
)
from quintessence_pcs.foundation.migrations import (  # noqa: E402
    default_migrations_dir,
)
from quintessence_pcs.store.cognition import CognitionStore  # noqa: E402
from quintessence_pcs.store.collaboration import (  # noqa: E402
    Approval,
    Decision,
)
from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.deletion import (  # noqa: E402
    DELETION_CLEANED,
    DELETION_PENDING_CLEANUP,
)
from quintessence_pcs.store.errors import ConsultContextNotCommittable  # noqa: E402

NS = "example.test.collab"
NS_B = "example.test.collab.other"
INPUT_ID = "12121212-1212-4212-8212-121212121212"
INPUT_ID_B = "13131313-1313-4313-8313-131313131313"
RECORD_ID = "22222222-2222-4222-8222-222222222222"
EVENT_ID = "66666666-6666-4666-8666-666666666666"
WORK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
CANDIDATE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
REQUEST_ID = "22223333-4444-4555-8666-777788889999"
RERUN_GRANT_ID = "14141414-1414-4414-8414-141414141414"
COMMIT_GRANT_ID = "15151515-1515-4515-8515-151515151515"
ACCESS_GRANT_ID = "16161616-1616-4616-8616-161616161616"
APPROVAL_REF = "17171717-1717-4717-8717-171717171717"
TS = "2026-10-22T00:00:00Z"
MANIFEST = "sha256:" + sha256_hex("synthetic manifest")

_ARTIFACT_COLUMNS = (
    "namespace",
    "artifact_id",
    "artifact_version",
    "schema_version",
    "kind",
    "interaction_id",
    "context_id",
    "attempt_ref_json",
    "origin_kind",
    "body_payload_ref",
    "content_digest",
    "retention_policy",
    "created_at",
    "proposal_payload_ref",
    "proposal_digest",
    "proposal_version",
    "work_id",
    "work_generation",
    "request_id",
    "command_digest",
    "structural_validation",
    "rerun_authorization_ref",
    "commit_authorization_ref",
    "status",
    "decided_at",
    "decision_ref",
)


class CollaborationTestCase(StoreTestCase):
    store_class = CognitionStore

    # ------------------------------------------------------------- fixtures

    def new_input(self, *, input_id: str = INPUT_ID, version: int = 1, body: str | None = None,
                  retention: str = "standard", namespace: str = NS, **overrides):
        return self.store.register_input(
            namespace=namespace,
            origin_kind="user_statement",
            body=body or f"private input body {version}",
            privacy_class="private",
            retention=retention,
            input_id=input_id,
            content_version=version,
            **overrides,
        )

    def input_ref(self, row, namespace: str = NS) -> dict:
        return {
            "ref_type": "collab_input",
            "namespace": namespace,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }

    def artifact_ref(self, row, namespace: str = NS) -> dict:
        return {
            "ref_type": "collab_artifact",
            "namespace": namespace,
            "artifact_id": row.artifact_id,
            "artifact_version": row.artifact_version,
        }

    def record_ref(self, version: int = 1, *, record_id: str = RECORD_ID,
                   namespace: str = NS) -> dict:
        return {
            "ref_type": "record",
            "namespace": namespace,
            "record_id": record_id,
            "record_version": version,
        }

    def event_ref(self, namespace: str = NS) -> dict:
        return {"ref_type": "event", "namespace": namespace, "event_id": EVENT_ID}

    def make_context(self, *, records=(), events=(), collab=()) -> str:
        snapshot = self.store.capture_snapshot(NS)
        if records or events:
            with self.store.transaction():
                for kind, identifier, version in (*records, *events):
                    self.store.connection.execute(
                        "INSERT INTO context_sources "
                        "(context_id, ref_kind, ref_id, ref_version) VALUES (?, ?, ?, ?)",
                        (snapshot.context_id, kind, identifier, version),
                    )
        if collab:
            with self.store.transaction():
                self.store.register_context_sources(
                    namespace=NS,
                    context_id=snapshot.context_id,
                    sources=list(collab),
                )
        return snapshot.context_id

    def new_interaction(self, *, mode: str = "consult", context_id: str | None = None,
                        input_refs=(), trigger_ref=None, work_id=None,
                        work_generation=None, **overrides) -> str:
        return self.store.register_interaction(
            namespace=NS,
            mode=mode,
            activation_kind="user_input" if mode == "consult" else "manual_review",
            host_instance="host:synthetic-1",
            access_ref="authorization:access:1",
            policy_ref="policy:collab.v1",
            input_refs=input_refs,
            context_id=context_id,
            trigger_ref=trigger_ref,
            work_id=work_id,
            work_generation=work_generation,
            **overrides,
        )

    def new_artifact(self, *, kind: str = "reply", context_id: str | None = None,
                     interaction_id: str | None = None, sources=(),
                     retention: str = "standard", **overrides):
        context = context_id or self.make_context()
        interaction = interaction_id or self.new_interaction(
            context_id=context, trigger_ref=None
        )
        return self.store.register_artifact(
            namespace=NS,
            kind=kind,
            interaction_id=interaction,
            context_id=context,
            origin_kind="model_output",
            body="synthetic artifact body",
            retention=retention,
            sources=list(sources),
            **overrides,
        )

    def new_candidate(self, *, context_id: str | None = None, work_id: str = WORK_ID,
                      generation: int = 1, sources=(), **overrides):
        context = context_id or self.make_context()
        interaction = self.new_interaction(
            mode="review",
            context_id=context,
            trigger_ref=self.event_ref(),
            work_id=work_id,
            work_generation=generation,
        )
        defaults = dict(
            proposal={"operation": "propose_belief", "reason": "synthetic"},
            proposal_version="v1",
            work_id=work_id,
            work_generation=generation,
            request_id=REQUEST_ID,
            artifact_id=CANDIDATE_ID,
        )
        defaults.update(overrides)
        return self.store.register_artifact(
            namespace=NS,
            kind="candidate",
            interaction_id=interaction,
            context_id=context,
            origin_kind="model_output",
            body="raw model output for a candidate",
            retention="standard",
            sources=list(sources),
            **defaults,
        )

    def new_event(self):
        return self.store.ingest_event(
            namespace=NS,
            source_kind="synthetic_fixture",
            source_instance="collab-instance",
            source_event_id=f"source-{uuid.uuid4()}",
            event_type="ObservationRecorded",
            observed_at=None,
            payload={"text": "synthetic event"},
            privacy_class="private",
            causation_refs=(),
            source_position=None,
            recorded_at=RECORDED_AT,
        )

    def grant_rerun(self, *, generation: int = 1) -> dict:
        return {
            "grant_id": RERUN_GRANT_ID,
            "namespace": NS,
            "schema_version": "collab.v1",
            "kind": "rerun",
            "grant_version": 1,
            "subject": "host:synthetic-1",
            "authorization_ref": "authorization:rerun:1",
            "issued_at": TS,
            "expires_at": None,
            "revoked_at": None,
            "work_id": WORK_ID,
            "generation": generation,
        }

    def grant_commit_policy(self) -> dict:
        return {
            "grant_id": COMMIT_GRANT_ID,
            "namespace": NS,
            "schema_version": "collab.v1",
            "kind": "commit",
            "grant_version": 1,
            "subject": "host:synthetic-1",
            "authorization_ref": "authorization:commit:1",
            "issued_at": TS,
            "expires_at": None,
            "revoked_at": None,
            "scope": "policy",
            "operations": ["propose_belief"],
            "trigger_kinds": ["manual_review"],
        }

    def grant_access(self) -> dict:
        return {
            "grant_id": ACCESS_GRANT_ID,
            "namespace": NS,
            "schema_version": "collab.v1",
            "kind": "access",
            "grant_version": 1,
            "subject": "host:synthetic-1",
            "authorization_ref": "authorization:access:1",
            "issued_at": TS,
            "expires_at": None,
            "revoked_at": None,
            "purposes": ["consult"],
            "data_classes": ["private"],
        }

    def grant_egress(self) -> dict:
        return {
            "grant_id": "18181818-1818-4818-8818-181818181818",
            "namespace": NS,
            "schema_version": "collab.v1",
            "kind": "egress",
            "grant_version": 1,
            "subject": "host:synthetic-1",
            "authorization_ref": "authorization:egress:1",
            "issued_at": TS,
            "expires_at": None,
            "revoked_at": None,
            "provider": "synthetic-provider",
            "endpoint": "https://model.invalid/v1",
            "model": "synthetic-model",
            "manifest_hash": MANIFEST,
            "privacy_classes": ["private"],
            "purposes": ["consult"],
            "model_authorization_ref": "19191919-1919-4919-8919-191919191919",
        }

    def control(self, *, generation: int = 0, blocked: bool = False,
                rerun_ref: str | None = None, control_version: int = 1):
        return self.store.register_work_control(
            namespace=NS,
            work_id=WORK_ID,
            generation=generation,
            blocked_after_rejection=blocked,
            control_version=control_version,
            rerun_authorization_ref=rerun_ref,
            block_reason="rejected" if blocked else None,
            updated_at=TS,
        )

    # --------------------------------------------------------------- probes

    def payload_ref(self, table: str, column: str, **where) -> str:
        clause = " AND ".join(f"{name} = ?" for name in where)
        row = self.store.connection.execute(
            f"SELECT {column} FROM {table} WHERE {clause}",
            tuple(where.values()),
        ).fetchone()
        self.assertIsNotNone(row)
        return str(row[0])

    def payload_length(self, payload_ref: str) -> int:
        row = self.store.connection.execute(
            "SELECT length(body_json) FROM private_payloads WHERE payload_ref = ?",
            (payload_ref,),
        ).fetchone()
        self.assertIsNotNone(row)
        return int(row[0])

    def table_columns(self, table: str) -> list[str]:
        return [
            row[1]
            for row in self.store.connection.execute(f'PRAGMA table_info("{table}")')
        ]

    def insert_artifact_row(self, *, kind: str, context_id: str, payload_ref: str,
                            digest: str, **overrides) -> None:
        values = {
            "namespace": NS,
            "artifact_id": str(uuid.uuid4()),
            "artifact_version": 1,
            "schema_version": "collab.v1",
            "kind": kind,
            "interaction_id": str(uuid.uuid4()),
            "context_id": context_id,
            "attempt_ref_json": None,
            "origin_kind": "model_output",
            "body_payload_ref": payload_ref,
            "content_digest": digest,
            "retention_policy": "standard",
            "created_at": TS,
            "proposal_payload_ref": None,
            "proposal_digest": None,
            "proposal_version": None,
            "work_id": None,
            "work_generation": None,
            "request_id": None,
            "command_digest": None,
            "structural_validation": None,
            "rerun_authorization_ref": None,
            "commit_authorization_ref": None,
            "status": None,
            "decided_at": None,
            "decision_ref": None,
        }
        values.update(overrides)
        self.store.connection.execute(
            "INSERT INTO collab_artifacts ("
            + ", ".join(_ARTIFACT_COLUMNS)
            + ") VALUES ("
            + ", ".join("?" for _ in _ARTIFACT_COLUMNS)
            + ")",
            tuple(values[column] for column in _ARTIFACT_COLUMNS),
        )

    def copy_migrations_without_0008(self) -> Path:
        import shutil

        target = self.tmp_path / "migrations-0007"
        shutil.copytree(default_migrations_dir(), target)
        (target / "0008_collaboration.sql").unlink()
        return target


class MigrationTests(CollaborationTestCase):
    def test_migration_0008_on_empty_database(self) -> None:
        ledger = [
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT migration_id, name FROM schema_migrations ORDER BY migration_id"
            )
        ]
        self.assertEqual(8, len(ledger))
        self.assertEqual(("0008", "collaboration"), (f"{ledger[-1][0]:04d}", ledger[-1][1]))
        tables = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(
            {
                "collab_interactions",
                "collab_inputs",
                "collab_artifacts",
                "collab_work_control",
                "collab_source_links",
                "collab_grants",
                "collab_interaction_inputs",
            }.issubset(tables)
        )
        self.assertIn("target_version", self.table_columns("deletion_targets"))
        self.assertIn("context_kind", self.table_columns("context_snapshots"))
        default = next(
            row[4]
            for row in self.store.connection.execute(
                "PRAGMA table_info(context_snapshots)"
            )
            if row[1] == "context_kind"
        )
        self.assertEqual("'review'", default)
        triggers = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        self.assertTrue(
            {
                "context_snapshots_kind_insert",
                "context_snapshots_kind_update",
                "collab_artifacts_update_guard",
                "collab_grants_revoke_only",
                "collab_inputs_no_update",
                "collab_source_links_no_update",
            }.issubset(triggers)
        )

    def _seed_0007_database(self) -> tuple[Path, str]:
        migrations = self.copy_migrations_without_0008()
        old_path = self.tmp_path / "old-0007.sqlite"
        old = ControlStore.open(old_path, migrations_dir=migrations)
        try:
            append(old, version=1, body={"text": "old body"})
            old.capture_snapshot(NS)
            old.connection.execute(
                "INSERT INTO deletion_targets ("
                "target_kind, namespace, target_id, status, authorization_ref, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    "record",
                    NS,
                    RECORD_ID,
                    DELETION_PENDING_CLEANUP,
                    "auth:old",
                    TS,
                    TS,
                ),
            )
        finally:
            old.close()
        return old_path, "auth:old"

    def test_migration_0008_upgrades_a_0007_database(self) -> None:
        old_path, authorization = self._seed_0007_database()
        upgraded = CognitionStore.open(old_path)
        self.addCleanup(upgraded.close)
        ledger = [
            tuple(row)
            for row in upgraded.connection.execute(
                "SELECT migration_id, name FROM schema_migrations ORDER BY migration_id"
            )
        ]
        self.assertEqual(8, len(ledger))
        target = upgraded.connection.execute(
            "SELECT target_kind, target_id, target_version, status, authorization_ref "
            "FROM deletion_targets"
        ).fetchone()
        self.assertEqual(
            ("record", RECORD_ID, 0, DELETION_PENDING_CLEANUP, authorization),
            tuple(target),
        )
        kind = upgraded.connection.execute(
            "SELECT context_kind FROM context_snapshots"
        ).fetchone()[0]
        self.assertEqual("review", kind)

    def test_deletion_targets_rebuild_preserves_rows_and_status(self) -> None:
        migrations = self.copy_migrations_without_0008()
        old_path = self.tmp_path / "old-status.sqlite"
        old = ControlStore.open(old_path, migrations_dir=migrations)
        try:
            append(old, version=1, body={"text": "cleaned body"})
            append(
                old,
                version=1,
                body={"text": "pending body"},
                record_id="33333333-3333-4333-8333-333333333333",
            )
            for target_id, status, authorization in (
                (RECORD_ID, DELETION_CLEANED, "auth:cleaned"),
                (
                    "33333333-3333-4333-8333-333333333333",
                    DELETION_PENDING_CLEANUP,
                    "auth:pending",
                ),
            ):
                old.connection.execute(
                    "INSERT INTO deletion_targets ("
                    "target_kind, namespace, target_id, status, authorization_ref, "
                    "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("record", NS, target_id, status, authorization, TS, TS),
                )
        finally:
            old.close()
        upgraded = CognitionStore.open(old_path)
        self.addCleanup(upgraded.close)
        rows = {
            tuple(row)[1]: tuple(row)
            for row in upgraded.connection.execute(
                "SELECT target_kind, target_id, target_version, status, authorization_ref "
                "FROM deletion_targets ORDER BY target_id"
            )
        }
        cleaned = rows[RECORD_ID]
        pending = rows["33333333-3333-4333-8333-333333333333"]
        self.assertEqual(0, cleaned[2])
        self.assertEqual(DELETION_CLEANED, cleaned[3])
        self.assertEqual("auth:cleaned", cleaned[4])
        self.assertEqual(0, pending[2])
        self.assertEqual(DELETION_PENDING_CLEANUP, pending[3])
        self.assertEqual("auth:pending", pending[4])


class InteractionTests(CollaborationTestCase):
    def test_interaction_persists_input_refs_with_exact_versions(self) -> None:
        first = self.new_input(version=1)
        second = self.new_input(version=2, body="second version")
        other = self.new_input(input_id=INPUT_ID_B)
        context = self.make_context()
        interaction_id = self.new_interaction(
            context_id=context,
            input_refs=[
                (first.input_id, 2),
                (other.input_id, 1),
            ],
        )
        rows = [
            tuple(row)
            for row in self.store.connection.execute(
                "SELECT input_id, content_version, position "
                "FROM collab_interaction_inputs WHERE interaction_id = ? "
                "ORDER BY position",
                (interaction_id,),
            )
        ]
        self.assertEqual(
            [(first.input_id, 2, 0), (other.input_id, 1, 1)], rows
        )
        self.assertEqual(2, second.content_version)
        self.assertEqual(1, first.content_version)

    def test_interaction_review_requires_trigger_and_work(self) -> None:
        context = self.make_context()
        with self.assertRaises(InvalidInput):
            self.new_interaction(mode="review", context_id=context)
        with self.assertRaises(InvalidInput):
            self.new_interaction(
                mode="review",
                context_id=context,
                trigger_ref=self.event_ref(),
            )
        with self.assertRaises(InvalidInput):
            self.new_interaction(
                mode="review",
                context_id=context,
                trigger_ref=self.event_ref(),
                work_id=WORK_ID,
            )
        with self.assertRaises(InvalidInput):
            self.new_interaction(mode="consult", context_id=context, trigger_ref=self.event_ref())
        interaction = self.new_interaction(
            mode="review",
            context_id=context,
            trigger_ref=self.event_ref(),
            work_id=WORK_ID,
            work_generation=0,
        )
        row = self.store.connection.execute(
            "SELECT mode, trigger_ref_json, work_id, work_generation "
            "FROM collab_interactions WHERE interaction_id = ?",
            (interaction,),
        ).fetchone()
        self.assertEqual("review", row[0])
        self.assertIsNotNone(row[1])
        self.assertEqual(WORK_ID, row[2])
        self.assertEqual(0, int(row[3]))


class InputTests(CollaborationTestCase):
    def test_input_is_immutable_and_retention_is_closed(self) -> None:
        row = self.new_input()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE collab_inputs SET body_payload_ref = 'payload:other' "
                "WHERE input_id = ?",
                (row.input_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM collab_inputs WHERE input_id = ?", (row.input_id,)
            )
        with self.assertRaises(InvalidInput):
            self.new_input(input_id=INPUT_ID_B, retention="forever")
        durable = self.new_input(input_id=INPUT_ID_B, retention="durable")
        self.assertEqual("durable", durable.retention)

    def test_register_and_load_input_roundtrip(self) -> None:
        first = self.new_input(observed_at=None)
        second = self.new_input(
            version=2,
            body="revised private body",
            observed_at="2026-10-22T01:02:03Z",
            supersedes=self.input_ref(first),
        )
        loaded = self.store.load_input(
            namespace=NS, input_id=second.input_id, content_version=2
        )
        self.assertEqual("revised private body", loaded.body)
        self.assertEqual(second.content_digest, loaded.content_digest)
        self.assertEqual("standard", loaded.retention)
        self.assertEqual("2026-10-22T01:02:03Z", loaded.observed_at)
        self.assertEqual(self.input_ref(first), loaded.supersedes)
        with self.assertRaises(RecordNotFound):
            self.store.load_input(
                namespace=NS, input_id=INPUT_ID_B, content_version=3
            )


class ArtifactTests(CollaborationTestCase):
    def test_candidate_columns_are_required_only_for_candidates(self) -> None:
        reply = self.new_artifact()
        payload = self.payload_ref(
            "collab_artifacts",
            "body_payload_ref",
            artifact_id=reply.artifact_id,
        )
        digest = reply.content_digest
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_artifact_row(
                kind="reply",
                context_id=reply.context_id,
                payload_ref=payload,
                digest=digest,
                proposal_payload_ref=payload,
                proposal_digest=digest,
                proposal_version="v1",
                work_id=WORK_ID,
                work_generation=0,
                request_id=REQUEST_ID,
                command_digest="a" * 64,
                structural_validation="passed",
                status="pending",
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_artifact_row(
                kind="candidate",
                context_id=reply.context_id,
                payload_ref=payload,
                digest=digest,
                proposal_payload_ref=payload,
                proposal_digest=digest,
                proposal_version="v1",
                work_id=WORK_ID,
                work_generation=0,
                request_id=REQUEST_ID,
                command_digest="a" * 64,
                structural_validation="passed",
                status=None,
            )
        with self.assertRaises(InvalidInput):
            self.new_artifact(proposal={"operation": "propose_belief"})

    def test_candidate_has_no_plaintext_proposal_column(self) -> None:
        columns = self.table_columns("collab_artifacts")
        self.assertIn("proposal_payload_ref", columns)
        for forbidden in ("proposal_json", "body", "body_json", "proposal"):
            self.assertNotIn(forbidden, columns)
        for table in (
            "collab_interactions",
            "collab_inputs",
            "collab_artifacts",
            "collab_work_control",
            "collab_source_links",
            "collab_grants",
            "collab_interaction_inputs",
        ):
            for column in self.table_columns(table):
                self.assertNotIn(
                    column.lower(),
                    {"body", "body_json", "proposal_json", "text", "content"},
                )

    def test_candidate_status_is_forward_only(self) -> None:
        candidate = self.new_candidate()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE collab_artifacts SET status = 'pending' WHERE artifact_id = ?",
                (candidate.artifact_id,),
            )
        self.store.connection.execute(
            "UPDATE collab_artifacts SET status = 'rejected', "
            "decided_at = ?, decision_ref = 'decision:1' WHERE artifact_id = ?",
            (TS, candidate.artifact_id),
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE collab_artifacts SET decision_ref = 'decision:2' "
                "WHERE artifact_id = ?",
                (candidate.artifact_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE collab_artifacts SET body_payload_ref = 'payload:other' "
                "WHERE artifact_id = ?",
                (candidate.artifact_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM collab_artifacts WHERE artifact_id = ?",
                (candidate.artifact_id,),
            )

    def test_artifact_retention_cannot_outlive_sources(self) -> None:
        session = self.new_input(input_id=INPUT_ID, retention="session")
        with self.assertRaises(InvalidInput):
            self.new_artifact(sources=[self.input_ref(session)], retention="standard")
        same = self.new_artifact(
            sources=[self.input_ref(session)], retention="session"
        )
        self.assertEqual("session", same.retention)
        standard = self.new_input(input_id=INPUT_ID_B, retention="standard")
        with self.assertRaises(InvalidInput):
            self.new_artifact(
                sources=[self.input_ref(session), self.input_ref(standard)],
                retention="durable",
            )

    def test_source_links_are_unique_immutable_and_namespace_bound(self) -> None:
        source = self.new_input()
        artifact = self.new_artifact(sources=[self.input_ref(source)])
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "INSERT INTO collab_source_links "
                "(namespace, owner_kind, owner_id, owner_version, "
                " source_kind, source_id, source_version, created_at) "
                "VALUES (?, 'artifact', ?, 1, 'collab_input', ?, 1, ?)",
                (NS, artifact.artifact_id, source.input_id, TS),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE collab_source_links SET source_version = 2 "
                "WHERE owner_id = ?",
                (artifact.artifact_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM collab_source_links WHERE owner_id = ?",
                (artifact.artifact_id,),
            )
        other = self.new_input(input_id=INPUT_ID_B, namespace=NS)
        crossing = dict(self.input_ref(other), namespace=NS_B)
        with self.assertRaises(ScopeViolation):
            self.new_artifact(sources=[crossing])

    def test_register_and_load_candidate_roundtrip(self) -> None:
        source = self.new_input()
        proposal = {"operation": "propose_belief", "reason": "roundtrip"}
        candidate = self.new_candidate(
            sources=[self.input_ref(source)],
            proposal=proposal,
            rerun_authorization_ref=RERUN_GRANT_ID,
        )
        loaded = self.store.load_artifact(
            namespace=NS, artifact_id=candidate.artifact_id, artifact_version=1
        )
        self.assertEqual("candidate", loaded.kind)
        self.assertEqual("pending", loaded.status)
        self.assertEqual(proposal, loaded.proposal)
        self.assertEqual("standard", loaded.retention)
        self.assertEqual(RERUN_GRANT_ID, loaded.rerun_authorization_ref)
        self.assertIsNone(loaded.commit_authorization_ref)
        self.assertEqual((self.input_ref(source),), loaded.source_refs)
        expected = sha256_hex(
            canonical_json_text(
                {
                    "context_id": loaded.context_id,
                    "namespace": NS,
                    "operation": "propose_belief",
                    "request_payload": proposal,
                }
            )
        )
        self.assertEqual(expected, loaded.command_digest)
        self.assertEqual(
            "sha256:" + sha256_hex(canonical_json_text(proposal)),
            loaded.proposal_digest,
        )

    def test_source_registration_failure_leaves_no_readable_artifact(self) -> None:
        context = self.make_context()
        missing = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": "99999999-9999-4999-8999-999999999999",
            "content_version": 1,
        }
        with self.assertRaises(RecordNotFound):
            self.store.register_artifact(
                namespace=NS,
                kind="reply",
                interaction_id=self.new_interaction(context_id=context),
                context_id=context,
                origin_kind="model_output",
                body="must not survive",
                retention="standard",
                sources=[missing],
                artifact_id="77777777-7777-4777-8777-777777777777",
            )
        count = self.store.connection.execute(
            "SELECT COUNT(*) FROM collab_artifacts WHERE artifact_id = ?",
            ("77777777-7777-4777-8777-777777777777",),
        ).fetchone()[0]
        self.assertEqual(0, count)
        with self.assertRaises(RecordNotFound):
            self.store.load_artifact(
                namespace=NS,
                artifact_id="77777777-7777-4777-8777-777777777777",
                artifact_version=1,
            )

    def test_source_must_exist_at_the_exact_version(self) -> None:
        first = self.new_input(version=1)
        self.new_input(version=2, body="second")
        with self.assertRaises(RecordNotFound):
            self.new_artifact(
                sources=[
                    {
                        "ref_type": "collab_input",
                        "namespace": NS,
                        "input_id": first.input_id,
                        "content_version": 3,
                    }
                ]
            )
        artifact = self.new_artifact(
            sources=[
                {
                    "ref_type": "collab_input",
                    "namespace": NS,
                    "input_id": first.input_id,
                    "content_version": 2,
                }
            ]
        )
        self.assertEqual(2, artifact.source_refs[0]["content_version"])

    def test_barred_source_cannot_be_registered(self) -> None:
        source = self.new_input()
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=source.input_id,
            ref_version=1,
            authorization_ref="auth:barrier",
        )
        with self.assertRaises(SourceRedacted):
            self.new_artifact(sources=[self.input_ref(source)])

    def test_artifact_sources_must_cover_the_context(self) -> None:
        append(self.store, version=1, body={"text": "context record"}, namespace=NS)
        source = self.new_input()
        context = self.make_context(
            records=[("record", RECORD_ID, 1)],
            collab=[self.input_ref(source)],
        )
        with self.assertRaises(InvalidInput):
            self.store.register_artifact(
                namespace=NS,
                kind="reply",
                interaction_id=self.new_interaction(context_id=context),
                context_id=context,
                origin_kind="model_output",
                body="partial coverage",
                retention="standard",
                sources=[self.record_ref(1)],
            )
        artifact = self.new_artifact(
            context_id=context,
            sources=[self.record_ref(1), self.input_ref(source)],
        )
        self.assertEqual(2, len(artifact.source_refs))


class GrantTests(CollaborationTestCase):
    def test_grant_branches_are_mutually_exclusive(self) -> None:
        branches = [
            self.grant_access(),
            self.grant_egress(),
            self.grant_commit_policy(),
            dict(
                self.grant_commit_policy(),
                grant_id="20202020-2020-4020-8020-202020202020",
                scope="work_generation",
                work_id=WORK_ID,
                generation=0,
            ),
            self.grant_rerun(),
        ]
        for index, branch in enumerate(branches):
            if index == 3:
                branch.pop("operations")
                branch.pop("trigger_kinds")
            loaded = self.store.register_grant(branch)
            self.assertEqual(branch["kind"], loaded.kind)
        mixed = dict(self.grant_access(), provider="synthetic-provider")
        with self.assertRaises(InvalidInput):
            self.store.register_grant(mixed)
        with self.assertRaises(InvalidInput):
            self.store.register_grant(dict(self.grant_rerun(), scope="policy"))
        with self.assertRaises(InvalidInput):
            self.store.register_grant(
                dict(self.grant_commit_policy(), scope=None)
            )

    def test_grant_revocation_only_touches_revoked_at(self) -> None:
        grant = self.store.register_grant(self.grant_access())
        revoked = self.store.revoke_grant(
            namespace=NS, grant_id=grant.grant_id, revoked_at="2026-10-23T00:00:00Z"
        )
        self.assertEqual("2026-10-23T00:00:00Z", revoked.revoked_at)
        self.assertEqual(grant.grant_id, revoked.grant_id)
        self.assertEqual(grant.kind, revoked.kind)
        self.assertEqual(grant.authorization_ref, revoked.authorization_ref)
        self.assertEqual(grant.purposes, revoked.purposes)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE collab_grants SET provider = 'other' WHERE grant_id = ?",
                (grant.grant_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM collab_grants WHERE grant_id = ?", (grant.grant_id,)
            )


class DecisionTests(CollaborationTestCase):
    def test_review_start_decision_denied_without_rerun_grant(self) -> None:
        self.control(generation=1, blocked=True)
        decision = self.store.review_start_decision(namespace=NS, work_id=WORK_ID)
        self.assertFalse(decision.allowed)
        self.assertEqual("blocked_after_rejection", decision.reason)
        self.assertEqual(1, decision.generation)

    def test_review_start_decision_allowed_with_matching_rerun_grant(self) -> None:
        grant = self.store.register_grant(self.grant_rerun(generation=1))
        self.control(generation=1, blocked=True, rerun_ref=grant.grant_id)
        decision = self.store.review_start_decision(namespace=NS, work_id=WORK_ID)
        self.assertTrue(decision.allowed)
        self.assertEqual("rerun_grant", decision.reason)

    def test_commit_decision_rejects_a_rerun_grant(self) -> None:
        grant = self.store.register_grant(self.grant_rerun(generation=1))
        self.control(generation=1, blocked=True, rerun_ref=grant.grant_id)
        candidate = self.new_candidate(
            commit_authorization_ref=grant.grant_id,
        )
        decision = self.store.commit_decision(
            namespace=NS,
            candidate_id=candidate.artifact_id,
            operation="propose_belief",
            trigger_kind="manual_review",
        )
        self.assertFalse(decision.allowed)
        self.assertEqual("no_commit_authority", decision.reason)

    def test_commit_decision_accepts_exact_manual_approval(self) -> None:
        self.control(generation=1)
        candidate = self.new_candidate()
        approval = Approval(
            candidate_id=candidate.artifact_id,
            proposal_digest=candidate.proposal_digest,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
        )
        allowed = self.store.commit_decision(
            namespace=NS, candidate_id=candidate.artifact_id, approval=approval
        )
        self.assertTrue(allowed.allowed)
        self.assertEqual("manual_approval", allowed.reason)
        wrong = Approval(
            candidate_id=candidate.artifact_id,
            proposal_digest="sha256:" + "0" * 64,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
        )
        denied = self.store.commit_decision(
            namespace=NS, candidate_id=candidate.artifact_id, approval=wrong
        )
        self.assertFalse(denied.allowed)

    def test_decision_core_runs_inside_an_open_transaction(self) -> None:
        self.control(generation=1)
        candidate = self.new_candidate()
        approval = Approval(
            candidate_id=candidate.artifact_id,
            proposal_digest=candidate.proposal_digest,
            context_id=candidate.context_id,
            request_id=candidate.request_id,
        )
        with self.store.transaction():
            depth = self.store._tx_depth  # noqa: SLF001 - deliberate invariant
            decision = self.store.commit_decision(
                namespace=NS,
                candidate_id=candidate.artifact_id,
                approval=approval,
            )
            self.assertEqual(depth, self.store._tx_depth)  # noqa: SLF001
        self.assertTrue(decision.allowed)
        control = self.store.work_control(namespace=NS, work_id=WORK_ID)
        core = self.store._commit_decision(  # noqa: SLF001
            candidate, control, None, approval, "propose_belief", "manual_review"
        )
        self.assertTrue(core.allowed)
        self.assertEqual("manual_approval", core.reason)


class ContextCommitTests(CollaborationTestCase):
    def test_consult_context_commit_is_rejected_with_invalid_transition(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        self.store.connection.execute(
            "UPDATE context_snapshots SET context_kind = 'consult' "
            "WHERE context_id = ?",
            (snapshot.context_id,),
        )
        with self.assertRaises(ConsultContextNotCommittable) as caught:
            self.store.commit_command(
                context_id=snapshot.context_id,
                request_id=REQUEST_ID,
                operation="propose_belief",
                request_payload={"operation": "propose_belief"},
                apply=lambda scope: None,
            )
        self.assertEqual("INVALID_TRANSITION", caught.exception.code)

    def test_public_request_deletion_still_rejects_collab_kinds(self) -> None:
        source = self.new_input()
        for kind in ("collab_input", "collab_artifact"):
            with self.assertRaises(InvalidDeletionTarget):
                self.store.request_deletion(
                    target_kind=kind,
                    namespace=NS,
                    target_id=source.input_id,
                    authorization_ref="auth:public",
                )


if __name__ == "__main__":
    unittest.main()
