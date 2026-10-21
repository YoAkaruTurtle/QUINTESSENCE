"""PCS2-009 D group: real HTTP adapter through the bounded step, fully offline.

Every test here talks to a loopback ``http.server`` fixture.  No test resolves
an external host, reads a real credential or spends money.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from model_support import (  # noqa: E402
    NS,
    ModelPortTestCase,
    NoAuthorizationTestCase,
    all_text_cells,
    make_manifest,
)

from quintessence_pcs.foundation.jsonutil import sha256_hex  # noqa: E402
from quintessence_pcs.runtime import CognitionRuntime, DirectHttpModel  # noqa: E402
from quintessence_pcs.runtime.step import MODEL_FAILURE_CODE  # noqa: E402
from quintessence_pcs.store.model import (  # noqa: E402
    MODEL_ATTEMPT_FAILED,
    MODEL_ATTEMPT_MALFORMED,
    MODEL_ATTEMPT_OK,
    MODEL_ATTEMPT_RESERVED,
    MODEL_ATTEMPT_TIMEOUT,
)


def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield key
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


class SuccessTests(ModelPortTestCase):
    def test_success_records_exact_bytes_usage_and_commits(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        request_seen = self.provider.request_event
        self.enqueue_proposal(event.event_id)

        before_revision = self.revision()
        result, context = self.run_and_context(event.event_id)

        self.assertEqual("committed", result.outcome, result.error_message)
        self.assertEqual(1, result.model_calls)
        self.assertEqual(0, result.repairs)
        self.assertEqual(1, self.provider.hits)
        self.assertIsNotNone(context)
        assert context is not None
        self.assertEqual("direct-http", context.adapter_kind)
        self.assertTrue(context.model_input_complete)
        self.assertEqual(before_revision + 1, result.state_revision)

        attempts = self.attempts_for(context)
        self.assertEqual(1, len(attempts))
        attempt = attempts[0]
        self.assertEqual(1, attempt.attempt_no)
        self.assertEqual(MODEL_ATTEMPT_OK, attempt.outcome)
        self.assertEqual("finished", attempt.status)
        self.assertEqual("deepseek", attempt.provider)
        self.assertEqual("deepseek-flash", attempt.model_requested)
        self.assertEqual("deepseek-flash", attempt.model_returned)
        self.assertEqual("fp_synthetic_001", attempt.system_fingerprint)
        self.assertEqual("stop", attempt.finish_reason)
        self.assertTrue(attempt.usage_known)
        self.assertFalse(attempt.cost_known)
        self.assertIsNone(attempt.cost_estimate)
        self.assertEqual(
            {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
            attempt.usage,
        )
        self.assertEqual(self.manifest.manifest_hash, attempt.manifest_hash)
        self.assertEqual(self.manifest.model, attempt.model_requested)

        request_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="request"
        )
        response_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="response"
        )
        self.assertEqual("sha256:" + sha256_hex(request_text), attempt.request_hash)
        self.assertEqual("sha256:" + sha256_hex(response_text), attempt.response_hash)

        # The provider saw exactly the persisted bytes, not a reconstruction.
        self.assertTrue(request_seen.is_set())
        received = self.provider.requests[0]
        self.assertEqual(request_text.encode("utf-8"), received["body"])
        self.assertEqual("/chat/completions", received["path"])

        request = json.loads(request_text)
        self.assertEqual("deepseek-flash", request["model"])
        self.assertEqual(
            [{"role": "user", "content": context.rendered_input}],
            request["messages"],
        )
        self.assertEqual({"type": "disabled"}, request["thinking"])
        self.assertEqual(0, request["temperature"])
        self.assertEqual(512, request["max_tokens"])
        self.assertIs(False, request["stream"])
        keys = set(_walk_keys(request))
        self.assertNotIn("tools", keys)
        self.assertNotIn("functions", keys)
        self.assertNotIn("tool_calls", keys)

        authorization = received["headers"].get("Authorization", "")
        self.assertTrue(
            authorization.startswith("Bearer ")
            and authorization[len("Bearer ") :] == self.sentinel_key,
            "the provider did not receive the key from the environment",
        )
        # The key must never be in the database, including payload wrappers.
        self.assert_key_absent_from_store()

        progress = self.store.work_progress(result.work_id)
        self.assertIsNotNone(progress)
        self.assertEqual("committed", progress.outcome)

    def test_attempt_rows_are_never_deletable_or_rewritable(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.enqueue_proposal(event.event_id)
        result, context = self.run_and_context(event.event_id)
        self.assertEqual("committed", result.outcome, result.error_message)
        assert context is not None
        attempt = self.attempts_for(context)[0]

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "DELETE FROM model_attempts WHERE attempt_id = ?",
                (attempt.attempt_id,),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE model_attempts SET outcome = 'failed' WHERE attempt_id = ?",
                (attempt.attempt_id,),
            )


class RetryTests(ModelPortTestCase):
    def test_500_is_retried_once_as_two_independent_attempts(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.provider.enqueue(
            self.response({"error": {"message": "synthetic upstream"}}, status=500),
            self.response(self.completion(self.proposal_text(event.event_id))),
        )

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("committed", result.outcome, result.error_message)
        self.assertEqual(1, result.model_calls)
        self.assertEqual(0, result.repairs)
        self.assertEqual(2, self.provider.hits)
        assert context is not None
        first, second = self.attempts_for(context)
        self.assertEqual((1, 2), (first.attempt_no, second.attempt_no))
        self.assertEqual(MODEL_ATTEMPT_FAILED, first.outcome)
        self.assertEqual("HTTP_500", first.error_label)
        self.assertIsNone(first.finish_reason)
        self.assertEqual(MODEL_ATTEMPT_OK, second.outcome)
        self.assertEqual("stop", second.finish_reason)
        self.assertNotEqual(first.response_hash, second.response_hash)
        # A transport retry resends exactly the same request bytes.  Each row
        # still owns its own hash column and payload row; only a format repair
        # changes the request bytes (covered in RepairTests).
        self.assertEqual(first.request_hash, second.request_hash)
        self.assertNotEqual(first.request_payload_ref, second.request_payload_ref)
        first_body = self.store.load_model_attempt_payload(
            first.attempt_id, kind="response"
        )
        self.assertIn("synthetic upstream", first_body)

    def test_400_is_not_retried(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.provider.enqueue(
            self.response({"error": {"message": "bad request"}}, status=400)
        )

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertEqual(1, self.provider.hits)
        assert context is not None
        attempts = self.attempts_for(context)
        self.assertEqual(1, len(attempts))
        self.assertEqual(MODEL_ATTEMPT_FAILED, attempts[0].outcome)
        self.assertEqual("HTTP_400", attempts[0].error_label)
        self.assertIsNone(self.store.work_progress(result.work_id))


class RepairTests(ModelPortTestCase):
    def test_non_json_then_valid_is_repaired_once_with_new_request_bytes(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.provider.enqueue(
            self.response(self.completion("this is not JSON")),
            self.response(self.completion(self.proposal_text(event.event_id))),
        )

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("committed", result.outcome, result.error_message)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(1, result.repairs)
        self.assertEqual(2, self.provider.hits)
        assert context is not None
        first, second = self.attempts_for(context)
        self.assertEqual(MODEL_ATTEMPT_OK, first.outcome)
        self.assertEqual(MODEL_ATTEMPT_OK, second.outcome)
        self.assertNotEqual(first.request_hash, second.request_hash)
        first_text = self.store.load_model_attempt_payload(
            first.attempt_id, kind="request"
        )
        second_text = self.store.load_model_attempt_payload(
            second.attempt_id, kind="request"
        )
        first_doc = json.loads(first_text)
        second_doc = json.loads(second_text)
        self.assertEqual(1, len(first_doc["messages"]))
        self.assertEqual(3, len(second_doc["messages"]))
        self.assertEqual(
            ["user", "assistant", "user"],
            [message["role"] for message in second_doc["messages"]],
        )
        self.assertEqual("this is not JSON", second_doc["messages"][1]["content"])
        # No hidden system prompt or example was added by the repair path.
        self.assertNotIn(
            "system",
            [message["role"] for message in second_doc["messages"]],
        )

    def test_two_invalid_outputs_fail_without_commit_or_request_id(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.provider.enqueue(
            self.response(self.completion("not json one")),
            self.response(self.completion("not json two")),
        )
        revision_before = self.revision()
        versions_before = int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM record_versions"
            ).fetchone()[0]
        )
        commands_before = int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM processed_commands"
            ).fetchone()[0]
        )

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("failed", result.outcome)
        # A proposal that is not valid JSON is a validation failure, not a
        # transport failure; PCS2-008 already maps this to INVALID_SCHEMA and
        # the frozen contract does not add a new public code.
        self.assertEqual("INVALID_SCHEMA", result.error_code)
        self.assertEqual(2, result.model_calls)
        self.assertEqual(1, result.repairs)
        self.assertEqual(revision_before, self.revision())
        self.assertEqual(
            versions_before,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM record_versions"
                ).fetchone()[0]
            ),
        )
        self.assertEqual(
            commands_before,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM processed_commands"
                ).fetchone()[0]
            ),
        )
        self.assertIsNone(self.store.work_progress(result.work_id))
        self.assertEqual(
            1, len(self.store.unfinished_work_for_event(NS, event.event_id))
        )
        assert context is not None
        self.assertEqual(2, len(self.attempts_for(context)))


class TimeoutTests(ModelPortTestCase):
    def test_timeout_fails_without_commit(self) -> None:
        manifest = make_manifest(
            self.provider.url,
            api_key_env=self.ENV_NAME,
            max_wall_seconds=1,
        )
        self.reconfigure(manifest, max_wall_seconds=1)
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.provider.enqueue(
            self.response(
                self.completion(self.proposal_text(event.event_id)), delay=3.0
            )
        )

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertEqual(1, self.provider.hits)
        assert context is not None
        attempts = self.attempts_for(context)
        self.assertEqual(1, len(attempts))
        self.assertEqual(MODEL_ATTEMPT_TIMEOUT, attempts[0].outcome)
        self.assertEqual("TIMEOUT", attempts[0].error_label)
        self.assertIsNone(attempts[0].response_payload_ref)
        self.assertIsNone(self.store.work_progress(result.work_id))


class ResponseValidationTests(ModelPortTestCase):
    def _failed_attempt(self, event_id: str):
        result, context = self.run_and_context(event_id)
        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertEqual(1, self.provider.hits)
        assert context is not None
        attempts = self.attempts_for(context)
        self.assertEqual(1, len(attempts))
        self.assertEqual(MODEL_ATTEMPT_MALFORMED, attempts[0].outcome)
        self.assertIsNone(self.store.work_progress(result.work_id))
        return result, attempts[0]

    def test_finish_reason_length_is_not_a_proposal(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.enqueue_json(
            self.completion(
                self.proposal_text(event.event_id), finish_reason="length"
            )
        )
        _result, attempt = self._failed_attempt(event.event_id)
        self.assertEqual("FINISH_REASON_LENGTH", attempt.error_label)
        self.assertEqual("length", attempt.finish_reason)

    def test_empty_content_is_not_a_proposal(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.enqueue_json(self.completion(""))
        _result, attempt = self._failed_attempt(event.event_id)
        self.assertEqual("EMPTY_CONTENT", attempt.error_label)

    def test_tool_calls_are_a_violation_even_with_valid_content(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.enqueue_json(
            self.completion(
                self.proposal_text(event.event_id),
                tool_calls=[
                    {
                        "id": "call_synthetic",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            )
        )
        _result, attempt = self._failed_attempt(event.event_id)
        self.assertEqual("TOOL_CALLS_PRESENT", attempt.error_label)
        response_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="response"
        )
        self.assertIn("tool_calls", response_text)

    def test_reasoning_content_is_recorded_but_never_parsed(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        reasoning = '{"operation":"wait","reason":"draft reasoning must not be believed"}'
        self.enqueue_json(
            self.completion(
                self.proposal_text(event.event_id),
                reasoning_content=reasoning,
            )
        )

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("committed", result.outcome, result.error_message)
        assert context is not None
        attempt = self.attempts_for(context)[0]
        response_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="response"
        )
        stored = json.loads(response_text)
        self.assertEqual(
            reasoning, stored["choices"][0]["message"]["reasoning_content"]
        )
        assert result.belief_ref is not None
        belief = self.store.get_current(NS, result.belief_ref.record_id)
        self.assertEqual("capability:file-read", belief.body["claim"])


class UsageTests(ModelPortTestCase):
    def test_missing_usage_is_unknown_not_zero(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.enqueue_json(
            self.completion(
                self.proposal_text(event.event_id),
                usage=None,
                include_usage=False,
            )
        )

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("committed", result.outcome, result.error_message)
        assert context is not None
        attempt = self.attempts_for(context)[0]
        self.assertFalse(attempt.usage_known)
        self.assertIsNone(attempt.usage)
        self.assertFalse(attempt.cost_known)
        self.assertIsNone(attempt.cost_estimate)
        row = self.store.connection.execute(
            "SELECT usage_json, usage_known, cost_estimate, cost_known "
            "FROM model_attempts WHERE attempt_id = ?",
            (attempt.attempt_id,),
        ).fetchone()
        self.assertIsNone(row["usage_json"])
        self.assertEqual(0, row["usage_known"])
        self.assertIsNone(row["cost_estimate"])
        self.assertEqual(0, row["cost_known"])


class SecretTests(ModelPortTestCase):
    def test_missing_key_refuses_before_any_http(self) -> None:
        os.environ.pop(self.ENV_NAME, None)
        event = self.ingest(event_type="CapabilityObserved:file-read")

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertIn(self.ENV_NAME, result.error_message or "")
        self.assertEqual(0, self.provider.hits)
        self.assertIsNone(context)
        self.assertEqual(0, self.store.count_model_attempts(self.auth.authorization_id))

    def test_http_error_and_database_never_contain_the_key(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.provider.enqueue(self.response({"error": "synthetic"}, status=500))

        result, _context = self.run_and_context(event.event_id)

        self.assertEqual("failed", result.outcome)
        self.assertNotIn(self.sentinel_key, result.error_message or "")
        self.assert_key_absent_from_store()

    def test_echoed_key_is_refused_without_persisting_response(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.provider.enqueue(
            self.response(self.completion(f"echo {self.sentinel_key}"))
        )

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("failed", result.outcome)
        assert context is not None
        attempt = self.attempts_for(context)[0]
        self.assertEqual(MODEL_ATTEMPT_FAILED, attempt.outcome)
        self.assertEqual("KEY_ECHO_REDACTED", attempt.error_label)
        self.assertIsNone(attempt.response_payload_ref)
        self.assert_key_absent_from_store()


class BudgetCrashTests(ModelPortTestCase):
    def test_reserved_attempt_survives_child_kill_and_blocks_next_call(self) -> None:
        manifest = make_manifest(
            self.provider.url,
            api_key_env=self.ENV_NAME,
            max_calls=1,
            max_wall_seconds=60,
        )
        self.reconfigure(manifest, max_calls=1, max_wall_seconds=60)
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.provider.enqueue(
            self.response(
                self.completion(self.proposal_text(event.event_id)), delay=10.0
            )
        )
        self.provider.request_event.clear()
        payload = {
            "db": str(self.db_path),
            "manifest": self.manifest.to_dict(),
            "namespace": NS,
            "event_id": event.event_id,
        }
        env = os.environ.copy()
        env[self.ENV_NAME] = self.sentinel_key
        child = subprocess.Popen(
            [sys.executable, str(HERE / "model_child.py"), json.dumps(payload)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.addCleanup(self._kill_if_running, child)

        self.assertTrue(
            self.provider.request_event.wait(10),
            "the child never sent its reserved HTTP request",
        )
        self.assertEqual(1, self.provider.hits)
        child.kill()
        stdout, stderr = child.communicate(timeout=15)
        self.assertNotIn(self.sentinel_key.encode("utf-8"), stdout)
        self.assertNotIn(self.sentinel_key.encode("utf-8"), stderr)

        second = self.open_second_store()
        try:
            attempts = second.model_attempts_for_authorization(
                self.auth.authorization_id
            )
            self.assertEqual(1, len(attempts))
            reserved = attempts[0]
            self.assertEqual(MODEL_ATTEMPT_RESERVED, reserved.status)
            self.assertIsNone(reserved.outcome)
            self.assertIsNone(reserved.finished_at)
            self.assertIsNone(reserved.response_payload_ref)
            self.assertTrue(
                second.load_model_attempt_payload(
                    reserved.attempt_id, kind="request"
                )
            )

            runtime = CognitionRuntime(
                second, model=DirectHttpModel(second, self.manifest)
            )
            retried = runtime.run_step(
                namespace=NS, trigger_event_id=event.event_id
            )
            self.assertEqual("failed", retried.outcome)
            self.assertEqual(MODEL_FAILURE_CODE, retried.error_code)
            self.assertIn("BUDGET", (retried.error_message or "").upper())
            # The refused call must not have reached the provider.
            self.assertEqual(1, self.provider.hits)
            self.assertEqual(
                1, second.count_model_attempts(self.auth.authorization_id)
            )
            self.assertIsNone(second.work_progress(retried.work_id))
            self.assertEqual(
                1, len(second.unfinished_work_for_event(NS, event.event_id))
            )
        finally:
            second.close()

    @staticmethod
    def _kill_if_running(child: subprocess.Popen) -> None:
        if child.poll() is None:
            child.kill()
            try:
                child.communicate(timeout=5)
            except Exception:  # pragma: no cover - best-effort cleanup
                pass


class BudgetExhaustionTests(ModelPortTestCase):
    def test_preflight_marks_the_grant_exhausted_and_stops_before_http(self) -> None:
        manifest = make_manifest(
            self.provider.url,
            api_key_env=self.ENV_NAME,
            max_calls=1,
            max_wall_seconds=60,
        )
        self.reconfigure(manifest, max_calls=1, max_wall_seconds=60)
        first = self.ingest(event_type="CapabilityObserved:file-read")
        self.enqueue_proposal(first.event_id)
        result, _context = self.run_and_context(first.event_id)
        self.assertEqual("committed", result.outcome, result.error_message)
        self.assertEqual(1, self.provider.hits)

        second = self.ingest(event_type="CapabilityObserved:file-read")
        contexts_before = self.context_count()
        refused, _context = self.run_and_context(second.event_id)
        self.assertEqual("failed", refused.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, refused.error_code)
        self.assertEqual(1, self.provider.hits)
        self.assertEqual(contexts_before, self.context_count())
        self.assertEqual(
            "exhausted",
            self.store.model_authorization(self.auth.authorization_id).status,
        )
        self.assertEqual(
            1, self.store.count_model_attempts(self.auth.authorization_id)
        )


class AuthorizationRefusalTests(ModelPortTestCase):
    def _assert_refused_without_http(self, event_id: str) -> None:
        before = self.context_count()
        result, context = self.run_and_context(event_id)
        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(before, self.context_count())
        self.assertIsNone(context)

    def test_revoked_authorization_refuses(self) -> None:
        self.store.revoke_model_authorization(self.auth.authorization_id)
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self._assert_refused_without_http(event.event_id)

    def test_expired_authorization_refuses(self) -> None:
        self.store.revoke_model_authorization(self.auth.authorization_id)
        self.grant_authorization(
            granted_at="2026-01-01T00:00:00Z",
            expires_at="2026-01-02T00:00:00Z",
        )
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self._assert_refused_without_http(event.event_id)

    def test_manifest_hash_mismatch_refuses(self) -> None:
        self.store.revoke_model_authorization(self.auth.authorization_id)
        other = make_manifest(
            self.provider.url,
            api_key_env=self.ENV_NAME,
            max_wall_seconds=31,
        )
        self.grant_authorization(manifest=other)
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self._assert_refused_without_http(event.event_id)

    def test_model_mismatch_refuses(self) -> None:
        self.store.revoke_model_authorization(self.auth.authorization_id)
        other = make_manifest(
            self.provider.url,
            api_key_env=self.ENV_NAME,
            model="deepseek-v4-pro",
        )
        self.grant_authorization(manifest=other)
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self._assert_refused_without_http(event.event_id)


class NoAuthorizationTests(NoAuthorizationTestCase):
    def test_manifest_alone_is_not_permission(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        before = self.context_count()

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(before, self.context_count())
        self.assertIsNone(context)
        self.assertEqual(
            0,
            int(
                self.store.connection.execute(
                    "SELECT COUNT(*) FROM model_attempts"
                ).fetchone()[0]
            ),
        )


class RedirectTests(ModelPortTestCase):
    def test_302_is_not_followed(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        self.provider.enqueue(
            self.response(
                "",
                status=302,
                headers={"Location": self.provider.url + "/elsewhere"},
            )
        )

        result, context = self.run_and_context(event.event_id)

        self.assertEqual("failed", result.outcome)
        self.assertEqual(MODEL_FAILURE_CODE, result.error_code)
        self.assertEqual(1, self.provider.hits)
        self.assertEqual(0, self.provider.path_hits.get("/elsewhere", 0))
        assert context is not None
        attempt = self.attempts_for(context)[0]
        self.assertEqual(MODEL_ATTEMPT_FAILED, attempt.outcome)
        self.assertEqual("REDIRECT_REFUSED", attempt.error_label)


class DeletionTests(ModelPortTestCase):
    def test_deleting_a_rendered_source_clears_attempt_request_and_response(self) -> None:
        marker = "SECRET-" + "a" * 24 + "-" + "b" * 8
        evidence = self.ingest(event_type="CapabilityObserved:file-read")
        old = self.propose(
            body=self.belief_body(
                claim=f"capability:{marker}",
                evidence_links=[self.evidence_link(self.event_ref(evidence.event_id))],
            )
        )
        trigger = self.ingest(event_type="CapabilityObserved:file-read")
        link = {
            "ref": self.event_ref(trigger.event_id),
            "relation": "supports",
            "origin_group_id": f"event:{trigger.event_id}",
            "applicability_reason": "synthetic deletion fixture",
        }
        body = dict(self.store.get_version(NS, old.record_id, old.record_version).body)
        body["evidence_links"] = list(body["evidence_links"]) + [link]
        proposal = {
            "operation": "revise_belief",
            "target": self.record_ref(old),
            "body": body,
            "scope": {"scene": "test.scene"},
        }
        self.enqueue_json(self.completion(json.dumps(proposal)))

        result, context = self.run_and_context(trigger.event_id)

        self.assertEqual("committed", result.outcome, result.error_message)
        assert context is not None
        attempt = self.attempts_for(context)[0]
        request_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="request"
        )
        response_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="response"
        )
        self.assertIn(marker, request_text)
        self.assertIn(marker, response_text)

        self.delete_record(old.record_id)
        self.store.cleanup_deletions()

        from quintessence_pcs.foundation.errors import Redacted

        with self.assertRaises(Redacted):
            self.contexts.load_context(context.context_id)
        with self.assertRaises(Redacted):
            self.store.load_model_attempt_payload(
                attempt.attempt_id, kind="request"
            )
        with self.assertRaises(Redacted):
            self.store.load_model_attempt_payload(
                attempt.attempt_id, kind="response"
            )
        for ref in (
            attempt.request_payload_ref,
            attempt.response_payload_ref,
        ):
            self.assertIsNotNone(ref)
            row = self.store.connection.execute(
                "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
                (ref,),
            ).fetchone()
            self.assertEqual("", row["body_json"])
        for location, value in all_text_cells(self.store.connection):
            self.assertNotIn(marker, value, location)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
