"""UI-F01: the pure usage/trace arithmetic (no database, no model).

Every value below is derived from stored facts only: a token number exists when
the supplier reported all three usage keys, a repair exists only when a marker
names the attempt, and a same-body resend is a transport retry.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from quintessence_pcs.conversation.usage import (  # noqa: E402
    KIND_PRIMARY,
    KIND_REPAIR,
    KIND_TRANSPORT_RETRY,
    KIND_UNMARKED,
    AttemptRecord,
    aggregate,
    complete_from_aggregate,
    context_bytes,
    is_complete,
    latest_record,
    summarize_scope,
    token_values,
)

KNOWN = {"prompt_tokens": 128, "completion_tokens": 32, "total_tokens": 160}


def attempt(
    attempt_no: int = 1,
    *,
    attempt_id: str | None = None,
    context_id: str = "ctx-a",
    status: str = "finished",
    outcome: str | None = "ok",
    usage: dict | None = None,
    usage_known: bool = True,
    started_at: str = "2026-10-29T00:00:00.000000Z",
    finished_at: str | None = "2026-10-29T00:00:01.000000Z",
    request_hash: str = "sha256:aaa",
    stage_kind: str = "content",
    purpose: str = "content_initial",
    ordinal: int = 1,
) -> AttemptRecord:
    return AttemptRecord(
        attempt_id=attempt_id or f"attempt-{context_id}-{attempt_no}",
        attempt_no=attempt_no,
        turn_id="turn-1",
        context_id=context_id,
        stage_kind=stage_kind,
        purpose=purpose,
        ordinal=ordinal,
        status=status,
        outcome=outcome,
        usage=KNOWN if usage is None and usage_known else usage,
        usage_known=usage_known,
        started_at=started_at,
        finished_at=finished_at,
        request_hash=request_hash,
        finish_reason="stop" if outcome == "ok" else None,
        error_label=None,
    )


class AttemptMathTests(unittest.TestCase):
    def test_known_usage_requires_all_three_integers(self) -> None:
        self.assertEqual((128, 32, 160), token_values(attempt()))
        self.assertIsNone(
            token_values(attempt(usage={"prompt_tokens": 5, "total_tokens": 9}))
        )
        self.assertIsNone(token_values(attempt(usage_known=False, usage={})))
        self.assertIsNone(token_values(attempt(status="reserved", finished_at=None)))

    def test_a_reserved_attempt_is_open_not_zero(self) -> None:
        scope = aggregate([attempt(status="reserved", finished_at=None)])
        self.assertEqual(1, scope["attempts_open"])
        self.assertEqual(0, scope["attempts_known"])
        self.assertFalse(scope["tokens"]["complete"])
        self.assertIsNone(scope["tokens"]["total"])

    def test_partial_usage_is_incomplete_and_never_zero(self) -> None:
        scope = aggregate(
            [
                attempt(1),
                attempt(2, usage_known=False, usage={}),
            ]
        )
        self.assertEqual(1, scope["attempts_known"])
        self.assertEqual(1, scope["attempts_unknown"])
        self.assertFalse(scope["tokens"]["complete"])
        self.assertEqual(160, scope["tokens"]["total"])

    def test_complete_scope_sums_each_attempt_once(self) -> None:
        scope = aggregate([attempt(1), attempt(2)])
        self.assertTrue(scope["tokens"]["complete"])
        self.assertEqual(320, scope["tokens"]["total"])
        self.assertEqual(256, scope["tokens"]["prompt"])
        self.assertEqual(64, scope["tokens"]["completion"])

    def test_sql_aggregate_guards_unreadable_rows(self) -> None:
        tokens = complete_from_aggregate(
            {
                "attempts_total": 3,
                "attempts_known": 1,
                "attempts_open": 1,
                "attempts_unreadable": 1,
                "prompt_tokens": 128,
                "completion_tokens": 32,
                "total_tokens": 160,
            }
        )
        self.assertFalse(tokens["complete"])
        self.assertEqual(160, tokens["total"])

    def test_empty_scope_is_not_complete(self) -> None:
        self.assertFalse(is_complete(attempts=0, unknown=0, open_attempts=0, unreadable=0))
        scope = aggregate([])
        self.assertEqual(0, scope["attempts_total"])
        self.assertFalse(scope["tokens"]["complete"])

    def test_marker_is_the_only_repair_proof(self) -> None:
        records = [attempt(1), attempt(2, request_hash="sha256:bbb")]
        marked = summarize_scope(records, {records[1].attempt_id: "format"})
        self.assertEqual(KIND_PRIMARY, marked["attempts"][0]["kind"])
        self.assertFalse(marked["attempts"][0]["repair"])
        self.assertEqual(KIND_REPAIR, marked["attempts"][1]["kind"])
        self.assertTrue(marked["attempts"][1]["repair"])
        self.assertEqual("format", marked["attempts"][1]["repair_reason"])
        self.assertEqual(1, marked["counts"]["repairs"])

    def test_unmarked_second_request_is_unknown_not_repair(self) -> None:
        records = [attempt(1), attempt(2, request_hash="sha256:bbb")]
        scope = summarize_scope(records, {})
        self.assertEqual(KIND_UNMARKED, scope["attempts"][1]["kind"])
        self.assertIsNone(scope["attempts"][1]["repair"])
        self.assertEqual(0, scope["counts"]["repairs"])
        self.assertEqual(1, scope["counts"]["repair_unknown"])

    def test_same_body_resend_is_a_transport_retry(self) -> None:
        records = [attempt(1), attempt(2, request_hash="sha256:aaa")]
        scope = summarize_scope(records, {})
        # Both members of a same-body pair are resends; only the later one is
        # the retry, and neither is a repair.
        self.assertTrue(scope["attempts"][0]["resend_same_body"])
        self.assertEqual(KIND_PRIMARY, scope["attempts"][0]["kind"])
        self.assertEqual(KIND_TRANSPORT_RETRY, scope["attempts"][1]["kind"])
        self.assertFalse(scope["attempts"][1]["repair"])
        self.assertTrue(scope["attempts"][1]["resend_same_body"])
        self.assertEqual(1, scope["counts"]["transport_retries"])
        self.assertEqual(0, scope["counts"]["repairs"])

    def test_repair_with_internal_transport_retry_marks_one_repair(self) -> None:
        records = [
            attempt(1, request_hash="sha256:first"),
            attempt(2, request_hash="sha256:repair", outcome="failed"),
            attempt(3, request_hash="sha256:repair"),
        ]
        scope = summarize_scope(records, {records[2].attempt_id: "format"})
        self.assertEqual(
            [KIND_PRIMARY, KIND_TRANSPORT_RETRY, KIND_REPAIR],
            [item["kind"] for item in scope["attempts"]],
        )
        self.assertFalse(scope["attempts"][1]["repair"])
        self.assertTrue(scope["attempts"][1]["resend_same_body"])
        self.assertTrue(scope["attempts"][2]["repair"])
        self.assertEqual("format", scope["attempts"][2]["repair_reason"])
        self.assertEqual(1, scope["counts"]["repairs"])
        self.assertEqual(0, scope["counts"]["repair_unknown"])
        self.assertEqual(1, scope["counts"]["transport_retries"])

    def test_speed_uses_attempt_time_and_never_averages(self) -> None:
        scope = summarize_scope(
            [
                attempt(1, finished_at="2026-10-29T00:00:02.000000Z"),
                attempt(2, finished_at="2026-10-29T00:00:00.500000Z"),
            ],
            {},
        )
        speeds = {item["attempt_no"]: item["tokens_per_second"] for item in scope["attempts"]}
        self.assertAlmostEqual(16.0, speeds[1], places=3)  # 32 tokens / 2.0 s
        self.assertAlmostEqual(64.0, speeds[2], places=3)  # 32 tokens / 0.5 s
        self.assertIsNone(
            summarize_scope([attempt(finished_at="2026-10-29T00:00:00.000000Z")], {})[
                "attempts"
            ][0]["tokens_per_second"]
        )

    def test_latest_request_is_the_newest_attempt(self) -> None:
        records = [
            attempt(1, started_at="2026-10-29T00:00:00.000000Z"),
            attempt(2, started_at="2026-10-29T00:00:05.000000Z"),
        ]
        self.assertEqual(records[1].attempt_id, latest_record(records).attempt_id)
        self.assertEqual(records[1].attempt_id, summarize_scope(records, {})["latest_request"]["attempt_id"])

    def test_context_bytes_are_bytes_not_a_window_percentage(self) -> None:
        self.assertEqual(
            {"used_bytes": 900, "limit_bytes": 24576},
            context_bytes({"used_bytes": 900, "limit_bytes": 24576}),
        )
        self.assertIsNone(context_bytes({"used_bytes": 900}))
        self.assertIsNone(context_bytes(None))


if __name__ == "__main__":
    unittest.main()
