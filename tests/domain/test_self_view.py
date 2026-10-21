"""PCS2-008 B group: minimal SelfView, freshness and idempotent rebuild.

The r2 correction is asserted directly: a changed input set is ``stale``, not
``CORRUPT_STORE``; a deleted referenced Belief is ``unavailable`` and no
content/ref is returned; only a genuinely broken reference structure is
corruption.  The projection never writes independent self-description text,
and an unchanged input set is a legal no-op (no new version, no revision).
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

from cognition_support import NS, NS_B, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition.views import (  # noqa: E402
    SELF_VIEW_FRESH,
    SELF_VIEW_STALE,
    SELF_VIEW_UNAVAILABLE,
    parse_self_view_body,
    self_view_record_id,
)
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CorruptStore,
    Redacted,
    StandaloneTransactionRequired,
)


class SelfViewTests(CognitionTestCase):
    # --------------------------------------------------------------- shape

    def test_empty_self_view_is_fresh_and_has_a_ref(self) -> None:
        state = self.self_view.get_self_view(NS)
        self.assertEqual(SELF_VIEW_FRESH, state.freshness)
        self.assertIsNotNone(state.ref)
        self.assertEqual(1, state.ref.record_version)
        version = self.store.get_version(NS, state.ref.record_id, 1)
        self.assertEqual("self_view", version.kind)
        body = parse_self_view_body(version)
        self.assertEqual("self", body["entity_id"])
        self.assertEqual([], body["belief_refs"])
        self.assertEqual([], version.body["observed_config_refs"])

    def test_no_independent_self_description_is_written(self) -> None:
        state = self.self_view.get_self_view(NS)
        version = self.store.get_version(
            NS, state.ref.record_id, state.ref.record_version
        )
        self.assertEqual(
            {"entity_id", "belief_refs", "observed_config_refs"},
            set(version.body),
        )
        self.assertEqual("self", version.body["entity_id"])

    def test_record_id_is_deterministic_per_namespace(self) -> None:
        self.assertEqual(self_view_record_id(NS), self_view_record_id(NS))
        self.assertNotEqual(self_view_record_id(NS), self_view_record_id(NS_B))

    # ------------------------------------------------------------ selection

    def _support_event(self, *, instance: str = "capability-tests"):
        return self.ingest(
            source_instance=instance, event_type="CapabilityObserved:file-read"
        )

    def _supported(self, *, instance: str = "capability-tests"):
        event = self._support_event(instance=instance)
        return self.propose(
            body=self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
            )
        )

    def _contested(self, *, instance: str = "contested-tests"):
        support = self._support_event(instance=instance)
        contradict = self.ingest(
            source_instance=instance, event_type="CapabilityFailed:file-read"
        )
        return self.propose(
            body=self.belief_body(
                evidence_links=[
                    self.evidence_link(self.event_ref(support.event_id)),
                    self.evidence_link(
                        self.event_ref(contradict.event_id), relation="contradicts"
                    ),
                ]
            )
        )

    def _proposed(self):
        return self.propose(body=self.belief_body(evidence_links=[]), scope={})

    def _contradicted(self, *, instance: str = "contradicted-tests"):
        event = self.ingest(
            source_instance=instance, event_type="CapabilityFailed:file-read"
        )
        return self.propose(
            body=self.belief_body(
                evidence_links=[
                    self.evidence_link(
                        self.event_ref(event.event_id), relation="contradicts"
                    )
                ]
            )
        )

    def _user_preference(self):
        report = self.ingest(source_kind="user_report", source_instance="user-1")
        return self.propose(
            body=self.belief_body(
                claim="user prefers terse answers",
                claim_type="user_preference",
                evidence_links=[self.evidence_link(self.event_ref(report.event_id))],
            ),
            scope={"scene": "chat"},
        )

    def test_selection_is_supported_or_contested_capability_only(self) -> None:
        supported = self._supported()
        contested = self._contested()
        proposed = self._proposed()
        contradicted = self._contradicted()
        preference = self._user_preference()

        version = self.self_view.build_self_view(namespace=NS)
        body = parse_self_view_body(version)
        ids = {ref.record_id for ref in body["belief_refs"]}
        self.assertIn(supported.record_id, ids)
        self.assertIn(contested.record_id, ids)
        self.assertNotIn(proposed.record_id, ids)
        self.assertNotIn(contradicted.record_id, ids)
        self.assertNotIn(preference.record_id, ids)
        self.assertEqual(
            sorted((ref.record_id, ref.record_version) for ref in body["belief_refs"]),
            sorted(
                (
                    (supported.record_id, supported.record_version),
                    (contested.record_id, contested.record_version),
                )
            ),
        )

    def test_refs_are_exact_current_versions(self) -> None:
        belief = self._supported()
        version = self.self_view.build_self_view(namespace=NS)
        body = parse_self_view_body(version)
        self.assertEqual(
            [(belief.record_id, belief.record_version)],
            [(ref.record_id, ref.record_version) for ref in body["belief_refs"]],
        )

    # ------------------------------------------------------------ freshness

    def test_idempotent_rebuild_does_not_add_a_version(self) -> None:
        self._supported()
        first = self.self_view.build_self_view(namespace=NS)
        revision = self.revision()
        second = self.self_view.build_self_view(namespace=NS)
        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(first.record_version, second.record_version)
        self.assertEqual(revision, self.revision())
        self.assertEqual(SELF_VIEW_FRESH, self.self_view.get_self_view(NS).freshness)

    def test_new_belief_makes_the_projection_stale_not_corrupt(self) -> None:
        self._supported()
        self.self_view.build_self_view(namespace=NS)
        self._supported(instance="second-test")
        state = self.self_view.get_self_view(NS)
        self.assertEqual(SELF_VIEW_STALE, state.freshness)
        self.assertIsNotNone(state.ref)

    def test_input_change_appends_exactly_one_version(self) -> None:
        first = self._supported()
        projection = self.self_view.build_self_view(namespace=NS)
        self._supported(instance="second-test")
        self.assertEqual(SELF_VIEW_STALE, self.self_view.get_self_view(NS).freshness)
        revision_before = self.revision()
        rebuilt = self.self_view.build_self_view(namespace=NS)
        self.assertEqual(projection.record_version + 1, rebuilt.record_version)
        self.assertEqual(revision_before + 1, self.revision())
        self.assertEqual(SELF_VIEW_FRESH, self.self_view.get_self_view(NS).freshness)
        refs = parse_self_view_body(rebuilt)["belief_refs"]
        self.assertEqual(2, len(refs))
        self.assertIn(first.record_id, {ref.record_id for ref in refs})

    def test_status_change_makes_the_projection_stale(self) -> None:
        belief = self._supported()
        self.self_view.build_self_view(namespace=NS)
        # A revision that removes the supporting evidence: the current version
        # is now proposed, so it leaves the SelfView input set.
        revised = self.revise(
            self.record_ref(belief),
            body=self.belief_body(evidence_links=[]),
            scope={},
        )
        self.assertEqual("proposed", revised.status)
        state = self.self_view.get_self_view(NS)
        self.assertEqual(SELF_VIEW_STALE, state.freshness)
        rebuilt = self.self_view.build_self_view(namespace=NS)
        self.assertEqual([], parse_self_view_body(rebuilt)["belief_refs"])
        self.assertEqual(SELF_VIEW_FRESH, self.self_view.get_self_view(NS).freshness)

    def test_stale_projection_keeps_its_ref(self) -> None:
        self._supported()
        projection = self.self_view.build_self_view(namespace=NS)
        self._supported(instance="second-test")
        state = self.self_view.get_self_view(NS)
        self.assertEqual(SELF_VIEW_STALE, state.freshness)
        self.assertIsNotNone(state.ref)
        self.assertEqual(projection.record_version, state.ref.record_version)

    # -------------------------------------------------------------- deletion

    def test_deleted_referenced_belief_is_unavailable(self) -> None:
        belief = self._supported()
        self.self_view.build_self_view(namespace=NS)
        self.delete_record(belief.record_id)
        state = self.self_view.get_self_view(NS)
        self.assertEqual(SELF_VIEW_UNAVAILABLE, state.freshness)
        self.assertIsNone(state.ref)
        # Rebuilding drops the deleted ref and is fresh again.
        rebuilt = self.self_view.build_self_view(namespace=NS)
        self.assertEqual([], parse_self_view_body(rebuilt)["belief_refs"])
        self.assertEqual(SELF_VIEW_FRESH, self.self_view.get_self_view(NS).freshness)

    def test_deleted_self_view_record_keeps_the_redaction_barrier(self) -> None:
        record_id = self_view_record_id(NS)
        self.delete_record(record_id)
        with self.assertRaises(Redacted):
            self.self_view.get_self_view(NS)
        with self.assertRaises(Redacted):
            self.self_view.build_self_view(namespace=NS)

    # ---------------------------------------------------------- corruption

    def _append_self_view(self, body: dict, *, version: int = 2) -> None:
        with self.store.transaction():
            self.store.append_record_version(
                namespace=NS,
                record_id=self_view_record_id(NS),
                record_version=version,
                kind="self_view",
                body=body,
                status="active",
                schema_version="1",
            )

    def _good_ref(self) -> dict:
        belief = self._supported()
        return self.record_ref(belief)

    def test_missing_belief_record_is_corrupt_store(self) -> None:
        self._append_self_view(
            {
                "entity_id": "self",
                "belief_refs": [
                    {
                        "ref_type": "record",
                        "namespace": NS,
                        "record_id": str(uuid.uuid4()),
                        "record_version": 1,
                    }
                ],
                "observed_config_refs": [],
            }
        )
        with self.assertRaises(CorruptStore):
            self.self_view.get_self_view(NS)

    def test_missing_version_is_corrupt_store(self) -> None:
        ref = self._good_ref()
        ref["record_version"] = 99
        self._append_self_view(
            {
                "entity_id": "self",
                "belief_refs": [ref],
                "observed_config_refs": [],
            }
        )
        with self.assertRaises(CorruptStore):
            self.self_view.get_self_view(NS)

    def test_wrong_kind_reference_is_corrupt_store(self) -> None:
        active = self.profiles.get_active_profile(NS)
        self._append_self_view(
            {
                "entity_id": "self",
                "belief_refs": [active.ref.to_dict()],
                "observed_config_refs": [],
            }
        )
        with self.assertRaises(CorruptStore):
            self.self_view.get_self_view(NS)

    def test_observed_config_refs_must_be_empty(self) -> None:
        self._append_self_view(
            {
                "entity_id": "self",
                "belief_refs": [],
                "observed_config_refs": [{"ref": "config:pcs2-008"}],
            }
        )
        with self.assertRaises(CorruptStore):
            self.self_view.get_self_view(NS)

    def test_build_requires_standalone_transaction(self) -> None:
        with self.store.transaction():
            with self.assertRaises(StandaloneTransactionRequired):
                self.self_view.build_self_view(namespace=NS)

    # --------------------------------------------------------------- Context

    def test_context_refreshes_a_stale_projection_before_capture(self) -> None:
        self._supported()
        old = self.self_view.build_self_view(namespace=NS)
        self._supported(instance="second-test")
        self.assertEqual(SELF_VIEW_STALE, self.self_view.get_self_view(NS).freshness)

        context = self.build_context(adapter_kind="fake-model")
        document = json.loads(context.rendered_input)
        self.assertEqual("fresh", document["self_view"]["freshness"])
        self.assertNotEqual(
            old.record_version, document["self_view"]["ref"]["record_version"]
        )
        usage = {
            (entry.ref.record_id, entry.usage) for entry in context.read_set
        }
        self.assertIn((document["self_view"]["ref"]["record_id"], "current"), usage)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
