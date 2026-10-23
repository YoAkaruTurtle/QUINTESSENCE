"""C11: bounded consult orchestration, binding, evidence and history checks.

Every test uses a fresh migrated database, real PCS2-008 Contexts, real C05b
inputs, the real C10 query service and ``DirectHttpModel`` against the loopback
fake provider from ``model_support`` (so every reply artifact cites a real,
settled attempt).  No real provider, private persona asset or network egress is
used.

The checks follow the card's groups: orchestration, P2/A lifecycle, P3/P4/B
artifacts and exact attempts, isolation/N1, and the history sequence
(old Belief -> review correction -> restart -> a new consult uses the corrected
version).  C06/C07 semantics are exercised, not modified.
"""

from __future__ import annotations

import dataclasses
import json
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
for _path in (
    REPO_ROOT / "tests" / "domain",
    REPO_ROOT / "tests" / "store",
    REPO_ROOT / "src",
):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from cognition_support import NS, NS_B, CognitionTestCase  # noqa: E402
from model_support import ModelPortTestCase, make_manifest  # noqa: E402

from quintessence_pcs.collaboration import (  # noqa: E402
    FACADE_PERMISSIONS,
    ConsultFacade,
    ConsultHostAccess,
    ConsultRequest,
    ConsultResult,
    HostAccess,
    ReviewFacade,
)
from quintessence_pcs.domains.cognition import (  # noqa: E402
    BeliefCurrentQuery,
    CognitionQueryService,
    QueryAccess,
)
from quintessence_pcs.domains.cognition.views import SelfViewService  # noqa: E402
from quintessence_pcs.domains.persona.profiles import PersonaProfileService  # noqa: E402
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CorruptStore,
    InvalidInput,
    RecordNotFound,
)
from quintessence_pcs.foundation.jsonutil import canonical_json_text  # noqa: E402
from quintessence_pcs.runtime import (  # noqa: E402
    ConsultQueryMaterial,
    ConsultSendPolicy,
    ContextService,
    DirectHttpModel,
    ModelCallEvidence,
    ReadSetEntry,
)
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.cognition import ContextSource  # noqa: E402

HOST = "host:synthetic-c11"
ACCESS_REF = "authorization:access:c11"
REVIEW_HOST = "host:synthetic-c11-review"
REVIEW_ACCESS_REF = "authorization:access:c11-review"
REPLY_TEXT = "Synthetic consult reply."
QUERY_TEXT = "Synthetic consult question requiring a deterministic query."


# --------------------------------------------------------------------------
# test doubles
# --------------------------------------------------------------------------


class _ScriptedModel:
    """Queue-style review model with the ``fake-model`` adapter label."""

    adapter_kind = "fake-model"

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.feedback: list[str | None] = []

    def propose(self, *, rendered_input, references, feedback=None):
        self.calls += 1
        self.feedback.append(feedback)
        value = self._outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class _EvidenceProxy:
    """Delegate to the real adapter but tamper with the evidence result."""

    def __init__(self, inner, *, mode: str, foreign: dict | None = None):
        self.inner = inner
        self.mode = mode
        self.foreign = foreign
        self.adapter_kind = inner.adapter_kind
        self.policy_version = inner.policy_version

    def preflight(self, *, namespace: str) -> None:
        method = getattr(self.inner, "preflight", None)
        if method is not None:
            method(namespace=namespace)

    def bind_run(self, *, namespace, context):
        return self.inner.bind_run(namespace=namespace, context=context)

    def propose(self, **kwargs):
        return self.inner.propose(**kwargs)

    def last_attempt_evidence(self, *, run_id, context_id):
        evidence = self.inner.last_attempt_evidence(
            run_id=run_id, context_id=context_id
        )
        if self.mode == "missing":
            return None
        if self.mode == "content":
            return ModelCallEvidence(
                content=evidence.content + " tampered",
                attempt_ref=evidence.attempt_ref,
            )
        if self.mode == "foreign":
            return ModelCallEvidence(
                content=evidence.content, attempt_ref=self.foreign
            )
        return evidence


class _Crash(BaseException):
    """A BaseException models an interrupted process, not a handled failure."""


class _CrashProxy(_EvidenceProxy):
    def __init__(self, inner):
        super().__init__(inner, mode="normal")

    def propose(self, **kwargs):
        self.inner.propose(**kwargs)
        raise _Crash("simulated process interruption")


class _RevisionBumpQueries:
    """Run the real query, then publish a v2 that moves the state revision."""

    def __init__(self, inner, store, record_id: str):
        self.inner = inner
        self.store = store
        self.record_id = record_id

    def query(self, access, plan):
        result = self.inner.query(access, plan)
        current = self.store.get_current(NS, self.record_id)
        self.store.append_record_version(
            namespace=NS,
            record_id=self.record_id,
            record_version=current.record_version + 1,
            kind="belief",
            body=dict(current.body),
            status="supported",
            schema_version="1",
            privacy_class="private",
            scope=dict(current.scope or {}),
        )
        return result


# --------------------------------------------------------------------------
# tests
# --------------------------------------------------------------------------


class ConsultFacadeTests(ModelPortTestCase):
    """C11 orchestration, lifecycle, artifact, isolation and history checks."""

    # ------------------------------------------------------------- fixtures

    def setUp(self) -> None:
        super().setUp()
        # C11 can spend four logical generations per test scenario; raise the
        # synthetic authorization above the model-support default of eight
        # *attempts* so a multi-consult history test is not budget-bound.
        self.reconfigure(
            make_manifest(
                self.provider.url,
                api_key_env=self.ENV_NAME,
                max_calls=64,
            ),
            max_calls=64,
        )
        self.queries = CognitionQueryService(self.store, clock=self.clock)
        self.consult_model = self.make_consult_model()
        self.facade = self.make_facade()

    def policy(self, **overrides) -> ConsultSendPolicy:
        values = {
            "namespace": NS,
            "host_instance": HOST,
            "access_ref": ACCESS_REF,
            "privacy_classes": frozenset({"private"}),
            "purposes": frozenset({"consult", "reasoning"}),
            "disclose": True,
        }
        values.update(overrides)
        return ConsultSendPolicy(**values)

    def access(self, **overrides) -> ConsultHostAccess:
        values = {
            "namespace": NS,
            "host_instance": HOST,
            "access_ref": ACCESS_REF,
            "privacy_classes": frozenset({"private"}),
            "purposes": frozenset({"consult", "reasoning"}),
            "disclose": True,
        }
        values.update(overrides)
        return ConsultHostAccess(**values)

    def query_access(self) -> QueryAccess:
        return QueryAccess(
            namespace=NS,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"consult", "reasoning"}),
        )

    def make_consult_model(self, *, policy=None) -> DirectHttpModel:
        return DirectHttpModel(
            self.store,
            self.manifest,
            protocol="collab.v1",
            consult_policy=self.policy() if policy is None else policy,
        )

    def make_facade(self, *, model=None, queries=None) -> ConsultFacade:
        return ConsultFacade(
            self.store,
            context_service=self.contexts,
            model=self.consult_model if model is None else model,
            query_service=self.queries if queries is None else queries,
        )

    # --------------------------------------------------------------- helpers

    def register_input(self, body: str = QUERY_TEXT) -> dict:
        row = self.store.register_input(
            namespace=NS,
            origin_kind="user_statement",
            body=body,
            privacy_class="private",
            retention="standard",
            content_version=1,
        )
        return {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }

    @staticmethod
    def request(refs=(), plan=None, **overrides) -> ConsultRequest:
        values = {
            "input_refs": tuple(refs),
            "query_plan": plan,
            "policy_ref": "policy:collab.v1",
        }
        values.update(overrides)
        return ConsultRequest(**values)

    def consult(self, refs=(), plan=None, *, facade=None, access=None, **kwargs):
        return (self.facade if facade is None else facade).interact(
            self.access() if access is None else access,
            self.request(refs, plan, **kwargs),
        )

    def enqueue(self, **fields) -> None:
        self.enqueue_content(canonical_json_text(fields))

    def propose_supported(self, *, claim: str = "capability:file-read"):
        event = self.ingest(event_type="CapabilityObserved:file-read")
        body = self.belief_body(
            claim=claim,
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
        )
        return self.propose(body=body, scope=self.default_scope("capability"))

    def belief_plan(self) -> BeliefCurrentQuery:
        return BeliefCurrentQuery(
            claim_types=("capability",),
            scope={"scene": "test.scene"},
            limit=5,
        )

    def interaction(self, interaction_id: str):
        row = self.store.interaction(
            namespace=NS, interaction_id=interaction_id
        )
        self.assertIsNotNone(row)
        return row

    def artifact(self, ref: dict):
        return self.store.load_artifact(
            namespace=NS,
            artifact_id=ref["artifact_id"],
            artifact_version=ref["artifact_version"],
        )

    def artifacts_for(self, interaction_id: str, *, kind: str | None = None):
        query = (
            "SELECT artifact_id, artifact_version, kind, context_id "
            "FROM collab_artifacts WHERE namespace = ? AND interaction_id = ?"
        )
        args = [NS, interaction_id]
        if kind is not None:
            query += " AND kind = ?"
            args.append(kind)
        query += " ORDER BY rowid"
        return self.store.connection.execute(query, tuple(args)).fetchall()

    def consult_contexts(self):
        return self.store.connection.execute(
            "SELECT context_id FROM context_snapshots "
            "WHERE namespace = ? AND context_kind = 'consult' ORDER BY rowid",
            (NS,),
        ).fetchall()

    @staticmethod
    def document(context) -> dict:
        return json.loads(context.rendered_input)

    @staticmethod
    def query_projection(context) -> str:
        return json.loads(context.rendered_input)["query_results"][0]["result"]

    @staticmethod
    def artifact_sources(context) -> list[dict]:
        refs = [dict(item) for item in context.collab_source_refs]
        for source in context.sources:
            if source.ref_kind == "event":
                refs.append(
                    {
                        "ref_type": "event",
                        "namespace": NS,
                        "event_id": source.ref_id,
                    }
                )
            else:
                refs.append(
                    {
                        "ref_type": "record",
                        "namespace": NS,
                        "record_id": source.ref_id,
                        "record_version": int(source.ref_version),
                    }
                )
        return refs

    def build_consult(self, refs=(), *, adapter_kind="direct-http"):
        return self.contexts.build_consult_context(
            namespace=NS,
            access=self.access().to_experience_access(),
            input_refs=list(refs),
            adapter_kind=adapter_kind,
        )

    def lifecycle_counts(self) -> dict[str, int]:
        return {
            "events": self.count_rows("event_journal"),
            "pending_work": self.count_rows("pending_work"),
            "work_progress": self.count_rows("work_progress"),
            "beliefs": len(self.store.current_versions_of_kind(NS, "belief")),
            "commands": self.count_rows("processed_commands"),
            "revision": self.revision(),
        }

    def review_access(self) -> HostAccess:
        return HostAccess(
            namespace=NS,
            host_instance=REVIEW_HOST,
            operator="operator:synthetic-c11",
            permissions=FACADE_PERMISSIONS,
            privacy_classes=frozenset({"private"}),
            purposes=frozenset({"review"}),
            access_ref=REVIEW_ACCESS_REF,
        )

    # ------------------------------------------------- 1. answered / artifacts

    def test_answered_persists_exact_reply_artifact_and_attempt(self) -> None:
        ref = self.register_input()
        self.enqueue(action="none", reply=REPLY_TEXT)
        before = self.lifecycle_counts()

        result = self.consult((ref,))

        self.assertEqual("answered", result.state)
        self.assertEqual(REPLY_TEXT, result.reply)
        self.assertEqual(1, result.generations)
        self.assertEqual(0, result.query_count)
        self.assertIsNone(result.detail)
        self.assertEqual(before, self.lifecycle_counts())

        interaction = self.interaction(result.interaction_id)
        self.assertEqual("consult", interaction.mode)
        self.assertEqual("answered", interaction.result)
        self.assertIsNotNone(interaction.completed_at)
        self.assertEqual(interaction.context_id, result.context_id)

        artifact = self.artifact(result.reply_artifact_ref)
        self.assertEqual("reply", artifact.kind)
        self.assertEqual("model_output", artifact.origin_kind)
        self.assertEqual(REPLY_TEXT, artifact.body)
        self.assertEqual("standard", artifact.retention)
        self.assertEqual(result.context_id, artifact.context_id)
        self.assertIn(ref, artifact.source_refs)

        attempts = self.store.model_attempts_for_run(
            self.contexts.load_consult_context(result.context_id).run_id
        )
        self.assertEqual(1, len(attempts))
        self.assertEqual("ok", attempts[0].outcome)
        self.assertEqual(
            {
                "authorization_id": attempts[0].authorization_id,
                "attempt_id": attempts[0].attempt_id,
                "attempt_no": attempts[0].attempt_no,
                "request_hash": attempts[0].request_hash,
            },
            artifact.attempt_ref,
        )
        self.assertEqual(1, self.provider.hits)
        request = json.loads(
            self.provider.requests[0]["body"].decode("utf-8")
        )
        context = self.contexts.load_consult_context(result.context_id)
        self.assertEqual(context.rendered_input, request["messages"][1]["content"])

    def test_wait_without_reply_writes_waiting_and_no_artifact(self) -> None:
        ref = self.register_input()
        self.enqueue(action="wait")

        result = self.consult((ref,))

        self.assertEqual("waiting", result.state)
        self.assertIsNone(result.reply)
        self.assertIsNone(result.reply_artifact_ref)
        self.assertEqual([], list(self.artifacts_for(result.interaction_id)))
        interaction = self.interaction(result.interaction_id)
        self.assertEqual("waiting", interaction.result)
        self.assertIsNotNone(interaction.completed_at)
        self.assertEqual(
            1,
            len(
                self.store.model_attempts_for_run(
                    self.contexts.load_consult_context(result.context_id).run_id
                )
            ),
        )

    def test_wait_with_reply_writes_reply_artifact(self) -> None:
        ref = self.register_input()
        self.enqueue(action="wait", reply="Waiting note with a real body.")

        result = self.consult((ref,))

        self.assertEqual("waiting", result.state)
        self.assertEqual("Waiting note with a real body.", result.reply)
        artifact = self.artifact(result.reply_artifact_ref)
        self.assertEqual("Waiting note with a real body.", artifact.body)
        self.assertEqual("reply", artifact.kind)
        self.assertEqual("waiting", self.interaction(result.interaction_id).result)

    def test_none_with_only_brief_fails_without_artifact(self) -> None:
        ref = self.register_input()
        self.enqueue(action="none", brief_reason="A brief is not a reply.")

        result = self.consult((ref,))

        self.assertEqual("failed", result.state)
        self.assertEqual("no_reply", result.detail)
        self.assertIsNone(result.reply)
        self.assertIsNone(result.reply_artifact_ref)
        self.assertEqual([], list(self.artifacts_for(result.interaction_id)))
        interaction = self.interaction(result.interaction_id)
        self.assertEqual("failed", interaction.result)
        self.assertIsNotNone(interaction.completed_at)
        self.assertNotIn("A brief is not a reply.", repr(result))
        self.assertNotIn("A brief is not a reply.", repr(interaction))

    # ----------------------------------------------------- 2. query lifecycle

    def test_query_flow_binds_final_context_and_registers_query_artifact(
        self,
    ) -> None:
        self.propose_supported()
        ref = self.register_input()
        plan = self.belief_plan()
        self.enqueue(action="query")
        self.enqueue(action="none", reply="Answer after deterministic query.")
        before = self.lifecycle_counts()

        result = self.consult((ref,), plan)

        self.assertEqual(before, self.lifecycle_counts())
        self.assertEqual("answered", result.state)
        self.assertEqual(2, result.generations)
        self.assertEqual(1, result.query_count)
        expected = self.queries.query(self.query_access(), plan)
        self.assertEqual(expected.to_body(), result.query_body)
        self.assertEqual(
            tuple(ref.to_dict() for ref in expected.source_refs),
            result.query_source_refs,
        )
        self.assertNotIn("state_revision", result.query_body)

        contexts = self.consult_contexts()
        self.assertEqual(2, len(contexts))
        initial = self.contexts.load_consult_context(contexts[0]["context_id"])
        final = self.contexts.load_consult_context(result.context_id)
        self.assertEqual(contexts[1]["context_id"], result.context_id)
        self.assertNotIn("query_results", self.document(initial))
        self.assertEqual(
            expected.to_body(), self.query_projection(final)
        )

        interaction = self.interaction(result.interaction_id)
        self.assertEqual("answered", interaction.result)
        self.assertEqual(final.context_id, interaction.context_id)
        self.assertNotEqual(initial.context_id, interaction.context_id)

        query_artifact = self.artifact(result.query_result_ref)
        self.assertEqual("query_result", query_artifact.kind)
        self.assertEqual("deterministic_query", query_artifact.origin_kind)
        self.assertIsNone(query_artifact.attempt_ref)
        self.assertEqual(final.context_id, query_artifact.context_id)
        self.assertIn('"state_revision"', query_artifact.body)
        self.assertNotIn('"state_revision"', result.query_body)
        self.assertNotEqual(query_artifact.body, result.query_body)

        reply_artifact = self.artifact(result.reply_artifact_ref)
        self.assertEqual(final.context_id, reply_artifact.context_id)
        reply_attempts = self.store.model_attempts_for_run(final.run_id)
        self.assertEqual(1, len(reply_attempts))
        self.assertEqual(
            reply_attempts[0].attempt_id,
            reply_artifact.attempt_ref["attempt_id"],
        )
        artifact_source_ids = {
            ref.get("record_id") or ref.get("event_id")
            for ref in query_artifact.source_refs
        }
        for source in final.sources:
            self.assertIn(source.ref_id, artifact_source_ids)

    def test_query_missing_plan_is_incomplete_without_completed_at(self) -> None:
        ref = self.register_input()
        self.enqueue(action="query")

        result = self.consult((ref,), None)

        self.assertEqual("incomplete", result.state)
        self.assertEqual("query_plan_missing", result.detail)
        self.assertEqual(1, result.generations)
        self.assertEqual(0, result.query_count)
        self.assertEqual([], list(self.artifacts_for(result.interaction_id)))
        interaction = self.interaction(result.interaction_id)
        self.assertEqual("incomplete", interaction.result)
        self.assertIsNone(interaction.completed_at)
        self.assertEqual(1, len(self.consult_contexts()))

    def test_second_query_fails(self) -> None:
        self.propose_supported()
        ref = self.register_input()
        plan = self.belief_plan()
        self.enqueue(action="query")
        self.enqueue(action="query")

        result = self.consult((ref,), plan)

        self.assertEqual("failed", result.state)
        self.assertEqual("second_query", result.detail)
        self.assertEqual(2, result.generations)
        self.assertEqual(1, result.query_count)
        self.assertEqual(1, len(self.artifacts_for(result.interaction_id, kind="query_result")))
        self.assertEqual([], list(self.artifacts_for(result.interaction_id, kind="reply")))
        self.assertEqual("failed", self.interaction(result.interaction_id).result)
        self.assertEqual(2, self.provider.hits)

    def test_query_then_format_error_cannot_repair(self) -> None:
        self.propose_supported()
        ref = self.register_input()
        plan = self.belief_plan()
        self.enqueue(action="query")
        self.enqueue_content("this is not protocol JSON")

        result = self.consult((ref,), plan)

        self.assertEqual("failed", result.state)
        self.assertEqual(2, result.generations)
        self.assertEqual(1, result.query_count)
        self.assertIsNone(result.reply_artifact_ref)
        self.assertEqual(
            1, len(self.artifacts_for(result.interaction_id, kind="query_result"))
        )
        self.assertEqual(
            0, len(self.artifacts_for(result.interaction_id, kind="reply"))
        )
        self.assertEqual(2, self.provider.hits)
        self.assertTrue(result.detail.startswith("format:"))

    def test_initial_format_error_repairs_and_uses_repair_attempt(self) -> None:
        ref = self.register_input()
        self.enqueue_content("this is not protocol JSON")
        self.enqueue(action="none", reply="Repaired answer.")

        result = self.consult((ref,))

        self.assertEqual("answered", result.state)
        self.assertEqual(2, result.generations)
        self.assertEqual(0, result.query_count)
        context = self.contexts.load_consult_context(result.context_id)
        attempts = self.store.model_attempts_for_run(context.run_id)
        self.assertEqual(2, len(attempts))
        artifact = self.artifact(result.reply_artifact_ref)
        self.assertEqual(attempts[1].attempt_id, artifact.attempt_ref["attempt_id"])
        self.assertNotEqual(
            attempts[0].attempt_id, artifact.attempt_ref["attempt_id"]
        )
        repair_request = json.loads(
            self.provider.requests[-1]["body"].decode("utf-8")
        )
        self.assertEqual(
            ["system", "user", "assistant", "user"],
            [message["role"] for message in repair_request["messages"]],
        )

    def test_propose_is_refused_without_repair(self) -> None:
        ref = self.register_input()
        self.enqueue(action="propose")

        result = self.consult((ref,))

        self.assertEqual("failed", result.state)
        self.assertEqual("propose", result.detail)
        self.assertEqual(1, result.generations)
        self.assertEqual(1, self.provider.hits)
        self.assertEqual([], list(self.artifacts_for(result.interaction_id)))
        self.assertEqual(0, len(self.store.current_versions_of_kind(NS, "belief")))
        self.assertEqual("failed", self.interaction(result.interaction_id).result)

    def test_transport_and_response_failures_are_failed(self) -> None:
        ref = self.register_input()
        self.provider.enqueue(self.response({"error": "boom"}, status=500))
        self.provider.enqueue(self.response({"error": "boom"}, status=500))
        transport = self.consult((ref,))

        self.assertEqual("failed", transport.state)
        self.assertEqual(1, transport.generations)
        self.assertIsNone(transport.reply_artifact_ref)
        interaction = self.interaction(transport.interaction_id)
        self.assertEqual("failed", interaction.result)
        self.assertIsNotNone(interaction.completed_at)
        self.assertEqual(
            2,
            len(
                self.store.model_attempts_for_run(
                    self.contexts.load_consult_context(
                        transport.context_id
                    ).run_id
                )
            ),
        )

        self.enqueue_content(
            canonical_json_text({"action": "none", "reply": "unused"}),
            finish_reason="length",
        )
        response = self.consult((ref,))
        self.assertEqual("failed", response.state)
        self.assertEqual(1, response.generations)
        self.assertIsNone(response.reply_artifact_ref)
        self.assertEqual(
            "failed", self.interaction(response.interaction_id).result
        )

        # Initial Context failure: the Facade never fabricates an interaction.
        interactions = self.count_rows("collab_interactions")
        contexts = len(self.consult_contexts())
        self.delete_record(self.profiles.get_active_profile(NS).ref.record_id)
        context_failure = self.consult((ref,))
        self.assertEqual("failed", context_failure.state)
        self.assertIsNone(context_failure.interaction_id)
        self.assertIsNone(context_failure.context_id)
        self.assertEqual(interactions, self.count_rows("collab_interactions"))
        self.assertEqual(contexts, len(self.consult_contexts()))

        # Preflight failure: zero Context and zero HTTP, still before any ID.
        hits = self.provider.hits
        self.store.revoke_model_authorization(self.auth.authorization_id)
        preflight_failure = self.consult((ref,))
        self.assertEqual("failed", preflight_failure.state)
        self.assertIsNone(preflight_failure.interaction_id)
        self.assertTrue(preflight_failure.detail.startswith("preflight:"))
        self.assertEqual(hits, self.provider.hits)
        self.assertEqual(interactions, self.count_rows("collab_interactions"))

    # ------------------------------------------------------ 3. P2/A lifecycle

    def test_final_context_stale_marks_interaction_stale(self) -> None:
        version = self.propose_supported()
        ref = self.register_input()
        plan = self.belief_plan()
        queries = _RevisionBumpQueries(self.queries, self.store, version.record_id)
        facade = self.make_facade(queries=queries)
        self.enqueue(action="query")
        before = len(self.consult_contexts())

        result = self.consult((ref,), plan, facade=facade)

        self.assertEqual("stale", result.state)
        self.assertEqual(1, result.generations)
        self.assertEqual(1, result.query_count)
        self.assertIsNotNone(result.query_body)
        self.assertIsNone(result.query_result_ref)
        self.assertIsNone(result.reply_artifact_ref)
        interaction = self.interaction(result.interaction_id)
        self.assertEqual("stale", interaction.result)
        self.assertIsNotNone(interaction.completed_at)
        self.assertEqual(before + 1, len(self.consult_contexts()))
        final_context = self.contexts.load_consult_context(result.context_id)
        self.assertNotIn("query_results", self.document(final_context))
        self.assertEqual([], list(self.artifacts_for(result.interaction_id)))

    def test_crash_leaves_open_interaction_and_no_fabricated_completion(
        self,
    ) -> None:
        ref = self.register_input()
        model = _CrashProxy(self.consult_model)
        facade = self.make_facade(model=model)
        self.enqueue(action="none", reply="never returned")
        with self.assertRaises(_Crash):
            self.consult((ref,), facade=facade)

        rows = self.store.connection.execute(
            "SELECT interaction_id, context_id, result, completed_at "
            "FROM collab_interactions WHERE namespace = ?",
            (NS,),
        ).fetchall()
        self.assertEqual(1, len(rows))
        interaction_id = rows[0]["interaction_id"]
        self.assertIsNone(rows[0]["result"])
        self.assertIsNone(rows[0]["completed_at"])
        self.assertEqual([], list(self.artifacts_for(interaction_id)))
        self.assertEqual(1, len(self.consult_contexts()))
        self.assertEqual(
            1,
            len(
                self.store.model_attempts_for_run(
                    self.store.load_context_snapshot(
                        rows[0]["context_id"]
                    ).run_id
                )
            ),
        )

        self.store.close()
        reopened = CognitionStore.open(self.db_path)
        self.addCleanup(reopened.close)
        row = reopened.interaction(
            namespace=NS, interaction_id=interaction_id
        )
        self.assertIsNotNone(row)
        self.assertIsNone(row.result)
        self.assertIsNone(row.completed_at)

    # ------------------------------------------------ 4. bind_interaction_context

    def test_bind_interaction_context_success_and_idempotence(self) -> None:
        ref = self.register_input()
        initial = self.build_consult((ref,))
        final = self.build_consult((ref,))
        interaction_id = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
            context_id=initial.context_id,
        )

        bound = self.store.bind_interaction_context(
            NS, interaction_id, final.context_id
        )
        self.assertEqual(final.context_id, bound.context_id)
        self.assertIsNone(bound.result)
        self.assertIsNone(bound.completed_at)
        self.assertIsNone(bound.receipt_request_id)

        changes = self.store.connection.total_changes
        again = self.store.bind_interaction_context(
            NS, interaction_id, final.context_id
        )
        self.assertEqual(bound, again)
        self.assertEqual(changes, self.store.connection.total_changes)

    def test_bind_interaction_context_rejects_terminal_and_wrong_contexts(
        self,
    ) -> None:
        ref = self.register_input()
        initial = self.build_consult((ref,))
        final = self.build_consult((ref,))
        interaction_id = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
            context_id=initial.context_id,
        )

        self.store.complete_interaction(
            namespace=NS, interaction_id=interaction_id, result="answered"
        )
        with self.assertRaises(CorruptStore):
            self.store.bind_interaction_context(
                NS, interaction_id, final.context_id
            )
        self.assertEqual(
            initial.context_id,
            self.interaction(interaction_id).context_id,
        )

        event = self.ingest(event_type="TestTrigger:bind")
        work = self.work_for(event.event_id)
        self.assertIsNotNone(work)
        review_id = self.store.register_interaction(
            namespace=NS,
            mode="review",
            activation_kind="manual_review",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            trigger_ref=self.event_ref(event.event_id),
            work_id=work.work_id,
            work_generation=0,
            context_id=initial.context_id,
        )
        with self.assertRaises(InvalidInput):
            self.store.bind_interaction_context(
                NS, review_id, final.context_id
            )

        other = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
            context_id=initial.context_id,
        )
        with self.assertRaises(RecordNotFound):
            self.store.bind_interaction_context(
                NS_B, other, final.context_id
            )
        with self.assertRaises(RecordNotFound):
            self.store.bind_interaction_context(
                NS, other, str(uuid.uuid4())
            )
        review_context = self.build_context(event.event_id)
        with self.assertRaises(InvalidInput):
            self.store.bind_interaction_context(
                NS, other, review_context.context_id
            )
        self.assertEqual(
            initial.context_id, self.interaction(other).context_id
        )

    def test_bind_interaction_context_rejects_artifact_context_conflict(
        self,
    ) -> None:
        ref = self.register_input()
        initial = self.build_consult((ref,))
        other = self.build_consult((ref,))
        interaction_id = self.store.register_interaction(
            namespace=NS,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=[ref],
            context_id=initial.context_id,
        )
        self.store.register_artifact(
            namespace=NS,
            kind="reply",
            interaction_id=interaction_id,
            context_id=initial.context_id,
            origin_kind="model_output",
            body="existing reply",
            retention="standard",
            attempt_ref=None,
            sources=self.artifact_sources(initial),
        )

        with self.assertRaises(CorruptStore):
            self.store.bind_interaction_context(
                NS, interaction_id, other.context_id
            )
        self.assertEqual(
            initial.context_id,
            self.interaction(interaction_id).context_id,
        )

    # ------------------------------------------------- 5. P3/P4/B exact attempts

    def test_missing_or_mismatched_attempt_evidence_fails(self) -> None:
        ref = self.register_input()
        self.enqueue(action="none", reply="first normal answer")
        first = self.consult((ref,))
        foreign = self.artifact(first.reply_artifact_ref).attempt_ref

        cases = (
            ("missing", None),
            ("content", None),
            ("foreign", foreign),
        )
        for mode, foreign_ref in cases:
            with self.subTest(mode=mode):
                model = _EvidenceProxy(
                    self.consult_model, mode=mode, foreign=foreign_ref
                )
                facade = self.make_facade(model=model)
                self.enqueue(action="none", reply="must not persist")
                result = self.consult((ref,), facade=facade)
                self.assertEqual("failed", result.state)
                self.assertIn(
                    result.detail,
                    ("attempt_evidence_missing", "attempt_evidence_mismatch"),
                )
                self.assertIsNone(result.reply_artifact_ref)
                self.assertEqual(
                    0,
                    len(
                        self.artifacts_for(
                            result.interaction_id, kind="reply"
                        )
                    ),
                )
                self.assertEqual(
                    "failed", self.interaction(result.interaction_id).result
                )

        # A model that does not expose the evidence surface fails before any
        # Context or HTTP call: no interaction, no fabricated attempt.
        class _NoEvidenceModel:
            adapter_kind = "direct-http"

            @staticmethod
            def bind_run(*, namespace, context):
                raise AssertionError("bind_run must not run")

            @staticmethod
            def propose(**kwargs):
                raise AssertionError("propose must not run")

        interactions = self.count_rows("collab_interactions")
        contexts = len(self.consult_contexts())
        hits = self.provider.hits
        result = self.consult(
            (ref,), facade=self.make_facade(model=_NoEvidenceModel())
        )
        self.assertEqual("failed", result.state)
        self.assertEqual("capability:INVALID_SCHEMA", result.detail)
        self.assertIsNone(result.interaction_id)
        self.assertEqual(hits, self.provider.hits)
        self.assertEqual(interactions, self.count_rows("collab_interactions"))
        self.assertEqual(contexts, len(self.consult_contexts()))

    def test_consecutive_calls_use_their_own_attempts(self) -> None:
        ref_a = self.register_input("first consult input")
        ref_b = self.register_input("second consult input")
        self.enqueue(action="none", reply="first answer")
        first = self.consult((ref_a,))
        first_context = self.contexts.load_consult_context(first.context_id)
        first_attempt = self.artifact(first.reply_artifact_ref).attempt_ref

        self.enqueue(action="none", reply="second answer")
        second = self.consult((ref_b,))
        second_attempt = self.artifact(second.reply_artifact_ref).attempt_ref

        self.assertNotEqual(
            first_attempt["attempt_id"], second_attempt["attempt_id"]
        )
        self.assertNotEqual(first.context_id, second.context_id)
        self.assertIsNone(
            self.consult_model.last_attempt_evidence(
                run_id=first_context.run_id,
                context_id=first_context.context_id,
            )
        )

    # --------------------------------------------------- 6. isolation / N1

    def test_query_artifact_and_context_projection_are_isolated(self) -> None:
        self.propose_supported()
        ref = self.register_input()
        plan = self.belief_plan()
        self.enqueue(action="query")
        self.enqueue(action="none", reply="isolated answer")

        result = self.consult((ref,), plan)

        context = self.contexts.load_consult_context(result.context_id)
        artifact = self.artifact(result.query_result_ref)
        prompt_text = self.provider.requests[-1]["body"].decode("utf-8")
        prompt = json.loads(prompt_text)
        user_message = prompt["messages"][1]["content"]

        self.assertIn('"state_revision"', artifact.body)
        self.assertNotIn("state_revision", result.query_body or "")
        self.assertNotIn("state_revision", context.rendered_input)
        self.assertNotIn("state_revision", user_message)
        self.assertEqual(context.rendered_input, user_message)
        revision = json.loads(artifact.body)["state_revision"]
        self.assertNotIn(f'"state_revision":{revision}', prompt_text)
        self.assertEqual(
            result.query_body,
            json.loads(user_message)["query_results"][0]["result"],
        )
        self.assertNotIn('"state_revision"', result.query_body)

    def test_internal_omissions_stay_out_of_prompt_and_result(self) -> None:
        self.propose_supported()
        restricted = self.store.append_record_version(
            namespace=NS,
            record_id=str(uuid.uuid4()),
            record_version=1,
            kind="belief",
            body=self.belief_body(claim="restricted-claim-secret"),
            status="supported",
            schema_version="1",
            privacy_class="restricted",
            scope={"scene": "test.scene"},
        )
        ref = self.register_input()
        plan = self.belief_plan()
        self.enqueue(action="query")
        self.enqueue(action="none", reply="partial material answer")

        result = self.consult((ref,), plan)

        self.assertEqual("answered", result.state)
        query_body = json.loads(result.query_body)
        self.assertEqual("partial", query_body["material_status"])
        for reason in (
            "not_authorized",
            "source_ref_not_authorized",
            "source_ref_redacted",
            "redacted",
        ):
            self.assertNotIn(reason, result.query_body)
            self.assertNotIn(reason, repr(result))
            self.assertNotIn(
                reason, self.contexts.load_consult_context(
                    result.context_id
                ).rendered_input
            )
        self.assertNotIn("restricted-claim-secret", result.query_body)
        self.assertNotIn(
            "restricted-claim-secret",
            self.contexts.load_consult_context(result.context_id).rendered_input,
        )
        self.assertNotIn(
            "restricted-claim-secret",
            self.provider.requests[-1]["body"].decode("utf-8"),
        )
        artifact = self.artifact(result.query_result_ref)
        for reason in ("not_authorized", "source_ref_not_authorized"):
            self.assertNotIn(reason, artifact.body)
        self.assertNotIn(str(restricted.record_id), result.query_body)

    def test_consult_result_field_boundary(self) -> None:
        names = [field.name for field in dataclasses.fields(ConsultResult)]
        self.assertEqual(
            [
                "interaction_id",
                "context_id",
                "state",
                "reply",
                "query_body",
                "query_source_refs",
                "query_result_ref",
                "reply_artifact_ref",
                "generations",
                "query_count",
                "detail",
            ],
            names,
        )
        for forbidden in (
            "state_revision",
            "omissions",
            "query_result",
            "query_artifact_body",
            "to_artifact_body",
        ):
            self.assertNotIn(forbidden, names)

    # ------------------------------------------------------------ 7. history

    def test_history_review_correction_survives_restart(self) -> None:
        version = self.propose_supported()
        ref = self.register_input("history question")
        plan = self.belief_plan()
        self.enqueue(action="query")
        self.enqueue(action="none", reply="first answer")
        first = self.consult((ref,), plan)
        self.assertEqual("answered", first.state)
        self.assertIn('"record_version":1', first.query_body)
        self.assertIn('"valid_until":null', first.query_body)
        first_context = self.contexts.load_consult_context(first.context_id)
        first_artifact = self.artifact(first.query_result_ref)

        event = self.ingest(event_type="CorrectionObserved:file-read")
        work = self.work_for(event.event_id)
        self.assertIsNotNone(work)
        current = self.store.get_current(NS, version.record_id)
        corrected_body = dict(current.body)
        corrected_body["valid_until"] = "2027-01-01T00:00:00Z"
        proposal = {
            "operation": "revise_belief",
            "target": self.record_ref(current),
            "body": corrected_body,
            "scope": {"scene": "test.scene"},
        }
        review_model = _ScriptedModel([self.raw(proposal)])
        review_facade = ReviewFacade(
            self.store, context_service=self.contexts, model=review_model
        )
        reviewed = review_facade.review(
            self.review_access(),
            work_id=work.work_id,
            trigger_event_id=event.event_id,
        )
        self.assertEqual("pending", reviewed.state)
        resolved = review_facade.resolve(
            self.review_access(),
            candidate_id=reviewed.candidate_id,
            decision="approve",
            decision_id="decision:c11-history",
        )
        self.assertEqual("committed", resolved.state)
        self.assertIsNotNone(resolved.receipt)
        corrected = self.store.get_current(NS, version.record_id)
        self.assertEqual(2, corrected.record_version)
        self.assertEqual("2027-01-01T00:00:00Z", corrected.body["valid_until"])

        # A fresh process: close, reopen, rebuild every service and the model.
        self.store.close()
        self.store = CognitionStore.open(self.db_path)
        self.addCleanup(self.store.close)
        self.profiles = PersonaProfileService(self.store)
        self.beliefs = self._belief_service()
        self.self_view = SelfViewService(self.store)
        self.contexts = ContextService(
            self.store,
            self_view_service=self.self_view,
            profile_service=self.profiles,
        )
        self.queries = CognitionQueryService(self.store, clock=self.clock)
        self.consult_model = self.make_consult_model()
        self.facade = self.make_facade()
        self.assertEqual(2, self.provider.hits)
        self.assertEqual([], self.provider._responses)

        self.enqueue(action="query")
        self.enqueue(action="none", reply="second answer")
        second = self.consult((ref,), plan)

        self.assertEqual("answered", second.state)
        self.assertNotEqual(first.context_id, second.context_id)
        self.assertIn('"record_version":2', second.query_body)
        self.assertIn("2027-01-01T00:00:00Z", second.query_body)
        self.assertNotIn('"valid_until":null', second.query_body)
        second_context = self.contexts.load_consult_context(second.context_id)
        self.assertEqual(
            second.query_body, self.query_projection(second_context)
        )
        self.assertNotIn(
            self.query_projection(first_context), second_context.rendered_input
        )
        self.assertNotEqual(first_artifact.body, self.artifact(second.query_result_ref).body)

        request = json.loads(
            self.provider.requests[-1]["body"].decode("utf-8")
        )
        self.assertEqual(
            second_context.rendered_input, request["messages"][1]["content"]
        )
        self.assertEqual(
            second.query_body,
            json.loads(request["messages"][1]["content"])["query_results"][0][
                "result"
            ],
        )
        second_read_set = list(
            self.store.load_context_snapshot(second.context_id).read_set
        )
        versions = [
            (entry["ref"]["record_id"], entry["ref"]["record_version"])
            for entry in second_read_set
        ]
        self.assertIn((version.record_id, 2), versions)
        self.assertNotIn((version.record_id, 1), versions)

    def test_history_negative_cases_do_not_leak_or_reuse_cache(self) -> None:
        version = self.propose_supported()
        ref = self.register_input("negative history question")
        plan = self.belief_plan()
        self.enqueue(action="query")
        self.enqueue(action="none", reply="baseline answer")
        baseline = self.consult((ref,), plan)
        baseline_artifact = self.artifact(baseline.query_result_ref)
        self.assertIn("capability:file-read", baseline.query_body)

        self.delete_record(version.record_id)
        self.enqueue(action="query")
        self.enqueue(action="none", reply="deleted material answer")
        after_delete = self.consult((ref,), plan)
        self.assertEqual("answered", after_delete.state)
        self.assertNotIn("capability:file-read", after_delete.query_body)
        self.assertNotEqual(baseline.query_body, after_delete.query_body)
        deleted_context = self.contexts.load_consult_context(
            after_delete.context_id
        )
        self.assertNotIn("capability:file-read", deleted_context.rendered_input)
        self.assertNotIn(
            "capability:file-read",
            self.provider.requests[-1]["body"].decode("utf-8"),
        )
        self.assertNotEqual(
            baseline_artifact.body,
            self.artifact(after_delete.query_result_ref).body,
        )

        restricted_claim = "restricted-history-secret"
        self.store.append_record_version(
            namespace=NS,
            record_id=str(uuid.uuid4()),
            record_version=1,
            kind="belief",
            body=self.belief_body(claim=restricted_claim),
            status="supported",
            schema_version="1",
            privacy_class="restricted",
            scope={"scene": "test.scene"},
        )
        self.enqueue(action="query")
        self.enqueue(action="none", reply="restricted material answer")
        restricted = self.consult((ref,), plan)
        self.assertNotIn(restricted_claim, restricted.query_body)
        self.assertNotIn(
            restricted_claim,
            self.contexts.load_consult_context(
                restricted.context_id
            ).rendered_input,
        )
        self.assertNotIn(
            restricted_claim,
            self.provider.requests[-1]["body"].decode("utf-8"),
        )

        expired_claim = "environment_state:history-window"
        self.store.append_record_version(
            namespace=NS,
            record_id=str(uuid.uuid4()),
            record_version=1,
            kind="belief",
            body=self.belief_body(
                claim=expired_claim,
                claim_type="environment_state",
                valid_until="2020-01-01T00:00:00Z",
            ),
            status="supported",
            schema_version="1",
            privacy_class="private",
            scope={"scene": "test.scene"},
        )
        expired_plan = BeliefCurrentQuery(
            claim_types=("environment_state",),
            scope={"scene": "test.scene"},
            limit=5,
        )
        self.enqueue(action="query")
        self.enqueue(action="none", reply="expired material answer")
        expired = self.consult((ref,), expired_plan)
        expired_body = json.loads(expired.query_body)
        self.assertEqual("ok", expired_body["status"])
        self.assertTrue(expired_body["items"][0]["expired"])
        self.assertEqual(
            "expired", expired_body["items"][0]["applicability"]
        )
        self.assertEqual(
            expired.query_body,
            self.query_projection(
                self.contexts.load_consult_context(expired.context_id)
            ),
        )
        self.assertIn('"applicability":"expired"', expired.query_body)

    def raw(self, value) -> str:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def _belief_service(self):
        from quintessence_pcs.domains.cognition import BeliefService

        return BeliefService(self.store, clock=self.clock)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
