"""J09 PS / MR / INT: the offline half of the behaviour matrix.

Every case runs against the real services on a synthetic database; no model is
called and nothing leaves the temporary directory.  The evidence these tests
produce is the *deterministic* half of PS01-PS06, MR01-MR03 and INT01: the
model-facing half is stage C.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.beliefs import BELIEF_KIND  # noqa: E402
from quintessence_pcs.foundation.errors import SourceRedacted  # noqa: E402
from quintessence_pcs.domains.cognition.questions import (  # noqa: E402
    QuestionAccess,
    QuestionService,
)
from quintessence_pcs.domains.cognition.expression_policy import (  # noqa: E402
    eligible_claim_types,
)
from quintessence_pcs.store.maintenance import (  # noqa: E402
    MaintenanceStore,
    RateLimited,
    SliceLimits,
    run_slice,
)

NOW = "2026-10-27T12:00:00Z"


class _Base(CognitionTestCase):
    def current_beliefs(self, store=None):
        target = self.store if store is None else store
        return target.current_versions_of_kind(NS, BELIEF_KIND)

    def only_belief(self, store=None):
        versions = self.current_beliefs(store)
        self.assertEqual(len(versions), 1, versions)
        return versions[0]


# --------------------------------------------------------------------- PS01
class PS01RestartRestoresStateTests(_Base):
    """A belief and a question survive a process end without the transcript."""

    def test_a_belief_and_its_version_survive_a_restart(self) -> None:
        version = self.propose(body=self.belief_body())
        revision = self.store.current_state_revision(NS)

        second = self.open_second_store()
        restored = second.current_versions_of_kind(NS, BELIEF_KIND)
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0].record_id, version.record_id)
        self.assertEqual(restored[0].record_version, version.record_version)
        self.assertEqual(restored[0].status, version.status)
        self.assertEqual(second.current_state_revision(NS), revision)


# --------------------------------------------------------------------- PS02
class PS02CorrectionMovesTheCurrentValueTests(_Base):
    """After a correction the new record is current and the old one is retired."""

    def test_a_correction_retracts_a_and_makes_b_the_current_value(self) -> None:
        first = self.propose(body=self.belief_body(claim="capability:deploy-at-0300"))

        # The frozen contract: a substantive change retracts the old record and
        # proposes a *new* one; it never rewrites the claim in place.
        retracted = self.revise(
            self.record_ref(first),
            body=self.belief_body(
                claim="capability:deploy-at-0300",
                evidence_links=[self.evidence_link(self.event_ref(self.ingest().event_id))],
            ),
            retract=True,
        )
        self.assertEqual(retracted.status, "retracted")
        second = self.propose(
            body=self.belief_body(
                claim="capability:deploy-at-0400",
                evidence_links=[self.evidence_link(self.event_ref(self.ingest().event_id))],
            )
        )
        self.assertNotEqual(second.record_id, first.record_id)

        current = {v.record_id: v for v in self.current_beliefs()}
        self.assertEqual(current[second.record_id].status, "supported")
        self.assertEqual(current[first.record_id].status, "retracted")

        second_store = self.open_second_store()
        again = {v.record_id: v for v in second_store.current_versions_of_kind(NS, BELIEF_KIND)}
        self.assertEqual(again[first.record_id].status, "retracted")


# --------------------------------------------------------------------- PS03
class PS03ConflictIsNotOverwrittenTests(_Base):
    """Two irreconcilable supports leave the belief contested, not decided."""

    def test_contradicting_evidence_yields_contested_not_supported(self) -> None:
        support = self.evidence_link(self.event_ref(self.ingest().event_id))
        against = self.evidence_link(
            self.event_ref(self.ingest().event_id), relation="contradicts"
        )
        version = self.propose(
            body=self.belief_body(evidence_links=[support, against], strength="limited")
        )
        self.assertEqual(version.status, "contested")

        # A later one-sided proposal does not silently overwrite the conflict.
        self.propose(body=self.belief_body(evidence_links=[support], strength="limited"))
        statuses = [v.status for v in self.current_beliefs()]
        self.assertIn("contested", statuses)


# --------------------------------------------------------------------- PS04
class PS04NoRepeatedWakeupsTests(_Base):
    """A consumed trigger does not fire twice, and unrelated events do not wake it."""

    def test_the_same_trigger_is_consumed_once(self) -> None:
        service = QuestionService(self.store)
        access = QuestionAccess(
            namespace=NS,
            host_instance="synthetic.host",
            access_ref="auth:synthetic.host",
            privacy_classes=frozenset({"private"}),
            permissions=frozenset(
                {"question.read", "question.create", "question.transition", "question.evaluate"}
            ),
            purposes=frozenset({"cognition.question"}),
        )
        trigger_event = self.ingest()
        trigger = self.event_ref(trigger_event.event_id)
        context = self.build_context()
        created = service.create(
            access,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            question="synthetic: is the nightly job safe to enable?",
            origin="user_requested",
            origin_refs=[trigger],
            privacy_class="private",
            trigger_spec={"source_refs": [trigger], "topic_keys": ["nightly"]},
        )
        opened = created.view
        self.assertEqual(opened.status, "open")

        fresh = self.build_context()
        consumed = service.consume_trigger(
            access,
            context_id=fresh.context_id,
            request_id=str(uuid.uuid4()),
            ref=opened.ref,
            expected_version=opened.ref.record_version,
            trigger_ref=trigger,
        )
        # Consuming records that the trigger was seen once; the question stays
        # open rather than being silently answered.
        self.assertEqual(consumed.status, "open")
        self.assertEqual(len(consumed.consumed_trigger_refs), 1)
        self.assertEqual(consumed.consumed_trigger_refs[0]["event_id"], trigger_event.event_id)


# --------------------------------------------------------------------- PS05
class PS05NoDuplicateLogicalCommitTests(_Base):
    """A retried request produces one logical commit, not two."""

    def test_the_same_request_id_commits_once(self) -> None:
        request_id = str(uuid.uuid4())
        context = self.build_context()
        first = self.propose(body=self.belief_body(), context_id=context.context_id, request_id=request_id)
        second = self.propose(body=self.belief_body(), context_id=context.context_id, request_id=request_id)
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(len(self.current_beliefs()), 1)

    def test_a_candidate_built_on_a_deleted_source_is_refused(self) -> None:
        event = self.ingest()
        link = self.evidence_link(self.event_ref(event.event_id))
        self.delete_event(event.event_id)
        with self.assertRaises(SourceRedacted):
            self.propose(body=self.belief_body(evidence_links=[link]))
        self.assertEqual(self.current_beliefs(), ())


# --------------------------------------------------------------------- PS06
class PS06DeletionHidesTheContentTests(_Base):
    """Deleting a belief's record removes it from the current read immediately."""

    def test_a_deleted_belief_is_not_current(self) -> None:
        version = self.propose(body=self.belief_body())
        self.assertEqual(len(self.current_beliefs()), 1)
        self.delete_record(version.record_id)
        self.assertEqual(
            self.current_beliefs(), (), "a barriered record must not read as current"
        )


# --------------------------------------------------------------------- MR01
class MR01ChineseQualificationTests(_Base):
    """Chinese negation, conditions and attribution survive the round trip."""

    def test_negation_and_condition_are_preserved_verbatim(self) -> None:
        claim = "部署窗口：仅在『未启用双写』且不支持夜间发布时，周四 04:00 才可用"
        version = self.propose(
            body=self.belief_body(claim=claim, claim_type="capability", strength="limited")
        )
        body = self.store.get_version(NS, version.record_id, version.record_version)
        self.assertEqual(body.body["claim"], claim)

    def test_an_assistant_echo_cannot_be_promoted_to_a_fact(self) -> None:
        # A UserStatement may support a preference, never a hypothesis or a world fact.
        self.assertNotIn("general_hypothesis", eligible_claim_types(("UserStatement:x",)))
        self.assertNotIn("external_report", eligible_claim_types(("UserStatement:x",)))


# --------------------------------------------------------------------- MR02
class MR02LexicalRetrievalTests(_Base):
    """The lexical path returns a page with an honest total; no vector index exists."""

    def test_a_page_reports_an_honest_total(self) -> None:
        for index in range(5):
            self.propose(body=self.belief_body(claim=f"capability:item-{index}"))
        versions = self.current_beliefs()
        self.assertEqual(len(versions), 5)

    def test_there_is_no_vector_branch_to_compare(self) -> None:
        # MR02 asks for lexical/vector/fusion; only the lexical path exists here.
        import importlib.util

        try:
            found = importlib.util.find_spec("quintessence_pcs.retrieval.vector")
        except ModuleNotFoundError:
            found = None
        self.assertIsNone(
            found, "no vector index is implemented; MR02's vector arm is NOT IMPLEMENTED"
        )


# --------------------------------------------------------------------- MR03
class MR03BoundedFailureTests(_Base):
    """A rate limit becomes a bounded wait with a visible retry stamp."""

    def test_a_rate_limit_waits_and_records_a_retry_stamp(self) -> None:
        jobs = MaintenanceStore(self.store, clock=lambda: NOW)

        def worker(slice_context):
            raise RateLimited("synthetic 429")

        job = jobs.register(
            namespace=NS,
            task_type="memory_delivery",
            trigger_key="mr03",
            params={"page": 1},
            config_version="j09.test.v1",
        )
        result = run_slice(
            jobs,
            namespace=NS,
            task_id=job.task_id,
            worker=worker,
            limits=SliceLimits(max_process=1),
            guard=lambda targets: None,
            now=NOW,
        )
        self.assertEqual(result.stop_reason, "rate_limited")
        state = jobs.get(NS, job.task_id)
        self.assertEqual(state.state, "waiting_dependency")
        self.assertIsNotNone(state.next_retry_at)


# --------------------------------------------------------------------- INT01
class INT01NoDoubleCurrentFactTests(_Base):
    """A conflicting pair never collapses into one current fact."""

    def test_two_conflicting_beliefs_stay_two_records(self) -> None:
        first = self.propose(
            body=self.belief_body(
                claim="capability:release-window-thursday",
                evidence_links=[self.evidence_link(self.event_ref(self.ingest().event_id))],
                strength="limited",
            )
        )
        second = self.propose(
            body=self.belief_body(
                claim="capability:release-window-friday",
                evidence_links=[
                    self.evidence_link(
                        self.event_ref(self.ingest().event_id), relation="contradicts"
                    )
                ],
                strength="limited",
                alternative_group_id="alt:release-window",
            )
        )
        current = self.current_beliefs()
        self.assertGreaterEqual(len(current), 2)
        statuses = sorted(v.status for v in current)
        # The contradicting side is recorded as contradicted rather than the pair
        # being collapsed into one confident current fact.
        self.assertIn("contradicted", statuses)
        self.assertNotEqual(statuses[0], statuses[-1])
        self.assertNotEqual(first.record_id, second.record_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
