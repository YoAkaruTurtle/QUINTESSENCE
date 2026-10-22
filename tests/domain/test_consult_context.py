"""C08 REV-B: consult input assembly, live guards and archive reads.

Every test uses a fresh migrated database, the real PCS2-008 Context service
and synthetic collaboration inputs only.  No model is called, no network is
used, and no private persona asset is read.

Checks 1-16 are the base C08 card.  Checks 17-23 are the REV-B/addendum race,
watermark, commit-refusal and size proofs.  The audit table may gain rows; the
lifecycle tables (event journal, pending work, Belief, SelfView version,
state revision) may not.
"""

from __future__ import annotations

import json
import sys
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, NS_B, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.cognition import SelfViewService  # noqa: E402
from quintessence_pcs.domains.cognition.views import (  # noqa: E402
    SELF_VIEW_STALE,
    self_view_record_id,
)
from quintessence_pcs.domains.persona.profiles import (  # noqa: E402
    PersonaProfileService,
)
from quintessence_pcs.foundation.errors import (  # noqa: E402
    ContextStale,
    CorruptStore,
    InvalidInput,
    ScopeViolation,
    SnapshotNotFound,
    SourceRedacted,
)
from quintessence_pcs.runtime import ContextService  # noqa: E402
from quintessence_pcs.store import CognitionStore  # noqa: E402
from quintessence_pcs.store.cognition import (  # noqa: E402
    MAX_CONTEXT_BYTES,
    context_content_hash,
)
from quintessence_pcs.store.errors import (  # noqa: E402
    ConsultContextNotCommittable,
    ContextTooLarge,
    NeedsContext,
)
from quintessence_pcs.store.experience import ExperienceAccess  # noqa: E402

HOST = "host:synthetic-c08"
ACCESS_REF = "authorization:access:c08"
CONSULT_BODY = "Synthetic consult question about the current scene."
SECRET_BODY = "Synthetic restricted body that must never be rendered."


class ConsultContextTests(CognitionTestCase):
    """C08 REV-B checks 1-23."""

    # ------------------------------------------------------------- fixtures

    def access(
        self,
        *,
        namespace: str = NS,
        privacy_classes=("private",),
        purposes=("consult",),
    ) -> ExperienceAccess:
        return ExperienceAccess(
            namespace=namespace,
            host_instance=HOST,
            access_ref=ACCESS_REF,
            privacy_classes=frozenset(privacy_classes),
            purposes=frozenset(purposes),
        )

    def input_ref(self, row, *, namespace: str = NS) -> dict:
        return {
            "ref_type": "collab_input",
            "namespace": namespace,
            "input_id": row.input_id,
            "content_version": row.content_version,
        }

    def link_inputs(self, refs, *, namespace: str = NS) -> str:
        return self.store.register_interaction(
            namespace=namespace,
            mode="consult",
            activation_kind="user_input",
            host_instance=HOST,
            access_ref=ACCESS_REF,
            policy_ref="policy:collab.v1",
            input_refs=list(refs),
        )

    def register_consult_input(
        self,
        *,
        body: str = CONSULT_BODY,
        privacy_class: str = "private",
        retention: str = "standard",
        namespace: str = NS,
        version: int = 1,
        input_id: str | None = None,
        link: bool = True,
    ):
        row = self.store.register_input(
            namespace=namespace,
            origin_kind="user_statement",
            body=body,
            privacy_class=privacy_class,
            retention=retention,
            content_version=version,
            input_id=input_id,
        )
        ref = self.input_ref(row, namespace=namespace)
        if link:
            self.link_inputs([ref], namespace=namespace)
        return row, ref

    def build_consult(
        self,
        refs=(),
        *,
        access: ExperienceAccess | None = None,
        namespace: str = NS,
        adapter_kind: str | None = None,
    ):
        return self.contexts.build_consult_context(
            namespace=namespace,
            access=access if access is not None else self.access(namespace=namespace),
            input_refs=list(refs),
            adapter_kind=adapter_kind,
        )

    @staticmethod
    def document(envelope) -> dict:
        return json.loads(envelope.rendered_input)

    def snapshot(self, envelope):
        return self.store.load_context_snapshot(envelope.context_id)

    def lifecycle_counts(self) -> dict[str, int]:
        return {
            "events": self.count_rows("event_journal"),
            "pending_work": self.count_rows("pending_work"),
            "work_progress": self.count_rows("work_progress"),
            "beliefs": len(self.store.current_versions_of_kind(NS, "belief")),
            "revision": self.revision(),
        }

    # ---------------------------------------------------------- base surface

    def test_build_consult_context_archives_without_trigger(self) -> None:
        _row, ref = self.register_consult_input()
        before = self.lifecycle_counts()
        snapshots = self.count_rows("context_snapshots")
        envelope = self.build_consult([ref])
        stored = self.snapshot(envelope)

        self.assertEqual("consult", stored.context_kind)
        self.assertEqual({}, stored.trigger_ref)
        self.assertEqual(snapshots + 1, self.count_rows("context_snapshots"))
        self.assertIsNone(envelope.adapter_kind)
        self.assertFalse(envelope.model_input_complete)
        self.assertFalse(hasattr(envelope, "trigger_ref"))
        self.assertEqual(before, self.lifecycle_counts())

    def test_consult_context_pins_active_profile(self) -> None:
        _row, ref = self.register_consult_input()
        profile_ref = self.profiles.get_active_profile(NS).ref
        envelope = self.build_consult([ref])
        stored = self.snapshot(envelope)

        self.assertEqual(profile_ref.to_dict(), stored.profile_ref)
        self.assertEqual(profile_ref.to_dict(), envelope.profile_ref.to_dict())
        self.assertEqual(profile_ref.to_dict(), self.document(envelope)["profile"]["ref"])

        _other_row, other_ref = self.register_consult_input(namespace=NS_B)
        before = self.count_rows("context_snapshots")
        with self.assertRaises(NeedsContext):
            self.build_consult(
                [other_ref],
                namespace=NS_B,
                access=self.access(namespace=NS_B),
            )
        self.assertEqual(before, self.count_rows("context_snapshots"))

    def test_consult_context_does_not_refresh_self_view(self) -> None:
        before = self.self_view.get_self_view(NS)
        versions = self.count_rows("record_versions")
        self.build_consult([])
        after = self.self_view.get_self_view(NS)
        self.assertEqual(before.ref, after.ref)
        self.assertEqual(before.freshness, after.freshness)
        self.assertEqual(versions, self.count_rows("record_versions"))

        event = self.ingest(event_type="CapabilityObserved:consult-stale")
        self.propose(
            body=self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
            )
        )
        stale = self.self_view.get_self_view(NS)
        self.assertEqual(SELF_VIEW_STALE, stale.freshness)
        self.assertEqual(stale.ref, self.self_view.get_self_view(NS).ref)
        envelope = self.build_consult([])
        document = self.document(envelope)
        self.assertEqual("stale", document["self_view"]["freshness"])
        self.assertIsNone(document["self_view"]["ref"])
        self.assertIsNotNone(document["self_view"]["warning"])
        self.assertEqual(stale.ref, self.self_view.get_self_view(NS).ref)

        self.delete_record(self_view_record_id(NS))
        unavailable = self.build_consult([])
        unavailable_document = self.document(unavailable)
        self.assertEqual("unavailable", unavailable_document["self_view"]["freshness"])
        self.assertIsNone(unavailable_document["self_view"]["ref"])

    def test_consult_context_records_exact_input_sources(self) -> None:
        input_id = str(uuid.uuid4())
        row_v1, ref_v1 = self.register_consult_input(
            body="first consult input", version=1, input_id=input_id, link=False
        )
        row_v2, ref_v2 = self.register_consult_input(
            body="second consult input", version=2, input_id=input_id, link=False
        )
        self.link_inputs([ref_v1, ref_v2])
        envelope = self.build_consult([ref_v1, ref_v2])
        stored = self.snapshot(envelope)

        self.assertEqual(
            (ref_v1, ref_v2),
            self.store.context_collab_sources(
                namespace=NS, context_id=envelope.context_id
            ),
        )
        self.assertEqual((ref_v1, ref_v2), envelope.collab_source_refs)
        self.assertEqual((ref_v1, ref_v2), self.contexts.load_consult_context(
            envelope.context_id
        ).collab_source_refs)
        self.assertEqual(
            {"record"}, {source.ref_kind for source in stored.sources}
        )
        self.assertNotIn(row_v1.input_id, {s.ref_id for s in stored.sources})
        self.assertNotIn(row_v2.input_id, {s.ref_id for s in stored.sources})

    def test_unauthorized_input_is_omitted(self) -> None:
        _private_row, private_ref = self.register_consult_input(
            body="authorized consult body"
        )
        _secret_row, secret_ref = self.register_consult_input(
            body=SECRET_BODY, privacy_class="restricted", link=False
        )
        self.link_inputs([private_ref, secret_ref])
        envelope = self.build_consult(
            [private_ref, secret_ref],
            access=self.access(privacy_classes=("private",)),
        )
        document = self.document(envelope)

        self.assertIn("authorized consult body", envelope.rendered_input)
        self.assertNotIn(SECRET_BODY, envelope.rendered_input)
        self.assertEqual((private_ref,), envelope.collab_source_refs)
        self.assertEqual(
            (private_ref,),
            self.store.context_collab_sources(
                namespace=NS, context_id=envelope.context_id
            ),
        )
        self.assertEqual([private_ref], [item["ref"] for item in document["inputs"]])
        self.assertEqual(1, len(envelope.omissions))
        self.assertEqual(secret_ref, envelope.omissions[0]["ref"])
        self.assertEqual("source_unavailable", envelope.omissions[0]["reason"])

    def test_deleted_input_is_omitted_without_leak(self) -> None:
        row, ref = self.register_consult_input()
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="auth:test-delete",
        )
        before = self.lifecycle_counts()
        envelope = self.build_consult([ref])
        document = self.document(envelope)

        self.assertNotIn(CONSULT_BODY, envelope.rendered_input)
        self.assertEqual([], document["inputs"])
        self.assertEqual((), envelope.collab_source_refs)
        self.assertEqual(
            (),
            self.store.context_collab_sources(
                namespace=NS, context_id=envelope.context_id
            ),
        )
        self.assertEqual("source_unavailable", envelope.omissions[0]["reason"])
        self.assertEqual(before, self.lifecycle_counts())

    def test_input_deletion_after_build_bars_context(self) -> None:
        row, ref = self.register_consult_input()
        envelope = self.build_consult([ref])
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="auth:test-delete",
        )
        self.assertTrue(
            self.store.context_barred(namespace=NS, context_id=envelope.context_id)
        )
        with self.assertRaises((ContextStale,)) as caught:
            self.contexts.load_consult_context(envelope.context_id)
        self.assertNotIn(CONSULT_BODY, str(caught.exception))

    def test_deleted_profile_or_self_view_bars_context(self) -> None:
        self_view_context = self.build_consult([])
        self.delete_record(self_view_record_id(NS))
        with self.assertRaises(ContextStale):
            self.contexts.load_consult_context(self_view_context.context_id)

        # A fresh Context now renders the unavailable SelfView verdict; its
        # independent Profile dependency is what the next deletion proves.
        profile_context = self.build_consult([])
        profile_ref = self.profiles.get_active_profile(NS).ref
        self.delete_record(profile_ref.record_id)
        with self.assertRaises(ContextStale):
            self.contexts.load_consult_context(profile_context.context_id)

    def test_load_context_rejects_consult_and_vice_versa(self) -> None:
        consult = self.build_consult([])
        review = self.build_context()
        with self.assertRaises(InvalidInput):
            self.contexts.load_context(consult.context_id)
        with self.assertRaises((InvalidInput, CorruptStore)):
            self.contexts.load_consult_context(review.context_id)

    def test_consult_context_survives_restart(self) -> None:
        _row, ref = self.register_consult_input()
        envelope = self.build_consult([ref])

        self.store.close()
        self.store = CognitionStore.open(self.db_path)
        self.addCleanup(self.store.close)
        service = ContextService(
            self.store,
            self_view_service=SelfViewService(self.store),
            profile_service=PersonaProfileService(self.store),
        )
        loaded = service.load_consult_context(envelope.context_id)

        self.assertEqual(envelope.context_id, loaded.context_id)
        self.assertEqual(envelope.collab_source_refs, loaded.collab_source_refs)
        self.assertEqual(envelope.content_hash, loaded.content_hash)
        self.assertEqual(envelope.rendered_input, loaded.rendered_input)

    def test_consult_context_does_not_create_event_or_work(self) -> None:
        _row, ref = self.register_consult_input()
        before = self.lifecycle_counts()
        self.build_consult([ref])
        self.assertEqual(before, self.lifecycle_counts())

    def test_consult_context_rejects_invalid_input_refs(self) -> None:
        row, ref = self.register_consult_input()
        snapshots = self.count_rows("context_snapshots")
        links = self.count_rows("collab_source_links")

        wrong_namespace = dict(ref, namespace=NS_B)
        with self.assertRaises(InvalidInput):
            self.build_consult([wrong_namespace])

        missing_content_version = {
            "ref_type": "collab_input",
            "namespace": NS,
            "input_id": row.input_id,
        }
        with self.assertRaises(InvalidInput):
            self.build_consult([missing_content_version])

        duplicate = dict(ref)
        with self.assertRaises(InvalidInput):
            self.build_consult([ref, duplicate])

        wrong_type = dict(ref, ref_type="collab_artifact")
        with self.assertRaises(InvalidInput):
            self.build_consult([wrong_type])

        # C09 lifts the C08 "no model, so adapter_kind must be None" stage
        # limit.  A known adapter is allowed and makes model_input_complete
        # true; an unknown adapter is still an argument error.
        with self.assertRaises(InvalidInput):
            self.build_consult([ref], adapter_kind="unknown-adapter")

        self.assertEqual(snapshots, self.count_rows("context_snapshots"))
        self.assertEqual(links, self.count_rows("collab_source_links"))

        for adapter_kind in ("fake-model", "direct-http"):
            with self.subTest(adapter_kind=adapter_kind):
                envelope = self.build_consult([ref], adapter_kind=adapter_kind)
                self.assertEqual(adapter_kind, envelope.adapter_kind)
                self.assertTrue(envelope.model_input_complete)
                stored = self.snapshot(envelope)
                self.assertEqual(adapter_kind, stored.adapter_kind)
                self.assertTrue(stored.model_input_complete)

        # A structurally valid ref whose exact version does not exist is not an
        # argument error: it goes through the same inaccessible-input path and
        # is omitted uniformly.  No body and no link can appear.
        missing_version = dict(ref, content_version=99)
        envelope = self.build_consult([missing_version])
        self.assertEqual((), envelope.collab_source_refs)
        self.assertEqual([], self.document(envelope)["inputs"])
        self.assertEqual(missing_version, envelope.omissions[0]["ref"])
        self.assertEqual("source_unavailable", envelope.omissions[0]["reason"])
        self.assertEqual(snapshots + 3, self.count_rows("context_snapshots"))
        self.assertEqual(links + 2, self.count_rows("collab_source_links"))

    def test_consult_adapter_kind_records_model_input_completeness(self) -> None:
        _row, ref = self.register_consult_input()

        default = self.build_consult([ref])
        self.assertIsNone(default.adapter_kind)
        self.assertFalse(default.model_input_complete)

        for adapter_kind in ("fake-model", "direct-http"):
            with self.subTest(adapter_kind=adapter_kind):
                envelope = self.build_consult([ref], adapter_kind=adapter_kind)
                self.assertEqual(adapter_kind, envelope.adapter_kind)
                self.assertTrue(envelope.model_input_complete)
                self.assertEqual(
                    adapter_kind,
                    self.snapshot(envelope).adapter_kind,
                )

        with self.assertRaises(InvalidInput):
            self.build_consult([ref], adapter_kind="unknown-adapter")

    def test_consult_context_watermarks_and_hash(self) -> None:
        _row, ref = self.register_consult_input()
        envelope = self.build_consult([ref])
        stored = self.snapshot(envelope)
        watermarks = self.store.context_watermarks(NS)

        self.assertEqual(watermarks["state_revision"], stored.state_revision)
        self.assertEqual(watermarks["last_event_seq"], stored.context_event_seq)
        self.assertEqual(watermarks["privacy_epoch"], stored.privacy_epoch)
        self.assertEqual(
            context_content_hash(envelope.rendered_input), envelope.content_hash
        )
        self.assertEqual(envelope.content_hash, stored.content_hash)
        self.assertEqual(
            watermarks["state_revision"], self.revision()
        )

    def test_consult_context_marks_inputs_untrusted(self) -> None:
        _row, ref = self.register_consult_input()
        document = self.document(self.build_consult([ref]))

        self.assertTrue(document["inputs"])
        for item in document["inputs"]:
            self.assertTrue(item["untrusted"])
            self.assertEqual(CONSULT_BODY, item["content"])
            self.assertEqual(ref, item["ref"])
        self.assertNotIn("untrusted", document["profile"])
        self.assertNotIn("untrusted", document["self_view"])
        self.assertNotIn(CONSULT_BODY, json.dumps(document["profile"]))

    def test_consult_context_empty_inputs(self) -> None:
        before = self.lifecycle_counts()
        envelope = self.build_consult([])
        stored = self.snapshot(envelope)

        self.assertEqual((), envelope.collab_source_refs)
        self.assertEqual(
            (),
            self.store.context_collab_sources(
                namespace=NS, context_id=envelope.context_id
            ),
        )
        self.assertEqual("consult", stored.context_kind)
        self.assertEqual(before, self.lifecycle_counts())

    def test_consult_context_audit_rows_can_increase(self) -> None:
        _row, ref = self.register_consult_input()
        before = {
            "audit": self.count_rows("collab_experience_reads"),
            "interactions": self.count_rows("collab_interactions"),
            "artifacts": self.count_rows("collab_artifacts"),
            "commands": self.count_rows("processed_commands"),
            "work_progress": self.count_rows("work_progress"),
        }
        self.build_consult([ref])
        after = {
            "audit": self.count_rows("collab_experience_reads"),
            "interactions": self.count_rows("collab_interactions"),
            "artifacts": self.count_rows("collab_artifacts"),
            "commands": self.count_rows("processed_commands"),
            "work_progress": self.count_rows("work_progress"),
        }
        self.assertEqual(before["audit"] + 1, after["audit"])
        for name in ("interactions", "artifacts", "commands", "work_progress"):
            self.assertEqual(before[name], after[name])

    # ------------------------------------------------- REV-B race/watermarks

    def test_consult_source_deleted_between_render_and_save_changes_nothing(
        self,
    ) -> None:
        row, ref = self.register_consult_input()
        before_snapshots = self.count_rows("context_snapshots")
        before_sources = self.count_rows("context_sources")
        before_links = self.count_rows("collab_source_links")
        before_revision = self.revision()
        observed: dict[str, int] = {}
        captured: dict[str, str] = {}

        original_render = self.contexts._render_consult

        def render_then_delete(**kwargs):
            rendered = original_render(**kwargs)
            self.store.delete_experience(
                namespace=NS,
                ref_type="collab_input",
                ref_id=row.input_id,
                ref_version=row.content_version,
                authorization_ref="auth:test-delete",
            )
            observed["revision"] = self.revision()
            return rendered

        original_insert = type(self.store).insert_context_snapshot_locked

        def recording_insert(store, entry, *, context_kind="review"):
            snapshot = original_insert(store, entry, context_kind=context_kind)
            captured["context_id"] = snapshot.context_id
            return snapshot

        self.contexts._render_consult = render_then_delete
        with patch.object(
            type(self.store),
            "insert_context_snapshot_locked",
            new=recording_insert,
        ):
            with self.assertRaises((SourceRedacted, ContextStale)):
                self.build_consult([ref])

        self.assertEqual(before_snapshots, self.count_rows("context_snapshots"))
        self.assertEqual(before_sources, self.count_rows("context_sources"))
        self.assertEqual(before_links, self.count_rows("collab_source_links"))
        self.assertEqual(before_revision + 1, observed["revision"])
        self.assertEqual(observed["revision"], self.revision())
        with self.assertRaises(SnapshotNotFound):
            self.contexts.load_consult_context(captured["context_id"])

    def test_consult_context_becomes_historical_after_revision_moves(self) -> None:
        _row, ref = self.register_consult_input()
        envelope = self.build_consult([ref])
        self.contexts.require_current_consult_context(
            context_id=envelope.context_id, namespace=NS
        )

        event = self.ingest(event_type="CapabilityObserved:consult-move")
        self.propose(
            body=self.belief_body(
                evidence_links=[self.evidence_link(self.event_ref(event.event_id))]
            )
        )
        self.assertGreater(self.revision(), envelope.state_revision)

        archived = self.contexts.load_consult_context(envelope.context_id)
        self.assertEqual((ref,), archived.collab_source_refs)
        with self.assertRaises(ContextStale):
            self.contexts.require_current_consult_context(
                context_id=envelope.context_id, namespace=NS
            )

    def test_consult_context_becomes_historical_after_event_seq_moves(self) -> None:
        _row, ref = self.register_consult_input()
        envelope = self.build_consult([ref])
        self.contexts.require_current_consult_context(
            context_id=envelope.context_id, namespace=NS
        )

        self.ingest(event_type="TestTrigger:consult-move")
        self.assertGreater(self.watermarks()[1], envelope.context_event_seq)

        archived = self.contexts.load_consult_context(envelope.context_id)
        self.assertEqual((ref,), archived.collab_source_refs)
        with self.assertRaises(ContextStale):
            self.contexts.require_current_consult_context(
                context_id=envelope.context_id, namespace=NS
            )

    def test_consult_context_cannot_be_committed(self) -> None:
        envelope = self.build_consult([])
        revision = self.revision()
        beliefs = len(self.store.current_versions_of_kind(NS, "belief"))
        commands = self.count_rows("processed_commands")

        with self.assertRaises(ConsultContextNotCommittable):
            self.store.commit_command(
                context_id=envelope.context_id,
                request_id=str(uuid.uuid4()),
                operation="cognition.wait",
                request_payload={"reason": "synthetic consult refusal"},
                apply=lambda tx: self.fail("apply must not run for a consult Context"),
                namespace=NS,
            )

        self.assertEqual(revision, self.revision())
        self.assertEqual(
            beliefs, len(self.store.current_versions_of_kind(NS, "belief"))
        )
        self.assertEqual(commands, self.count_rows("processed_commands"))

    def test_rendered_input_size_covers_the_whole_document(self) -> None:
        body = "x" * (MAX_CONTEXT_BYTES - 512)
        _row, ref = self.register_consult_input(body=body)
        self.assertLess(len(body.encode("utf-8")), MAX_CONTEXT_BYTES)
        before_snapshots = self.count_rows("context_snapshots")
        before_sources = self.count_rows("context_sources")
        before_links = self.count_rows("collab_source_links")
        revision = self.revision()

        with self.assertRaises(ContextTooLarge):
            self.build_consult([ref])

        self.assertEqual(before_snapshots, self.count_rows("context_snapshots"))
        self.assertEqual(before_sources, self.count_rows("context_sources"))
        self.assertEqual(before_links, self.count_rows("collab_source_links"))
        self.assertEqual(revision, self.revision())

    # --------------------------------------------------- addendum A/B guards

    def test_load_consult_context_reads_everything_in_one_read_snapshot(self) -> None:
        _row, ref = self.register_consult_input()
        envelope = self.build_consult([ref])
        calls: list[tuple[str, bool]] = []

        original_load = self.store.load_context_snapshot
        original_barred = self.store.context_barred
        original_collab = self.store.context_collab_sources

        def load(context_id):
            calls.append(("load_context_snapshot", self.store.connection.in_transaction))
            return original_load(context_id)

        def barred(**kwargs):
            calls.append(("context_barred", self.store.connection.in_transaction))
            return original_barred(**kwargs)

        def collab(**kwargs):
            calls.append(
                ("context_collab_sources", self.store.connection.in_transaction)
            )
            return original_collab(**kwargs)

        self.store.load_context_snapshot = load
        self.store.context_barred = barred
        self.store.context_collab_sources = collab
        archived = self.contexts.load_consult_context(envelope.context_id)

        self.assertEqual((ref,), archived.collab_source_refs)
        self.assertEqual(
            ["load_context_snapshot", "context_barred", "context_collab_sources"],
            [name for name, _inside in calls],
        )
        self.assertTrue(all(inside for _name, inside in calls))

        row = self.store.load_input(
            namespace=NS, input_id=ref["input_id"], content_version=1
        )
        self.store.delete_experience(
            namespace=NS,
            ref_type="collab_input",
            ref_id=row.input_id,
            ref_version=row.content_version,
            authorization_ref="auth:test-delete",
        )
        with self.assertRaises(ContextStale) as caught:
            self.contexts.load_consult_context(envelope.context_id)
        self.assertNotIn(CONSULT_BODY, str(caught.exception))

    def test_current_consult_context_checks_watermarks_in_the_same_snapshot(
        self,
    ) -> None:
        _row, ref = self.register_consult_input()
        envelope = self.build_consult([ref])
        calls: list[tuple[str, bool]] = []

        original_load = self.store.load_context_snapshot
        original_watermarks = self.store.context_watermarks

        def load(context_id):
            calls.append(("load_context_snapshot", self.store.connection.in_transaction))
            return original_load(context_id)

        def watermarks(namespace):
            calls.append(("context_watermarks", self.store.connection.in_transaction))
            return original_watermarks(namespace)

        self.store.load_context_snapshot = load
        self.store.context_watermarks = watermarks
        snapshot = self.contexts.require_current_consult_context(
            context_id=envelope.context_id, namespace=NS
        )

        self.assertEqual(envelope.context_id, snapshot.context_id)
        self.assertEqual(
            ["load_context_snapshot", "context_watermarks"],
            [name for name, _inside in calls],
        )
        self.assertTrue(all(inside for _name, inside in calls))

        self.ingest(event_type="TestTrigger:current-move")
        archived = self.contexts.load_consult_context(envelope.context_id)
        self.assertEqual((ref,), archived.collab_source_refs)
        with self.assertRaises(ContextStale):
            self.contexts.require_current_consult_context(
                context_id=envelope.context_id, namespace=NS
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
