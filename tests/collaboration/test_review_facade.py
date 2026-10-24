"""C07: the review-only Facade, receipt disclosure and Lab isolation.

Every test uses a fresh migrated database, the real PCS2-008 Context service
and a queue-style ``ScriptedModel`` that never touches the network.  A Facade
call is the only review entry point used below; the C06a/b/c tests remain the
authority for the internal service semantics and are not re-run here.
"""

from __future__ import annotations

import json
import shutil
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

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration import (  # noqa: E402
    FACADE_PERMISSIONS,
    HostAccess,
    ReviewFacade,
    ReviewView,
    ReviewWorkItem,
    open_store,
)
from quintessence_pcs.foundation import identifiers  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    EventNotFound,
    InvalidInput,
    Redacted,
)
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.experience import (  # noqa: E402
    READABILITY_AVAILABLE,
    ExperienceUnavailable,
    ExperienceView,
)

HOST = "host:synthetic-c07"
HOST_B = "host:synthetic-c07-other"
OPERATOR = "operator:synthetic-c07"
ACCESS_REF = "authorization:access:c07"
ALL_PERMISSIONS = FACADE_PERMISSIONS
WIDE_PRIVACY = frozenset(identifiers.PRIVACY_CLASSES)
NARROW_PRIVACY = frozenset({"public-example"})
REVIEW_PURPOSE = frozenset({"review"})
GRANT_RERUN = "14141414-1414-4414-8414-141414141414"
TS = "2026-10-23T00:00:00Z"


class ScriptedModel:
    """Queue-style model double with the ``fake-model`` adapter label."""

    adapter_kind = "fake-model"

    def __init__(self, outputs):
        self._outputs = list(outputs)
        self.calls = 0
        self.feedback: list[str | None] = []
        self.rendered_inputs: list[str] = []

    def propose(self, *, rendered_input, references, feedback=None):
        self.calls += 1
        self.feedback.append(feedback)
        self.rendered_inputs.append(rendered_input)
        value = self._outputs.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class ReviewFacadeTests(CognitionTestCase):
    """The 26 card checks plus the deterministic candidate-landing race."""

    # ------------------------------------------------------------- fixtures

    def make_facade(self, model=None) -> ReviewFacade:
        return ReviewFacade(
            self.store,
            context_service=self.contexts,
            model=model if model is not None else ScriptedModel([]),
        )

    def access(
        self,
        *,
        permissions=None,
        privacy_classes=None,
        host_instance: str = HOST,
        purposes: frozenset[str] = REVIEW_PURPOSE,
    ) -> HostAccess:
        return HostAccess(
            namespace=NS,
            host_instance=host_instance,
            operator=OPERATOR,
            permissions=(
                ALL_PERMISSIONS
                if permissions is None
                else frozenset(permissions)
            ),
            privacy_classes=(
                WIDE_PRIVACY
                if privacy_classes is None
                else frozenset(privacy_classes)
            ),
            purposes=purposes,
            access_ref=ACCESS_REF,
        )

    def pending_event(self, event_type: str = "CapabilityObserved:file-read"):
        event = self.ingest(event_type=event_type)
        work = self.work_for(event.event_id)
        self.assertIsNotNone(work)
        return event, work

    def proposal(
        self,
        event,
        *,
        claim: str = "capability:file-read",
        strength_reason: str = "synthetic evidence for a mechanism test",
    ) -> dict:
        link = self.evidence_link(self.event_ref(event.event_id))
        body = self.belief_body(
            claim=claim,
            evidence_links=[link],
            strength_reason=strength_reason,
        )
        return {
            "operation": "propose_belief",
            "body": body,
            "scope": self.default_scope(),
        }

    def retraction(self, event, target: dict) -> dict:
        return {
            "operation": "retract_belief",
            "target": target,
            "evidence_links": [
                self.evidence_link(self.event_ref(event.event_id))
            ],
            "reason": "synthetic correction for the C07 facade",
        }

    @staticmethod
    def wait(reason: str = "synthetic wait") -> dict:
        return {"operation": "wait", "reason": reason}

    @staticmethod
    def raw(value: dict) -> str:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    def review(self, facade: ReviewFacade, access: HostAccess, event, work):
        return facade.review(
            access,
            work_id=work.work_id,
            trigger_event_id=event.event_id,
        )

    def approve(
        self,
        facade: ReviewFacade,
        access: HostAccess,
        candidate_id: str,
        *,
        decision_id: str = "decision:approve:c07",
    ) -> ReviewView:
        return facade.resolve(
            access,
            candidate_id=candidate_id,
            decision="approve",
            decision_id=decision_id,
        )

    def reject(
        self,
        facade: ReviewFacade,
        access: HostAccess,
        candidate_id: str,
        *,
        decision_id: str = "decision:reject:c07",
    ) -> ReviewView:
        return facade.resolve(
            access,
            candidate_id=candidate_id,
            decision="reject",
            decision_id=decision_id,
        )

    def interaction(self, interaction_id: str):
        interaction = self.store.interaction(
            namespace=NS, interaction_id=interaction_id
        )
        self.assertIsNotNone(interaction)
        return interaction

    def candidates(self, interaction_id: str):
        return self.store.candidate_states_for_interaction(
            namespace=NS, interaction_id=interaction_id
        )

    # ---------------------------------------------------------- diagnostics

    def review_counts(self) -> dict[str, int]:
        connection = self.store.connection
        return {
            "interactions": int(
                connection.execute(
                    "SELECT COUNT(*) FROM collab_interactions"
                ).fetchone()[0]
            ),
            "candidates": int(
                connection.execute(
                    "SELECT COUNT(*) FROM collab_artifacts "
                    "WHERE namespace = ? AND kind = 'candidate'",
                    (NS,),
                ).fetchone()[0]
            ),
            "beliefs": int(
                connection.execute(
                    "SELECT COUNT(*) FROM record_versions "
                    "WHERE namespace = ? AND kind = 'belief'",
                    (NS,),
                ).fetchone()[0]
            ),
            "belief_records": int(
                connection.execute(
                    "SELECT COUNT(*) FROM records "
                    "WHERE namespace = ? AND kind = 'belief'",
                    (NS,),
                ).fetchone()[0]
            ),
            "commands": int(
                connection.execute(
                    "SELECT COUNT(*) FROM processed_commands "
                    "WHERE namespace = ?",
                    (NS,),
                ).fetchone()[0]
            ),
            "contexts": int(
                connection.execute(
                    "SELECT COUNT(*) FROM context_snapshots"
                ).fetchone()[0]
            ),
            "work_progress": int(
                connection.execute(
                    "SELECT COUNT(*) FROM work_progress"
                ).fetchone()[0]
            ),
        }

    def assert_no_writes(self, baseline: dict[str, int]) -> None:
        self.assertEqual(baseline, self.review_counts())

    # ------------------------------------------------------------ 1. access

    def test_facade_requires_permissions(self) -> None:
        facade = self.make_facade()
        event, work = self.pending_event()
        ref = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": str(uuid.uuid4()),
            "content_version": 1,
        }
        cases = (
            (
                "review.generate",
                lambda access: facade.preview(
                    access, raw_output=self.raw(self.proposal(event))
                ),
            ),
            (
                "review.generate",
                lambda access: facade.review(
                    access,
                    work_id=work.work_id,
                    trigger_event_id=event.event_id,
                ),
            ),
            (
                "review.approve",
                lambda access: facade.resolve(
                    access,
                    candidate_id=str(uuid.uuid4()),
                    decision="approve",
                    decision_id="decision:probe",
                ),
            ),
            (
                "review.reject",
                lambda access: facade.resolve(
                    access,
                    candidate_id=str(uuid.uuid4()),
                    decision="reject",
                    decision_id="decision:probe",
                ),
            ),
            (
                "result.read",
                lambda access: facade.result(
                    access, interaction_id=str(uuid.uuid4())
                ),
            ),
            (
                "experience.read",
                lambda access: facade.read_experience(
                    access, ref=ref, purpose="review"
                ),
            ),
            ("work.scan", lambda access: facade.legacy_work(access)),
            ("work.scan", lambda access: facade.managed_work(access)),
        )
        baseline = self.review_counts()

        for permission, call in cases:
            with self.subTest(permission=permission):
                access = self.access(
                    permissions=ALL_PERMISSIONS - {permission}
                )
                with self.assertRaises(AuthorizationRequired):
                    call(access)

        self.assert_no_writes(baseline)

    # ------------------------------------------------------ 2. open transaction

    def test_facade_refuses_open_store_transaction(self) -> None:
        facade = self.make_facade()
        access = self.access()
        event, work = self.pending_event()

        with self.store.read_snapshot():
            for call in (
                lambda: facade.result(
                    access, interaction_id=str(uuid.uuid4())
                ),
                lambda: facade.review(
                    access,
                    work_id=work.work_id,
                    trigger_event_id=event.event_id,
                ),
                lambda: facade.resolve(
                    access,
                    candidate_id=str(uuid.uuid4()),
                    decision="approve",
                    decision_id="decision:probe",
                ),
                lambda: facade.preview(
                    access, raw_output=self.raw(self.proposal(event))
                ),
            ):
                with self.assertRaisesRegex(
                    InvalidInput,
                    "must not run inside an open store transaction",
                ):
                    call()

    # ----------------------------------------------------------- 3. preview

    def test_preview_returns_structural_only_without_writes(self) -> None:
        facade = self.make_facade()
        access = self.access()
        event = self.ingest(event_type="CapabilityObserved:file-read")
        baseline = self.review_counts()

        valid = facade.preview(
            access, raw_output=self.raw(self.proposal(event))
        )
        self.assertEqual("structural_only", valid.state)
        self.assertIsNone(valid.detail)
        self.assertIsNone(valid.interaction_id)
        self.assertIsNone(valid.candidate_id)
        self.assertIsNone(valid.receipt)

        wait = facade.preview(access, raw_output=self.raw(self.wait()))
        self.assertEqual("structural_only", wait.state)

        invalid = facade.preview(access, raw_output="{not strict json")
        self.assertEqual("failed", invalid.state)
        self.assertEqual("INVALID_SCHEMA", invalid.detail)

        unsupported = facade.preview(
            access,
            raw_output=self.raw({"operation": "consult", "question": "x"}),
        )
        self.assertEqual("failed", unsupported.state)
        self.assertEqual("UNSUPPORTED_OPERATION", unsupported.detail)

        self.assert_no_writes(baseline)

    # ------------------------------------------------------------- 4. pending

    def test_review_generates_pending_candidate(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)
        access = self.access()

        view = self.review(facade, access, event, work)

        self.assertEqual("pending", view.state)
        self.assertIsNotNone(view.interaction_id)
        self.assertIsNotNone(view.candidate_id)
        interaction = self.interaction(view.interaction_id)
        self.assertEqual("candidate_pending", interaction.result)
        candidates = self.candidates(view.interaction_id)
        self.assertEqual(1, len(candidates))
        self.assertEqual("pending", candidates[0].status)
        self.assertEqual(view.candidate_id, candidates[0].artifact_id)
        counts = self.review_counts()
        self.assertEqual(0, counts["beliefs"])
        self.assertEqual(0, counts["commands"])

    # -------------------------------------------------------------- 5. denied

    def test_review_denied_for_blocked_work(self) -> None:
        event, work = self.pending_event()
        self.store.register_work_control(
            namespace=NS,
            work_id=work.work_id,
            generation=1,
            blocked_after_rejection=True,
            control_version=1,
            block_reason="rejected",
        )
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)
        baseline = self.review_counts()

        view = self.review(facade, self.access(), event, work)

        self.assertEqual("denied", view.state)
        self.assertEqual("blocked_after_rejection", view.detail)
        self.assertIsNone(view.interaction_id)
        self.assertEqual(0, model.calls)
        self.assert_no_writes(baseline)

    # ---------------------------------------------------------------- 6. wait

    def test_two_pending_works_review_only_their_own_trigger_fact(self) -> None:
        first = self.ingest(
            event_type="Gray47ProjectBrief",
            payload={"text": "A: the coordinator prefers cyan."},
        )
        second = self.ingest(
            event_type="Gray47ProjectRevision",
            payload={"text": "B: the coordinator changes the preference to red."},
        )
        first_work = self.work_for(first.event_id)
        second_work = self.work_for(second.event_id)
        self.assertIsNotNone(first_work)
        self.assertIsNotNone(second_work)
        model = ScriptedModel(
            [
                self.raw(self.wait("first Work has no candidate")),
                self.raw(self.wait("second Work has no candidate")),
            ]
        )
        facade = self.make_facade(model)
        access = self.access()

        first_view = self.review(facade, access, first, first_work)
        second_view = self.review(facade, access, second, second_work)
        self.assertEqual("waiting", first_view.state)
        self.assertEqual("waiting", second_view.state)

        documents = [
            json.loads(rendered) for rendered in model.rendered_inputs
        ]
        first_doc, second_doc = documents
        self.assertEqual(
            [item["ref"]["event_id"] for item in first_doc["facts"]],
            [first.event_id],
        )
        self.assertEqual(
            [item["ref"]["event_id"] for item in second_doc["facts"]],
            [second.event_id],
        )
        self.assertEqual(
            [item["ref"]["event_id"] for item in first_doc["pending_sources"]],
            [first.event_id],
        )
        self.assertEqual(
            [item["ref"]["event_id"] for item in second_doc["pending_sources"]],
            [second.event_id],
        )
        self.assertNotIn(
            second.event_id,
            json.dumps(first_doc["facts"]),
        )
        self.assertNotIn(
            first.event_id,
            json.dumps(second_doc["facts"]),
        )

    def test_review_candidate_uses_only_its_own_trigger_evidence(self) -> None:
        first = self.ingest(
            event_type="Gray47ProjectBrief",
            payload={"text": "A: the coordinator prefers cyan."},
        )
        second = self.ingest(
            event_type="Gray47ProjectRevision",
            payload={"text": "B: the coordinator changes the preference to red."},
        )
        first_work = self.work_for(first.event_id)
        self.assertIsNotNone(first_work)
        model = ScriptedModel([self.raw(self.proposal(first))])
        facade = self.make_facade(model)

        view = self.review(facade, self.access(), first, first_work)

        self.assertEqual("pending", view.state)
        self.assertIsNotNone(view.candidate_id)
        document = json.loads(model.rendered_inputs[0])
        self.assertEqual(
            [item["ref"]["event_id"] for item in document["facts"]],
            [first.event_id],
        )
        self.assertNotIn(second.event_id, document["facts"][0]["content"])
        detail = facade.candidate_detail(
            self.access(),
            candidate_id=view.candidate_id,
            artifact_version=1,
        )
        self.assertIsNotNone(detail.proposal)
        self.assertEqual(len(detail.proposal.evidence_links), 1)
        self.assertEqual(
            detail.proposal.evidence_links[0]["ref"]["event_id"],
            first.event_id,
        )

    def test_deleted_trigger_is_not_replaced_by_other_pending_work(self) -> None:
        first = self.ingest(event_type="Gray47ProjectBrief")
        second = self.ingest(event_type="Gray47ProjectRevision")
        first_work = self.work_for(first.event_id)
        self.assertIsNotNone(first_work)
        self.assertIsNotNone(self.work_for(second.event_id))
        self.delete_event(first.event_id)

        with self.assertRaises((EventNotFound, Redacted)):
            self.contexts.build_context(
                namespace=NS,
                trigger_ref=self.event_ref(first.event_id),
                focus_work_id=first_work.work_id,
                trigger_as_fact=True,
            )

    def test_question_trigger_remains_user_input_not_fact(self) -> None:
        question = self.ingest(
            event_type="UserQuestion.question",
            payload={"text": "What is the current preference?"},
        )
        work = self.work_for(question.event_id)
        model = ScriptedModel([self.raw(self.wait("question review"))])
        facade = self.make_facade(model)

        view = self.review(facade, self.access(), question, work)

        self.assertEqual("waiting", view.state)
        document = json.loads(model.rendered_inputs[0])
        self.assertEqual(
            [item["ref"]["event_id"] for item in document["user_input"]],
            [question.event_id],
        )
        self.assertEqual(document["facts"], [])

    def test_review_wait_commits_waiting_and_completes_work(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.wait("nothing to commit"))])
        facade = self.make_facade(model)

        view = self.review(facade, self.access(), event, work)

        self.assertEqual("waiting", view.state)
        self.assertIsNotNone(view.interaction_id)
        self.assertIsNotNone(view.receipt)
        self.assertEqual("cognition.wait", view.receipt.operation)
        self.assertFalse(view.redacted)
        progress = self.store.work_progress(work.work_id)
        self.assertIsNotNone(progress)
        self.assertEqual("checked_no_belief", progress.outcome)
        interaction = self.interaction(view.interaction_id)
        self.assertEqual("waiting", interaction.result)
        self.assertEqual(view.receipt.request_id, interaction.receipt_request_id)

    # -------------------------------------------------------- 7. model label

    def test_review_marks_fake_model(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)

        view = self.review(facade, self.access(), event, work)

        self.assertEqual("fake-model", view.model_adapter)
        self.assertEqual("fake-model", model.adapter_kind)
        self.assertNotEqual("direct-http", view.model_adapter)

    # ------------------------------------------------------- 8. pending/commit

    def test_result_maps_pending_and_committed(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)
        access = self.access()

        pending = self.review(facade, access, event, work)
        pending_result = facade.result(
            access, interaction_id=pending.interaction_id
        )
        self.assertEqual("pending", pending_result.state)
        self.assertIsNone(pending_result.receipt)

        approved = self.approve(facade, access, pending.candidate_id)
        self.assertEqual("committed", approved.state)
        self.assertIsNotNone(approved.receipt)

        committed = facade.result(
            access, interaction_id=pending.interaction_id
        )
        self.assertEqual("committed", committed.state)
        self.assertIsNotNone(committed.receipt)
        self.assertEqual(
            approved.receipt.request_id, committed.receipt.request_id
        )

    # ------------------------------------------------------------- 9. reject

    def test_reject_blocks_work_and_result_is_rejected(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)
        access = self.access()

        pending = self.review(facade, access, event, work)
        rejected = self.reject(facade, access, pending.candidate_id)

        self.assertEqual("rejected", rejected.state)
        control = self.store.work_control(
            namespace=NS, work_id=work.work_id
        )
        self.assertIsNotNone(control)
        self.assertTrue(control.blocked_after_rejection)
        self.assertEqual(1, control.generation)
        recovered = facade.result(
            access, interaction_id=pending.interaction_id
        )
        self.assertEqual("rejected", recovered.state)
        self.assertIsNone(recovered.receipt)

    # ------------------------------------------------------ 10. text is data

    def test_pending_never_reported_as_committed(self) -> None:
        event, work = self.pending_event()
        proposal = self.proposal(
            event,
            claim="capability:remembered",
            strength_reason="已记住: this candidate text is not a commit",
        )
        model = ScriptedModel([self.raw(proposal)])
        facade = self.make_facade(model)
        access = self.access()

        pending = self.review(facade, access, event, work)
        candidate = self.store.load_artifact(
            namespace=NS, artifact_id=pending.candidate_id, artifact_version=1
        )
        self.assertIn("remembered", candidate.body)
        self.assertIn("已记住", candidate.body)

        recovered = facade.result(
            access, interaction_id=pending.interaction_id
        )
        self.assertEqual("pending", recovered.state)
        self.assertIsNone(recovered.receipt)
        self.assertEqual(0, self.review_counts()["beliefs"])
        self.assertEqual(0, self.review_counts()["commands"])

    # ------------------------------------------------- 11. current access

    def test_result_suppresses_receipt_without_current_access(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)
        access = self.access()

        pending = self.review(facade, access, event, work)
        self.approve(facade, access, pending.candidate_id)

        wide = facade.result(access, interaction_id=pending.interaction_id)
        self.assertEqual("committed", wide.state)
        self.assertIsNotNone(wide.receipt)
        self.assertFalse(wide.redacted)

        narrow = facade.result(
            self.access(privacy_classes=NARROW_PRIVACY),
            interaction_id=pending.interaction_id,
        )
        self.assertEqual("committed", narrow.state)
        self.assertIsNone(narrow.receipt)
        self.assertTrue(narrow.redacted)

    # --------------------------------------------------- 12. deletion barrier

    def test_result_suppresses_receipt_for_barriered_candidate(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)
        access = self.access()

        pending = self.review(facade, access, event, work)
        self.approve(facade, access, pending.candidate_id)
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_artifact",
            ref_id=pending.candidate_id,
            ref_version=1,
            authorization_ref="authorization:delete:c07",
        )

        recovered = facade.result(
            access, interaction_id=pending.interaction_id
        )
        self.assertEqual("committed", recovered.state)
        self.assertIsNone(recovered.receipt)
        self.assertTrue(recovered.redacted)

    # ------------------------------------------------------ 13. experience read

    def test_read_experience_uses_host_access(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)

        pending = self.review(facade, self.access(), event, work)
        candidate = self.store.load_artifact(
            namespace=NS, artifact_id=pending.candidate_id, artifact_version=1
        )
        ref = {
            "ref_type": "collab_artifact",
            "namespace": NS,
            "artifact_id": pending.candidate_id,
            "artifact_version": 1,
        }
        baseline = self.review_counts()

        wide = facade.read_experience(
            self.access(), ref=ref, purpose="review"
        )
        self.assertIsInstance(wide, ExperienceView)
        self.assertEqual(READABILITY_AVAILABLE, wide.readability)
        self.assertEqual(candidate.body, wide.body)

        narrow = facade.read_experience(
            self.access(privacy_classes=NARROW_PRIVACY),
            ref=ref,
            purpose="review",
        )
        self.assertIsInstance(narrow, ExperienceUnavailable)

        self.assert_no_writes(baseline)

    # ------------------------------------------------------- 14. restart

    def test_result_survives_restart(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)
        access = self.access()

        pending = self.review(facade, access, event, work)
        approved = self.approve(facade, access, pending.candidate_id)
        receipt_id = approved.receipt.request_id

        self.store.close()
        reopened = CognitionStore.open(self.db_path)
        self.addCleanup(reopened.close)
        restarted = ReviewFacade(
            reopened,
            context_service=self.contexts,
            model=ScriptedModel([]),
        )

        recovered = restarted.result(
            access, interaction_id=pending.interaction_id
        )
        self.assertEqual("committed", recovered.state)
        self.assertIsNotNone(recovered.receipt)
        self.assertEqual(receipt_id, recovered.receipt.request_id)

    # ------------------------------------------------------- 15. cross host

    def test_resolve_rejects_cross_host_interaction(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)

        pending = self.review(facade, self.access(), event, work)
        other = self.access(host_instance=HOST_B)

        with self.assertRaisesRegex(
            AuthorizationRequired, "another host instance"
        ):
            self.approve(facade, other, pending.candidate_id)

        candidate = self.store.load_artifact(
            namespace=NS, artifact_id=pending.candidate_id, artifact_version=1
        )
        self.assertEqual("pending", candidate.status)

    # ---------------------------------------------------- 16. reject permission

    def test_reject_requires_reject_permission(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)

        pending = self.review(facade, self.access(), event, work)
        access = self.access(
            permissions=ALL_PERMISSIONS - {"review.reject"}
        )

        with self.assertRaises(AuthorizationRequired):
            self.reject(facade, access, pending.candidate_id)

        candidate = self.store.load_artifact(
            namespace=NS, artifact_id=pending.candidate_id, artifact_version=1
        )
        self.assertEqual("pending", candidate.status)

    # ----------------------------------------------------------- 17. scans

    def test_legacy_and_managed_scans_are_separated(self) -> None:
        event_a, work_a = self.pending_event()
        event_b, work_b = self.pending_event()
        self.store.register_work_control(
            namespace=NS,
            work_id=work_b.work_id,
            generation=0,
            blocked_after_rejection=False,
            control_version=1,
        )
        facade = self.make_facade()
        access = self.access()

        universe = self.store.unfinished_work(NS)
        legacy = facade.legacy_work(access)
        managed = facade.managed_work(access)
        legacy_ids = {work.work_id for work in legacy}
        managed_ids = {item.work_id for item in managed}

        # The fixture itself leaves one unmanaged pending work item behind
        # (the synthetic profile review event); the two scans must still
        # partition every pending work item exactly once.
        self.assertEqual(
            {work.work_id for work in universe},
            legacy_ids | managed_ids,
        )
        self.assertFalse(legacy_ids & managed_ids)
        self.assertIn(work_a.work_id, legacy_ids)
        self.assertIn(work_b.work_id, managed_ids)
        for work in legacy:
            self.assertIsNone(
                self.store.work_control(namespace=NS, work_id=work.work_id)
            )
        self.assertIsInstance(managed[0], ReviewWorkItem)
        controlled = [
            item for item in managed if item.work_id == work_b.work_id
        ]
        self.assertEqual(1, len(controlled))
        self.assertEqual(0, controlled[0].generation)
        self.assertFalse(controlled[0].blocked)
        self.assertIsNone(controlled[0].rerun_authorization_ref)
        self.assertEqual(event_b.event_id, controlled[0].event_id)

    # ------------------------------------------------------ 18. correction

    def test_correction_two_step_retract_then_replacement(self) -> None:
        first_event, first_work = self.pending_event()
        second_event = self.ingest(event_type="CorrectionObserved:file-read")
        third_event = self.ingest(event_type="CapabilityObserved:file-read")
        model = ScriptedModel(
            [
                self.raw(self.proposal(first_event)),
                "",
                self.raw(self.proposal(third_event)),
            ]
        )
        facade = self.make_facade(model)
        access = self.access()

        first = self.review(facade, access, first_event, first_work)
        approved = self.approve(facade, access, first.candidate_id)
        self.assertEqual("committed", approved.state)
        old_record_id = approved.receipt.record_refs[0].record_id
        old = self.store.get_current(NS, old_record_id)
        self.assertEqual(1, old.record_version)
        self.assertEqual("supported", old.status)

        # The retract proposal is built from the exact committed version and
        # the new event; it replaces the placeholder queued above.
        model._outputs[0] = self.raw(
            self.retraction(second_event, self.record_ref(old))
        )
        second_work = self.work_for(second_event.event_id)
        self.assertIsNotNone(second_work)
        second = self.review(facade, access, second_event, second_work)
        retracted = self.approve(facade, access, second.candidate_id)
        self.assertEqual("committed", retracted.state)
        old_after_retract = self.store.get_current(NS, old_record_id)
        self.assertEqual(2, old_after_retract.record_version)
        self.assertEqual("retracted", old_after_retract.status)

        third_work = self.work_for(third_event.event_id)
        self.assertIsNotNone(third_work)
        third = self.review(facade, access, third_event, third_work)
        replaced = self.approve(facade, access, third.candidate_id)
        self.assertEqual("committed", replaced.state)
        new_record_id = replaced.receipt.record_refs[0].record_id
        self.assertNotEqual(old_record_id, new_record_id)
        new = self.store.get_current(NS, new_record_id)
        self.assertEqual(1, new.record_version)
        self.assertEqual("supported", new.status)
        self.assertEqual(
            "retracted",
            self.store.get_current(NS, old_record_id).status,
        )

    # ---------------------------------------- 19. failed replacement is honest

    def test_failed_replacement_does_not_fabricate(self) -> None:
        first_event, first_work = self.pending_event()
        second_event = self.ingest(event_type="CorrectionObserved:file-read")
        third_event = self.ingest(event_type="CorrectionObserved:file-read")
        model = ScriptedModel(
            [
                self.raw(self.proposal(first_event)),
                "",
                RuntimeError("synthetic replacement failure"),
            ]
        )
        facade = self.make_facade(model)
        access = self.access()

        first = self.review(facade, access, first_event, first_work)
        approved = self.approve(facade, access, first.candidate_id)
        old_record_id = approved.receipt.record_refs[0].record_id
        old = self.store.get_current(NS, old_record_id)
        model._outputs[0] = self.raw(
            self.retraction(second_event, self.record_ref(old))
        )
        second_work = self.work_for(second_event.event_id)
        second = self.review(facade, access, second_event, second_work)
        self.assertEqual(
            "committed",
            self.approve(facade, access, second.candidate_id).state,
        )

        third_work = self.work_for(third_event.event_id)
        replacement = self.review(facade, access, third_event, third_work)

        self.assertEqual("failed", replacement.state)
        self.assertEqual("model:RuntimeError", replacement.detail)
        self.assertIsNone(replacement.interaction_id)
        counts = self.review_counts()
        self.assertEqual(1, counts["belief_records"])
        self.assertEqual(2, counts["beliefs"])
        self.assertEqual(
            "retracted",
            self.store.get_current(NS, old_record_id).status,
        )

    # ------------------------------------------------------- 20. open_store

    def test_open_store_rejects_repository_paths(self) -> None:
        probe_root = REPO_ROOT / "c07-lab-probe"
        self.addCleanup(shutil.rmtree, probe_root, ignore_errors=True)

        with self.assertRaisesRegex(
            InvalidInput, "outside the source checkout"
        ):
            open_store(probe_root / "data" / "pcs.sqlite")
        self.assertFalse(probe_root.exists())

        lab = self.tmp_path / "lab" / "run-1" / "data" / "pcs.sqlite"
        store = open_store(lab)
        try:
            self.assertTrue(lab.exists())
            self.assertEqual(0, store.current_state_revision(NS))
        finally:
            store.close()
        self.assertFalse(probe_root.exists())

    # ------------------------------------------------ 21. work/event binding

    def test_review_rejects_work_event_mismatch(self) -> None:
        _event_a, work_a = self.pending_event()
        event_b, _work_b = self.pending_event()
        model = ScriptedModel([self.raw(self.wait())])
        facade = self.make_facade(model)
        baseline = self.review_counts()

        with self.assertRaisesRegex(
            InvalidInput, "does not belong to this event"
        ):
            facade.review(
                self.access(),
                work_id=work_a.work_id,
                trigger_event_id=event_b.event_id,
            )

        self.assertEqual(0, model.calls)
        self.assert_no_writes(baseline)

    # ---------------------------------------- 22. WAIT guard aborts on change

    def test_wait_guard_aborts_on_work_change_during_generation(self) -> None:
        event, work = self.pending_event()
        store = self.store

        class _TakeoverWaitModel(ScriptedModel):
            def __init__(self) -> None:
                super().__init__([ReviewFacadeTests.raw(ReviewFacadeTests.wait())])

            def propose(self, *, rendered_input, references, feedback=None):
                if self.calls == 0:
                    store.register_work_control(
                        namespace=NS,
                        work_id=work.work_id,
                        generation=1,
                        blocked_after_rejection=True,
                        control_version=1,
                        block_reason="rejected",
                    )
                return super().propose(
                    rendered_input=rendered_input,
                    references=references,
                    feedback=feedback,
                )

        model = _TakeoverWaitModel()
        facade = self.make_facade(model)

        view = self.review(facade, self.access(), event, work)

        self.assertIn(view.state, ("stale", "denied"))
        self.assertIsNone(view.receipt)
        self.assertIsNone(self.store.work_progress(work.work_id))
        self.assertEqual(0, self.review_counts()["commands"])
        interaction = self.interaction(view.interaction_id)
        self.assertEqual("stale", interaction.result)
        pending = self.store.unfinished_work_for_event(NS, event.event_id)
        self.assertEqual([work.work_id], [item.work_id for item in pending])

    # ---------------------------------------------- 23. immediate WAIT permission

    def test_review_immediate_wait_receipt_requires_result_read(self) -> None:
        facade = self.make_facade(
            ScriptedModel(
                [
                    self.raw(self.wait("first wait")),
                    self.raw(self.wait("second wait")),
                ]
            )
        )
        without_read = self.access(
            permissions=ALL_PERMISSIONS - {"result.read"}
        )

        first_event, first_work = self.pending_event()
        hidden = self.review(facade, without_read, first_event, first_work)
        self.assertEqual("waiting", hidden.state)
        self.assertIsNone(hidden.receipt)
        self.assertTrue(hidden.redacted)

        second_event, second_work = self.pending_event()
        visible = self.review(facade, self.access(), second_event, second_work)
        self.assertEqual("waiting", visible.state)
        self.assertIsNotNone(visible.receipt)
        self.assertFalse(visible.redacted)

    # ---------------------------------------- 24. historical WAIT after deletion

    def test_result_historical_wait_receipt_hidden_after_source_deletion(
        self,
    ) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.wait("historical wait"))])
        facade = self.make_facade(model)
        access = self.access()

        waiting = self.review(facade, access, event, work)
        self.assertIsNotNone(waiting.receipt)
        self.store.request_deletion(
            target_kind="event",
            namespace=NS,
            target_id=event.event_id,
            authorization_ref="authorization:delete:c07",
        )

        recovered = facade.result(
            access, interaction_id=waiting.interaction_id
        )
        self.assertEqual("waiting", recovered.state)
        self.assertIsNone(recovered.receipt)
        self.assertTrue(recovered.redacted)

    # ------------------------------------- 25. historical WAIT disclosure + privacy

    def test_result_historical_wait_receipt_disclosed_when_authorized(
        self,
    ) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.wait("historical wait"))])
        facade = self.make_facade(model)
        access = self.access()

        waiting = self.review(facade, access, event, work)
        self.assertIsNotNone(waiting.receipt)
        receipt_id = waiting.receipt.request_id

        recovered = facade.result(
            access, interaction_id=waiting.interaction_id
        )
        self.assertEqual("waiting", recovered.state)
        self.assertIsNotNone(recovered.receipt)
        self.assertEqual(receipt_id, recovered.receipt.request_id)
        self.assertFalse(recovered.redacted)

        narrow = facade.result(
            self.access(privacy_classes=NARROW_PRIVACY),
            interaction_id=waiting.interaction_id,
        )
        self.assertEqual("waiting", narrow.state)
        self.assertIsNone(narrow.receipt)
        self.assertTrue(narrow.redacted)

    # ------------------------------------------- 26. resolve unified disclosure

    def test_resolve_committed_uses_unified_receipt_disclosure(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel([self.raw(self.proposal(event))])
        facade = self.make_facade(model)

        pending = self.review(facade, self.access(), event, work)
        visible = self.approve(
            facade, self.access(), pending.candidate_id
        )
        self.assertEqual("committed", visible.state)
        self.assertIsNotNone(visible.receipt)
        self.assertFalse(visible.redacted)

        narrow = self.approve(
            facade,
            self.access(privacy_classes=NARROW_PRIVACY),
            pending.candidate_id,
            decision_id="decision:approve:c07:repeat",
        )
        self.assertEqual("committed", narrow.state)
        self.assertIsNone(narrow.receipt)
        self.assertTrue(narrow.redacted)

        without_read = self.approve(
            facade,
            self.access(permissions=ALL_PERMISSIONS - {"result.read"}),
            pending.candidate_id,
            decision_id="decision:approve:c07:repeat",
        )
        self.assertEqual("committed", without_read.state)
        self.assertIsNone(without_read.receipt)
        self.assertTrue(without_read.redacted)

    # --------------------------- 27. candidate never lands on a finished Work

    def test_candidate_does_not_land_when_work_finishes_during_generation(
        self,
    ) -> None:
        event, work = self.pending_event()
        case = self

        class _CompletingModel(ScriptedModel):
            def __init__(self) -> None:
                super().__init__([case.raw(case.proposal(event))])

            def propose(self, *, rendered_input, references, feedback=None):
                if self.calls == 0:
                    # Deterministic state change while the model "thinks".
                    case.complete_work_as_wait(event.event_id)
                return super().propose(
                    rendered_input=rendered_input,
                    references=references,
                    feedback=feedback,
                )

        model = _CompletingModel()
        facade = self.make_facade(model)

        view = self.review(facade, self.access(), event, work)

        self.assertEqual(1, model.calls)
        self.assertEqual("stale", view.state)
        self.assertEqual("work_moved", view.detail)
        self.assertIsNone(view.candidate_id)
        self.assertEqual(1, self.review_counts()["commands"])
        self.assertEqual(0, self.review_counts()["candidates"])
        self.assertEqual(0, self.review_counts()["beliefs"])
        interaction = self.interaction(view.interaction_id)
        self.assertEqual("stale", interaction.result)
        self.assertEqual((), self.candidates(view.interaction_id))
        self.assertIsNotNone(self.store.work_progress(work.work_id))

    # ------------------------- 28. an authorized rerun lands the next candidate

    def test_review_after_authorized_rerun_lands_candidate(self) -> None:
        event, work = self.pending_event()
        model = ScriptedModel(
            [
                self.raw(self.proposal(event)),
                self.raw(self.proposal(event)),
                self.raw(self.proposal(event)),
            ]
        )
        facade = self.make_facade(model)
        access = self.access()

        # 1) One committed review; 2) one rejected review blocks the Work.
        first = self.review(facade, access, event, work)
        self.assertEqual("pending", first.state)
        self.assertEqual(
            "committed",
            self.approve(facade, access, first.candidate_id).state,
        )
        second = self.review(facade, access, event, work)
        self.assertEqual("pending", second.state)
        self.assertEqual(
            "rejected",
            self.reject(facade, access, second.candidate_id).state,
        )
        control = self.store.work_control(
            namespace=NS, work_id=work.work_id
        )
        self.assertTrue(control.blocked_after_rejection)
        self.assertEqual(1, control.generation)
        self.assertIsNone(control.rerun_authorization_ref)

        # 3) A rerun without a registered authorization is still denied and
        # never reaches the model.
        calls_before = model.calls
        denied = self.review(facade, access, event, work)
        self.assertEqual("denied", denied.state)
        self.assertEqual(calls_before, model.calls)

        # 4) The host registers and authorizes one exact rerun grant (gen 1).
        self.store.register_grant(
            {
                "grant_id": GRANT_RERUN,
                "namespace": NS,
                "schema_version": "collab.v1",
                "kind": "rerun",
                "grant_version": 1,
                "subject": HOST,
                "authorization_ref": "authorization:rerun:c07",
                "issued_at": TS,
                "expires_at": None,
                "revoked_at": None,
                "work_id": work.work_id,
                "generation": 1,
            }
        )
        self.store.authorize_rerun(
            namespace=NS, work_id=work.work_id, grant_id=GRANT_RERUN
        )

        # 5) The start gate now allows the next generation.
        decision = self.store.review_start_decision(
            namespace=NS, work_id=work.work_id
        )
        self.assertTrue(decision.allowed)
        self.assertEqual(1, decision.generation)

        # 6-7) The candidate lands at generation 1 with the exact registered
        # rerun reference; the Facade never invents or widens one.
        rerun = self.review(facade, access, event, work)
        self.assertEqual("pending", rerun.state)
        candidate = self.store.load_artifact(
            namespace=NS,
            artifact_id=rerun.candidate_id,
            artifact_version=1,
        )
        self.assertEqual(1, candidate.work_generation)
        self.assertEqual(GRANT_RERUN, candidate.rerun_authorization_ref)

        # 8-9) Committing does not silently unblock the Work or consume the
        # registered rerun reference.
        approved = self.approve(
            facade,
            access,
            rerun.candidate_id,
            decision_id="decision:approve:c07:rerun",
        )
        self.assertEqual("committed", approved.state)
        final = self.store.work_control(namespace=NS, work_id=work.work_id)
        self.assertTrue(final.blocked_after_rejection)
        self.assertEqual(1, final.generation)
        self.assertEqual(GRANT_RERUN, final.rerun_authorization_ref)


if __name__ == "__main__":
    unittest.main()
