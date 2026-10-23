"""C12c: the ``review.*`` desktop bridge methods and their boundaries.

Every test runs the real Python sidecar over framed stdio against a temporary
synthetic SQLite store outside the checkout.  Work rows are seeded by the test
harness through the trusted Store API (an operational fixture), then the review
candidate and every decision are created by the real C07 ``ReviewFacade``.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
import uuid

from tests.desktop.support import (
    DesktopCase,
    HOST_INSTANCE,
    NAMESPACE,
    REVIEW_PERMISSIONS,
    config_document,
    review_proposal,
    seed_work,
    utc_now,
)
import pcs_bridge  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    InvalidInput,
    StateRevisionConflict,
)
from quintessence_pcs.store import CognitionStore  # noqa: E402


class ReviewBridgeCase(DesktopCase):
    def prepare(
        self,
        *,
        managed: int = 1,
        legacy: int = 0,
        permissions: list[str] | None = None,
        review_script: list[dict] | None = None,
        external: bool = True,
    ):
        database = self.tmp_path / "review.sqlite"
        managed_works = [seed_work(database, managed=True) for _ in range(managed)]
        legacy_works = [seed_work(database, managed=False) for _ in range(legacy)]
        if review_script is None:
            review_script = [
                review_proposal(work["event_id"]) for work in managed_works
            ]
        document = config_document(
            database,
            external=external,
            review_permissions=(
                REVIEW_PERMISSIONS if permissions is None else permissions
            ),
            review_script=review_script,
        )
        process = self.start(document=document, db_path=database)
        info = process.request("app.info")["result"]
        return process, info, managed_works, legacy_works

    def confirm(self, process, info) -> None:
        reply = process.request(
            "app.confirm_destination",
            {
                "session_id": info["session_id"],
                "destination_id": info["destination"]["destination_id"],
            },
        )
        self.assertTrue(reply["ok"], reply)

    def generate(self, process, info, work, *, confirmed: bool = True):
        return process.request(
            "review.generate",
            {
                "session_id": info["session_id"],
                "work_id": work["work_id"],
                "event_id": work["event_id"],
                "confirmed_destination_id": (
                    info["destination"]["destination_id"] if confirmed else None
                ),
            },
        )

    def resolve(self, process, info, candidate_id, version, decision):
        return process.request(
            "review.resolve",
            {
                "session_id": info["session_id"],
                "candidate_id": candidate_id,
                "artifact_version": version,
                "decision": decision,
            },
        )


class ReviewBridgeTests(ReviewBridgeCase):
    def test_work_list_and_capabilities(self) -> None:
        process, info, managed, legacy = self.prepare(managed=1, legacy=1)
        self.assertTrue(info["workspace"]["review"])
        self.assertIsNone(info["workspace"]["review_reason"])
        self.assertEqual(
            info["capabilities"]["review"],
            {
                "read": True,
                "generate": True,
                "approve": True,
                "reject": True,
                "result": True,
            },
        )
        reply = process.request(
            "review.work.list", {"session_id": info["session_id"]}
        )
        self.assertTrue(reply["ok"], reply)
        result = reply["result"]
        self.assertEqual(len(result["managed"]), 1)
        self.assertGreaterEqual(len(result["legacy"]), 1)
        managed_item = result["managed"][0]
        self.assertEqual(managed_item["work_id"], managed[0]["work_id"])
        self.assertEqual(managed_item["event_id"], managed[0]["event_id"])
        self.assertFalse(managed_item["blocked"])
        self.assertFalse(managed_item["rerun_registered"])
        # Only the existence of a rerun reference is public; the reference
        # value itself is never returned.
        self.assertNotIn("rerun_authorization_ref", managed_item)
        legacy_item = next(
            item
            for item in result["legacy"]
            if item["work_id"] == legacy[0]["work_id"]
        )
        self.assertEqual(legacy_item["work_id"], legacy[0]["work_id"])
        self.assertIn("kind", legacy_item)
        self.assertIn("created_at", legacy_item)

    def test_generate_detail_approve_and_result_recovery(self) -> None:
        process, info, works, _ = self.prepare(managed=1)
        work = works[0]
        self.confirm(process, info)
        generated = self.generate(process, info, work)
        self.assertTrue(generated["ok"], generated)
        view = generated["result"]
        self.assertEqual(view["state"], "pending")
        self.assertIsNotNone(view["candidate_id"])
        self.assertIsNotNone(view["interaction_id"])
        self.assertIsNone(view["receipt"])
        self.assertEqual(view["model_adapter"], "direct-http")

        listed = process.request(
            "review.candidates.list",
            {
                "session_id": info["session_id"],
                "work_id": work["work_id"],
            },
        )
        self.assertTrue(listed["ok"], listed)
        candidates = listed["result"]["candidates"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["candidate_id"], view["candidate_id"])
        self.assertEqual(candidate["artifact_version"], 1)
        self.assertEqual(candidate["work_generation"], 0)
        self.assertEqual(candidate["status"], "pending")
        self.assertNotIn("decision_ref", candidate)
        self.assertNotIn("attempt_ref", candidate)
        self.assertNotIn("authorization_id", json.dumps(candidate))

        detail_reply = process.request(
            "review.candidate.detail",
            {
                "session_id": info["session_id"],
                "candidate_id": candidate["candidate_id"],
                "artifact_version": 1,
            },
        )
        self.assertTrue(detail_reply["ok"], detail_reply)
        detail = detail_reply["result"]
        self.assertFalse(detail["redacted"])
        self.assertEqual(detail["current_state"], "not_applicable")
        self.assertTrue(detail["can_approve"])
        self.assertTrue(detail["can_reject"])
        self.assertIsNone(detail["eligibility_reason"])
        self.assertEqual(detail["proposal"]["claim"], "capability:lab-file-read")
        self.assertEqual(detail["proposal"]["operation"], "propose_belief")
        self.assertEqual(len(detail["diff"]), 6)
        self.assertTrue(all(row["changed"] for row in detail["diff"]))
        self.assertTrue(detail["sources"])
        for source in detail["sources"]:
            self.assertIn(
                source["availability"], ("available", "redacted", "unavailable")
            )
        wire = json.dumps(detail, ensure_ascii=False)
        for hidden in (
            "state_revision",
            "origin_group_id",
            "applicability_reason",
            "authorization_id",
            "request_hash",
            "prompt",
        ):
            self.assertNotIn(hidden, wire.lower())

        resolved = self.resolve(
            process, info, candidate["candidate_id"], 1, "approve"
        )
        self.assertTrue(resolved["ok"], resolved)
        committed = resolved["result"]
        self.assertEqual(committed["state"], "committed")
        self.assertIsNotNone(committed["receipt"])
        self.assertFalse(committed["redacted"])
        receipt = committed["receipt"]
        self.assertEqual(receipt["operation"], "propose_belief")
        self.assertNotIn("state_revision", receipt)
        self.assertEqual(len(receipt["record_refs"]), 1)

        # A second approve is idempotent: the same receipt and one write.
        again = self.resolve(
            process, info, candidate["candidate_id"], 1, "approve"
        )
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["result"]["state"], "committed")
        self.assertEqual(
            again["result"]["receipt"]["request_id"], receipt["request_id"]
        )
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM processed_commands"), 1)
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM record_versions WHERE kind = 'belief'"
            ),
            2,  # the Lab seed belief plus the committed candidate
        )

        recovered = process.request(
            "review.result",
            {
                "session_id": info["session_id"],
                "interaction_id": view["interaction_id"],
            },
        )
        self.assertTrue(recovered["ok"], recovered)
        self.assertEqual(recovered["result"]["state"], "committed")
        self.assertEqual(
            recovered["result"]["receipt"]["request_id"], receipt["request_id"]
        )

        # A committed work is not reviewed again.
        blocked = self.generate(process, info, work)
        self.assertFalse(blocked["ok"], blocked)
        self.assertEqual(blocked["error"]["code"], "WORK_ALREADY_RESOLVED")

    def test_reject_pauses_work_and_never_revives_the_candidate(self) -> None:
        process, info, works, _ = self.prepare(managed=1)
        work = works[0]
        self.confirm(process, info)
        generated = self.generate(process, info, work)["result"]
        candidate_id = generated["candidate_id"]

        rejected = self.resolve(process, info, candidate_id, 1, "reject")
        self.assertTrue(rejected["ok"], rejected)
        self.assertEqual(rejected["result"]["state"], "rejected")

        listed = process.request(
            "review.work.list", {"session_id": info["session_id"]}
        )["result"]
        item = listed["managed"][0]
        self.assertTrue(item["blocked"])
        self.assertFalse(item["rerun_registered"])

        candidates = process.request(
            "review.candidates.list",
            {
                "session_id": info["session_id"],
                "work_id": work["work_id"],
            },
        )["result"]["candidates"]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["status"], "rejected")

        # A blocked work without a rerun gate is denied before any model call.
        denied = self.generate(process, info, work)
        self.assertTrue(denied["ok"], denied)
        self.assertEqual(denied["result"]["state"], "denied")
        self.assertEqual(denied["result"]["detail"], "blocked_after_rejection")
        self.assertIsNone(denied["result"]["candidate_id"])

        # The rejected candidate can never be approved.
        old = self.resolve(process, info, candidate_id, 1, "approve")
        self.assertFalse(old["ok"], old)
        self.assertEqual(old["error"]["code"], "CANDIDATE_STALE")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM processed_commands"), 0)

    def test_wait_makes_no_candidate_and_completes_the_work(self) -> None:
        database = self.tmp_path / "wait.sqlite"
        work = seed_work(database, managed=True)
        document = config_document(
            database,
            review_permissions=REVIEW_PERMISSIONS,
            review_script=[{"operation": "wait", "reason": "合成 WAIT"}],
        )
        process = self.start(document=document, db_path=database)
        info = process.request("app.info")["result"]
        self.confirm(process, info)
        view = self.generate(process, info, work)["result"]
        self.assertEqual(view["state"], "waiting")
        self.assertIsNone(view["candidate_id"])
        self.assertIsNotNone(view["receipt"])
        self.assertEqual(view["receipt"]["operation"], "cognition.wait")
        self.assertEqual(
            process.request(
                "review.candidates.list",
                {"session_id": info["session_id"], "work_id": work["work_id"]},
            )["result"]["candidates"],
            [],
        )
        listed = process.request(
            "review.work.list", {"session_id": info["session_id"]}
        )["result"]
        self.assertEqual(listed["managed"], [])
        recovered = process.request(
            "review.result",
            {
                "session_id": info["session_id"],
                "interaction_id": view["interaction_id"],
            },
        )["result"]
        self.assertEqual(recovered["state"], "waiting")

    def test_legacy_is_read_only_and_feedback_is_rejected(self) -> None:
        process, info, managed, legacy = self.prepare(managed=1, legacy=1)
        self.confirm(process, info)
        legacy_work = legacy[0]
        before = self.counts()
        reply = self.generate(process, info, legacy_work)
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], "LEGACY_WORK_READ_ONLY")
        self.assertEqual(before, self.counts())

        feedback = process.request(
            "review.generate",
            {
                "session_id": info["session_id"],
                "work_id": managed[0]["work_id"],
                "event_id": managed[0]["event_id"],
                "feedback": "请改写候选",
            },
        )
        self.assertFalse(feedback["ok"], feedback)
        self.assertEqual(feedback["error"]["code"], "INVALID_PARAMS")

        poisoned = process.request(
            "review.generate",
            {
                "session_id": info["session_id"],
                "work_id": managed[0]["work_id"],
                "event_id": managed[0]["event_id"],
                "provider": "forged",
            },
        )
        self.assertFalse(poisoned["ok"], poisoned)
        self.assertEqual(poisoned["error"]["code"], "AUTHORITY_FIELD_REJECTED")

    def test_resolve_rejects_forged_authority_and_exact_version(self) -> None:
        process, info, works, _ = self.prepare(managed=1)
        self.confirm(process, info)
        candidate = self.generate(process, info, works[0])["result"]
        candidate_id = candidate["candidate_id"]
        before = self.counts()

        for field, value in (
            ("approval", {"candidate_id": candidate_id}),
            ("grant", "forged"),
            ("grant_id", "00000000-0000-4000-8000-000000000000"),
            ("candidate_body", {"claim": "forged"}),
            ("provider", "forged"),
            ("model", "forged"),
            ("decision_id", "forged"),
        ):
            reply = process.request(
                "review.resolve",
                {
                    "session_id": info["session_id"],
                    "candidate_id": candidate_id,
                    "artifact_version": 1,
                    "decision": "approve",
                    field: value,
                },
            )
            self.assertFalse(reply["ok"], field)
            self.assertEqual(
                reply["error"]["code"], "AUTHORITY_FIELD_REJECTED", field
            )
        self.assertEqual(before, self.counts())

        wrong_version = self.resolve(process, info, candidate_id, 2, "approve")
        self.assertFalse(wrong_version["ok"], wrong_version)
        self.assertEqual(
            wrong_version["error"]["code"], "CANDIDATE_VERSION_MISMATCH"
        )
        self.assertEqual(before, self.counts())

        bad_decision = self.resolve(process, info, candidate_id, 1, "maybe")
        self.assertFalse(bad_decision["ok"], bad_decision)
        self.assertEqual(bad_decision["error"]["code"], "INVALID_PARAMS")
        self.assertEqual(before, self.counts())

    def test_stale_work_generation_is_refused_without_a_write(self) -> None:
        process, info, works, _ = self.prepare(managed=1)
        work = works[0]
        self.confirm(process, info)
        candidate = self.generate(process, info, work)["result"]
        connection = sqlite3.connect(self.db_path)
        try:
            connection.execute(
                "UPDATE collab_work_control SET generation = generation + 1 "
                "WHERE namespace = ? AND work_id = ?",
                ("example.test.desktop.lab", work["work_id"]),
            )
            connection.commit()
        finally:
            connection.close()
        before = self.counts()
        reply = self.resolve(process, info, candidate["candidate_id"], 1, "approve")
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], "CANDIDATE_STALE")
        self.assertEqual(before, self.counts())

    def test_can_approve_is_not_a_permission(self) -> None:
        process, info, works, _ = self.prepare(
            managed=1,
            permissions=["work.scan", "experience.read", "review.generate"],
        )
        self.assertTrue(info["capabilities"]["review"]["read"])
        self.assertTrue(info["capabilities"]["review"]["generate"])
        self.assertFalse(info["capabilities"]["review"]["approve"])
        self.assertFalse(info["capabilities"]["review"]["reject"])
        self.assertFalse(info["capabilities"]["review"]["result"])
        self.confirm(process, info)
        candidate = self.generate(process, info, works[0])["result"]
        detail = process.request(
            "review.candidate.detail",
            {
                "session_id": info["session_id"],
                "candidate_id": candidate["candidate_id"],
                "artifact_version": 1,
            },
        )["result"]
        self.assertTrue(detail["can_approve"])  # content hint only
        before = self.counts()
        reply = self.resolve(
            process, info, candidate["candidate_id"], 1, "approve"
        )
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], "AUTHORIZATION_REQUIRED")
        self.assertEqual(before, self.counts())
        result = process.request(
            "review.result",
            {
                "session_id": info["session_id"],
                "interaction_id": candidate["interaction_id"],
            },
        )
        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error"]["code"], "AUTHORIZATION_REQUIRED")

    def test_confirmation_session_and_not_found(self) -> None:
        process, info, works, _ = self.prepare(managed=1)
        unconfirmed = self.generate(process, info, works[0], confirmed=False)
        self.assertFalse(unconfirmed["ok"], unconfirmed)
        self.assertEqual(unconfirmed["error"]["code"], "CONFIRMATION_REQUIRED")
        self.confirm(process, info)
        foreign_session = process.request(
            "review.work.list",
            {"session_id": "00000000-0000-4000-8000-000000000000"},
        )
        self.assertFalse(foreign_session["ok"], foreign_session)
        self.assertEqual(foreign_session["error"]["code"], "SESSION_MISMATCH")
        missing = process.request(
            "review.result",
            {
                "session_id": info["session_id"],
                "interaction_id": "00000000-0000-4000-8000-000000000000",
            },
        )
        self.assertFalse(missing["ok"], missing)
        self.assertEqual(missing["error"]["code"], "NOT_FOUND")

    def test_registered_rerun_is_still_gated_by_c07(self) -> None:
        database = self.tmp_path / "rerun.sqlite"
        work = seed_work(database, managed=True)
        proposal = review_proposal(work["event_id"])
        document = config_document(
            database,
            review_permissions=REVIEW_PERMISSIONS,
            review_script=[proposal, proposal],
        )
        process = self.start(document=document, db_path=database)
        info = process.request("app.info")["result"]
        self.confirm(process, info)
        first = self.generate(process, info, work)["result"]
        self.assertEqual(first["state"], "pending")
        rejected = self.resolve(process, info, first["candidate_id"], 1, "reject")
        self.assertEqual(rejected["result"]["state"], "rejected")

        grant_id = str(uuid.uuid4())
        store = CognitionStore.open(self.db_path)
        try:
            store.register_grant(
                {
                    "grant_id": grant_id,
                    "namespace": NAMESPACE,
                    "schema_version": "collab.v1",
                    "kind": "rerun",
                    "grant_version": 1,
                    "subject": HOST_INSTANCE,
                    "authorization_ref": "authorization:rerun:c12c-test",
                    "issued_at": utc_now(),
                    "expires_at": None,
                    "revoked_at": None,
                    "work_id": work["work_id"],
                    "generation": 1,
                }
            )
            store.authorize_rerun(
                namespace=NAMESPACE, work_id=work["work_id"], grant_id=grant_id
            )
        finally:
            store.close()

        listed = process.request(
            "review.work.list", {"session_id": info["session_id"]}
        )["result"]
        item = next(
            entry
            for entry in listed["managed"]
            if entry["work_id"] == work["work_id"]
        )
        self.assertTrue(item["blocked"])
        self.assertTrue(item["rerun_registered"])

        rerun = self.generate(process, info, work)
        self.assertTrue(rerun["ok"], rerun)
        self.assertEqual(rerun["result"]["state"], "pending")
        candidates = process.request(
            "review.candidates.list",
            {"session_id": info["session_id"], "work_id": work["work_id"]},
        )["result"]["candidates"]
        self.assertEqual([item["status"] for item in candidates], ["rejected", "pending"])
        self.assertEqual(candidates[1]["work_generation"], 1)
        # A second click sees the current pending candidate and does not
        # generate another one.
        duplicate = self.generate(process, info, work)
        self.assertFalse(duplicate["ok"], duplicate)
        self.assertEqual(duplicate["error"]["code"], "CANDIDATE_EXISTS")

    def test_rerun_reference_is_not_a_licence_after_revocation(self) -> None:
        database = self.tmp_path / "rerun-revoked.sqlite"
        work = seed_work(database, managed=True)
        document = config_document(
            database,
            review_permissions=REVIEW_PERMISSIONS,
            review_script=[review_proposal(work["event_id"])],
        )
        process = self.start(document=document, db_path=database)
        info = process.request("app.info")["result"]
        self.confirm(process, info)
        first = self.generate(process, info, work)["result"]
        self.resolve(process, info, first["candidate_id"], 1, "reject")

        grant_id = str(uuid.uuid4())
        store = CognitionStore.open(self.db_path)
        try:
            store.register_grant(
                {
                    "grant_id": grant_id,
                    "namespace": NAMESPACE,
                    "schema_version": "collab.v1",
                    "kind": "rerun",
                    "grant_version": 1,
                    "subject": HOST_INSTANCE,
                    "authorization_ref": "authorization:rerun:c12c-test",
                    "issued_at": utc_now(),
                    "expires_at": None,
                    "revoked_at": None,
                    "work_id": work["work_id"],
                    "generation": 1,
                }
            )
            store.authorize_rerun(
                namespace=NAMESPACE, work_id=work["work_id"], grant_id=grant_id
            )
            store.revoke_grant(namespace=NAMESPACE, grant_id=grant_id)
        finally:
            store.close()

        listed = process.request(
            "review.work.list", {"session_id": info["session_id"]}
        )["result"]
        item = next(
            entry
            for entry in listed["managed"]
            if entry["work_id"] == work["work_id"]
        )
        self.assertTrue(item["rerun_registered"])
        before = self.counts()
        denied = self.generate(process, info, work)
        self.assertTrue(denied["ok"], denied)
        self.assertEqual(denied["result"]["state"], "denied")
        self.assertEqual(denied["result"]["detail"], "blocked_after_rejection")
        self.assertEqual(before, self.counts())

    def test_deletion_barrier_hides_candidate_and_blocks_actions(self) -> None:
        process, info, works, _ = self.prepare(managed=1)
        self.confirm(process, info)
        candidate = self.generate(process, info, works[0])["result"]
        # The deletion barrier is a formal Store operation, not a UI read.
        store = CognitionStore.open(self.db_path)
        try:
            store.delete_experience(
                namespace=NAMESPACE,
                ref_type="collab_artifact",
                ref_id=candidate["candidate_id"],
                ref_version=1,
                authorization_ref="authorization:delete:c12c-test",
            )
        finally:
            store.close()

        listed = process.request(
            "review.candidates.list",
            {"session_id": info["session_id"], "work_id": works[0]["work_id"]},
        )
        self.assertEqual(listed["result"]["candidates"], [])
        detail = process.request(
            "review.candidate.detail",
            {
                "session_id": info["session_id"],
                "candidate_id": candidate["candidate_id"],
                "artifact_version": 1,
            },
        )
        self.assertTrue(detail["ok"], detail)
        self.assertTrue(detail["result"]["redacted"])
        self.assertIsNone(detail["result"]["proposal"])
        self.assertFalse(detail["result"]["can_approve"])
        self.assertFalse(detail["result"]["can_reject"])
        self.assertEqual(
            detail["result"]["detail"], "candidate_redacted"
        )
        before = self.counts()
        reply = self.resolve(
            process, info, candidate["candidate_id"], 1, "approve"
        )
        self.assertFalse(reply["ok"], reply)
        self.assertEqual(reply["error"]["code"], "CANDIDATE_STALE")
        self.assertEqual(before, self.counts())

    def test_double_resolve_writes_once(self) -> None:
        process, info, works, _ = self.prepare(managed=1)
        self.confirm(process, info)
        candidate = self.generate(process, info, works[0])["result"]
        params = {
            "session_id": info["session_id"],
            "candidate_id": candidate["candidate_id"],
            "artifact_version": 1,
            "decision": "approve",
        }
        first_id = process.send_request("review.resolve", params)
        second_id = process.send_request("review.resolve", params)
        first = process.wait_for(first_id)
        second = process.wait_for(second_id)
        replies = [first, second]
        self.assertTrue(any(reply["ok"] for reply in replies), replies)
        for reply in replies:
            if reply["ok"]:
                self.assertEqual(reply["result"]["state"], "committed")
            else:
                self.assertEqual(reply["error"]["code"], "BUSY")
        self.assertEqual(self.scalar("SELECT COUNT(*) FROM processed_commands"), 1)
        self.assertEqual(
            self.scalar(
                "SELECT COUNT(*) FROM collab_artifacts WHERE status = 'committed'"
            ),
            1,
        )


class ReviewErrorMappingTests(unittest.TestCase):
    """The bridge's public labels for C07 failures, including unknown results."""

    def test_known_failures_are_definite(self) -> None:
        code, _, unknown = pcs_bridge._map_review_error(
            AuthorizationRequired("missing"), default_unknown=True
        )
        self.assertEqual(code, "AUTHORIZATION_REQUIRED")
        self.assertFalse(unknown)

        code, _, unknown = pcs_bridge._map_review_error(
            StateRevisionConflict("moved"), default_unknown=True
        )
        self.assertEqual(code, "REVISION_CONFLICT")
        self.assertFalse(unknown)

        code, _, unknown = pcs_bridge._map_review_error(
            InvalidInput("bad"), default_unknown=True
        )
        self.assertEqual(code, "INVALID_PARAMS")
        self.assertFalse(unknown)

    def test_unexpected_failures_are_unknown_only_when_a_write_is_possible(self) -> None:
        code, _, unknown = pcs_bridge._map_review_error(
            RuntimeError("boom"), default_unknown=True
        )
        self.assertEqual(code, "INTERNAL_ERROR")
        self.assertTrue(unknown)

        code, _, unknown = pcs_bridge._map_review_error(
            RuntimeError("boom"), default_unknown=False
        )
        self.assertEqual(code, "INTERNAL_ERROR")
        self.assertFalse(unknown)


if __name__ == "__main__":
    unittest.main()
