"""Shared fixtures for the PCS2-008 cognition tests.

Every test gets a fresh database in a temporary directory, a fixed clock
(2026-10-17T12:00:00Z), an active synthetic PersonaProfile and one empty
SelfView.  Only synthetic data crosses the API: no real persona asset, no
model call, no network.

The default revision baseline is 3: profile publish (1), activation (2) and
the empty SelfView (3).  Context archival never advances it.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
STORE_TESTS = HERE.parent / "store"
SRC_DIR = HERE.parents[1] / "src"
for _path in (HERE, STORE_TESTS, SRC_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from support import all_text_cells  # noqa: E402,F401  (tests/store/support.py)

from quintessence_pcs.domains.cognition import (  # noqa: E402
    BeliefService,
    SelfViewService,
)
from quintessence_pcs.domains.persona.profiles import (  # noqa: E402
    REVIEW_APPROVE_EVENT_TYPE,
    PersonaProfileService,
)
from quintessence_pcs.domains.refs import EventRef, RecordRef  # noqa: E402
from quintessence_pcs.runtime import CognitionRuntime, ContextService  # noqa: E402
from quintessence_pcs.store import CognitionStore  # noqa: E402

NS = "example.test.cognition"
NS_B = "example.test.cognition.other"
RECORDED_AT = "2026-10-17T00:00:00Z"
OBSERVED_AT = "2026-10-17T01:02:03Z"
NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
PROFILE_ID = "example.synthetic.cognition"


class CognitionTestCase(unittest.TestCase):
    """Fresh ``CognitionStore`` plus every PCS2-008 service for one test."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="pcs2-008-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)
        self.db_path = self.tmp_path / "pcs.sqlite"
        self.store = CognitionStore.open(self.db_path)
        self.addCleanup(self.store.close)
        self.clock = lambda: NOW
        self.profiles = PersonaProfileService(self.store)
        self.beliefs = BeliefService(self.store, clock=self.clock)
        self.self_view = SelfViewService(self.store)
        self.contexts = ContextService(
            self.store,
            self_view_service=self.self_view,
            profile_service=self.profiles,
        )
        self.runtime = CognitionRuntime(
            self.store,
            belief_service=self.beliefs,
            context_service=self.contexts,
            self_view_service=self.self_view,
            clock=self.clock,
        )
        self._setup_profile()
        self.base_revision = self.store.current_state_revision(NS)

    # ------------------------------------------------------------- lifecycle

    def open_second_store(self) -> CognitionStore:
        store = CognitionStore.open(self.db_path)
        self.addCleanup(store.close)
        return store

    # ---------------------------------------------------------------- events

    def ingest(
        self,
        *,
        namespace: str = NS,
        source_kind: str = "host_observation",
        source_instance: str = "cognition-tests",
        source_event_id: str | None = None,
        event_type: str = "TestTrigger",
        payload: Any = None,
        observed_at: str | None = OBSERVED_AT,
        recorded_at: str = RECORDED_AT,
        causation_refs: tuple[dict[str, Any], ...] = (),
    ):
        return self.store.ingest_event(
            namespace=namespace,
            source_kind=source_kind,
            source_instance=source_instance,
            source_event_id=source_event_id or f"source-{uuid.uuid4()}",
            event_type=event_type,
            observed_at=observed_at,
            payload={"text": "synthetic event body"} if payload is None else payload,
            recorded_at=recorded_at,
            causation_refs=causation_refs,
        )

    def event_ref(self, event_id: str, *, namespace: str = NS) -> dict[str, Any]:
        return {"ref_type": "event", "namespace": namespace, "event_id": event_id}

    def record_ref(self, version: Any) -> dict[str, Any]:
        return {
            "ref_type": "record",
            "namespace": version.namespace,
            "record_id": version.record_id,
            "record_version": version.record_version,
        }

    @staticmethod
    def origin_group(event_id: str) -> str:
        return f"event:{event_id}"

    def episode_origin_group(self, version: Any) -> str:
        sources = self.store.episode_sources_for_record(
            version.namespace, version.record_id
        )
        if len(sources) == 1:
            return f"event:{sources[0].event_id}"
        return f"episode:{version.record_id}"

    # -------------------------------------------------------------- profiles

    def approve_review(
        self, *, namespace: str = NS, source_event_id: str | None = None
    ) -> dict[str, Any]:
        receipt = self.ingest(
            namespace=namespace,
            source_kind="user_report",
            source_instance="admin-fixture",
            source_event_id=source_event_id or f"review-{uuid.uuid4()}",
            event_type=REVIEW_APPROVE_EVENT_TYPE,
            payload={"decision": "approve", "synthetic": True},
        )
        return self.event_ref(receipt.event_id, namespace=namespace)

    def profile_body(
        self,
        *,
        review_ref: dict[str, Any],
        profile_id: str = PROFILE_ID,
        profile_version: str = "0.0.1",
        identity_summary: str = "Synthetic profile used only to test the mechanism.",
    ) -> dict[str, Any]:
        return {
            "profile_id": profile_id,
            "profile_version": profile_version,
            "identity_summary": identity_summary,
            "principles": [
                "State what is unknown instead of inventing an answer.",
                "Do not claim a tool ran without evidence.",
            ],
            "style": {
                "language": "zh-CN",
                "tone": "plain",
                "verbosity_by_context": {"casual": "short", "technical": "detailed"},
            },
            "attention_preferences": [
                {"topic": "mechanism testing", "reason": "synthetic fixture"}
            ],
            "examples": [
                {
                    "example_id": "synthetic-example-1",
                    "scene": "synthetic mechanism test",
                    "user": "Synthetic user line.",
                    "assistant": "Synthetic assistant line.",
                    "synthetic": True,
                }
            ],
            "asset_hash": "0" * 64,
            "review_ref": review_ref,
            "renderer_compatibility": "pcs.renderer.synthetic-v1",
        }

    def publish(
        self,
        *,
        namespace: str = NS,
        profile_id: str = PROFILE_ID,
        body: dict[str, Any] | None = None,
        review_ref: dict[str, Any] | None = None,
    ):
        if review_ref is None:
            review_ref = self.approve_review(namespace=namespace)
        if body is None:
            body = self.profile_body(review_ref=review_ref, profile_id=profile_id)
        return self.profiles.publish_profile(
            namespace=namespace,
            profile_id=profile_id,
            body=body,
            review_ref=review_ref,
            authorization_ref="auth:cognition-test",
        )

    def activate(self, version: Any, *, namespace: str = NS) -> None:
        self.profiles.activate_profile(
            namespace=namespace,
            record_id=version.record_id,
            record_version=version.record_version,
            authorization_ref="auth:cognition-test",
        )

    def _setup_profile(self) -> None:
        version = self.publish()
        self.activate(version)
        # Create the empty projection now so Context archival has a stable
        # revision baseline and every later change is attributable.
        self.self_view.build_self_view(namespace=NS)

    # --------------------------------------------------------------- beliefs

    def evidence_link(
        self,
        ref: dict[str, Any],
        *,
        relation: str = "supports",
        origin_group_id: str | None = None,
        applicability_reason: str = "synthetic evidence for a mechanism test",
    ) -> dict[str, Any]:
        if origin_group_id is None:
            if ref.get("ref_type") == "event":
                origin_group_id = self.origin_group(ref["event_id"])
            else:
                version = self.store.get_version(
                    ref["namespace"], ref["record_id"], ref["record_version"]
                )
                origin_group_id = self.episode_origin_group(version)
        return {
            "ref": dict(ref),
            "relation": relation,
            "origin_group_id": origin_group_id,
            "applicability_reason": applicability_reason,
        }

    def belief_body(
        self,
        *,
        claim: str = "capability:file-read",
        claim_type: str = "capability",
        evidence_links: list[dict[str, Any]] | None = None,
        strength: str = "strong",
        strength_reason: str = "synthetic evidence for a mechanism test",
        inference_basis: str | None = None,
        premise_refs: list[dict[str, Any]] | None = None,
        alternative_group_id: str | None = None,
        valid_until: str | None = None,
        review_condition: str = "review when new independent evidence appears",
    ) -> dict[str, Any]:
        if inference_basis is None:
            inference_basis = {
                "user_preference": "direct_report",
                "environment_state": "observation",
                "capability": "observation",
                "general_hypothesis": "inductive",
                "derived_rule": "deductive",
            }[claim_type]
        return {
            "claim": claim,
            "claim_type": claim_type,
            "evidence_links": [] if evidence_links is None else evidence_links,
            "strength": strength,
            "strength_reason": strength_reason,
            "inference_basis": inference_basis,
            "premise_refs": [] if premise_refs is None else premise_refs,
            "alternative_group_id": alternative_group_id,
            "valid_until": valid_until,
            "review_condition": review_condition,
        }

    def default_scope(self, claim_type: str = "capability") -> dict[str, Any]:
        if claim_type in ("user_preference", "environment_state", "capability"):
            return {"scene": "test.scene"}
        return {}

    def build_context(
        self,
        event_id: str | None = None,
        *,
        adapter_kind: str | None = None,
        namespace: str = NS,
    ):
        if event_id is None:
            event_id = self.ingest(namespace=namespace).event_id
        return self.contexts.build_context(
            namespace=namespace,
            trigger_ref=self.event_ref(event_id, namespace=namespace),
            adapter_kind=adapter_kind,
        )

    def propose(
        self,
        *,
        body: dict[str, Any],
        scope: dict[str, Any] | None = None,
        context_id: str | None = None,
        request_id: str | None = None,
    ):
        if context_id is None:
            context_id = self.build_context().context_id
        return self.beliefs.propose_belief(
            namespace=NS,
            context_id=context_id,
            request_id=request_id or str(uuid.uuid4()),
            body=body,
            scope=self.default_scope(body["claim_type"])
            if scope is None
            else scope,
        )

    def revise(
        self,
        target: dict[str, Any],
        *,
        body: dict[str, Any],
        scope: dict[str, Any] | None = None,
        retract: bool = False,
        context_id: str | None = None,
        request_id: str | None = None,
    ):
        if context_id is None:
            context_id = self.build_context().context_id
        return self.beliefs.revise_belief(
            namespace=NS,
            context_id=context_id,
            request_id=request_id or str(uuid.uuid4()),
            target=target,
            body=body,
            scope=self.default_scope(body["claim_type"])
            if scope is None
            else scope,
            retract=retract,
        )

    # ---------------------------------------------------------------- runtime

    def run_step(self, event_id: str, *, namespace: str = NS):
        return self.runtime.run_step(namespace=namespace, trigger_event_id=event_id)

    def work_for(self, event_id: str, *, namespace: str = NS):
        works = self.store.unfinished_work_for_event(namespace, event_id)
        return works[0] if works else None

    def complete_work_as_wait(self, event_id: str) -> None:
        """Finish one pending work item with a legal WAIT (test setup)."""

        context = self.build_context(event_id)
        work = self.work_for(event_id)
        if work is None:
            raise AssertionError(f"no pending work for {event_id}")
        self.commit_wait(context, work, reason="test setup")

    def commit_wait(self, context: Any, work: Any, *, reason: str = "test wait"):
        """Commit a WAIT on an already-built context for a specific work row."""

        from quintessence_pcs.domains.cognition.context_guard import (
            require_live_context,
        )

        def apply(tx: Any) -> None:
            require_live_context(
                self.store, context_id=context.context_id, namespace=NS
            )
            tx.complete_work(
                work_id=work.work_id,
                context_id=context.context_id,
                outcome="checked_no_belief",
            )

        return self.store.commit_command(
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            operation="cognition.wait",
            request_payload={"reason": reason, "work_id": work.work_id},
            apply=apply,
            namespace=NS,
        )

    # ------------------------------------------------------------- diagnostics

    def revision(self, namespace: str = NS) -> int:
        return self.store.current_state_revision(namespace)

    def watermarks(self, namespace: str = NS) -> tuple[int, int, int]:
        row = self.store.connection.execute(
            "SELECT state_revision, last_event_seq, privacy_epoch "
            "FROM namespace_state WHERE namespace = ?",
            (namespace,),
        ).fetchone()
        if row is None:
            return (0, 0, 0)
        return (
            int(row["state_revision"]),
            int(row["last_event_seq"]),
            int(row["privacy_epoch"]),
        )

    def count_rows(self, table: str) -> int:
        return int(
            self.store.connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        )

    def delete_record(self, record_id: str, *, namespace: str = NS) -> None:
        self.store.request_deletion(
            target_kind="record",
            namespace=namespace,
            target_id=record_id,
            authorization_ref="auth:test-delete",
        )

    def delete_event(self, event_id: str, *, namespace: str = NS) -> None:
        self.store.request_deletion(
            target_kind="event",
            namespace=namespace,
            target_id=event_id,
            authorization_ref="auth:test-delete",
        )


__all__ = [
    "CognitionTestCase",
    "NS",
    "NS_B",
    "NOW",
    "OBSERVED_AT",
    "PROFILE_ID",
    "RECORDED_AT",
    "all_text_cells",
]
