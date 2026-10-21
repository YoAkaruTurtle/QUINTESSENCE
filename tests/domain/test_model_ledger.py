"""PCS2-009 C group: authorization grants, reservations and the attempt ledger.

These tests use a persisted PCS2-008 Context but never open a socket.  The
manifest points at an external URL only to exercise validation/hash code; no
request is made.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402
from model_support import make_manifest  # noqa: E402

from quintessence_pcs.foundation.errors import (  # noqa: E402
    CorruptStore,
    InvalidInput,
    RecordNotFound,
)
from quintessence_pcs.foundation.jsonutil import (  # noqa: E402
    canonical_json_text,
    sha256_hex,
)
from quintessence_pcs.store.model import (  # noqa: E402
    MODEL_ATTEMPT_FINISHED,
    MODEL_ATTEMPT_OK,
    MODEL_ATTEMPT_RESERVED,
    ModelAttemptConflict,
    ModelAuthorizationExpired,
    ModelAuthorizationMismatch,
    ModelBudgetExceeded,
    ModelNotAuthorized,
)


class ModelLedgerTestCase(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.manifest = make_manifest("https://api.deepseek.com")
        self.auth = self.grant()
        event = self.ingest()
        self.context = self.build_context(event.event_id)

    def grant(self, **overrides):
        values = {
            "namespace": NS,
            "authorization_ref": "用户原话（合成测试）：只允许绑定此 manifest 的调用",
            "provider": self.manifest.provider,
            "model": self.manifest.model,
            "manifest_hash": self.manifest.manifest_hash,
            "max_calls": 8,
            "max_output_tokens_per_call": 512,
            "max_wall_seconds": 30,
        }
        values.update(overrides)
        return self.store.grant_model_authorization(**values)

    def reserve(self, **overrides):
        values = {
            "run_id": self.context.run_id,
            "namespace": NS,
            "context_id": self.context.context_id,
            "authorization_id": self.auth.authorization_id,
            "provider": self.manifest.provider,
            "model_requested": self.manifest.model,
            "manifest_hash": self.manifest.manifest_hash,
            "adapter_kind": "direct-http",
            "policy_version": "pcs2-009.direct-http.v1",
            "request_body": '{"model":"deepseek-flash","messages":[]}',
            "manifest_max_calls": self.manifest.max_calls,
            "manifest_max_output_tokens_per_call": (
                self.manifest.max_output_tokens_per_call
            ),
            "manifest_max_wall_seconds": self.manifest.max_wall_seconds,
        }
        values.update(overrides)
        return self.store.reserve_model_attempt(**values)

    def finish_ok(self, attempt_id: str, **overrides):
        values = {
            "attempt_id": attempt_id,
            "outcome": MODEL_ATTEMPT_OK,
            "response_body": '{"choices":[{"finish_reason":"stop"}]}',
            "finish_reason": "stop",
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            "usage_known": True,
        }
        values.update(overrides)
        return self.store.finish_model_attempt(**values)


class AuthorizationTests(ModelLedgerTestCase):
    def test_grant_records_the_user_words_and_scope(self) -> None:
        self.assertEqual(NS, self.auth.namespace)
        self.assertEqual(self.manifest.provider, self.auth.provider)
        self.assertEqual(self.manifest.model, self.auth.model)
        self.assertEqual(self.manifest.manifest_hash, self.auth.manifest_hash)
        self.assertEqual("active", self.auth.status)
        self.assertIn("用户原话", self.auth.authorization_ref)
        self.assertEqual(
            self.auth.authorization_id,
            self.store.model_authorization(self.auth.authorization_id).authorization_id,
        )

    def test_revoke_is_idempotent_and_terminal(self) -> None:
        revoked = self.store.revoke_model_authorization(self.auth.authorization_id)
        self.assertEqual("revoked", revoked.status)
        again = self.store.revoke_model_authorization(self.auth.authorization_id)
        self.assertEqual("revoked", again.status)
        self.assertIsNone(
            self.store.active_model_authorization(
                namespace=NS,
                provider=self.manifest.provider,
                model=self.manifest.model,
                manifest_hash=self.manifest.manifest_hash,
            )
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE model_authorizations SET status = 'active' "
                "WHERE authorization_id = ?",
                (self.auth.authorization_id,),
            )

    def test_scope_fields_are_immutable_and_rows_are_undeletable(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE model_authorizations SET provider = 'other' "
                "WHERE authorization_id = ?",
                (self.auth.authorization_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM model_authorizations WHERE authorization_id = ?",
                (self.auth.authorization_id,),
            )

    def test_expiry_must_be_after_the_grant(self) -> None:
        with self.assertRaises(InvalidInput):
            self.grant(
                granted_at="2026-01-02T00:00:00Z",
                expires_at="2026-01-01T00:00:00Z",
            )

    def test_expired_grants_are_skipped_and_an_older_valid_one_wins(self) -> None:
        self.store.revoke_model_authorization(self.auth.authorization_id)
        older = self.grant(
            granted_at="2026-09-01T00:00:00Z",
            expires_at="2026-12-01T00:00:00Z",
        )
        self.grant(
            granted_at="2026-10-01T00:00:00Z",
            expires_at="2026-10-02T00:00:00Z",
        )
        selected = self.store.active_model_authorization(
            namespace=NS,
            provider=self.manifest.provider,
            model=self.manifest.model,
            manifest_hash=self.manifest.manifest_hash,
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(older.authorization_id, selected.authorization_id)

    def test_exhausted_status_is_reachable_and_terminal(self) -> None:
        marked = self.store.mark_model_authorization_exhausted(
            self.auth.authorization_id
        )
        self.assertEqual("exhausted", marked.status)
        again = self.store.mark_model_authorization_exhausted(
            self.auth.authorization_id
        )
        self.assertEqual("exhausted", again.status)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE model_authorizations SET status = 'active' "
                "WHERE authorization_id = ?",
                (self.auth.authorization_id,),
            )

    def test_status_can_only_leave_active_once(self) -> None:
        self.store.revoke_model_authorization(self.auth.authorization_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE model_authorizations SET status = 'exhausted' "
                "WHERE authorization_id = ?",
                (self.auth.authorization_id,),
            )


class ReservationTests(ModelLedgerTestCase):
    def test_reservation_charges_budget_and_increments_attempt_numbers(self) -> None:
        first = self.reserve()
        second = self.reserve()
        self.assertEqual(MODEL_ATTEMPT_RESERVED, first.attempt.status)
        self.assertEqual(1, first.attempt.attempt_no)
        self.assertEqual(2, second.attempt.attempt_no)
        self.assertEqual(2, self.store.count_model_attempts(self.auth.authorization_id))
        self.assertFalse(first.attempt.usage_known)
        self.assertFalse(first.attempt.cost_known)
        self.assertIsNone(first.attempt.outcome)
        self.assertIsNone(first.attempt.finished_at)

        grant = self.grant(max_calls=2)
        self.auth = grant
        # The two rows above are charged to the old grant; this new one starts
        # empty, then exhausts after two reservations.
        self.reserve()
        self.reserve()
        with self.assertRaises(ModelBudgetExceeded):
            self.reserve()
        self.assertEqual(2, self.store.count_model_attempts(grant.authorization_id))

    def test_the_stricter_of_manifest_and_authorization_wins(self) -> None:
        self.auth = self.grant(max_calls=2, max_output_tokens_per_call=256)
        first = self.reserve()
        self.assertEqual(2, first.effective_max_calls)
        self.assertEqual(256, first.effective_max_output_tokens)
        self.assertEqual(8, self.manifest.max_calls)
        self.assertEqual(512, self.manifest.max_output_tokens_per_call)

    def test_reservation_refuses_wrong_scope_and_inactive_grants(self) -> None:
        with self.assertRaises(ModelAuthorizationMismatch):
            self.reserve(provider="other")
        with self.assertRaises(ModelAuthorizationMismatch):
            self.reserve(model_requested="deepseek-v4-pro")
        with self.assertRaises(ModelAuthorizationMismatch):
            self.reserve(manifest_hash="sha256:" + "0" * 64)
        self.store.revoke_model_authorization(self.auth.authorization_id)
        with self.assertRaises(ModelNotAuthorized):
            self.reserve()
        self.assertEqual(0, self.store.count_model_attempts(self.auth.authorization_id))

    def test_expired_reservation_is_refused_without_a_row(self) -> None:
        self.store.revoke_model_authorization(self.auth.authorization_id)
        expired = self.grant(
            granted_at="2026-01-01T00:00:00Z",
            expires_at="2026-01-02T00:00:00Z",
        )
        with self.assertRaises(ModelAuthorizationExpired):
            self.reserve(authorization_id=expired.authorization_id)
        self.assertEqual(0, self.store.count_model_attempts(expired.authorization_id))

    def test_a_reserved_row_survives_reopen_and_still_counts(self) -> None:
        self.auth = self.grant(max_calls=1)
        self.reserve()
        second = self.open_second_store()
        try:
            self.assertEqual(
                1, second.count_model_attempts(self.auth.authorization_id)
            )
            with self.assertRaises(ModelBudgetExceeded):
                self.reserve()
        finally:
            second.close()

    def test_run_id_cannot_be_rebound_to_another_context(self) -> None:
        self.reserve()
        event = self.ingest()
        other = self.build_context(event.event_id)
        with self.assertRaises(ModelAttemptConflict):
            self.reserve(context_id=other.context_id)

    def test_missing_context_is_not_found(self) -> None:
        with self.assertRaises(RecordNotFound):
            self.reserve(context_id="0" * 8 + "-0000-0000-0000-" + "0" * 12)


class FinishTests(ModelLedgerTestCase):
    def test_finish_transitions_reserved_to_finished_once(self) -> None:
        reservation = self.reserve()
        finished = self.finish_ok(reservation.attempt.attempt_id)
        self.assertEqual(MODEL_ATTEMPT_FINISHED, finished.status)
        self.assertEqual(MODEL_ATTEMPT_OK, finished.outcome)
        self.assertEqual("stop", finished.finish_reason)
        self.assertTrue(finished.usage_known)
        self.assertEqual(
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            finished.usage,
        )
        self.assertIsNotNone(finished.response_payload_ref)
        self.assertEqual(
            "sha256:" + sha256_hex('{"choices":[{"finish_reason":"stop"}]}'),
            finished.response_hash,
        )
        self.assertEqual(
            '{"choices":[{"finish_reason":"stop"}]}',
            self.store.load_model_attempt_payload(
                finished.attempt_id, kind="response"
            ),
        )
        with self.assertRaises(ModelAttemptConflict):
            self.finish_ok(reservation.attempt.attempt_id)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE model_attempts SET outcome = 'failed' WHERE attempt_id = ?",
                (finished.attempt_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM model_attempts WHERE attempt_id = ?",
                (finished.attempt_id,),
            )

    def test_request_columns_are_immutable_while_reserved(self) -> None:
        reservation = self.reserve()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE model_attempts SET request_hash = ? WHERE attempt_id = ?",
                ("sha256:" + "0" * 64, reservation.attempt.attempt_id),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE model_attempts SET attempt_no = 99 WHERE attempt_id = ?",
                (reservation.attempt.attempt_id,),
            )

    def test_ok_requires_a_response_and_stop(self) -> None:
        reservation = self.reserve()
        with self.assertRaises(InvalidInput):
            self.store.finish_model_attempt(
                attempt_id=reservation.attempt.attempt_id,
                outcome=MODEL_ATTEMPT_OK,
                response_body=None,
                finish_reason="stop",
            )
        with self.assertRaises(InvalidInput):
            self.store.finish_model_attempt(
                attempt_id=reservation.attempt.attempt_id,
                outcome=MODEL_ATTEMPT_OK,
                response_body="{}",
                finish_reason="length",
            )
        still = self.store.model_attempts_for_run(self.context.run_id)[0]
        self.assertEqual(MODEL_ATTEMPT_RESERVED, still.status)
        finished = self.finish_ok(reservation.attempt.attempt_id)
        self.assertEqual(MODEL_ATTEMPT_OK, finished.outcome)

    def test_cost_unknown_is_not_written_as_zero(self) -> None:
        reservation = self.reserve()
        with self.assertRaises(InvalidInput):
            self.store.finish_model_attempt(
                attempt_id=reservation.attempt.attempt_id,
                outcome="failed",
                cost_known=True,
                cost_estimate=None,
                error_label="NETWORK_ERROR",
            )
        finished = self.store.finish_model_attempt(
            attempt_id=reservation.attempt.attempt_id,
            outcome="failed",
            error_label="NETWORK_ERROR",
        )
        self.assertFalse(finished.cost_known)
        self.assertIsNone(finished.cost_estimate)
        row = self.store.connection.execute(
            "SELECT cost_known, cost_estimate, usage_known, usage_json "
            "FROM model_attempts WHERE attempt_id = ?",
            (finished.attempt_id,),
        ).fetchone()
        self.assertEqual(0, row["cost_known"])
        self.assertIsNone(row["cost_estimate"])
        self.assertEqual(0, row["usage_known"])
        self.assertIsNone(row["usage_json"])

    def test_payload_tampering_is_detected(self) -> None:
        reservation = self.reserve()
        finished = self.finish_ok(reservation.attempt.attempt_id)
        tampered = canonical_json_text({"request_body": "tampered"})
        self.store.connection.execute(
            "UPDATE private_payloads SET body_json = ?, sha256 = ? "
            "WHERE payload_ref = ?",
            (tampered, sha256_hex(tampered), finished.request_payload_ref),
        )
        with self.assertRaises(CorruptStore):
            self.store.load_model_attempt_payload(
                finished.attempt_id, kind="request"
            )

    def test_unknown_attempt_is_not_found(self) -> None:
        with self.assertRaises(RecordNotFound):
            self.store.load_model_attempt_payload(
                "00000000-0000-0000-0000-000000000000", kind="request"
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
