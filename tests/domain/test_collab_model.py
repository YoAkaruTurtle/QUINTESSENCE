"""C09: DirectHttpModel protocol selection, the collab send gate and repair.

Every HTTP call goes to the loopback fake provider from ``model_support``.
These tests never contact a real provider.  The send gate is exercised with
real consult Contexts, real Profile/SelfView versions and real collaboration
inputs; tampered Contexts are built with ``dataclasses.replace`` only to prove
that unknown source accounting is refused before any reservation.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, NS_B  # noqa: E402
from model_support import ModelPortTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.views import self_view_record_id  # noqa: E402
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402
from quintessence_pcs.foundation.errors import ContextStale, InvalidInput, Redacted  # noqa: E402
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime import (  # noqa: E402
    COLLAB_MODEL_POLICY_VERSION,
    DIRECT_HTTP_POLICY_VERSION,
    ConsultSendPolicy,
    DirectHttpModel,
    ReadSetEntry,
    build_request_body,
    parse_collab_output,
    repair_feedback,
)
from quintessence_pcs.runtime.model_port import (  # noqa: E402
    ModelAdapterError,
    ModelConfigurationError,
    ModelNotAuthorizedError,
    ModelResponseError,
    ModelTransportError,
)
from quintessence_pcs.store.cognition import ContextSource  # noqa: E402
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:synthetic-c09-model"
ACCESS_REF = "authorization:access:c09-model"
CONSULT_BODY = "Synthetic consult body used by the C09 transport tests."
RECORDED_AT = "2026-10-17T00:00:00Z"


class CollabModelTests(ModelPortTestCase):
    # ------------------------------------------------------------- fixtures

    def policy(self, **overrides):
        values = {
            "namespace": NS,
            "host_instance": HOST,
            "access_ref": ACCESS_REF,
            "privacy_classes": frozenset({"private"}),
            "purposes": frozenset({"consult"}),
            "disclose": True,
        }
        values.update(overrides)
        return ConsultSendPolicy(**values)

    def collab_model(self, *, policy=None) -> DirectHttpModel:
        return DirectHttpModel(
            self.store,
            self.manifest,
            protocol="collab.v1",
            consult_policy=self.policy() if policy is None else policy,
        )

    def access(self, *, privacy_classes=("private",), purposes=("consult",)):
        return ExperienceAccess(
            namespace=NS,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset(privacy_classes),
            purposes=frozenset(purposes),
        )

    def register_consult_input(
        self,
        *,
        body: str = CONSULT_BODY,
        privacy_class: str = "private",
        version: int = 1,
    ):
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=body,
            privacy_class=privacy_class,
            retention="standard",
            content_version=version,
        )
        ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }
        self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
        )
        return row, ref

    def build_consult(self, refs=(), *, access=None, adapter_kind="direct-http"):
        return self.contexts.build_consult_context(
            namespace=NS,
            access=self.access() if access is None else access,
            input_refs=list(refs),
            adapter_kind=adapter_kind,
        )

    def output(self, **overrides) -> str:
        values = {"action": "none", "reply": "synthetic consult reply"}
        values.update(overrides)
        return canonical_json_text(values)

    def send(self, model: DirectHttpModel, context, *, feedback=None) -> str:
        with model.bind_run(namespace=NS, context=context):
            return model.propose(
                rendered_input=context.rendered_input,
                references=context.collab_source_refs,
                feedback=feedback,
            )

    def attempt_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM model_attempts"
            ).fetchone()[0]
        )

    def move_revision(self) -> None:
        """Advance state_revision without touching the Context's sources."""

        with self.store.transaction():
            self.store.append_record_version(
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

    # --------------------------------------------------------- protocol 17

    def test_protocol_selection_and_policy_requirement(self) -> None:
        legacy = DirectHttpModel(self.store, self.manifest)
        self.assertEqual(DIRECT_HTTP_POLICY_VERSION, legacy.policy_version)

        with self.assertRaises(ModelConfigurationError):
            DirectHttpModel(self.store, self.manifest, protocol="collab.v2")
        with self.assertRaises(ModelConfigurationError):
            DirectHttpModel(self.store, self.manifest, protocol="collab.v1")

        model = self.collab_model()
        self.assertEqual(COLLAB_MODEL_POLICY_VERSION, model.policy_version)

        review = self.build_context(adapter_kind="direct-http")
        with self.assertRaises(ModelConfigurationError):
            with model.bind_run(namespace=NS, context=review):
                self.fail("a review Context must not bind to collab.v1")

        _row, ref = self.register_consult_input()
        fake_consult = self.build_consult([ref], adapter_kind="fake-model")
        with self.assertRaises(ModelConfigurationError):
            with model.bind_run(namespace=NS, context=fake_consult):
                self.fail("a fake-model consult Context must not bind to direct-http")

    # --------------------------------------------------------- transport 18

    def test_collab_send_records_messages_attempt_and_policy_version(self) -> None:
        _row, ref = self.register_consult_input()
        context = self.build_consult([ref])
        model = self.collab_model()
        self.enqueue_content(self.output())

        returned = self.send(model, context)

        self.assertEqual(self.output(), returned)
        attempts = self.store.model_attempts_for_run(context.run_id)
        self.assertEqual(1, len(attempts))
        attempt = attempts[0]
        self.assertEqual(COLLAB_MODEL_POLICY_VERSION, attempt.policy_version)
        self.assertEqual("direct-http", attempt.adapter_kind)
        self.assertTrue(attempt.finished)

        request_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="request"
        )
        body = json.loads(request_text)
        self.assertEqual(
            ["system", "user"], [item["role"] for item in body["messages"]]
        )
        self.assertEqual(context.rendered_input, body["messages"][1]["content"])
        self.assertIn(CONSULT_BODY, request_text)
        self.assertEqual(0, body["temperature"])
        self.assertFalse(body["stream"])
        self.assertNotIn("tools", body)
        self.assertNotIn("functions", body)
        self.assertEqual(
            request_text, self.provider.requests[0]["body"].decode("utf-8")
        )
        response_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="response"
        )
        self.assertIn("synthetic consult reply", response_text)

    # ------------------------------------------------------- pre-send 19-26

    def test_send_gate_refuses_after_revision_moves(self) -> None:
        _row, ref = self.register_consult_input()
        context = self.build_consult([ref])
        model = self.collab_model()
        self.move_revision()
        before = self.attempt_count()

        with self.assertRaises(ModelAdapterError) as caught:
            self.send(model, context)

        self.assertIn("pre-send check refused", str(caught.exception))
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(before, self.attempt_count())
        self.assertEqual(0, self.store.count_model_attempts(self.auth.authorization_id))

    def test_send_policy_namespace_must_match_even_for_empty_input(self) -> None:
        context = self.build_consult([])
        model = self.collab_model(policy=self.policy(namespace=NS_B))

        with self.assertRaises(ModelNotAuthorizedError) as caught:
            self.send(model, context)

        self.assertIn("namespace", str(caught.exception))
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(0, self.attempt_count())

    def test_send_policy_purpose_and_disclosure_are_required(self) -> None:
        context = self.build_consult([])
        cases = [
            self.policy(purposes=frozenset({"review"})),
            self.policy(disclose=False),
        ]
        for policy in cases:
            with self.subTest(policy=policy):
                model = self.collab_model(policy=policy)
                with self.assertRaises(ModelNotAuthorizedError):
                    self.send(model, context)
                self.assertEqual(0, self.provider.hits)
                self.assertEqual(0, self.attempt_count())

    def test_profile_and_self_view_privacy_classes_are_checked(self) -> None:
        profile_context = self.build_consult([])
        profile_model = self.collab_model(
            policy=self.policy(privacy_classes=frozenset({"public-example"}))
        )
        with self.assertRaises(ModelNotAuthorizedError) as caught:
            self.send(profile_model, profile_context)
        self.assertIn("profile privacy class", str(caught.exception))
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(0, self.attempt_count())

        record_id = self_view_record_id(NS)
        current = self.store.get_current(NS, record_id)
        self.store.append_record_version(
            namespace=NS,
            record_id=record_id,
            record_version=current.record_version + 1,
            kind="self_view",
            body=dict(current.body),
            schema_version=current.schema_version,
            privacy_class="public-example",
        )
        self_view_context = self.build_consult([])
        self.assertIsNotNone(self_view_context.read_set)
        self_view_model = self.collab_model(
            policy=self.policy(privacy_classes=frozenset({"private"}))
        )
        with self.assertRaises(ModelNotAuthorizedError) as caught:
            self.send(self_view_model, self_view_context)
        self.assertIn("self-view privacy class", str(caught.exception))
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(0, self.attempt_count())

    def test_unreadable_collaboration_source_is_refused(self) -> None:
        _row, ref = self.register_consult_input(privacy_class="restricted")
        context = self.build_consult(
            [ref],
            access=self.access(privacy_classes=("private", "restricted")),
        )
        self.assertEqual((ref,), context.collab_source_refs)
        model = self.collab_model(
            policy=self.policy(privacy_classes=frozenset({"private"}))
        )

        with self.assertRaises(ModelNotAuthorizedError) as caught:
            self.send(model, context)

        self.assertIn("not readable", str(caught.exception))
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(0, self.attempt_count())

    def test_send_gate_uses_only_the_contexts_exact_record_versions(self) -> None:
        _row, ref = self.register_consult_input()
        # Make the record's *current* version stricter than the version the
        # Context will pin.  Appending a version moves records.current_version
        # but not the separate active-profile pointer, so the Context pins the
        # active v1 while get_current() would see the restricted v2.
        active = self.profiles.get_active_profile(NS)
        self.assertIsNotNone(active.ref)
        current = self.store.get_current(NS, active.ref.record_id)
        self.assertEqual(1, current.record_version)
        self.store.append_record_version(
            namespace=NS,
            record_id=active.ref.record_id,
            record_version=current.record_version + 1,
            kind="persona_profile",
            body=dict(current.body),
            schema_version=current.schema_version,
            privacy_class="restricted",
        )
        context = self.build_consult([ref])
        self.assertEqual(1, context.profile_ref.record_version)
        self.assertEqual(
            "restricted",
            self.store.get_current(NS, active.ref.record_id).privacy_class,
        )
        model = self.collab_model()

        expected = {
            (context.profile_ref.record_id, context.profile_ref.record_version)
        }
        for entry in context.read_set:
            if entry.usage == "current":
                expected.add((entry.ref.record_id, entry.ref.record_version))
        seen: list[tuple[str, int]] = []
        real_get_version = self.store.get_version

        def spy(namespace, record_id, record_version):
            seen.append((record_id, record_version))
            version = real_get_version(namespace, record_id, record_version)
            # A stricter *current* version must never influence this send: the
            # gate only reads the exact frozen version.
            if (
                record_id == context.profile_ref.record_id
                and record_version != context.profile_ref.record_version
            ):
                return dataclasses.replace(version, privacy_class="restricted")
            return version

        def current_profile(_namespace):
            raise AssertionError("the send gate must not read the current profile")

        def current_self_view(_namespace):
            raise AssertionError("the send gate must not read the current self view")

        self.enqueue_content(self.output())
        with patch.object(self.store, "get_version", new=spy), patch.object(
            self.profiles, "get_active_profile", new=current_profile
        ), patch.object(
            self.self_view, "get_self_view", new=current_self_view
        ):
            returned = self.send(model, context)

        self.assertEqual(self.output(), returned)
        self.assertEqual(expected, set(seen))
        self.assertEqual(1, self.provider.hits)

    def test_tampered_source_accounting_is_refused(self) -> None:
        _row, ref = self.register_consult_input()
        context = self.build_consult([ref])
        model = self.collab_model()

        missing_profile = dataclasses.replace(
            context,
            read_set=tuple(
                entry
                for entry in context.read_set
                if entry.ref != context.profile_ref
            ),
        )
        extra_entry = ReadSetEntry(
            RecordRef(
                namespace=NS,
                record_id=str(uuid.uuid4()),
                record_version=1,
            ),
            "historical",
        )
        extra_read = dataclasses.replace(
            context, read_set=context.read_set + (extra_entry,)
        )
        event_source = ContextSource(
            context_id=context.context_id,
            ref_kind="event",
            ref_id=str(uuid.uuid4()),
            ref_version=0,
        )
        extra_source = dataclasses.replace(
            context, sources=context.sources + (event_source,)
        )
        duplicate_source = dataclasses.replace(
            context, sources=context.sources + (context.sources[0],)
        )
        for tampered in (
            missing_profile,
            extra_read,
            extra_source,
            duplicate_source,
        ):
            with self.subTest(tampered=tampered):
                with self.assertRaises(ModelAdapterError):
                    self.send(model, tampered)
        self.assertEqual(0, self.provider.hits)
        self.assertEqual(0, self.attempt_count())

    def test_transport_retry_rechecks_the_send_gate(self) -> None:
        _row, ref = self.register_consult_input()
        context = self.build_consult([ref])
        model = self.collab_model()
        self.provider.enqueue(self.response({"error": "boom"}, status=500))
        self.provider.enqueue(self.response(self.completion(self.output())))
        real_send = model._send
        sends = {"count": 0}

        def send_then_move(body_text, *, timeout, api_key):
            sends["count"] += 1
            if sends["count"] == 1:
                try:
                    return real_send(body_text, timeout=timeout, api_key=api_key)
                finally:
                    self.move_revision()
            return real_send(body_text, timeout=timeout, api_key=api_key)

        model._send = send_then_move

        with self.assertRaises(ModelAdapterError) as caught:
            self.send(model, context)

        self.assertIn("pre-send check refused", str(caught.exception))
        self.assertEqual(1, self.provider.hits)
        self.assertEqual(1, sends["count"])
        self.assertEqual(1, self.attempt_count())

    # --------------------------------------------------------- repair 25-26

    def test_repair_request_reuses_the_same_run_and_records_messages(self) -> None:
        _row, ref = self.register_consult_input()
        context = self.build_consult([ref])
        model = self.collab_model()
        self.enqueue_content("this is not protocol JSON")
        self.enqueue_content(self.output(reply="repaired reply"))

        first = self.send(model, context)
        self.assertEqual("this is not protocol JSON", first)
        with self.assertRaises(InvalidInput) as caught:
            parse_collab_output(
                first,
                mode="consult",
                allowed_references=context.collab_source_refs,
            )
        feedback = repair_feedback(caught.exception)
        second = self.send(model, context, feedback=feedback)

        self.assertEqual(self.output(reply="repaired reply"), second)
        attempts = self.store.model_attempts_for_run(context.run_id)
        self.assertEqual(2, len(attempts))
        self.assertEqual(
            {COLLAB_MODEL_POLICY_VERSION},
            {attempt.policy_version for attempt in attempts},
        )
        self.assertEqual(2, self.provider.hits)
        request_text = self.store.load_model_attempt_payload(
            attempts[1].attempt_id, kind="request"
        )
        body = json.loads(request_text)
        self.assertEqual(
            ["system", "user", "assistant", "user"],
            [message["role"] for message in body["messages"]],
        )
        self.assertEqual(context.rendered_input, body["messages"][1]["content"])
        self.assertEqual(first, body["messages"][2]["content"])
        self.assertEqual(feedback, body["messages"][3]["content"])

    def test_repair_previous_output_is_bound_to_the_same_run(self) -> None:
        _row_a, ref_a = self.register_consult_input(body="run A input")
        context_a = self.build_consult([ref_a])
        _row_b, ref_b = self.register_consult_input(body="run B input")
        context_b = self.build_consult([ref_b])
        model = self.collab_model()
        self.enqueue_content(self.output(reply="A first"))
        self.send(model, context_a)

        with self.assertRaises(ModelConfigurationError):
            self.send(model, context_b, feedback="repair run B")
        self.assertEqual(1, self.provider.hits)
        self.assertEqual(1, self.attempt_count())

        self.enqueue_content(self.output(reply="A repaired"))
        repaired = self.send(
            model, context_a, feedback="Return valid collab.v1 JSON."
        )
        self.assertEqual(self.output(reply="A repaired"), repaired)
        attempts = self.store.model_attempts_for_run(context_a.run_id)
        self.assertEqual(2, len(attempts))
        body = json.loads(
            self.store.load_model_attempt_payload(
                attempts[1].attempt_id, kind="request"
            )
        )
        self.assertEqual(
            ["system", "user", "assistant", "user"],
            [message["role"] for message in body["messages"]],
        )

        self.move_revision()
        hits = self.provider.hits
        with self.assertRaises(ModelAdapterError):
            self.send(model, context_a, feedback="one more repair")
        self.assertEqual(hits, self.provider.hits)
        self.assertEqual(2, len(self.store.model_attempts_for_run(context_a.run_id)))

    # ------------------------------------------------- provider errors 27-28

    def test_collab_provider_errors_settle_the_attempt(self) -> None:
        context = self.build_consult([])
        model = self.collab_model()
        self.provider.enqueue(self.response({"error": "bad request"}, status=400))

        with self.assertRaises(ModelTransportError):
            self.send(model, context)
        attempts = self.store.model_attempts_for_run(context.run_id)
        self.assertEqual(1, len(attempts))
        self.assertEqual("failed", attempts[0].outcome)
        self.assertEqual("HTTP_400", attempts[0].error_label)
        self.assertEqual(COLLAB_MODEL_POLICY_VERSION, attempts[0].policy_version)
        self.assertTrue(attempts[0].finished)

        self.provider.enqueue(
            self.response(self.completion("truncated", finish_reason="length"))
        )
        with self.assertRaises(ModelResponseError):
            self.send(model, context)
        attempts = self.store.model_attempts_for_run(context.run_id)
        self.assertEqual(2, len(attempts))
        self.assertEqual("malformed", attempts[1].outcome)
        self.assertEqual("FINISH_REASON_LENGTH", attempts[1].error_label)

    def test_legacy_review_request_is_byte_identical(self) -> None:
        context = self.build_context(adapter_kind="direct-http")
        self.enqueue_json(
            self.completion(canonical_json_text(self.wait_proposal()))
        )
        legacy = DirectHttpModel(self.store, self.manifest)

        with legacy.bind_run(namespace=NS, context=context):
            returned = legacy.propose(
                rendered_input=context.rendered_input,
                references=context.available_references,
            )

        self.assertEqual(canonical_json_text(self.wait_proposal()), returned)
        attempts = self.store.model_attempts_for_run(context.run_id)
        self.assertEqual(1, len(attempts))
        expected = build_request_body(
            self.manifest,
            rendered_input=context.rendered_input,
            max_tokens=512,
        )
        self.assertEqual(
            expected,
            self.store.load_model_attempt_payload(
                attempts[0].attempt_id, kind="request"
            ),
        )
        self.assertEqual(DIRECT_HTTP_POLICY_VERSION, attempts[0].policy_version)
        body = json.loads(expected)
        self.assertEqual(["user"], [item["role"] for item in body["messages"]])
        self.assertEqual(
            context.rendered_input, body["messages"][0]["content"]
        )

    # ------------------------------------------------------- deletion 29-30

    def test_deleted_collab_input_bars_context_and_attempt_payloads(self) -> None:
        row, ref = self.register_consult_input()
        context = self.build_consult([ref])
        model = self.collab_model()
        self.enqueue_content(self.output(reply="synthetic protected reply"))

        self.send(model, context)
        attempts = self.store.model_attempts_for_run(context.run_id)
        self.assertEqual(1, len(attempts))
        attempt = attempts[0]
        request_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="request"
        )
        response_text = self.store.load_model_attempt_payload(
            attempt.attempt_id, kind="response"
        )
        self.assertIn(CONSULT_BODY, request_text)
        self.assertIn("synthetic protected reply", response_text)

        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="auth:test-delete",
        )
        with self.assertRaises(ContextStale):
            self.contexts.require_current_consult_context(
                context_id=context.context_id, namespace=NS
            )
        for kind in ("request", "response"):
            with self.assertRaises(Redacted):
                self.store.load_model_attempt_payload(
                    attempt.attempt_id, kind=kind
                )

        hits = self.provider.hits
        with self.assertRaises(ModelAdapterError):
            self.send(model, context)
        self.assertEqual(hits, self.provider.hits)
        self.assertEqual(1, self.attempt_count())

        self.store.cleanup_deletions()
        for payload_ref in (
            attempt.request_payload_ref,
            attempt.response_payload_ref,
        ):
            payload = self.store.connection.execute(
                "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
                (payload_ref,),
            ).fetchone()
            self.assertEqual("", payload["body_json"])
        for kind in ("request", "response"):
            with self.assertRaises(Redacted):
                self.store.load_model_attempt_payload(
                    attempt.attempt_id, kind=kind
                )
        self.assertEqual(1, len(self.store.model_attempts_for_run(context.run_id)))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
