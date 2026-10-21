"""PCS2-008 A group: Belief bodies, evidence rules, transitions and revision.

The assertions follow the handoff card's A section one bullet at a time:
the ten frozen body fields, the five claim types and their support sources,
same-origin non-voting, the conservative independence count, the transition
matrix, frozen claim text, explicit scope in the Record envelope, and "Tx B
only".  Every test uses ``CognitionStore`` and synthetic events.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, NS_B, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains import EpisodeService  # noqa: E402
from quintessence_pcs.domains.cognition import (  # noqa: E402
    parse_belief_body,
    require_live_context,
)
from quintessence_pcs.domains.cognition.errors import (  # noqa: E402
    EvidenceMissing,
    InvalidTransition,
)
from quintessence_pcs.domains.errors import SourceUntrusted  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CommandConflict,
    ContextStale,
    InvalidInput,
    RecordKindMismatch,
    ScopeViolation,
    SourceRedacted,
    StateRevisionConflict,
)
from quintessence_pcs.store.errors import NeedsContext  # noqa: E402


class BeliefProposalTests(CognitionTestCase):
    # --------------------------------------------------------- basic shapes

    def test_capability_observation_is_supported_with_limited_strength(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
        )
        version = self.propose(body=body, scope={"scene": "test.scene"})

        self.assertEqual("belief", version.kind)
        self.assertEqual(1, version.record_version)
        self.assertEqual("supported", version.status)
        self.assertEqual("limited", version.body["strength"])
        # scope lives on the record envelope, not in the body
        self.assertNotIn("scope", version.body)
        self.assertEqual({"scene": "test.scene"}, version.scope)
        stored = self.store.get_version(NS, version.record_id, version.record_version)
        self.assertEqual({"scene": "test.scene"}, stored.scope)
        row = self.store.connection.execute(
            "SELECT scope_json FROM record_versions "
            "WHERE namespace = ? AND record_id = ? AND record_version = ?",
            (NS, version.record_id, version.record_version),
        ).fetchone()
        self.assertEqual('{"scene":"test.scene"}', row["scope_json"])
        self.assertEqual(
            set(
                {
                    "claim",
                    "claim_type",
                    "evidence_links",
                    "strength",
                    "strength_reason",
                    "inference_basis",
                    "premise_refs",
                    "alternative_group_id",
                    "valid_until",
                    "review_condition",
                }
            ),
            set(version.body),
        )

    def test_no_evidence_is_proposed_with_insufficient_strength(self) -> None:
        body = self.belief_body(evidence_links=[])
        version = self.propose(body=body, scope={})
        self.assertEqual("proposed", version.status)
        self.assertEqual("insufficient", version.body["strength"])

    def test_weak_requested_strength_is_not_upgraded(self) -> None:
        first = self.ingest(source_instance="one")
        second = self.ingest(source_instance="two")
        body = self.belief_body(
            strength="insufficient",
            evidence_links=[
                self.evidence_link(self.event_ref(first.event_id)),
                self.evidence_link(self.event_ref(second.event_id)),
            ],
        )
        version = self.propose(body=body)
        self.assertEqual("supported", version.status)
        self.assertEqual("insufficient", version.body["strength"])

    # ------------------------------------------------- independence / count

    def test_two_independent_sources_reach_moderate_not_strong(self) -> None:
        first = self.ingest(source_instance="one")
        second = self.ingest(source_instance="two")
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(first.event_id)),
                self.evidence_link(self.event_ref(second.event_id)),
            ]
        )
        version = self.propose(body=body)
        self.assertEqual("supported", version.status)
        self.assertEqual("moderate", version.body["strength"])

    def test_three_independent_sources_can_reach_strong(self) -> None:
        events = [self.ingest(source_instance=f"source-{index}") for index in range(3)]
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(event.event_id))
                for event in events
            ]
        )
        version = self.propose(body=body)
        self.assertEqual("strong", version.body["strength"])

    def test_same_event_via_event_and_episode_counts_once(self) -> None:
        event = self.ingest(event_type="CapabilityObserved:file-read")
        episode = EpisodeService(self.store).build_episode_from_event(
            namespace=NS,
            event_id=event.event_id,
            scope={},
            authorization_ref="auth:episode-test",
        )
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(event.event_id)),
                self.evidence_link(self.record_ref(episode)),
            ]
        )
        version = self.propose(body=body)
        self.assertEqual("limited", version.body["strength"])

    def test_causation_between_sources_collapses_the_count(self) -> None:
        first = self.ingest(source_instance="one")
        second = self.ingest(
            source_instance="two",
            causation_refs=(self.event_ref(first.event_id),),
        )
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(first.event_id)),
                self.evidence_link(self.event_ref(second.event_id)),
            ]
        )
        version = self.propose(body=body)
        self.assertEqual("limited", version.body["strength"])

    def test_imported_memory_source_collapses_the_count(self) -> None:
        observed = self.ingest(source_instance="one")
        imported = self.ingest(
            source_kind="imported_memory", source_instance="two"
        )
        body = self.belief_body(
            claim="hypothesis:files-are-cached",
            claim_type="general_hypothesis",
            evidence_links=[
                self.evidence_link(self.event_ref(observed.event_id)),
                self.evidence_link(self.event_ref(imported.event_id)),
            ],
        )
        version = self.propose(body=body, scope={})
        self.assertEqual("proposed", version.status)
        self.assertEqual("limited", version.body["strength"])

    # ----------------------------------------------------- evidence failures

    def test_duplicate_evidence_ref_is_rejected(self) -> None:
        event = self.ingest()
        link = self.evidence_link(self.event_ref(event.event_id))
        body = self.belief_body(evidence_links=[link, dict(link)])
        with self.assertRaises(InvalidInput):
            self.propose(body=body)

    def test_origin_group_cannot_be_claimed_by_the_model(self) -> None:
        event = self.ingest()
        link = self.evidence_link(
            self.event_ref(event.event_id), origin_group_id="event:invented"
        )
        with self.assertRaises(InvalidInput) as caught:
            self.propose(body=self.belief_body(evidence_links=[link]))
        self.assertIn("origin_group_id", str(caught.exception))

    def test_missing_event_is_evidence_missing(self) -> None:
        missing = str(uuid.uuid4())
        body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(missing))]
        )
        with self.assertRaises(EvidenceMissing) as caught:
            self.propose(body=body)
        self.assertEqual("EVIDENCE_MISSING", caught.exception.code)

    def test_deleted_event_is_source_redacted(self) -> None:
        event = self.ingest()
        # The deletion barrier is tested independently: finish the event's
        # pending work first, otherwise a 006-era-style "deleted source still
        # pending" condition blocks Context assembly before evidence is read.
        self.complete_work_as_wait(event.event_id)
        self.delete_event(event.event_id)
        body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
        )
        with self.assertRaises(SourceRedacted) as caught:
            self.propose(body=body)
        self.assertEqual("SOURCE_REDACTED", caught.exception.code)

    def test_non_episode_record_is_evidence_missing(self) -> None:
        active = self.profiles.get_active_profile(NS)
        body = self.belief_body(evidence_links=[self.evidence_link(active.ref.to_dict())])
        with self.assertRaises(EvidenceMissing):
            self.propose(body=body)

    def test_evidence_namespace_mismatch_is_scope_violation(self) -> None:
        foreign = self.ingest(namespace=NS_B)
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(foreign.event_id, namespace=NS_B))
            ]
        )
        with self.assertRaises(ScopeViolation):
            self.propose(body=body)

    def test_untrusted_source_kind_is_source_untrusted(self) -> None:
        inferred = self.ingest(source_kind="model_inference")
        body = self.belief_body(
            claim="hypothesis:model-says-so",
            claim_type="general_hypothesis",
            evidence_links=[self.evidence_link(self.event_ref(inferred.event_id))],
        )
        with self.assertRaises(SourceUntrusted) as caught:
            self.propose(body=body, scope={})
        self.assertEqual("SOURCE_UNTRUSTED", caught.exception.code)

    # ---------------------------------------------------------- claim types

    def test_user_preference_requires_report_scope_and_one_person(self) -> None:
        report = self.ingest(source_kind="user_report", source_instance="user-1")
        link = self.evidence_link(self.event_ref(report.event_id))
        body = self.belief_body(
            claim="user prefers terse answers",
            claim_type="user_preference",
            evidence_links=[link],
        )
        version = self.propose(body=body, scope={"scene": "chat"})
        self.assertEqual("supported", version.status)
        self.assertEqual("limited", version.body["strength"])

        with self.assertRaises(EvidenceMissing):
            self.propose(body=body, scope={})

        observed = self.ingest(source_kind="host_observation")
        wrong_source = self.belief_body(
            claim="user prefers terse answers",
            claim_type="user_preference",
            evidence_links=[self.evidence_link(self.event_ref(observed.event_id))],
        )
        with self.assertRaises(EvidenceMissing):
            self.propose(body=wrong_source, scope={"scene": "chat"})

        other = self.ingest(source_kind="user_report", source_instance="user-2")
        two_people = self.belief_body(
            claim="user prefers terse answers",
            claim_type="user_preference",
            evidence_links=[
                link,
                self.evidence_link(self.event_ref(other.event_id)),
            ],
        )
        with self.assertRaises(EvidenceMissing) as caught:
            self.propose(body=two_people, scope={"scene": "chat"})
        self.assertIn("one reporting person", str(caught.exception))

    def test_environment_state_needs_scope_and_deadline_and_goes_stale(self) -> None:
        observed = self.ingest(source_kind="host_observation")
        link = self.evidence_link(self.event_ref(observed.event_id))
        body = self.belief_body(
            claim="file h1 exposes the stable interface",
            claim_type="environment_state",
            evidence_links=[link],
            valid_until="2026-10-17T13:00:00Z",
        )
        version = self.propose(
            body=body, scope={"entity_ids": ["file-h1"]}
        )
        self.assertEqual("supported", version.status)

        without_deadline = self.belief_body(
            claim="file h1 exposes the stable interface",
            claim_type="environment_state",
            evidence_links=[link],
            valid_until=None,
        )
        with self.assertRaises(EvidenceMissing):
            self.propose(body=without_deadline, scope={"entity_ids": ["file-h1"]})

        with self.assertRaises(EvidenceMissing):
            self.propose(body=body, scope={})

        expired = self.belief_body(
            claim="file h1 exposes the stable interface",
            claim_type="environment_state",
            evidence_links=[link],
            valid_until="2026-10-17T11:00:00Z",
        )
        stale = self.revise(
            self.record_ref(version),
            body=expired,
            scope={"entity_ids": ["file-h1"]},
        )
        self.assertEqual("stale", stale.status)
        self.assertEqual(2, stale.record_version)

    def test_capability_support_cannot_come_from_a_user_report(self) -> None:
        report = self.ingest(source_kind="user_report")
        body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(report.event_id))]
        )
        with self.assertRaises(EvidenceMissing):
            self.propose(body=body)

    def test_capability_support_must_declare_a_scope(self) -> None:
        observed = self.ingest(source_kind="host_observation")
        body = self.belief_body(
            evidence_links=[self.evidence_link(self.event_ref(observed.event_id))]
        )
        with self.assertRaises(EvidenceMissing):
            self.propose(body=body, scope={})

    def test_general_hypothesis_stays_proposed(self) -> None:
        observed = self.ingest(source_kind="host_observation")
        body = self.belief_body(
            claim="hypothesis:the cache explains the latency",
            claim_type="general_hypothesis",
            evidence_links=[self.evidence_link(self.event_ref(observed.event_id))],
        )
        version = self.propose(body=body, scope={})
        self.assertEqual("proposed", version.status)
        self.assertEqual("limited", version.body["strength"])

    def test_derived_rule_needs_supported_premises_and_stays_proposed(self) -> None:
        observed = self.ingest(source_kind="host_observation")
        premise = self.propose(
            body=self.belief_body(
                claim="capability:file-read",
                evidence_links=[self.evidence_link(self.event_ref(observed.event_id))],
            )
        )
        premise_ref = self.record_ref(premise)
        derived = self.belief_body(
            claim="derived:file-read-implies-file-list",
            claim_type="derived_rule",
            evidence_links=[],
            premise_refs=[premise_ref],
            strength="insufficient",
        )
        version = self.propose(body=derived)
        self.assertEqual("proposed", version.status)

        missing = self.belief_body(
            claim="derived:other",
            claim_type="derived_rule",
            evidence_links=[],
            premise_refs=[
                {
                    "ref_type": "record",
                    "namespace": NS,
                    "record_id": str(uuid.uuid4()),
                    "record_version": 1,
                }
            ],
            strength="insufficient",
        )
        with self.assertRaises(EvidenceMissing):
            self.propose(body=missing)

        unsupported = self.propose(body=self.belief_body(evidence_links=[]), scope={})
        unsupported_derived = self.belief_body(
            claim="derived:unsupported",
            claim_type="derived_rule",
            evidence_links=[],
            premise_refs=[self.record_ref(unsupported)],
            strength="insufficient",
        )
        with self.assertRaises(EvidenceMissing):
            self.propose(body=unsupported_derived)

    def test_derived_rule_without_premises_is_rejected(self) -> None:
        body = self.belief_body(
            claim="derived:empty",
            claim_type="derived_rule",
            evidence_links=[],
            premise_refs=[],
        )
        with self.assertRaises(EvidenceMissing):
            self.propose(body=body)

    # ------------------------------------------------------- status matrix

    def test_support_and_contradiction_produce_contested(self) -> None:
        support = self.ingest(event_type="CapabilityObserved:file-read")
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(support.event_id)),
                self.evidence_link(
                    self.event_ref(contradict.event_id), relation="contradicts"
                ),
            ]
        )
        version = self.propose(body=body)
        self.assertEqual("contested", version.status)

    def test_contradiction_only_produces_contradicted(self) -> None:
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(
                    self.event_ref(contradict.event_id), relation="contradicts"
                )
            ]
        )
        version = self.propose(body=body)
        self.assertEqual("contradicted", version.status)
        self.assertEqual("insufficient", version.body["strength"])

    # ---------------------------------------------------------- body rules

    def test_unknown_or_missing_body_field_is_rejected(self) -> None:
        body = self.belief_body()
        body["status"] = "supported"
        with self.assertRaises(InvalidInput):
            self.propose(body=body)
        missing = self.belief_body()
        del missing["review_condition"]
        with self.assertRaises(InvalidInput):
            self.propose(body=missing)

    def test_inference_basis_must_match_claim_type(self) -> None:
        body = self.belief_body(inference_basis="deductive")
        with self.assertRaises(InvalidInput):
            self.propose(body=body)

    def test_strength_is_an_ordinal_not_a_probability(self) -> None:
        body = self.belief_body(strength=0.9)
        with self.assertRaises(InvalidInput):
            self.propose(body=body)

    def test_valid_until_must_be_a_timestamp(self) -> None:
        body = self.belief_body(valid_until="tomorrow")
        with self.assertRaises(InvalidInput):
            self.propose(body=body)

    # ------------------------------------------------------- command / Tx B

    def test_proposal_is_a_tx_b_command_with_a_receipt(self) -> None:
        context = self.build_context()
        body = self.belief_body(evidence_links=[])
        request_id = str(uuid.uuid4())
        before = self.revision()
        version = self.propose(
            body=body, context_id=context.context_id, request_id=request_id
        )
        self.assertEqual(before + 1, self.revision())
        row = self.store.connection.execute(
            "SELECT operation, receipt_json FROM processed_commands "
            "WHERE namespace = ? AND request_id = ?",
            (NS, request_id),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual("belief.propose", row["operation"])
        self.assertIn('"outcome":"committed"', row["receipt_json"])

    def test_retry_with_the_same_request_id_returns_the_same_version(self) -> None:
        context = self.build_context()
        body = self.belief_body(evidence_links=[])
        request_id = str(uuid.uuid4())
        first = self.propose(
            body=body, context_id=context.context_id, request_id=request_id
        )
        versions_before = self.count_rows("record_versions")
        second = self.propose(
            body=body, context_id=context.context_id, request_id=request_id
        )
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(first.record_version, second.record_version)
        self.assertEqual(versions_before, self.count_rows("record_versions"))

    def test_same_request_id_with_different_content_is_id_conflict(self) -> None:
        context = self.build_context()
        request_id = str(uuid.uuid4())
        self.propose(
            body=self.belief_body(claim="capability:one", evidence_links=[]),
            context_id=context.context_id,
            request_id=request_id,
        )
        with self.assertRaises(CommandConflict):
            self.propose(
                body=self.belief_body(claim="capability:two", evidence_links=[]),
                context_id=context.context_id,
                request_id=request_id,
            )

    def test_old_006_snapshot_is_needs_context(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        with self.assertRaises(NeedsContext) as caught:
            self.beliefs.propose_belief(
                namespace=NS,
                context_id=snapshot.context_id,
                request_id=str(uuid.uuid4()),
                body=self.belief_body(evidence_links=[]),
                scope={},
            )
        self.assertEqual("NEEDS_CONTEXT", caught.exception.code)

    def test_unknown_context_id_is_not_found(self) -> None:
        from quintessence_pcs.foundation.errors import SnapshotNotFound

        with self.assertRaises(SnapshotNotFound):
            self.beliefs.propose_belief(
                namespace=NS,
                context_id=str(uuid.uuid4()),
                request_id=str(uuid.uuid4()),
                body=self.belief_body(evidence_links=[]),
                scope={},
            )

    def test_event_watermark_move_is_context_stale(self) -> None:
        context = self.build_context()
        self.ingest()  # another accepted event moves last_event_seq
        with self.assertRaises(ContextStale):
            self.beliefs.propose_belief(
                namespace=NS,
                context_id=context.context_id,
                request_id=str(uuid.uuid4()),
                body=self.belief_body(evidence_links=[]),
                scope={},
            )

    def test_state_revision_move_is_revision_conflict(self) -> None:
        context = self.build_context()
        self.propose(
            body=self.belief_body(claim="capability:other", evidence_links=[]),
            scope={},
            context_id=context.context_id,
        )
        with self.assertRaises(StateRevisionConflict):
            self.beliefs.propose_belief(
                namespace=NS,
                context_id=context.context_id,
                request_id=str(uuid.uuid4()),
                body=self.belief_body(claim="capability:third", evidence_links=[]),
                scope={},
            )

    def test_require_live_context_rejects_a_namespace_mismatch(self) -> None:
        context = self.build_context()
        with self.assertRaises(ScopeViolation):
            require_live_context(
                self.store,
                context_id=context.context_id,
                namespace=NS_B,
            )


class BeliefRevisionTests(CognitionTestCase):
    def _supported_belief(self, event_type: str = "CapabilityObserved:file-read"):
        event = self.ingest(event_type=event_type)
        return (
            self.propose(
                body=self.belief_body(
                    evidence_links=[
                        self.evidence_link(self.event_ref(event.event_id))
                    ]
                )
            ),
            event,
        )

    def test_revise_appends_a_new_version_and_keeps_the_claim(self) -> None:
        version, first_event = self._supported_belief()
        second = self.ingest(source_instance="another-test")
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(first_event.event_id)),
                self.evidence_link(self.event_ref(second.event_id)),
            ]
        )
        revised = self.revise(self.record_ref(version), body=body)
        self.assertEqual(version.record_id, revised.record_id)
        self.assertEqual(2, revised.record_version)
        self.assertEqual(version.body["claim"], revised.body["claim"])
        self.assertEqual("moderate", revised.body["strength"])
        self.assertEqual(1, self.store.get_version(NS, version.record_id, 1).record_version)

    def test_revise_adds_a_contradiction_and_becomes_contested(self) -> None:
        version, first_event = self._supported_belief()
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        body = self.belief_body(
            evidence_links=[
                self.evidence_link(self.event_ref(first_event.event_id)),
                self.evidence_link(
                    self.event_ref(contradict.event_id), relation="contradicts"
                ),
            ]
        )
        revised = self.revise(self.record_ref(version), body=body)
        self.assertEqual("contested", revised.status)

    def test_revise_cannot_change_the_claim_text(self) -> None:
        version, _event = self._supported_belief()
        body = self.belief_body(claim="capability:different", evidence_links=[])
        with self.assertRaises(InvalidTransition):
            self.revise(self.record_ref(version), body=body)

    def test_revise_cannot_change_the_claim_type(self) -> None:
        version, _event = self._supported_belief()
        body = self.belief_body(
            claim=version.body["claim"],
            claim_type="general_hypothesis",
            evidence_links=[],
        )
        with self.assertRaises(InvalidTransition):
            self.revise(self.record_ref(version), body=body)

    def test_retract_proposal_withdraws_and_keeps_history(self) -> None:
        version, event = self._supported_belief()
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        context = self.build_context()
        work = self.work_for(event.event_id)
        self.assertIsNotNone(work)
        submission = self.beliefs.submit_proposal(
            namespace=NS,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            proposal={
                "operation": "retract_belief",
                "target": self.record_ref(version),
                "evidence_links": [
                    self.evidence_link(
                        self.event_ref(contradict.event_id),
                        relation="contradicts",
                    )
                ],
                "reason": "new contradicting observation",
            },
            work_id=work.work_id,
        )
        self.assertEqual("retracted", submission.version.status)
        self.assertEqual(version.record_id, submission.version.record_id)
        self.assertEqual(version.record_version + 1, submission.version.record_version)
        self.assertEqual(
            "supported",
            self.store.get_version(
                NS, version.record_id, version.record_version
            ).status,
        )
        self.assertIsNone(self.work_for(event.event_id))

    def test_retracted_is_terminal(self) -> None:
        version, event = self._supported_belief()
        retraction = self.belief_body(
            evidence_links=[
                self.evidence_link(
                    self.event_ref(event.event_id), relation="contradicts"
                )
            ]
        )
        retracted = self.revise(
            self.record_ref(version), body=retraction, retract=True
        )
        self.assertEqual("retracted", retracted.status)
        with self.assertRaises(InvalidTransition):
            self.revise(self.record_ref(retracted), body=retraction)

    def test_retraction_without_evidence_is_rejected(self) -> None:
        version, _event = self._supported_belief()
        with self.assertRaises(EvidenceMissing):
            self.revise(
                self.record_ref(version),
                body=self.belief_body(evidence_links=[]),
                retract=True,
            )

    def test_contradicted_cannot_be_reopened(self) -> None:
        contradict = self.ingest(event_type="CapabilityFailed:file-read")
        version = self.propose(
            body=self.belief_body(
                evidence_links=[
                    self.evidence_link(
                        self.event_ref(contradict.event_id), relation="contradicts"
                    )
                ]
            )
        )
        self.assertEqual("contradicted", version.status)
        with self.assertRaises(InvalidTransition):
            self.revise(
                self.record_ref(version),
                body=self.belief_body(evidence_links=[]),
            )

    def test_revise_an_older_version_is_revision_conflict(self) -> None:
        version, _event = self._supported_belief()
        body = self.belief_body(evidence_links=[])
        second = self.revise(self.record_ref(version), body=body)
        self.assertEqual(2, second.record_version)
        with self.assertRaises(StateRevisionConflict):
            self.revise(self.record_ref(version), body=body)

    def test_revise_narrows_scope_in_the_record_envelope(self) -> None:
        event = self.ingest()
        version = self.propose(
            body=self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
            ),
            scope={"scene": "test.scene"},
        )
        narrowed = self.revise(
            self.record_ref(version),
            body=self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
            ),
            scope={"scene": "test.scene", "project_id": "project-a"},
        )
        self.assertEqual(2, narrowed.record_version)
        self.assertEqual(
            {"scene": "test.scene", "project_id": "project-a"}, narrowed.scope
        )
        self.assertEqual({"scene": "test.scene"}, version.scope)
        self.assertEqual(version.body["claim"], narrowed.body["claim"])

    def test_revise_without_change_is_a_legal_noop(self) -> None:
        version, _event = self._supported_belief()
        context = self.build_context()
        before_revision = self.revision()
        before_versions = self.count_rows("record_versions")
        result = self.beliefs.revise_belief(
            namespace=NS,
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            target=self.record_ref(version),
            body=dict(version.body),
            scope=dict(version.scope or {}),
        )
        self.assertEqual(version.record_id, result.record_id)
        self.assertEqual(version.record_version, result.record_version)
        self.assertEqual(before_revision, self.revision())
        self.assertEqual(before_versions, self.count_rows("record_versions"))

    def test_substantive_change_retracts_then_creates_a_new_id(self) -> None:
        old, event = self._supported_belief()
        retraction = self.belief_body(
            evidence_links=[
                self.evidence_link(
                    self.event_ref(event.event_id), relation="contradicts"
                )
            ]
        )
        retracted = self.revise(
            self.record_ref(old), body=retraction, retract=True
        )
        self.assertEqual("retracted", retracted.status)

        new = self.propose(
            body=self.belief_body(
                claim="capability:file-write",
                evidence_links=[
                    self.evidence_link(self.event_ref(event.event_id))
                ],
            )
        )
        self.assertNotEqual(old.record_id, new.record_id)
        self.assertEqual("supported", new.status)
        self.assertEqual(
            "retracted",
            self.store.get_current(NS, old.record_id).status,
        )

    def test_revise_target_must_be_a_belief(self) -> None:
        active = self.profiles.get_active_profile(NS)
        with self.assertRaises(RecordKindMismatch):
            self.revise(active.ref.to_dict(), body=self.belief_body(evidence_links=[]))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
