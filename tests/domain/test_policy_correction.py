"""J06 policy correction (v0.2): the PC-01..PC-10 acceptance cases.

These drive the real service path -- propose, review, commit -- against synthetic
events.  No model call, no network, no private data.

The cases pin four behaviours that used to be impossible or ambiguous:

* a local, evidence-free tentative claim can be submitted autonomously;
* an ``external_report`` becomes ``verified`` only when a verbatim quote really
  appears in a captured fetch body whose digest still matches;
* a supporting link of the wrong kind is *rejected*, not quietly downgraded;
* ``strength`` can never mask a high-impact change.
"""

from __future__ import annotations

import hashlib
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.collaboration.change_service import (  # noqa: E402
    ChangeAccess,
    CognitionChangeService,
)
from quintessence_pcs.collaboration.policy import CommitAuthority  # noqa: E402
from quintessence_pcs.domains.cognition.errors import EvidenceMissing  # noqa: E402
from quintessence_pcs.store.governance import GovernanceAccess, GovernanceStore  # noqa: E402

PAGE_TEXT = (
    "Project note: the build pipeline uses a single queue. "
    "Later sections discuss caching."
)
QUOTE = "the build pipeline uses a single queue"


def access(**overrides: object) -> ChangeAccess:
    values: dict[str, object] = {
        "namespace": NS,
        "host_instance": "synthetic-host",
        "operator": "operator-a",
        "access_ref": "access:synthetic",
        "permissions": frozenset(
            {"change.propose", "change.review", "change.commit", "change.result"}
        ),
        "privacy_classes": frozenset({"private"}),
        "purposes": frozenset({"cognition.change"}),
    }
    values.update(overrides)
    return ChangeAccess(**values)  # type: ignore[arg-type]


def governance_access() -> GovernanceAccess:
    return GovernanceAccess(
        namespace=NS,
        host_instance="synthetic-host",
        operator="operator-a",
        access_ref="access:governance",
        permissions=frozenset({"governance.admin", "governance.read"}),
    )


class PolicyCorrectionTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.changes = CognitionChangeService(
            self.store, beliefs=self.beliefs, clock=lambda: self.now_string()
        )
        self.governance = GovernanceStore(self.store)

    def now_string(self) -> str:
        return "2026-10-27T00:00:00Z"

    # ------------------------------------------------------------- helpers

    def fetch_event(self, *, text: str | None = PAGE_TEXT, bad_digest: bool = False):
        payload: dict[str, object] = {
            "url": "https://example.test/note",
            "fetched_at": self.now_string(),
            "status": 200,
            "content_type": "text/markdown",
            "truncated": False,
        }
        if text is not None:
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if bad_digest:
                digest = "0" * 64
            payload["text"] = text
            payload["content_sha256"] = digest
        return self.ingest(event_type="WebPageFetched:v1", payload=payload)

    def evidence_link(self, event_id: str, *, quote: str | None = QUOTE, relation: str = "supports"):
        link: dict[str, object] = {
            "ref": self.event_ref(event_id),
            "relation": relation,
            "origin_group_id": f"event:{event_id}",
            "applicability_reason": "the page states it in the first sentence",
        }
        if quote is not None:
            link["quote"] = quote
        return link

    def bundle(self, body: dict, *, scope: dict | None = None, op: str = "propose_belief"):
        change: dict[str, object] = {"op": op, "body": body}
        if op == "propose_belief":
            change["scope"] = {"scene": "test.scene"} if scope is None else scope
        return {"summary": "synthetic policy-correction bundle", "changes": [change]}

    def propose(self, bundle: dict):
        context = self.build_context()
        return self.changes.propose(
            access(),
            context_id=context.context_id,
            request_id=str(uuid.uuid4()),
            bundle=bundle,
        )

    def grant(self, *, claim_types=("external_report",), max_impact="E1") -> str:
        authorization_id = str(uuid.uuid4())
        self.governance.grant_commit_authorization(
            governance_access(),
            authorization_id=authorization_id,
            host_instance="synthetic-host",
            access_ref="access:synthetic",
            operations=("cognition.change",),
            claim_types=claim_types,
            scope={"scene": "test.scene"},
            max_impact=max_impact,
            max_changes_per_bundle=2,
            max_targets_per_bundle=2,
            policy_id="policy:cognition.risk.v1",
            policy_version="1",
            authorization_source="host_config",
            issued_at=self.now_string(),
            expires_at="2027-01-01T00:00:00Z",
        )
        return authorization_id

    def record_count(self) -> int:
        return int(
            self.store.connection.execute(
                "SELECT COUNT(*) FROM record_versions"
            ).fetchone()[0]
        )

    def decision_rows(self, candidate_id: str):
        return list(
            self.store.connection.execute(
                "SELECT decision, reason_code, impact, evidence_linkage "
                "FROM cognition_review_decisions WHERE candidate_id = ? "
                "ORDER BY decision_seq",
                (candidate_id,),
            )
        )

    # ---------------------------------------------------------------- PC-01

    def test_pc_01_local_evidence_free_tentative_claim_is_autonomous(self) -> None:
        body = self.belief_body(
            claim="this scene's retry budget looks too small",
            claim_type="environment_state",
            strength="insufficient",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[],
        )
        result = self.propose(self.bundle(body))
        reviewed = self.changes.review(
            access(), candidate_id=result.candidate_id, decision_id=str(uuid.uuid4())
        )
        self.assertEqual("E0", reviewed.impact)
        self.assertFalse(reviewed.requires_human)
        self.assertEqual("PROPOSED", reviewed.state)

        authorization_id = self.grant(
            claim_types=("environment_state",), max_impact="E0"
        )
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization_id),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        self.assertEqual(1, len(committed.record_refs))

    # ---------------------------------------------------------------- PC-02

    def test_pc_02_external_report_with_matching_quote_is_verified(self) -> None:
        event = self.fetch_event()
        body = self.belief_body(
            claim="the project note says the build pipeline uses a single queue",
            claim_type="external_report",
            strength="limited",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[self.evidence_link(event.event_id)],
        )
        result = self.propose(self.bundle(body))
        reviewed = self.changes.review(
            access(), candidate_id=result.candidate_id, decision_id=str(uuid.uuid4())
        )
        self.assertEqual("E1", reviewed.impact)
        self.assertEqual("verified", reviewed.evidence_linkage)

        authorization_id = self.grant()
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=authorization_id),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)

    # ---------------------------------------------------------------- PC-03

    def test_pc_03_quote_not_in_the_captured_body_is_rejected(self) -> None:
        event = self.fetch_event()
        body = self.belief_body(
            claim="the project note says the build pipeline uses a single queue",
            claim_type="external_report",
            strength="limited",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[self.evidence_link(event.event_id, quote="the queue is sharded")],
        )
        result = self.propose(self.bundle(body))
        before = self.record_count()
        with self.assertRaises(EvidenceMissing) as caught:
            self.changes.commit(
                access(),
                candidate_id=result.candidate_id,
                authority=CommitAuthority(auto_authorization_id=self.grant()),
                decision_id=str(uuid.uuid4()),
            )
        self.assertEqual(
            "evidence_quote_mismatch", getattr(caught.exception, "reason_code", None)
        )
        self.assertEqual(before, self.record_count())

    # ---------------------------------------------------------------- PC-04

    def test_pc_04_digest_mismatch_is_rejected(self) -> None:
        event = self.fetch_event(bad_digest=True)
        body = self.belief_body(
            claim="the project note says the build pipeline uses a single queue",
            claim_type="external_report",
            strength="limited",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[self.evidence_link(event.event_id)],
        )
        result = self.propose(self.bundle(body))
        before = self.record_count()
        with self.assertRaises(EvidenceMissing) as caught:
            self.changes.commit(
                access(),
                candidate_id=result.candidate_id,
                authority=CommitAuthority(auto_authorization_id=self.grant()),
                decision_id=str(uuid.uuid4()),
            )
        self.assertEqual(
            "evidence_content_hash_mismatch",
            getattr(caught.exception, "reason_code", None),
        )
        self.assertEqual(before, self.record_count())

    # ---------------------------------------------------------------- PC-05

    def test_pc_05_uncaptured_fetch_cannot_support_a_report(self) -> None:
        event = self.fetch_event(text=None)  # metadata only, no captured body
        body = self.belief_body(
            claim="the project note says the build pipeline uses a single queue",
            claim_type="external_report",
            strength="limited",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[self.evidence_link(event.event_id)],
        )
        result = self.propose(self.bundle(body))
        before = self.record_count()
        with self.assertRaises(EvidenceMissing) as caught:
            self.changes.commit(
                access(),
                candidate_id=result.candidate_id,
                authority=CommitAuthority(auto_authorization_id=self.grant()),
                decision_id=str(uuid.uuid4()),
            )
        self.assertEqual(
            "evidence_text_unavailable", getattr(caught.exception, "reason_code", None)
        )
        self.assertEqual(before, self.record_count())

    # ---------------------------------------------------------------- PC-06

    def test_pc_06_unrelated_event_is_a_mismatch_not_a_weak_support(self) -> None:
        trigger = self.ingest(event_type="TestTrigger")
        body = self.belief_body(
            claim="the scene retries three times",
            claim_type="environment_state",
            strength="insufficient",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[self.evidence_link(trigger.event_id, quote=None)],
        )
        reviewed = self.propose(self.bundle(body))
        result = self.changes.review(
            access(), candidate_id=reviewed.candidate_id, decision_id=str(uuid.uuid4())
        )
        self.assertEqual("REJECTED", result.state)
        self.assertEqual("evidence_type_mismatch", result.reason_code)
        rows = self.decision_rows(reviewed.candidate_id)
        self.assertEqual("rejected", rows[-1][0])
        self.assertEqual("evidence_type_mismatch", rows[-1][1])

        # A second, still-pending candidate: the commit path refuses the same
        # association before consulting any authority, and records why.
        other = self.propose(self.bundle(body))
        committed = self.changes.commit(
            access(),
            candidate_id=other.candidate_id,
            authority=CommitAuthority(auto_authorization_id=self.grant()),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("REJECTED", committed.state)
        self.assertEqual("evidence_type_mismatch", committed.reason_code)

    # ---------------------------------------------------------------- PC-07

    def test_pc_07_weak_strength_does_not_lower_a_global_change(self) -> None:
        body = self.belief_body(
            claim="a global hypothesis",
            claim_type="general_hypothesis",
            strength="insufficient",
            evidence_links=[],
        )
        bundle = self.bundle(body, scope={})
        result = self.propose(bundle)
        reviewed = self.changes.review(
            access(), candidate_id=result.candidate_id, decision_id=str(uuid.uuid4())
        )
        self.assertEqual("E2", reviewed.impact)
        self.assertTrue(reviewed.requires_human)

    # ---------------------------------------------------------------- PC-08

    def test_pc_08_high_impact_retraction_stays_e2(self) -> None:
        event = self.fetch_event()
        body = self.belief_body(
            claim="the project note says the build pipeline uses a single queue",
            claim_type="external_report",
            strength="limited",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[self.evidence_link(event.event_id)],
        )
        result = self.propose(self.bundle(body))
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=self.grant()),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        target = {"ref_type": "record", "namespace": NS, **committed.record_refs[0]}
        counter = self.ingest(event_type="WebPageFetched:v1", payload={
            "url": "https://example.test/note",
            "fetched_at": self.now_string(),
            "status": 200,
            "content_type": "text/markdown",
            "truncated": False,
            "text": PAGE_TEXT,
            "content_sha256": hashlib.sha256(PAGE_TEXT.encode("utf-8")).hexdigest(),
        })

        retract = {
            "summary": "retract the report",
            "changes": [
                {
                    "op": "retract_belief",
                    "target": dict(target),
                    "evidence_links": [
                        self.evidence_link(counter.event_id, quote=None, relation="contradicts")
                    ],
                    "reason": "a later fetch contradicts the earlier report",
                }
            ],
        }
        retraction = self.propose(retract)
        reviewed = self.changes.review(
            access(),
            candidate_id=retraction.candidate_id,
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("E2", reviewed.impact)
        self.assertTrue(reviewed.requires_human)

    # ---------------------------------------------------------------- PC-09

    def test_pc_09_a_verified_report_is_not_a_fact_about_the_world(self) -> None:
        event = self.fetch_event()
        report = self.belief_body(
            claim="the project note says the build pipeline uses a single queue",
            claim_type="external_report",
            strength="limited",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[self.evidence_link(event.event_id)],
        )
        result = self.propose(self.bundle(report))
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=self.grant()),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        self.assertEqual("verified", committed.evidence_linkage)

        # The same fetch cannot support a statement about the world.
        world = self.belief_body(
            claim="the build pipeline uses a single queue",
            claim_type="environment_state",
            strength="insufficient",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[self.evidence_link(event.event_id, quote=None)],
        )
        other = self.propose(self.bundle(world))
        rejected = self.changes.commit(
            access(),
            candidate_id=other.candidate_id,
            authority=CommitAuthority(auto_authorization_id=self.grant()),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("REJECTED", rejected.state)
        self.assertEqual("evidence_type_mismatch", rejected.reason_code)

    # ---------------------------------------------------------------- PC-10

    def test_pc_10_a_tentative_claim_can_be_revised_against_new_evidence(self) -> None:
        first = self.ingest(event_type="WebPageFetched:v1", payload={
            "url": "https://example.test/note",
            "fetched_at": self.now_string(),
            "status": 200,
            "content_type": "text/markdown",
            "truncated": False,
            "text": PAGE_TEXT,
            "content_sha256": hashlib.sha256(PAGE_TEXT.encode("utf-8")).hexdigest(),
        })
        report = self.belief_body(
            claim="the project note says the build pipeline uses a single queue",
            claim_type="external_report",
            strength="limited",
            valid_until="2026-12-31T00:00:00Z",
            evidence_links=[self.evidence_link(first.event_id)],
        )
        result = self.propose(self.bundle(report))
        committed = self.changes.commit(
            access(),
            candidate_id=result.candidate_id,
            authority=CommitAuthority(auto_authorization_id=self.grant()),
            decision_id=str(uuid.uuid4()),
        )
        self.assertEqual("COMMITTED", committed.state)
        target = {"ref_type": "record", "namespace": NS, **committed.record_refs[0]}

        counter = self.ingest(event_type="WebPageFetched:v1", payload={
            "url": "https://example.test/note",
            "fetched_at": self.now_string(),
            "status": 200,
            "content_type": "text/markdown",
            "truncated": False,
            "text": PAGE_TEXT,
            "content_sha256": hashlib.sha256(PAGE_TEXT.encode("utf-8")).hexdigest(),
        })
        revision = {
            "summary": "a later note contradicts the queue claim",
            "changes": [
                {
                    "op": "revise_belief",
                    "target": dict(target),
                    "body": self.belief_body(
                        claim=report["claim"],
                        claim_type="external_report",
                        strength="limited",
                        valid_until="2026-12-31T00:00:00Z",
                        evidence_links=[
                            self.evidence_link(counter.event_id, relation="contradicts")
                        ],
                    ),
                    "scope": {"scene": "test.scene"},
                }
            ],
        }
        proposed = self.propose(revision)
        self.assertEqual("PROPOSED", proposed.state)
        versions = self.store.connection.execute(
            "SELECT COUNT(*) FROM record_versions WHERE record_id = ?",
            (target["record_id"],),
        ).fetchone()[0]
        self.assertEqual(1, versions)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
