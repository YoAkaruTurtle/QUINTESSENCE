"""J06 S4: cognition governance tables, append-only triggers and Tx B seams.

Every test builds its own temporary database.  The two migration tests exercise
both supported paths: a brand new database that migrates straight to 0013, and
an existing 0012 database that is upgraded in place.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import RECORDED_AT, StoreTestCase  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    CommandConflict,
    InvalidInput,
    MigrationError,
    ScopeViolation,
)
from quintessence_pcs.foundation.migrations import (  # noqa: E402
    apply_migrations,
    default_migrations_dir,
)
from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.tx_b import OUTCOME_NOOP  # noqa: E402

MIGRATIONS_DIR = default_migrations_dir()
NS = "example.test.governance"
RECORD_ID = "77777777-7777-4777-8777-777777777777"
DECISION_OPERATION = "cognition.decision"

#: Frozen byte hashes from the J04 closure.  0011/0012 must never change again.
MIGRATION_0011_SHA256 = "45e2c75763cc781ed5760deeec28eff2543005027f45ba226f2ec0852c5c331b"
MIGRATION_0012_SHA256 = "443ce926eaf0367c8ca345801dc4e5509936f1b455b72dd83735f41628594ef6"

GOVERNANCE_TABLES = (
    "cognition_commit_authorizations",
    "cognition_record_decisions",
    "cognition_review_decisions",
    "cognition_subject_authority",
)
GOVERNANCE_TRIGGERS = (
    "cognition_review_decisions_no_delete",
    "cognition_review_decisions_no_update",
)


def new_id() -> str:
    return str(uuid.uuid4())


def decision_fields(**overrides: object) -> dict[str, object]:
    """One valid awaiting_human decision row; overrides replace single fields."""

    fields: dict[str, object] = {
        "decision_id": new_id(),
        "decision_seq": 1,
        "candidate_id": new_id(),
        "artifact_version": 1,
        "decision": "awaiting_human",
        "reason_code": "impact_requires_human",
        "impact": "E2",
        "derived_status": "supported",
        "support_basis": "evidence_backed",
        "requires_human": 1,
        "evidence_linkage": "verified",
        "policy_id": "policy:cognition.review.base.v2",
        "policy_version": "1",
        "impact_policy_id": "policy:cognition.risk.v1",
        "impact_policy_version": "1",
        "authority_kind": "none",
        "change_digest": "sha256:" + "a" * 64,
        "decision_request_id": new_id(),
        "decided_at": RECORDED_AT,
    }
    fields.update(overrides)
    return fields


def copy_migrations_upto(limit: int, target: Path) -> Path:
    """Materialise a migrations directory containing versions 1..limit."""

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if int(path.name[:4]) <= limit:
            shutil.copy2(path, target / path.name)
    return target


#: The migration files whose bytes may never change again.
FROZEN_MIGRATION_SHA256 = {
    "0011_memory_delivery.sql": "45e2c75763cc781ed5760deeec28eff2543005027f45ba226f2ec0852c5c331b",
    "0012_bridge_gaps.sql": "443ce926eaf0367c8ca345801dc4e5509936f1b455b72dd83735f41628594ef6",
    "0013_cognition_governance.sql": "91d472cf796b48970020668d59f1c121d018bc5271d401aa43916d93058550f6",
}

DIGEST = "sha256:" + "0" * 64
TS = "2026-10-26T00:00:00Z"


def insert_artifact(
    connection: object,
    *,
    namespace: str = NS,
    artifact_id: str | None = None,
    kind: str = "reply",
    payload_ref: str = "payload-artifact",
    work_id: str | None = None,
    work_generation: int | None = None,
    with_proposal: bool = False,
) -> str:
    """Insert one artifact row directly, bypassing the store API."""

    identifier = artifact_id or new_id()
    columns = [
        "namespace", "artifact_id", "artifact_version", "schema_version",
        "kind", "interaction_id", "context_id", "origin_kind",
        "body_payload_ref", "content_digest", "retention_policy", "created_at",
    ]
    values: list[object] = [
        namespace, identifier, 1, "collab.v1", kind, new_id(), new_id(),
        "model_output", payload_ref, DIGEST, "standard", TS,
    ]
    if kind == "candidate" or with_proposal:
        columns += [
            "proposal_payload_ref", "proposal_digest", "proposal_version",
            "request_id", "command_digest", "structural_validation", "status",
            "work_id", "work_generation",
        ]
        values += [
            payload_ref, DIGEST, "cognition.change.v1", new_id(), "0" * 64,
            "passed", "pending", work_id, work_generation,
        ]
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(  # type: ignore[attr-defined]
        "INSERT INTO collab_artifacts (" + ", ".join(columns) + ") VALUES ("
        + placeholders + ")",
        values,
    )
    return identifier


def insert_payload(connection: object, payload_ref: str, body: str = "{}") -> None:
    connection.execute(  # type: ignore[attr-defined]
        "INSERT INTO private_payloads (payload_ref, sha256, body_json, created_at) "
        "VALUES (?, ?, ?, ?)",
        (payload_ref, "a" * 64, body, TS),
    )


def insert_decision(connection: object, **overrides: object) -> None:
    fields = decision_fields(**overrides)
    columns = ["namespace"] + list(fields)
    values = [NS] + [fields[name] for name in fields]
    placeholders = ", ".join("?" for _ in columns)
    connection.execute(  # type: ignore[attr-defined]
        "INSERT INTO cognition_review_decisions ("
        + ", ".join(columns)
        + ") VALUES ("
        + placeholders
        + ")",
        values,
    )


class WorklessCandidateMigrationTests(unittest.TestCase):
    """M14: the 0014 rebuild of collab_artifacts and the decision impact column."""

    def _fresh_store(self, root: Path) -> ControlStore:
        return ControlStore.open(root / "fresh.sqlite")

    def _open_upto(self, root: Path, limit: int) -> Path:
        partial = root / f"m{limit}"
        partial.mkdir()
        copy_migrations_upto(limit, partial)
        return partial

    # ---------------------------------------------------------------- M14-01
    def test_m14_01_empty_database_reaches_the_latest_migration(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcs-j06-14-") as raw:
            store = self._fresh_store(Path(raw))
            try:
                ledger = dict(
                    store.connection.execute(
                        "SELECT migration_id, name FROM schema_migrations"
                    ).fetchall()
                )
                self.assertEqual(17, max(ledger))
                self.assertEqual("workless_candidates_and_blocked_impact", ledger[14])
                self.assertEqual("model_attempt_tool_rounds", ledger[15])
                self.assertEqual("maintenance_jobs", ledger[16])
                self.assertEqual("conversation_turns", ledger[17])
                indexes = {
                    row[0]
                    for row in store.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'index'"
                    )
                }
                for name in (
                    "collab_artifacts_interaction_idx",
                    "collab_artifacts_work_idx",
                    "collab_artifacts_status_idx",
                    # 0010 added these two partial unique indexes; a table rebuild
                    # drops them unless the migration recreates them.
                    "collab_artifacts_request_key_idx",
                    "collab_candidates_single_version_idx",
                    "idx_decisions_approval_once",
                    "idx_decisions_candidate",
                    "idx_decisions_fingerprint",
                ):
                    self.assertIn(name, indexes)
                triggers = {
                    row[0]
                    for row in store.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                    )
                }
                for name in (
                    "collab_artifacts_no_delete",
                    "collab_artifacts_update_guard",
                    "cognition_review_decisions_no_delete",
                    "cognition_review_decisions_no_update",
                ):
                    self.assertIn(name, triggers)
                self.assertEqual(
                    [], store.connection.execute("PRAGMA foreign_key_check").fetchall()
                )
            finally:
                store.close()

    # ---------------------------------------------------------------- M14-02
    def test_m14_02_upgrade_preserves_rows_and_payloads(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcs-j06-14-") as raw:
            root = Path(raw)
            partial = self._open_upto(root, 13)
            db = root / "old.sqlite"
            old = ControlStore.open(db, migrations_dir=partial)
            try:
                with old.transaction():
                    insert_payload(old.connection, "payload-a", '{"keep": true}')
                    insert_artifact(
                        old.connection, artifact_id=RECORD_ID, payload_ref="payload-a"
                    )
                    insert_decision(old.connection, impact="E1")
                before = old.connection.execute(
                    "SELECT artifact_id, body_payload_ref, content_digest "
                    "FROM collab_artifacts"
                ).fetchall()
                before_payload = old.connection.execute(
                    "SELECT payload_ref, sha256, body_json FROM private_payloads"
                ).fetchall()
                before_decisions = old.connection.execute(
                    "SELECT decision_id, impact, decision FROM cognition_review_decisions"
                ).fetchall()
            finally:
                old.close()

            upgraded = ControlStore.open(db)
            try:
                self.assertEqual(
                    17,
                    upgraded.connection.execute(
                        "SELECT MAX(migration_id) FROM schema_migrations"
                    ).fetchone()[0],
                )
                after = upgraded.connection.execute(
                    "SELECT artifact_id, body_payload_ref, content_digest "
                    "FROM collab_artifacts"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in before], [tuple(row) for row in after]
                )
                after_payload = upgraded.connection.execute(
                    "SELECT payload_ref, sha256, body_json FROM private_payloads"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in before_payload],
                    [tuple(row) for row in after_payload],
                )
                after_decisions = upgraded.connection.execute(
                    "SELECT decision_id, impact, decision FROM cognition_review_decisions"
                ).fetchall()
                self.assertEqual(
                    [tuple(row) for row in before_decisions],
                    [tuple(row) for row in after_decisions],
                )
                self.assertEqual(
                    [], upgraded.connection.execute("PRAGMA foreign_key_check").fetchall()
                )
                self.assertEqual(
                    "ok",
                    upgraded.connection.execute("PRAGMA integrity_check").fetchone()[0],
                )
            finally:
                upgraded.close()

    # ---------------------------------------------------------------- M14-03
    def test_m14_03_artifact_triggers_survive_the_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcs-j06-14-") as raw:
            root = Path(raw)
            store = self._fresh_store(root)
            try:
                with store.transaction():
                    insert_payload(store.connection, "payload-t", "{}")
                    insert_artifact(
                        store.connection, artifact_id=RECORD_ID, payload_ref="payload-t"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute("DELETE FROM collab_artifacts")
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute(
                        "UPDATE collab_artifacts SET body_payload_ref = 'payload-x'"
                    )
                self.assertEqual(1, store.connection.execute(
                    "SELECT COUNT(*) FROM collab_artifacts"
                ).fetchone()[0])
            finally:
                store.close()

    # ---------------------------------------------------------------- M14-04
    def test_m14_04_decision_triggers_survive_the_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcs-j06-14-") as raw:
            store = self._fresh_store(Path(raw))
            try:
                with store.transaction():
                    insert_decision(store.connection)
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute(
                        "UPDATE cognition_review_decisions SET reason_code = 'x'"
                    )
                with self.assertRaises(sqlite3.IntegrityError):
                    store.connection.execute("DELETE FROM cognition_review_decisions")
            finally:
                store.close()

    # ---------------------------------------------------------------- M14-05
    def test_m14_05_work_columns_are_all_or_nothing(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcs-j06-14-") as raw:
            store = self._fresh_store(Path(raw))
            try:
                with store.transaction():
                    insert_payload(store.connection, "payload-w", "{}")
                    insert_artifact(
                        store.connection,
                        kind="candidate",
                        payload_ref="payload-w",
                        work_id=None,
                        work_generation=None,
                    )
                self.assertEqual(
                    1,
                    store.connection.execute(
                        "SELECT COUNT(*) FROM collab_artifacts WHERE kind = 'candidate'"
                    ).fetchone()[0],
                )
                for work_id, generation in ((new_id(), None), (None, 0)):
                    with self.subTest(work_id=work_id, generation=generation):
                        with self.assertRaises(sqlite3.IntegrityError):
                            with store.transaction():
                                insert_artifact(
                                    store.connection,
                                    kind="candidate",
                                    payload_ref="payload-w",
                                    work_id=work_id,
                                    work_generation=generation,
                                )
                with store.transaction():
                    insert_artifact(
                        store.connection,
                        kind="candidate",
                        payload_ref="payload-w",
                        work_id=new_id(),
                        work_generation=0,
                    )
            finally:
                store.close()

    # ---------------------------------------------------------------- M14-06
    def test_m14_06_non_candidates_still_reject_proposal_columns(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcs-j06-14-") as raw:
            store = self._fresh_store(Path(raw))
            try:
                with store.transaction():
                    insert_payload(store.connection, "payload-n", "{}")
                with self.assertRaises(sqlite3.IntegrityError):
                    with store.transaction():
                        insert_artifact(
                            store.connection,
                            kind="reply",
                            payload_ref="payload-n",
                            with_proposal=True,
                        )
            finally:
                store.close()

    # ---------------------------------------------------------------- M14-07
    def test_m14_07_blocked_impact_is_storable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcs-j06-14-") as raw:
            store = self._fresh_store(Path(raw))
            try:
                with store.transaction():
                    insert_decision(
                        store.connection, impact="BLOCKED", decision="deferred",
                        reason_code="rule_publication_not_supported",
                    )
                self.assertEqual(
                    "BLOCKED",
                    store.connection.execute(
                        "SELECT impact FROM cognition_review_decisions"
                    ).fetchone()[0],
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    with store.transaction():
                        insert_decision(store.connection, impact="E9", decision_seq=2)
            finally:
                store.close()

    # ---------------------------------------------------------------- M14-08
    def test_m14_08_frozen_migrations_keep_their_bytes(self) -> None:
        for name, expected in FROZEN_MIGRATION_SHA256.items():
            with self.subTest(migration=name):
                digest = hashlib.sha256(
                    (MIGRATIONS_DIR / name).read_bytes()
                ).hexdigest()
                self.assertEqual(expected, digest)



class GovernanceTestCase(StoreTestCase):
    store_class = ControlStore

    # ------------------------------------------------------------- helpers

    def decision_command(
        self,
        *,
        request_id: str | None = None,
        fields: dict[str, object] | None = None,
        namespace: str = NS,
        context_id: str | None = None,
    ) -> object:
        if context_id is None:
            context_id = self.store.capture_snapshot(NS).context_id
        payload = fields if fields is not None else decision_fields()
        request = request_id if request_id is not None else new_id()

        def apply(scope: object) -> None:
            scope.record_review_decision(  # type: ignore[attr-defined]
                namespace=namespace, fields=payload  # type: ignore[arg-type]
            )

        return self.store.commit_command(
            context_id=context_id,
            request_id=request,
            operation=DECISION_OPERATION,
            request_payload={"decision_id": payload["decision_id"]},
            apply=apply,
            namespace=NS,
        )

    def append_and_link(
        self,
        *,
        record_id: str = RECORD_ID,
        linked: str | None = None,
        decision_id: str | None = None,
    ) -> object:
        snapshot = self.store.capture_snapshot(NS)
        request = new_id()
        target = linked if linked is not None else record_id
        decision = decision_id if decision_id is not None else new_id()

        def apply(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=record_id,
                record_version=1,
                kind="Observation",
                body={"text": "governance"},
            )
            scope.link_record_decision(  # type: ignore[attr-defined]
                namespace=NS,
                record_id=target,
                record_version=1,
                decision_id=decision,
                decided_at=RECORDED_AT,
            )

        return self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=request,
            operation="cognition.change",
            request_payload={"text": "governance"},
            apply=apply,
            namespace=NS,
        )

    def insert_decision_row(self, **overrides: object) -> None:
        fields = decision_fields(**overrides)
        columns = ["namespace"] + list(fields)
        values = [NS] + [fields[name] for name in fields]
        placeholders = ", ".join("?" for _ in columns)
        self.store.connection.execute(
            "INSERT INTO cognition_review_decisions ("
            + ", ".join(columns)
            + ") VALUES ("
            + placeholders
            + ")",
            values,
        )

    # ---------------------------------------------------------- GS-01..04

    def test_gs_01_empty_database_migrates_to_0013(self) -> None:
        tables = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertTrue(set(GOVERNANCE_TABLES) <= tables)
        triggers = {
            row[0]
            for row in self.store.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            )
        }
        self.assertTrue(set(GOVERNANCE_TRIGGERS) <= triggers)
        ledger = dict(
            self.store.connection.execute(
                "SELECT migration_id, name FROM schema_migrations"
            ).fetchall()
        )
        self.assertEqual("workless_candidates_and_blocked_impact", ledger[14])
        self.assertEqual(17, max(ledger))

    def test_gs_02_existing_0012_database_upgrades_in_place(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcs-j06-mig-") as raw:
            root = Path(raw)
            partial_dir = root / "partial"
            partial_dir.mkdir()
            partial = copy_migrations_upto(13, partial_dir)
            old_db = root / "old.sqlite"
            old = ControlStore.open(old_db, migrations_dir=partial)
            try:
                with old.transaction():
                    old.append_record_version(
                        namespace=NS,
                        record_id=RECORD_ID,
                        record_version=1,
                        kind="Observation",
                        body={"text": "before upgrade"},
                        recorded_at=RECORDED_AT,
                    )
                before = int(
                    old.connection.execute(
                        "SELECT COUNT(*) FROM record_versions"
                    ).fetchone()[0]
                )
                self.assertEqual(
                    13,
                    old.connection.execute(
                        "SELECT MAX(migration_id) FROM schema_migrations"
                    ).fetchone()[0],
                )
            finally:
                old.close()

            upgraded = ControlStore.open(old_db)
            try:
                self.assertEqual(
                    17,
                    upgraded.connection.execute(
                        "SELECT MAX(migration_id) FROM schema_migrations"
                    ).fetchone()[0],
                )
                after = int(
                    upgraded.connection.execute(
                        "SELECT COUNT(*) FROM record_versions"
                    ).fetchone()[0]
                )
                self.assertEqual(before, after)
                tables = {
                    row[0]
                    for row in upgraded.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertTrue(set(GOVERNANCE_TABLES) <= tables)
            finally:
                upgraded.close()

    def test_gs_03_reopening_is_idempotent(self) -> None:
        self.store.close()
        self.store = ControlStore.open(self.db_path)
        self.addCleanup(lambda: self.store.close())
        applied = apply_migrations(self.store.connection, MIGRATIONS_DIR)
        self.assertEqual((), applied)
        self.assertEqual(17, self.store.connection.execute(
            "SELECT MAX(migration_id) FROM schema_migrations"
        ).fetchone()[0])

    def test_gs_04_frozen_migration_bytes_are_unchanged(self) -> None:
        hashes = {}
        for name in ("0011_memory_delivery.sql", "0012_bridge_gaps.sql"):
            hashes[name] = hashlib.sha256(
                (MIGRATIONS_DIR / name).read_bytes()
            ).hexdigest()
        self.assertEqual(
            MIGRATION_0011_SHA256, hashes["0011_memory_delivery.sql"]
        )
        self.assertEqual(MIGRATION_0012_SHA256, hashes["0012_bridge_gaps.sql"])

    # ---------------------------------------------------------- GS-05..06

    def test_gs_05_decision_rows_reject_update(self) -> None:
        self.decision_command()
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.store.connection.execute(
                "UPDATE cognition_review_decisions SET reason_code = 'tampered'"
            )
        self.assertIn("append-only", str(caught.exception))
        stored = self.store.connection.execute(
            "SELECT reason_code FROM cognition_review_decisions"
        ).fetchall()
        self.assertEqual([("impact_requires_human",)], [tuple(row) for row in stored])

    def test_gs_06_decision_rows_reject_delete(self) -> None:
        self.decision_command()
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.store.connection.execute("DELETE FROM cognition_review_decisions")
        self.assertIn("append-only", str(caught.exception))
        self.assertEqual(1, self.count_rows("cognition_review_decisions"))

    # ---------------------------------------------------------- GS-07..08

    def test_gs_07_decision_only_command_is_a_legal_noop(self) -> None:
        fields = decision_fields()
        before_revision = self.store.current_state_revision(NS)
        before_records = self.count_rows("record_versions")
        receipt = self.decision_command(fields=fields)
        self.assertEqual(OUTCOME_NOOP, receipt.outcome)
        self.assertEqual((), receipt.record_refs)
        self.assertEqual(before_revision, self.store.current_state_revision(NS))
        self.assertEqual(before_records, self.count_rows("record_versions"))
        rows = self.store.connection.execute(
            "SELECT decision, impact, decision_request_id "
            "FROM cognition_review_decisions"
        ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("awaiting_human", rows[0]["decision"])
        self.assertEqual("E2", rows[0]["impact"])
        self.assertEqual(
            fields["decision_request_id"], rows[0]["decision_request_id"]
        )
        processed = self.store.connection.execute(
            "SELECT receipt_json FROM processed_commands "
            "WHERE namespace = ? AND request_id = ?",
            (NS, receipt.request_id),
        ).fetchone()
        self.assertIsNotNone(processed)
        stored = json.loads(processed["receipt_json"])
        self.assertEqual(OUTCOME_NOOP, stored["outcome"])

    def test_gs_08_replaying_the_same_decision_returns_the_same_receipt(self) -> None:
        fields = decision_fields()
        request = new_id()
        context = self.store.capture_snapshot(NS).context_id
        first = self.decision_command(
            request_id=request, fields=fields, context_id=context
        )
        second = self.decision_command(
            request_id=request, fields=fields, context_id=context
        )
        self.assertEqual(first.request_id, second.request_id)
        self.assertEqual(first.committed_at, second.committed_at)
        self.assertEqual(first.state_revision, second.state_revision)
        self.assertEqual(1, self.count_rows("cognition_review_decisions"))
        self.assertEqual(1, self.count_rows("processed_commands"))

    def test_gs_08b_same_request_with_different_payload_conflicts(self) -> None:
        request = new_id()
        context = self.store.capture_snapshot(NS).context_id
        self.decision_command(
            request_id=request, fields=decision_fields(), context_id=context
        )
        with self.assertRaises(CommandConflict):
            self.decision_command(
                request_id=request,
                fields=decision_fields(decision_seq=2),
                context_id=context,
            )
        self.assertEqual(1, self.count_rows("cognition_review_decisions"))

    # ---------------------------------------------------------- GS-09..10

    def test_gs_09_linking_a_foreign_record_rolls_everything_back(self) -> None:
        foreign = "88888888-8888-4888-8888-888888888888"
        with self.assertRaises(InvalidInput):
            self.append_and_link(linked=foreign)
        self.assertEqual(0, self.count_rows("record_versions"))
        self.assertEqual(0, self.count_rows("cognition_record_decisions"))
        self.assertEqual(0, self.count_rows("processed_commands"))

    def test_gs_10_linking_an_appended_record_writes_provenance(self) -> None:
        decision = new_id()
        receipt = self.append_and_link(decision_id=decision)
        self.assertEqual(1, len(receipt.record_refs))
        rows = self.store.connection.execute(
            "SELECT record_id, record_version, decision_id "
            "FROM cognition_record_decisions"
        ).fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual(RECORD_ID, rows[0]["record_id"])
        self.assertEqual(1, rows[0]["record_version"])
        self.assertEqual(decision, rows[0]["decision_id"])

    def test_gs_10b_linking_the_same_version_twice_rolls_back(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        request = new_id()
        decision = new_id()

        def apply(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=RECORD_ID,
                record_version=1,
                kind="Observation",
                body={"text": "governance"},
            )
            scope.link_record_decision(  # type: ignore[attr-defined]
                namespace=NS,
                record_id=RECORD_ID,
                record_version=1,
                decision_id=decision,
                decided_at=RECORDED_AT,
            )
            scope.link_record_decision(  # type: ignore[attr-defined]
                namespace=NS,
                record_id=RECORD_ID,
                record_version=1,
                decision_id=new_id(),
                decided_at=RECORDED_AT,
            )

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.commit_command(
                context_id=snapshot.context_id,
                request_id=request,
                operation="cognition.change",
                request_payload={"text": "duplicate provenance"},
                apply=apply,
                namespace=NS,
            )
        self.assertEqual(0, self.count_rows("record_versions"))
        self.assertEqual(0, self.count_rows("cognition_record_decisions"))

    def test_gs_10c_namespace_mismatch_is_refused(self) -> None:
        with self.assertRaises(ScopeViolation):
            self.decision_command(namespace="example.test.other")

    # ---------------------------------------------------------------- GS-11

    def test_gs_11_unknown_and_oversized_fields_are_rejected(self) -> None:
        cases = {
            "unknown": decision_fields(bogus_column="x"),
            "overlong reason": decision_fields(reason_code="r" * 65),
            "bad decision": decision_fields(decision="accepted"),
            "bad impact": decision_fields(impact="E9"),
            "bad linkage": decision_fields(evidence_linkage="maybe"),
            "bad requires_human": decision_fields(requires_human=2),
            "short digest": decision_fields(change_digest="sha256:abc"),
        }
        for label, fields in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(InvalidInput):
                    self.decision_command(fields=fields)
        self.assertEqual(0, self.count_rows("cognition_review_decisions"))

    def test_gs_11b_missing_required_field_is_rejected(self) -> None:
        fields = decision_fields()
        del fields["change_digest"]
        with self.assertRaises(InvalidInput):
            self.decision_command(fields=fields)
        self.assertEqual(0, self.count_rows("cognition_review_decisions"))

    # ---------------------------------------------------------------- GS-12

    def test_gs_12_one_approval_can_commit_once(self) -> None:
        approval = new_id()
        candidate = new_id()
        self.insert_decision_row(
            decision="committed", approval_id=approval, candidate_id=candidate
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_decision_row(
                decision="committed",
                approval_id=approval,
                candidate_id=candidate,
                decision_seq=2,
                decision_id=new_id(),
            )
        # A non-committed row for the same approval is outside the partial index.
        self.insert_decision_row(
            decision="deferred",
            approval_id=approval,
            candidate_id=candidate,
            decision_seq=3,
            decision_id=new_id(),
        )
        self.assertEqual(2, self.count_rows("cognition_review_decisions"))

    # ---------------------------------------------------------------- GS-13

    def test_gs_13_failed_migration_rolls_back_and_database_stays_usable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pcs-j06-broken-") as raw:
            root = Path(raw)
            upgrade_db = root / "upgrade.sqlite"
            partial_dir = root / "partial"
            partial_dir.mkdir()
            partial = copy_migrations_upto(13, partial_dir)
            old = ControlStore.open(upgrade_db, migrations_dir=partial)
            old.close()

            broken_dir = root / "broken"
            broken_dir.mkdir()
            broken = copy_migrations_upto(13, broken_dir)
            (broken / "0014_broken.sql").write_text(
                "CREATE TABLE broken_table (id TEXT);\n"
                "CREATE TABLE broken_table (id TEXT);\n",
                encoding="utf-8",
            )
            with self.assertRaises(MigrationError):
                ControlStore.open(upgrade_db, migrations_dir=broken)

            recovered = ControlStore.open(upgrade_db)
            try:
                self.assertEqual(
                    17,
                    recovered.connection.execute(
                        "SELECT MAX(migration_id) FROM schema_migrations"
                    ).fetchone()[0],
                )
                tables = {
                    row[0]
                    for row in recovered.connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertNotIn("broken_table", tables)
                self.assertTrue(set(GOVERNANCE_TABLES) <= tables)
            finally:
                recovered.close()


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
