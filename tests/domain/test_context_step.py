"""PCS2-008 C1–C3: Context reproducibility, read_set/pending and FakeModel.

Every assertion here is about a fact the card calls an acceptance condition:

* all 22 J.5 fields exist;
* the exact rendered text is recoverable from ``private_payloads`` and its
  ``content_hash`` recomputes;
* deletion cleanup reverses the Context→source lookup and makes the rendered
  text unrecoverable while keeping the hash;
* ``read_set`` matches the loaded records one-for-one and pending sources are
  all listed and marked;
* the FakeModel is deterministic, structured, offline, and never believed about
  ``state_revision`` / ``request_id`` / ``read_set``.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, NS_B, CognitionTestCase, all_text_cells  # noqa: E402

from quintessence_pcs.domains import EpisodeService  # noqa: E402
from quintessence_pcs.domains.cognition.errors import (  # noqa: E402
    UnsupportedOperation,
)
from quintessence_pcs.domains.errors import SourceUntrusted  # noqa: E402
from quintessence_pcs.domains.refs import EventRef, RecordRef  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    EventNotFound,
    InvalidInput,
    Redacted,
)
from quintessence_pcs.foundation.jsonutil import sha256_hex, strict_json_loads  # noqa: E402
from quintessence_pcs.runtime import FakeModel, parse_proposal  # noqa: E402
from quintessence_pcs.store.cognition import (  # noqa: E402
    CONTEXT_ADAPTER_FAKE_MODEL,
    MAX_CONTEXT_BYTES,
    context_content_hash,
)
from quintessence_pcs.store.errors import NeedsContext  # noqa: E402

J5_FIELDS = (
    "context_id",
    "namespace",
    "run_id",
    "trigger_ref",
    "state_revision",
    "context_event_seq",
    "privacy_epoch",
    "profile_ref",
    "read_set",
    "memory_hits",
    "belief_refs",
    "question_refs",
    "focus_refs",
    "unknown_execution_ids",
    "permission_view_ref",
    "budget_view",
    "pending_source_event_refs",
    "omissions",
    "index_generation",
    "index_coverage",
    "content_hash",
    "model_input_complete",
)


class ContextReproducibilityTests(CognitionTestCase):
    def test_all_22_j5_fields_are_present(self) -> None:
        context = self.build_context()
        fields = context.j5_fields()
        self.assertEqual(set(J5_FIELDS), set(fields))
        self.assertEqual(22, len(fields))
        for name in J5_FIELDS:
            self.assertIsNotNone(fields[name], name)
        self.assertFalse(context.model_input_complete)
        self.assertIsNone(context.adapter_kind)
        self.assertEqual([], fields["question_refs"])
        self.assertEqual([], fields["focus_refs"])
        self.assertEqual([], fields["unknown_execution_ids"])

    def test_rendered_input_is_recoverable_and_hash_recomputes(self) -> None:
        context = self.build_context()
        self.assertRegex(context.content_hash, r"^sha256:[0-9a-f]{64}$")
        row = self.store.connection.execute(
            "SELECT body_json, sha256 FROM private_payloads WHERE payload_ref = ?",
            (context.rendered_input_payload_ref,),
        ).fetchone()
        self.assertIsNotNone(row)
        payload = strict_json_loads(row["body_json"])
        self.assertEqual({"rendered_input"}, set(payload))
        text = payload["rendered_input"]
        self.assertEqual(context.rendered_input, text)
        self.assertEqual(sha256_hex(row["body_json"]), row["sha256"])
        self.assertEqual(context_content_hash(text), context.content_hash)

        reloaded = self.contexts.load_context(context.context_id)
        self.assertEqual(context.rendered_input, reloaded.rendered_input)
        self.assertEqual(context.content_hash, reloaded.content_hash)

    def test_context_payload_is_not_reused_for_record_bodies(self) -> None:
        context = self.build_context()
        row = self.store.connection.execute(
            "SELECT payload_ref, body_json FROM private_payloads WHERE payload_ref = ?",
            (context.rendered_input_payload_ref,),
        ).fetchone()
        body = strict_json_loads(row["body_json"])
        with self.store.transaction():
            version = self.store.append_record_version(
                namespace=NS,
                record_id=str(uuid.uuid4()),
                record_version=1,
                kind="Observation",
                body=body,
                status="active",
                schema_version="1",
            )
        self.assertNotEqual(
            context.rendered_input_payload_ref, version.body_payload_ref
        )
        still_there = self.store.connection.execute(
            "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
            (context.rendered_input_payload_ref,),
        ).fetchone()
        self.assertEqual(row["body_json"], still_there["body_json"])

    def test_trigger_must_be_an_existing_typed_event(self) -> None:
        missing = str(uuid.uuid4())
        with self.assertRaises(EventNotFound):
            self.contexts.build_context(
                namespace=NS,
                trigger_ref=self.event_ref(missing),
            )
        with self.assertRaises(InvalidInput):
            self.contexts.build_context(namespace=NS, trigger_ref=missing)

    def test_deleted_trigger_is_redacted(self) -> None:
        event = self.ingest()
        self.delete_event(event.event_id)
        with self.assertRaises(Redacted):
            self.build_context(event.event_id)

    def test_untrusted_trigger_source_is_rejected(self) -> None:
        event = self.ingest(source_kind="synthetic_fixture")
        with self.assertRaises(SourceUntrusted):
            self.build_context(event.event_id)

    def test_context_archival_does_not_advance_state_revision(self) -> None:
        before = self.revision()
        self.build_context()
        self.assertEqual(before, self.revision())

    def test_old_006_snapshot_is_needs_context(self) -> None:
        snapshot = self.store.capture_snapshot(NS)
        with self.assertRaises(NeedsContext) as caught:
            self.store.load_context_snapshot(snapshot.context_id)
        self.assertEqual("NEEDS_CONTEXT", caught.exception.code)

    def test_context_sources_record_every_material(self) -> None:
        evidence = self.ingest(event_type="CapabilityObserved:file-read")
        belief = self.propose(
            body=self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(evidence.event_id))]
            )
        )
        trigger = self.ingest()
        context = self.build_context(trigger.event_id)
        sources = {
            (source.ref_kind, source.ref_id, source.ref_version)
            for source in context.sources
        }
        active = self.profiles.get_active_profile(NS).ref
        self.assertIn(("event", trigger.event_id, 0), sources)
        self.assertIn(("event", evidence.event_id, 0), sources)
        self.assertIn(("record", active.record_id, active.record_version), sources)
        self.assertIn(
            ("record", belief.record_id, belief.record_version), sources
        )
        state = self.self_view.get_self_view(NS)
        self.assertIn(
            ("record", state.ref.record_id, state.ref.record_version), sources
        )

    def test_deletion_cleanup_clears_the_context_payload(self) -> None:
        marker = f"UNIQUE-CLAIM-{uuid.uuid4().hex}"
        event = self.ingest(event_type="CapabilityObserved:file-read")
        belief = self.propose(
            body=self.belief_body(
                claim=f"capability:{marker}",
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
            )
        )
        context = self.build_context()
        self.assertIn(marker, context.rendered_input)

        self.delete_record(belief.record_id)
        self.store.cleanup_deletions()

        with self.assertRaises(Redacted):
            self.contexts.load_context(context.context_id)
        row = self.store.connection.execute(
            "SELECT body_json, sha256 FROM private_payloads WHERE payload_ref = ?",
            (context.rendered_input_payload_ref,),
        ).fetchone()
        self.assertEqual("", row["body_json"])
        snapshot = self.store.connection.execute(
            "SELECT content_hash FROM context_snapshots WHERE context_id = ?",
            (context.context_id,),
        ).fetchone()
        # The hash is retained as evidence that a text existed; the text is not
        # recoverable.
        self.assertEqual(context.content_hash, snapshot["content_hash"])
        for location, value in all_text_cells(self.store.connection):
            self.assertNotIn(marker, value, location)

    def test_deletion_of_a_used_event_clears_the_context_payload(self) -> None:
        event = self.ingest()
        context = self.build_context(event.event_id)
        self.delete_event(event.event_id)
        self.store.cleanup_deletions()
        with self.assertRaises(Redacted):
            self.contexts.load_context(context.context_id)
        row = self.store.connection.execute(
            "SELECT body_json FROM private_payloads WHERE payload_ref = ?",
            (context.rendered_input_payload_ref,),
        ).fetchone()
        self.assertEqual("", row["body_json"])

    def test_model_input_complete_is_fake_model_only(self) -> None:
        direct = self.build_context()
        self.assertFalse(direct.model_input_complete)
        self.assertIsNone(direct.adapter_kind)

        event = self.ingest(event_type="CapabilityObserved:file-read")
        fake = self.build_context(event.event_id, adapter_kind="fake-model")
        self.assertTrue(fake.model_input_complete)
        self.assertEqual(CONTEXT_ADAPTER_FAKE_MODEL, fake.adapter_kind)
        loaded = self.store.load_context_snapshot(fake.context_id)
        self.assertTrue(loaded.model_input_complete)
        self.assertEqual(CONTEXT_ADAPTER_FAKE_MODEL, loaded.adapter_kind)
        self.assertTrue(loaded.rendered_input_payload_ref)

    def test_context_reports_an_unavailable_projection_explicitly(self) -> None:
        from quintessence_pcs.domains.cognition.views import self_view_record_id

        self.delete_record(self_view_record_id(NS))
        context = self.build_context()
        document = json.loads(context.rendered_input)
        self.assertEqual("unavailable", document["self_view"]["freshness"])
        self.assertIsNone(document["self_view"]["ref"])
        self.assertNotIn(
            self_view_record_id(NS),
            {entry.ref.record_id for entry in context.read_set},
        )

    def test_context_requires_an_active_profile(self) -> None:
        from quintessence_pcs.domains.cognition.views import self_view_record_id

        event = self.ingest(namespace=NS_B, event_type="CapabilityObserved:file-read")
        with self.assertRaises(NeedsContext):
            self.build_context(event.event_id, namespace=NS_B)
        # No projection is created as a side effect of a failed assembly.
        self.assertIsNone(
            self.store.record_pointer(NS_B, self_view_record_id(NS_B))
        )

    def test_unknown_adapter_is_rejected(self) -> None:
        with self.assertRaises(InvalidInput):
            self.build_context(adapter_kind="real-model")


class ReadSetTests(CognitionTestCase):
    def test_read_set_matches_loaded_records_exactly(self) -> None:
        evidence = self.ingest(event_type="CapabilityObserved:file-read")
        episode = EpisodeService(self.store).build_episode_from_event(
            namespace=NS,
            event_id=evidence.event_id,
            scope={},
            authorization_ref="auth:episode-test",
        )
        belief = self.propose(
            body=self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(evidence.event_id))]
            )
        )
        context = self.build_context()

        active = self.profiles.get_active_profile(NS).ref
        projection = self.self_view.get_self_view(NS)
        self.assertEqual("fresh", projection.freshness)
        expected = {
            (active.record_id, active.record_version, "pinned_asset"),
            (
                projection.ref.record_id,
                projection.ref.record_version,
                "current",
            ),
            (belief.record_id, belief.record_version, "current"),
            (episode.record_id, episode.record_version, "historical"),
        }
        actual = {
            (entry.ref.record_id, entry.ref.record_version, entry.usage)
            for entry in context.read_set
        }
        self.assertEqual(expected, actual)

    def test_read_set_usage_agrees_with_the_store(self) -> None:
        evidence = self.ingest(event_type="CapabilityObserved:file-read")
        self.propose(
            body=self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(evidence.event_id))]
            )
        )
        context = self.build_context()
        for entry in context.read_set:
            if entry.usage == "current":
                pointer = self.store.record_pointer(
                    NS, entry.ref.record_id
                )
                self.assertEqual(
                    pointer.current_version, entry.ref.record_version
                )
            else:
                self.store.get_version(
                    NS, entry.ref.record_id, entry.ref.record_version
                )

    def test_pending_sources_are_all_listed_and_marked(self) -> None:
        first = self.ingest()
        second = self.ingest()
        context = self.build_context(first.event_id)
        expected = {
            work.event_id for work in self.store.unfinished_work(NS)
        }
        actual = {
            ref.event_id for ref in context.pending_source_event_refs
        }
        self.assertEqual(expected, actual)
        self.assertIn(second.event_id, actual)
        document = json.loads(context.rendered_input)
        for item in document["pending_sources"]:
            self.assertEqual("尚未形成结论", item["note"])

    def test_deleted_pending_source_is_skipped_and_omitted(self) -> None:
        bad = self.ingest()
        bad_work = self.work_for(bad.event_id)
        self.delete_event(bad.event_id)
        # R1.1: the deleted source is in this round's candidate range because
        # the new trigger explicitly cites it; a merely historical skipped item
        # is not enumerated here.
        trigger = self.ingest(
            causation_refs=(self.event_ref(bad.event_id),)
        )

        context = self.build_context(trigger.event_id)
        self.assertNotIn(
            bad.event_id,
            {ref.event_id for ref in context.pending_source_event_refs},
        )
        self.assertTrue(
            any(
                omission.get("reason") == "source_redacted"
                and omission.get("ref", {}).get("event_id") == bad.event_id
                for omission in context.omissions
            )
        )
        progress = self.store.work_progress(bad_work.work_id)
        self.assertIsNotNone(progress)
        self.assertEqual("skipped", progress.outcome)
        self.assertEqual("source_redacted", progress.reason)
        self.assertIsNone(progress.context_id)

    def test_budget_trims_episodes_and_records_omissions(self) -> None:
        for index in range(3):
            event = self.ingest(source_event_id=f"budget-event-{index}")
            EpisodeService(self.store).build_episode_from_event(
                namespace=NS,
                event_id=event.event_id,
                scope={},
                authorization_ref="auth:episode-test",
            )
        limit = 6500
        with mock.patch(
            "quintessence_pcs.runtime.context.MAX_CONTEXT_BYTES", limit
        ), mock.patch(
            "quintessence_pcs.store.cognition.MAX_CONTEXT_BYTES", limit
        ):
            context = self.build_context()
        self.assertTrue(context.omissions)
        self.assertLessEqual(
            context.budget_view["used_bytes"], limit
        )
        self.assertEqual(limit, context.budget_view["limit_bytes"])
        self.assertLess(len(context.memory_hits), 3)
        episode_entries = [
            entry
            for entry in context.read_set
            if entry.usage == "historical"
        ]
        self.assertEqual(
            {(ref.record_id, ref.record_version) for ref in context.memory_hits},
            {
                (entry.ref.record_id, entry.ref.record_version)
                for entry in episode_entries
            },
        )
        for omission in context.omissions:
            self.assertEqual("budget", omission["reason"])
            self.assertIn("ref", omission)

    def test_belief_with_deleted_evidence_is_not_presented_as_current(self) -> None:
        marker = f"UNIQUE-EVIDENCE-{uuid.uuid4().hex}"
        event = self.ingest(
            source_instance="redacted-evidence",
            event_type="CapabilityObserved:file-read",
        )
        belief = self.propose(
            body=self.belief_body(
                claim=f"capability:{marker}",
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))],
            )
        )
        self.complete_work_as_wait(event.event_id)
        self.delete_event(event.event_id)
        trigger = self.ingest()
        context = self.build_context(trigger.event_id)
        self.assertNotIn(marker, context.rendered_input)
        self.assertNotIn(
            belief.record_id, {ref.record_id for ref in context.belief_refs}
        )
        self.assertNotIn(
            belief.record_id,
            {entry.ref.record_id for entry in context.read_set},
        )
        self.assertTrue(
            any(
                omission.get("reason") == "source_redacted"
                and omission.get("ref", {}).get("record_id") == belief.record_id
                for omission in context.omissions
            )
        )
        state = self.self_view.get_self_view(NS)
        self.assertIn(state.freshness, ("fresh", "stale", "unavailable"))
        if state.ref is not None:
            from quintessence_pcs.domains.cognition.views import parse_self_view_body

            version = self.store.get_version(
                NS, state.ref.record_id, state.ref.record_version
            )
            referenced = {
                ref.record_id for ref in parse_self_view_body(version)["belief_refs"]
            }
            self.assertNotIn(belief.record_id, referenced)

    def test_event_body_text_is_not_copied_into_the_context(self) -> None:
        secret = f"UNIQUE-EVENT-BODY-{uuid.uuid4().hex}"
        event = self.ingest(payload={"secret": secret})
        context = self.build_context(event.event_id)
        self.assertNotIn(secret, context.rendered_input)
        self.assertIn(event.event_id, context.rendered_input)


class FakeModelTests(CognitionTestCase):
    def _capability_context(self):
        event = self.ingest(event_type="CapabilityObserved:file-read")
        return self.build_context(event.event_id, adapter_kind="fake-model")

    def test_fake_model_is_deterministic_and_structured(self) -> None:
        context = self._capability_context()
        model = FakeModel()
        first = model.propose(
            rendered_input=context.rendered_input,
            references=context.available_references,
        )
        second = model.propose(
            rendered_input=context.rendered_input,
            references=context.available_references,
        )
        self.assertEqual(first, second)
        proposal = parse_proposal(first)
        self.assertEqual("propose_belief", proposal["operation"])
        self.assertEqual("capability", proposal["body"]["claim_type"])
        self.assertEqual(1, len(proposal["body"]["evidence_links"]))
        self.assertTrue(proposal["body"]["evidence_links"][0]["origin_group_id"])

    def test_parse_accepts_only_the_three_shapes(self) -> None:
        wait = parse_proposal('{"operation":"wait","reason":"nothing to do"}')
        self.assertEqual("wait", wait["operation"])
        propose = parse_proposal(
            json.dumps(
                {
                    "operation": "propose_belief",
                    "body": {"claim": "x"},
                    "scope": {},
                }
            )
        )
        self.assertEqual("propose_belief", propose["operation"])
        revise = parse_proposal(
            json.dumps(
                {
                    "operation": "revise_belief",
                    "target": {
                        "ref_type": "record",
                        "namespace": NS,
                        "record_id": str(uuid.uuid4()),
                        "record_version": 1,
                    },
                    "body": {"claim": "x"},
                    "scope": {},
                }
            )
        )
        self.assertEqual("revise_belief", revise["operation"])

    def test_parse_rejects_free_text_and_unknown_keys(self) -> None:
        with self.assertRaises(InvalidInput):
            parse_proposal("I think the file exists.")
        for key, value in (
            ("state_revision", 9),
            ("request_id", str(uuid.uuid4())),
            ("read_set", []),
            ("context_id", "forged"),
        ):
            payload = {
                "operation": "wait",
                "reason": "x",
                key: value,
            }
            with self.assertRaises(InvalidInput, msg=key):
                parse_proposal(json.dumps(payload))

    def test_parse_rejects_unsupported_operation(self) -> None:
        with self.assertRaises(UnsupportedOperation) as caught:
            parse_proposal('{"operation":"open_question"}')
        self.assertEqual("UNSUPPORTED_OPERATION", caught.exception.code)

    def test_parse_rejects_non_json(self) -> None:
        with self.assertRaises(InvalidInput):
            parse_proposal('```json\n{"operation":"wait","reason":"x"}\n```')


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
