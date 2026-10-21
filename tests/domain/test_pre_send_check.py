"""PCS2-010-P0 C / C2: pre-send live-basis checks and deletion invalidation.

The adapter must refuse the next HTTP try *before* reserving an attempt when a
source was deleted or ``privacy_epoch`` moved.  The check runs on the first
request, on a transport retry and on a format-repair request.  The check and the
socket write are not atomic; these tests only assert the documented promise
(deleted before the check point never leaves the process).

C2 then proves the stronger deletion property: after a projected body was sent
once through a loopback mock, deleting its source and running ``cleanup`` makes
the Context copy and both attempt bodies unreadable, and a whole-database TEXT
scan cannot find the body or its summary.
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

from model_support import ModelPortTestCase  # noqa: E402

from cognition_support import NS, all_text_cells  # noqa: E402

from quintessence_pcs.foundation.errors import Redacted  # noqa: E402
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime.model_port import (  # noqa: E402
    ModelAdapterError,
    ModelTransportError,
)
from quintessence_pcs.runtime.step import MODEL_FAILURE_CODE  # noqa: E402

RECORDED_AT = "2026-10-17T00:00:00Z"


def wait_text(reason: str = "pre-send check") -> str:
    return canonical_json_text({"operation": "wait", "reason": reason})


class PreSendCheckTests(ModelPortTestCase):
    # ------------------------------------------------------------- helpers

    def _fact_and_context(self, *, marker: str | None = None):
        fact = self.ingest(
            event_type="tool.path_probe_dev.scan",
            source_event_id=f"fact-{uuid.uuid4().hex}",
            payload={
                "summary": (
                    "长中文路径扫描失败"
                    if marker is None
                    else f"长中文路径扫描失败：{marker}"
                ),
                "receipt": "回执未定位失败原因",
            },
        )
        trigger = self.ingest(
            source_kind="user_report",
            event_type="exp01.question",
            source_event_id=f"question-{uuid.uuid4().hex}",
            payload={"question": "可以放心交给它吗？", "synthetic_fixture": True},
        )
        context = self.contexts.build_context(
            namespace=NS,
            trigger_ref=self.event_ref(trigger.event_id),
            adapter_kind="direct-http",
        )
        return fact, trigger, context

    def _attempt_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM model_attempts"
            ).fetchone()[0]
        )

    def _propose(self, context):
        with self.model.bind_run(namespace=NS, context=context):
            return self.model.propose(
                rendered_input=context.rendered_input,
                references=context.available_references,
            )

    def _wait_text(self, reason: str = "pre-send check") -> str:
        return wait_text(reason)

    def _count_checks(self) -> dict[str, int]:
        calls = {"checks": 0}
        real_check = self.model._require_send_basis

        def check(namespace, context):
            calls["checks"] += 1
            return real_check(namespace, context)

        self.model._require_send_basis = check
        return calls

    # --------------------------------------------------------------- cases

    def test_first_send_is_refused_when_a_source_was_deleted_after_build(self) -> None:
        fact, _trigger, context = self._fact_and_context()
        self.delete_event(fact.event_id)
        before = self._attempt_count()

        with self.assertRaises(ModelAdapterError) as caught:
            self._propose(context)
        self.assertIn("pre-send check refused", str(caught.exception))
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(before, self._attempt_count())
        self.assertEqual(0, self.store.count_model_attempts(self.auth.authorization_id))

    def test_every_transport_try_is_checked_even_when_it_succeeds(self) -> None:
        _fact, _trigger, context = self._fact_and_context()
        self.provider.enqueue(self.response({"error": "boom"}, status=500))
        self.provider.enqueue(self.response(self.completion(self._wait_text())))
        checks = self._count_checks()

        returned = self._propose(context)

        self.assertIn("operation", returned)
        self.assertEqual(2, self.provider.hits)
        self.assertEqual(2, self._attempt_count())
        self.assertEqual(2, checks["checks"], "one check before each HTTP try")

    def test_format_repair_request_is_checked_again(self) -> None:
        fact = self.ingest(
            event_type="tool.path_probe_dev.scan",
            source_event_id="repair-fact",
            payload={"summary": "长中文路径扫描失败"},
        )
        trigger = self.ingest(
            source_kind="user_report",
            event_type="exp01.question",
            source_event_id="repair-question",
            payload={"question": "可以放心交给它吗？"},
        )
        self.enqueue_content("this is not json")
        self.enqueue_json(self.completion(self._wait_text("repaired")))
        checks = self._count_checks()

        result = self.runtime.run_step(namespace=NS, trigger_event_id=trigger.event_id)

        self.assertEqual("checked_no_belief", result.outcome, result.error_message)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(1, result.repairs)
        self.assertEqual(2, self.provider.hits)
        self.assertEqual(2, self._attempt_count())
        self.assertEqual(2, checks["checks"])

    def test_source_deleted_before_retry_check_is_never_sent_twice(self) -> None:
        fact, trigger, _context = self._fact_and_context()
        self.provider.enqueue(self.response({"error": "boom"}, status=500))
        self.provider.enqueue(self.response(self.completion(self._wait_text("must not be sent"))))
        checks = self._count_checks()

        real_send = self.model._send
        sends = {"count": 0}

        def send_then_delete(body_text, *, timeout, api_key):
            sends["count"] += 1
            if sends["count"] == 1:
                # The deletion commits *after* the first check and before the
                # retry check: the window the card documents.
                self.delete_event(fact.event_id)
            return real_send(body_text, timeout=timeout, api_key=api_key)

        self.model._send = send_then_delete
        result = self.runtime.run_step(namespace=NS, trigger_event_id=trigger.event_id)

        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertIn("pre-send check refused", result.error_message or "")
        self.assertEqual(1, self.provider.hits, "the retry must not be sent")
        self.assertEqual(1, self._attempt_count())
        self.assertEqual(2, checks["checks"])
        self.assertEqual(1, sends["count"])

    def test_privacy_epoch_move_blocks_the_send_before_reserve(self) -> None:
        _fact, _trigger, context = self._fact_and_context()
        with self.store.transaction():
            unrelated = self.store.append_record_version(
                namespace=NS,
                record_id=str(uuid.uuid4()),
                record_version=1,
                kind="Observation",
                body={"unrelated": True},
                status="active",
                schema_version="1",
                privacy_class="private",
                recorded_at=RECORDED_AT,
            )
        self.delete_record(unrelated.record_id)

        with self.assertRaises(ModelAdapterError) as caught:
            self._propose(context)
        self.assertIn("privacy_epoch moved", str(caught.exception))
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(0, self._attempt_count())

    def test_cleared_context_payload_blocks_the_send_before_reserve(self) -> None:
        fact, _trigger, context = self._fact_and_context()
        self.delete_event(fact.event_id)
        self.store.cleanup_deletions()

        with self.assertRaises(ModelAdapterError) as caught:
            self._propose(context)
        self.assertIn("ContextStale", str(caught.exception))
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(0, self._attempt_count())

    def test_a_stale_check_never_becomes_a_retryable_transport_failure(self) -> None:
        fact, _trigger, context = self._fact_and_context()
        self.delete_event(fact.event_id)
        with self.assertRaises(ModelAdapterError) as caught:
            self._propose(context)
        self.assertNotIsInstance(caught.exception, ModelTransportError)
        self.assertEqual(0, self.provider.hits)


class DeletedProjectionInvalidationTests(ModelPortTestCase):
    def _projected_fact(self, context, event_id: str) -> dict:
        document = json.loads(context.rendered_input)
        return next(
            item
            for item in document["facts"]
            if item["ref"]["event_id"] == event_id
        )

    def test_deleted_projected_source_invalidates_context_and_attempt_copies(self) -> None:
        marker = f"PROJECTED-BODY-{uuid.uuid4().hex}"
        fact = self.ingest(
            event_type="tool.path_probe_dev.scan",
            source_event_id="invalidation-fact",
            payload={
                "summary": f"长中文路径扫描失败：{marker}",
                "receipt": "回执未定位失败原因",
            },
        )
        trigger = self.ingest(
            source_kind="user_report",
            event_type="exp01.question",
            source_event_id="invalidation-question",
            payload={"question": "可以放心交给它吗？"},
        )
        # The scripted provider echoes the private excerpt back, so the
        # response body is a real second copy that must also be cleared.
        self.enqueue_json(
            self.completion(wait_text(f"echo {marker}"))
        )

        result, context = self.run_and_context(trigger.event_id)

        self.assertEqual("checked_no_belief", result.outcome, result.error_message)
        self.assertIn(marker, context.rendered_input)
        projected = self._projected_fact(context, fact.event_id)
        projected_content = projected["content"]
        self.assertIn(marker, projected_content)
        attempt = self.attempts_for(context)[0]
        request_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="request"
        )
        response_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="response"
        )
        self.assertIn(marker, request_text)
        self.assertIn(marker, response_text)
        self.assertIsNotNone(attempt.request_payload_ref)
        self.assertIsNotNone(attempt.response_payload_ref)

        self.delete_event(fact.event_id)
        self.store.cleanup_deletions()

        # 1. The stored Context copy is gone.
        with self.assertRaises(Redacted):
            self.contexts.load_context(context.context_id)
        context_payload = self.store.connection.execute(
            "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
            (context.rendered_input_payload_ref,),
        ).fetchone()
        self.assertEqual("", context_payload["body_json"])
        snapshot = self.store.connection.execute(
            "SELECT content_hash FROM context_snapshots WHERE context_id = ?",
            (context.context_id,),
        ).fetchone()
        self.assertEqual(context.content_hash, snapshot["content_hash"])

        # 2. Both attempt bodies are gone.
        with self.assertRaises(Redacted):
            self.store.load_model_attempt_payload(attempt.attempt_id, kind="request")
        with self.assertRaises(Redacted):
            self.store.load_model_attempt_payload(attempt.attempt_id, kind="response")
        for payload_ref in (
            attempt.request_payload_ref,
            attempt.response_payload_ref,
        ):
            row = self.store.connection.execute(
                "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
                (payload_ref,),
            ).fetchone()
            self.assertEqual("", row["body_json"])

        # 3. No TEXT cell in the whole database still holds the body or summary.
        for location, value in all_text_cells(self.store.connection):
            self.assertNotIn(marker, value, location)
            self.assertNotIn(projected_content, value, location)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
