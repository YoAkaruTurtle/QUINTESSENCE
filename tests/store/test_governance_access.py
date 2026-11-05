"""J06 S5a/S5b: governance accessors and change-candidate lifecycle.

Every test builds its own migrated database.  The access layer under test never
fabricates a record version: decision rows that the layer only *reads* are
inserted directly, and everything else goes through the real ``commit_command``
seam so the append-only triggers and the no-op path are exercised for real.

S5b adds ``lifecycle`` and ``require_current_change_candidate``.  Their fixtures
insert candidate rows directly because the *creation* policy (with or without a
work anchor) is still undecided; the accessors themselves only read receipts,
terminal decisions and the proposal's operation -- never ``work_id``.
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

from support import RECORDED_AT, StoreTestCase  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    CorruptStore,
    InvalidInput,
    RecordKindMismatch,
    RecordNotFound,
    ScopeViolation,
)
from quintessence_pcs.foundation.jsonutil import (  # noqa: E402
    canonical_json_text,
    sha256_hex,
)
from quintessence_pcs.store.cognition import CognitionStore  # noqa: E402
from quintessence_pcs.store.control import ControlStore  # noqa: E402
from quintessence_pcs.store.errors import CandidateTransitionDenied  # noqa: E402
from quintessence_pcs.store.governance import (  # noqa: E402
    CHANGE_OPERATION,
    GovernanceAccess,
    GovernanceStore,
)
from quintessence_pcs.store.tx_b import OUTCOME_NOOP  # noqa: E402

NS = "example.test.governance.access"
HOST = "host:synthetic-governance"
ACCESS_REF = "authorization:governance:1"


def new_id() -> str:
    return str(uuid.uuid4())


def decision_fields(**overrides: object) -> dict[str, object]:
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


class GovernanceAccessTestCase(StoreTestCase):
    store_class = ControlStore

    def setUp(self) -> None:
        super().setUp()
        self.governance = GovernanceStore(self.store)

    # ------------------------------------------------------------- helpers

    def admin_access(self, **overrides: object) -> GovernanceAccess:
        values: dict[str, object] = {
            "namespace": NS,
            "host_instance": HOST,
            "operator": "operator:synthetic",
            "access_ref": ACCESS_REF,
            "permissions": frozenset({"governance.admin", "governance.read"}),
        }
        values.update(overrides)
        return GovernanceAccess(**values)  # type: ignore[arg-type]

    def insert_decision(self, **overrides: object) -> dict[str, object]:
        fields = decision_fields(**overrides)
        columns = ["namespace"] + list(fields)
        values = [NS] + [fields[name] for name in fields]
        self.store.connection.execute(
            "INSERT INTO cognition_review_decisions ("
            + ", ".join(columns)
            + ") VALUES ("
            + ", ".join("?" for _ in columns)
            + ")",
            values,
        )
        return fields

    def decision_command(
        self, fields: dict[str, object], *, request_id: str | None = None
    ):
        context = self.store.capture_snapshot(NS)
        request = request_id if request_id is not None else new_id()

        def apply(scope: object) -> None:
            scope.record_review_decision(  # type: ignore[attr-defined]
                namespace=NS, fields=fields
            )

        return self.store.commit_command(
            context_id=context.context_id,
            request_id=request,
            operation="cognition.decision",
            request_payload={"decision_id": fields["decision_id"]},
            apply=apply,
            namespace=NS,
        )

    # ---------------------------------------------------- GA-01 decision_seq

    def test_ga_01_next_decision_seq_requires_a_transaction(self) -> None:
        with self.assertRaises(InvalidInput):
            self.governance.next_decision_seq(NS)
        with self.store.transaction():
            self.assertEqual(1, self.governance.next_decision_seq(NS))
            # The cursor is MAX+1, so asking twice before inserting is stable.
            self.assertEqual(1, self.governance.next_decision_seq(NS))
        self.insert_decision(decision_seq=1)
        with self.store.transaction():
            self.assertEqual(2, self.governance.next_decision_seq(NS))

    # -------------------------------------------------- GA-02/GA-03 cursor

    def test_ga_02_list_decisions_honours_the_cursor(self) -> None:
        self.insert_decision(decision_seq=1)
        self.insert_decision(decision_seq=2)
        self.insert_decision(decision_seq=3)

        everything = self.governance.list_decisions(NS)
        self.assertEqual([1, 2, 3], [row.decision_seq for row in everything])

        after = self.governance.list_decisions(NS, after_seq=2)
        self.assertEqual([3], [row.decision_seq for row in after])

        limited = self.governance.list_decisions(NS, limit=2)
        self.assertEqual([1, 2], [row.decision_seq for row in limited])

    def test_ga_03_decision_row_is_visible_without_a_revision_move(self) -> None:
        # A decision-only command is a legal no-op: the cognitive watermark does
        # not move, so the read cursor must not depend on it.
        before_revision = self.store.current_state_revision(NS)
        fields = decision_fields()
        receipt = self.decision_command(fields)
        self.assertEqual(OUTCOME_NOOP, receipt.outcome)
        self.assertEqual(before_revision, self.store.current_state_revision(NS))

        rows = self.governance.list_decisions(NS, after_seq=0)
        self.assertEqual(1, len(rows))
        self.assertEqual(fields["decision_id"], rows[0].decision_id)
        self.assertEqual([], list(self.governance.list_decisions(NS, after_seq=1)))

        by_candidate = self.governance.decisions_for_candidate(
            NS, rows[0].candidate_id
        )
        self.assertEqual(
            [fields["decision_id"]], [r.decision_id for r in by_candidate]
        )
        self.assertIsNone(self.governance.latest_decision(NS, new_id(), 1))

    # --------------------------------------------------- GA-11 authorisations

    def test_ga_11_authorization_read_and_window(self) -> None:
        authorization_id = new_id()
        self.store.connection.execute(
            "INSERT INTO cognition_commit_authorizations ("
            "namespace, authorization_id, host_instance, access_ref, "
            "operations_json, claim_types_json, scope_json, max_impact, "
            "max_changes_per_bundle, max_targets_per_bundle, policy_id, "
            "policy_version, authorization_source, issued_at, expires_at, "
            "revoked_at, revoke_reason, issued_by"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)",
            (
                NS,
                authorization_id,
                HOST,
                ACCESS_REF,
                json.dumps(["propose_belief"]),
                json.dumps(["general_hypothesis"]),
                json.dumps({"project_id": "alpha"}),
                "E1",
                2,
                2,
                "policy:cognition.auto.v2",
                "1",
                "host_config",
                "2026-10-26T00:00:00Z",
                "2026-10-27T00:00:00Z",
                "operator:synthetic",
            ),
        )
        row = self.governance.load_commit_authorization(
            namespace=NS, authorization_id=authorization_id
        )
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(("propose_belief",), row.operations)
        self.assertEqual({"project_id": "alpha"}, row.scope)
        self.assertTrue(
            self.governance.authorization_is_active(row, "2026-10-26T12:00:00Z")
        )
        self.assertFalse(
            self.governance.authorization_is_active(row, "2026-10-28T00:00:00Z")
        )
        self.assertIsNone(
            self.governance.load_commit_authorization(
                namespace=NS, authorization_id=new_id()
            )
        )

    # ---------------------------------------------- GA-12/13 subject authority

    def test_ga_12_subject_authority_seed_and_idempotent_revoke(self) -> None:
        access = self.admin_access()
        row = self.governance.grant_subject_authority(
            access,
            project_id="alpha",
            subject_ref="operator:synthetic",
            basis="owner",
            now="2026-10-26T00:00:00Z",
        )
        self.assertIsNone(row.revoked_at)
        self.assertTrue(
            self.governance.subject_authority_is_active(row, "2026-10-26T00:00:00Z")
        )

        first = self.governance.revoke_subject_authority(
            access,
            project_id="alpha",
            subject_ref="operator:synthetic",
            reason="left the project",
            now="2026-10-26T01:00:00Z",
        )
        self.assertEqual("2026-10-26T01:00:00Z", first.revoked_at)
        self.assertFalse(
            self.governance.subject_authority_is_active(first, "2026-10-26T02:00:00Z")
        )

        # Revoking again is idempotent and keeps the first audit values.
        second = self.governance.revoke_subject_authority(
            access,
            project_id="alpha",
            subject_ref="operator:synthetic",
            reason="ignored second reason",
            now="2026-10-26T03:00:00Z",
        )
        self.assertEqual(first.revoked_at, second.revoked_at)
        self.assertEqual("left the project", second.revoke_reason)

    def test_ga_13_admin_permission_is_required_and_writes_nothing(self) -> None:
        read_only = self.admin_access(permissions=frozenset({"governance.read"}))
        with self.assertRaises(ScopeViolation):
            self.governance.grant_subject_authority(
                read_only,
                project_id="alpha",
                subject_ref="operator:synthetic",
                basis="owner",
                now="2026-10-26T00:00:00Z",
            )
        self.assertEqual(0, self.count_rows("cognition_subject_authority"))

        with self.assertRaises(ScopeViolation):
            self.governance.revoke_subject_authority(
                read_only,
                project_id="alpha",
                subject_ref="operator:synthetic",
                reason="nope",
                now="2026-10-26T00:00:00Z",
            )
        self.assertEqual(0, self.count_rows("cognition_subject_authority"))

    def test_ga_13b_unknown_permission_is_refused_by_the_access_object(self) -> None:
        with self.assertRaises(InvalidInput):
            self.admin_access(permissions=frozenset({"governance.root"}))

    # ------------------------------------------------------- GA-14 provenance

    def test_ga_14_provenance_round_trip(self) -> None:
        record_id = new_id()
        decision_id = new_id()
        context = self.store.capture_snapshot(NS)

        def apply(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=record_id,
                record_version=1,
                kind="Observation",
                body={"text": "with provenance"},
            )
            scope.record_review_decision(  # type: ignore[attr-defined]
                namespace=NS,
                fields=decision_fields(decision_id=decision_id, decision="committed"),
            )
            scope.link_record_decision(  # type: ignore[attr-defined]
                namespace=NS,
                record_id=record_id,
                record_version=1,
                decision_id=decision_id,
                decided_at=RECORDED_AT,
            )

        self.store.commit_command(
            context_id=context.context_id,
            request_id=new_id(),
            operation="cognition.change",
            request_payload={"text": "with provenance"},
            apply=apply,
            namespace=NS,
        )

        found = self.governance.decision_for_record(NS, record_id, 1)
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(decision_id, found.decision_id)
        self.assertIsNone(self.governance.decision_for_record(NS, new_id(), 1))

    # --------------------------------------------------- GA-15 strict decode

    def test_ga_15_missing_column_or_bad_json_is_corrupt(self) -> None:
        complete = {
            "namespace": NS,
            "decision_id": new_id(),
            "decision_seq": 1,
            "candidate_id": new_id(),
            "artifact_version": 1,
            "interaction_id": None,
            "decision": "deferred",
            "reason_code": "insufficient_basis",
            "impact": "E0",
            "derived_status": "proposed",
            "support_basis": "provisional_hypothesis",
            "requires_human": 0,
            "evidence_linkage": "not_applicable",
            "policy_id": "policy:cognition.review.base.v2",
            "policy_version": "1",
            "impact_policy_id": "policy:cognition.risk.v1",
            "impact_policy_version": "1",
            "authority_kind": "none",
            "authorization_id": None,
            "directive_digest": None,
            "approval_id": None,
            "change_digest": "sha256:" + "b" * 64,
            "change_fingerprint": None,
            "target_refs_json": "[]",
            "evidence_refs_json": "[]",
            "scope_json": "{}",
            "verification_result": "unavailable",
            "approval_scope_json": None,
            "targets_expected_json": None,
            "approval_expires_at": None,
            "decision_request_id": new_id(),
            "commit_request_id": None,
            "review_after": None,
            "decided_at": RECORDED_AT,
        }
        # A complete mapping decodes.
        self.assertEqual(
            "deferred", self.governance._decode_decision(complete).decision
        )

        missing = dict(complete)
        del missing["impact"]
        with self.assertRaises(CorruptStore):
            self.governance._decode_decision(missing)

        broken = dict(complete)
        broken["target_refs_json"] = "{not json"
        with self.assertRaises(CorruptStore):
            self.governance._decode_decision(broken)

        # A corrupt row is refused on the real read path too (the append-only
        # trigger forbids UPDATE, so the bad value is inserted directly).
        self.insert_decision(decision="deferred", target_refs_json="{not json")
        with self.assertRaises(CorruptStore):
            self.governance.list_decisions(NS)


class ChangeCandidateStateTests(StoreTestCase):
    """S5b: ``lifecycle`` and ``require_current_change_candidate``.

    Candidate rows are inserted directly so the test can choose a synthetic
    ``work_id`` *or* a NULL one: the currentness judgement must not depend on how
    a candidate was created.  Receipts and decision rows are produced through the
    real Tx B seam.
    """

    store_class = CognitionStore

    def setUp(self) -> None:
        super().setUp()
        self.governance = GovernanceStore(self.store)

    # ------------------------------------------------------------- fixtures

    def _payload(self, ref: str, text: str, key: str) -> None:
        wrapper = canonical_json_text({key: text})
        self.store.connection.execute(
            "INSERT INTO private_payloads (payload_ref, sha256, body_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            (ref, sha256_hex(wrapper), wrapper, RECORDED_AT),
        )

    def insert_candidate(
        self,
        *,
        candidate_id: str | None = None,
        request_id: str | None = None,
        work_id: str | None = "11111111-1111-4111-8111-111111111111",
        work_generation: int | None = 0,
        operation: str = CHANGE_OPERATION,
        kind: str = "candidate",
        status: str | None = "pending",
    ) -> tuple[str, str]:
        cid = candidate_id if candidate_id is not None else new_id()
        req = request_id if request_id is not None else new_id()
        proposal = {"operation": operation, "summary": "synthetic", "changes": []}
        proposal_text = canonical_json_text(proposal)
        body_text = "synthetic change candidate body"
        self._payload(f"proposal:{cid}", proposal_text, "proposal")
        self._payload(f"body:{cid}", body_text, "body")
        self.store.connection.execute(
            "INSERT INTO collab_artifacts ("
            "namespace, artifact_id, artifact_version, schema_version, kind, "
            "interaction_id, context_id, origin_kind, body_payload_ref, "
            "content_digest, retention_policy, created_at, proposal_payload_ref, "
            "proposal_digest, proposal_version, work_id, work_generation, "
            "request_id, command_digest, structural_validation, status"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                NS,
                cid,
                1,
                "collab.v1",
                kind,
                new_id(),
                new_id(),
                "model_output",
                f"body:{cid}",
                "sha256:" + sha256_hex(body_text),
                "standard",
                RECORDED_AT,
                f"proposal:{cid}" if kind == "candidate" else None,
                "sha256:" + sha256_hex(proposal_text) if kind == "candidate" else None,
                "v1" if kind == "candidate" else None,
                work_id,
                work_generation,
                req if kind == "candidate" else None,
                sha256_hex("command:" + cid) if kind == "candidate" else None,
                "passed" if kind == "candidate" else None,
                status,
            ),
        )
        return cid, req

    def commit_receipt(self, request_id: str) -> object:
        snapshot = self.store.capture_snapshot(NS)

        def apply(scope: object) -> None:
            scope.append_record_version(  # type: ignore[attr-defined]
                record_id=new_id(),
                record_version=1,
                kind="Observation",
                body={"text": "committed by the candidate"},
            )

        return self.store.commit_command(
            context_id=snapshot.context_id,
            request_id=request_id,
            operation=CHANGE_OPERATION,
            request_payload={"synthetic": True},
            apply=apply,
            namespace=NS,
        )

    def reject_row(self, candidate_id: str, *, seq: int = 1) -> None:
        self.insert_decision(
            decision="rejected",
            candidate_id=candidate_id,
            decision_seq=seq,
        )

    def conflict_row(self, candidate_id: str, *, seq: int) -> None:
        self.insert_decision(
            decision="conflict",
            candidate_id=candidate_id,
            decision_seq=seq,
        )

    def insert_decision(self, **overrides: object) -> dict[str, object]:
        fields = decision_fields(**overrides)
        columns = ["namespace"] + list(fields)
        values = [NS] + [fields[name] for name in fields]
        self.store.connection.execute(
            "INSERT INTO cognition_review_decisions ("
            + ", ".join(columns)
            + ") VALUES ("
            + ", ".join("?" for _ in columns)
            + ")",
            values,
        )
        return fields

    # ---------------------------------------------------------------- GA-04

    def test_ga_04_receipt_only_is_committed(self) -> None:
        cid, req = self.insert_candidate()
        self.commit_receipt(req)
        self.assertEqual("committed", self.governance.lifecycle(NS, cid))

    # ---------------------------------------------------------------- GA-05

    def test_ga_05_rejected_row_only_is_rejected(self) -> None:
        cid, _ = self.insert_candidate()
        self.reject_row(cid)
        self.assertEqual("rejected", self.governance.lifecycle(NS, cid))

    # ---------------------------------------------------------------- GA-06

    def test_ga_06_receipt_and_rejection_is_corrupt(self) -> None:
        cid, req = self.insert_candidate()
        self.commit_receipt(req)
        self.reject_row(cid)
        with self.assertRaises(CorruptStore):
            self.governance.lifecycle(NS, cid)

    # ---------------------------------------------------------------- GA-07

    def test_ga_07_neither_is_pending(self) -> None:
        cid, _ = self.insert_candidate()
        self.assertEqual("pending", self.governance.lifecycle(NS, cid))

    # --------------------------------------------------------------- GA-07b

    def test_ga_07b_a_late_attempt_does_not_rewrite_a_terminal_state(self) -> None:
        committed, req = self.insert_candidate()
        self.commit_receipt(req)
        self.conflict_row(committed, seq=2)
        self.assertEqual("committed", self.governance.lifecycle(NS, committed))

        rejected, _ = self.insert_candidate()
        self.reject_row(rejected, seq=3)
        self.conflict_row(rejected, seq=4)
        self.assertEqual("rejected", self.governance.lifecycle(NS, rejected))

        # The attempt rows really are in the log: the point is that lifecycle
        # ignores them, not that they were dropped.
        self.assertEqual(1, len(self.governance.decisions_for_candidate(NS, committed)))
        self.assertEqual(2, len(self.governance.decisions_for_candidate(NS, rejected)))

    # ---------------------------------------------------------------- GA-08

    def test_ga_08_pending_change_candidate_is_current(self) -> None:
        cid, _ = self.insert_candidate()
        row = self.governance.require_current_change_candidate(NS, cid)
        self.assertEqual(cid, row.artifact_id)
        self.assertEqual("candidate", row.kind)

    # ---------------------------------------------------------------- GA-09

    def test_ga_09_terminal_candidates_are_not_current(self) -> None:
        committed, req = self.insert_candidate()
        self.commit_receipt(req)
        with self.assertRaises(CandidateTransitionDenied):
            self.governance.require_current_change_candidate(NS, committed)

        rejected, _ = self.insert_candidate()
        self.reject_row(rejected)
        with self.assertRaises(CandidateTransitionDenied):
            self.governance.require_current_change_candidate(NS, rejected)

    # ---------------------------------------------------------------- GA-10

    def test_ga_10_wrong_kind_or_operation_is_refused(self) -> None:
        other_kind, _ = self.insert_candidate(kind="reply")
        with self.assertRaises(RecordKindMismatch):
            self.governance.require_current_change_candidate(NS, other_kind)

        wrong_op, _ = self.insert_candidate(operation="belief.propose")
        with self.assertRaises(CandidateTransitionDenied):
            self.governance.require_current_change_candidate(NS, wrong_op)

        with self.assertRaises(RecordNotFound):
            self.governance.require_current_change_candidate(NS, new_id())

    # --------------------------------------------------------------- GA-10b

    def test_ga_10b_the_work_anchor_value_is_irrelevant(self) -> None:
        """The judgement must not depend on how the candidate was created.

        A literally NULL ``work_id`` is currently impossible: the frozen CHECK in
        ``0008_collaboration.sql`` requires the nine candidate columns (including
        ``work_id``/``work_generation``) to be non-null.  That shape is exactly
        what the pending A/C″ storage decision is about.  What can be proven
        today is the weaker but sufficient property: two candidates that differ
        only in their work anchor are judged identically, so neither
        ``lifecycle`` nor ``require_current_change_candidate`` reads it.
        """

        first, _ = self.insert_candidate(
            work_id="11111111-1111-4111-8111-111111111111", work_generation=0
        )
        second, _ = self.insert_candidate(
            work_id="22222222-2222-4222-8222-222222222222", work_generation=7
        )
        for cid in (first, second):
            row = self.governance.require_current_change_candidate(NS, cid)
            self.assertEqual("pending", self.governance.lifecycle(NS, cid))
            self.assertEqual("candidate", row.kind)

        # And a terminal one stays terminal regardless of its anchor.
        done, req = self.insert_candidate(
            work_id="33333333-3333-4333-8333-333333333333", work_generation=3
        )
        self.commit_receipt(req)
        self.assertEqual("committed", self.governance.lifecycle(NS, done))
        with self.assertRaises(CandidateTransitionDenied):
            self.governance.require_current_change_candidate(NS, done)


if __name__ == "__main__":
    unittest.main()
