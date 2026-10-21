"""PCS2-007 A group: PersonaProfile publish / activate / read.

The exhaustive behaviour of this card is asserted here: publish is a domain
commit but never activation, activation accepts one exact existing version,
rollback is just activating an older version, neutral is explicit, the review
reference is trusted by provenance and the active version does not drift.
"""

from __future__ import annotations

import sqlite3
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from domain_support import (  # noqa: E402
    NS,
    NS_B,
    PROFILE_ID,
    PROFILE_ID_B,
    DomainTestCase,
)

from quintessence_pcs.domains import ActiveProfileState, SourceUntrusted  # noqa: E402
from quintessence_pcs.domains.persona.profiles import (  # noqa: E402
    PERSONA_PROFILE_KIND,
    profile_record_id,
)
from quintessence_pcs.foundation.errors import (  # noqa: E402
    AuthorizationRequired,
    CorruptStore,
    EventNotFound,
    InvalidInput,
    RecordKindMismatch,
    RecordNotFound,
    Redacted,
    StandaloneTransactionRequired,
)


class ProfileLifecycleTests(DomainTestCase):
    def test_neutral_before_any_publish(self) -> None:
        state = self.persona.get_active_profile(NS)
        self.assertIsInstance(state, ActiveProfileState)
        self.assertEqual(NS, state.namespace)
        self.assertFalse(state.active)
        self.assertTrue(state.degraded)
        self.assertIsNone(state.ref)
        self.assertEqual(0, self.revision())

    def test_publish_is_not_activation_and_advances_revision_once(self) -> None:
        first = self.publish()

        self.assertEqual(PERSONA_PROFILE_KIND, first.kind)
        self.assertEqual(1, first.record_version)
        self.assertEqual(1, first.committed_state_revision)
        self.assertEqual(1, self.revision())

        # The active pointer is untouched: neutral remains neutral.
        state = self.persona.get_active_profile(NS)
        self.assertFalse(state.active)
        self.assertTrue(state.degraded)
        self.assertIsNone(state.ref)

        pointer = self.store.connection.execute(
            "SELECT active_profile_record_id, active_profile_version "
            "FROM namespace_state WHERE namespace = ?",
            (NS,),
        ).fetchone()
        self.assertIsNone(pointer["active_profile_record_id"])
        self.assertIsNone(pointer["active_profile_version"])

        audit = self.audit_rows()
        self.assertEqual(1, len(audit))
        self.assertEqual("persona.profile.publish", audit[0]["operation"])
        self.assertEqual(first.record_id, audit[0]["target_id"])
        self.assertEqual(1, audit[0]["target_version"])
        self.assertEqual("auth:profile-publish", audit[0]["authorization_ref"])

    def test_publish_of_a_new_version_keeps_the_active_pointer(self) -> None:
        first = self.publish()
        self.activate(first)
        self.assertEqual(2, self.revision())

        second = self.publish(profile_version="0.0.2", identity_summary="Second synthetic body.")

        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(2, second.record_version)
        self.assertEqual(3, self.revision())

        state = self.persona.get_active_profile(NS)
        self.assertTrue(state.active)
        self.assertFalse(state.degraded)
        self.assertIsNotNone(state.ref)
        self.assertEqual(1, state.ref.record_version)  # no drift

    def test_activation_is_exact_and_advances_revision_once(self) -> None:
        first = self.publish()
        before = self.revision()

        self.activate(first)

        self.assertEqual(before + 1, self.revision())
        state = self.persona.get_active_profile(NS)
        self.assertTrue(state.active)
        self.assertFalse(state.degraded)
        self.assertEqual(first.record_id, state.ref.record_id)
        self.assertEqual(1, state.ref.record_version)
        self.assertEqual(
            {
                "ref_type": "record",
                "namespace": NS,
                "record_id": first.record_id,
                "record_version": 1,
            },
            state.ref.to_dict(),
        )

        audit = [row for row in self.audit_rows() if row["operation"].endswith("activate")]
        self.assertEqual(1, len(audit))
        self.assertEqual("auth:profile-activate", audit[0]["authorization_ref"])

    def test_rollback_is_activating_an_older_version(self) -> None:
        first = self.publish()
        second = self.publish(profile_version="0.0.2", identity_summary="Second synthetic body.")
        self.activate(second)
        self.assertEqual(2, self.persona.get_active_profile(NS).ref.record_version)

        before = self.revision()
        self.activate(first)

        self.assertEqual(before + 1, self.revision())
        self.assertEqual(1, self.persona.get_active_profile(NS).ref.record_version)
        # There is no "rollback" state: the pointer is simply an exact version.
        row = self.store.connection.execute(
            "SELECT status, kind FROM record_versions WHERE record_id = ? "
            "AND record_version = 1",
            (first.record_id,),
        ).fetchone()
        self.assertEqual(("active", PERSONA_PROFILE_KIND), (row["status"], row["kind"]))

    def test_activation_of_a_missing_version_is_not_found(self) -> None:
        first = self.publish()
        before = self.revision()
        audits = len(self.audit_rows())

        with self.assertRaises(RecordNotFound) as caught:
            self.persona.activate_profile(
                namespace=NS,
                record_id=str(uuid.uuid4()),
                record_version=1,
                authorization_ref="auth:profile-activate",
            )
        self.assertEqual("NOT_FOUND", caught.exception.code)

        with self.assertRaises(RecordNotFound) as caught:
            self.persona.activate_profile(
                namespace=NS,
                record_id=first.record_id,
                record_version=2,
                authorization_ref="auth:profile-activate",
            )
        self.assertEqual("NOT_FOUND", caught.exception.code)

        self.assertEqual(before, self.revision())
        self.assertEqual(audits, len(self.audit_rows()))
        self.assertFalse(self.persona.get_active_profile(NS).active)

    def test_activation_of_a_non_profile_is_invalid_transition(self) -> None:
        receipt = self.ingest()
        episode = self.build_episode(receipt.event_id)
        before = self.revision()

        with self.assertRaises(RecordKindMismatch) as caught:
            self.persona.activate_profile(
                namespace=NS,
                record_id=episode.record_id,
                record_version=1,
                authorization_ref="auth:profile-activate",
            )
        self.assertEqual("INVALID_TRANSITION", caught.exception.code)
        self.assertEqual(before, self.revision())
        self.assertFalse(self.persona.get_active_profile(NS).active)

    def test_deleted_profile_cannot_be_activated_and_read_is_redacted(self) -> None:
        first = self.publish()
        self.activate(first)
        self.delete_record(first.record_id)
        self.store.cleanup_deletions()
        before = self.revision()

        with self.assertRaises(Redacted) as caught:
            self.activate(first)
        self.assertEqual("REDACTED", caught.exception.code)
        self.assertEqual(before, self.revision())

        # The deletion barrier wins over "neutral": hiding the deletion by
        # reporting a neutral state would be a lie.
        with self.assertRaises(Redacted):
            self.persona.get_active_profile(NS)

    def test_profile_record_identity_is_deterministic_per_namespace(self) -> None:
        first = self.publish()
        second = self.publish(profile_version="0.0.2", identity_summary="Second.")

        self.assertEqual(first.record_id, second.record_id)
        self.assertEqual(profile_record_id(NS, PROFILE_ID), first.record_id)

        other_profile = self.publish(
            profile_id=PROFILE_ID_B,
            identity_summary="Other synthetic profile.",
        )
        self.assertNotEqual(first.record_id, other_profile.record_id)
        self.assertEqual(1, other_profile.record_version)

        other_namespace = self.publish(namespace=NS_B)
        self.assertNotEqual(first.record_id, other_namespace.record_id)

    def test_active_pointer_is_per_namespace(self) -> None:
        first = self.publish()
        self.activate(first)
        self.assertTrue(self.persona.get_active_profile(NS).active)

        other = self.persona.get_active_profile(NS_B)
        self.assertFalse(other.active)
        self.assertTrue(other.degraded)
        self.assertIsNone(other.ref)

        second = self.publish(namespace=NS_B)
        self.activate(second, namespace=NS_B)
        self.assertEqual(first.record_id, self.persona.get_active_profile(NS).ref.record_id)
        self.assertEqual(second.record_id, self.persona.get_active_profile(NS_B).ref.record_id)

    def test_publish_and_activate_require_a_standalone_transaction(self) -> None:
        review_ref = self.approve_review()
        body = self.profile_body(review_ref=review_ref)

        with self.store.transaction():
            with self.assertRaises(StandaloneTransactionRequired) as caught:
                self.persona.publish_profile(
                    namespace=NS,
                    profile_id=PROFILE_ID,
                    body=body,
                    review_ref=review_ref,
                    authorization_ref="auth:profile-publish",
                )
            self.assertEqual("INVALID_TRANSITION", caught.exception.code)
        self.assertEqual(0, self.revision())
        self.assertEqual(0, self.count_rows("records"))

        first = self.publish()
        with self.store.transaction():
            with self.assertRaises(StandaloneTransactionRequired):
                self.persona.activate_profile(
                    namespace=NS,
                    record_id=first.record_id,
                    record_version=1,
                    authorization_ref="auth:profile-activate",
                )
        self.assertEqual(1, self.revision())
        self.assertFalse(self.persona.get_active_profile(NS).active)

    def test_revision_accounting_is_exact_across_publish_and_activation(self) -> None:
        self.assertEqual(0, self.revision())
        first = self.publish()
        self.assertEqual(1, self.revision())
        second = self.publish(profile_version="0.0.2", identity_summary="Second.")
        self.assertEqual(2, self.revision())
        self.assertFalse(self.persona.get_active_profile(NS).active)
        self.activate(first)
        self.assertEqual(3, self.revision())
        self.assertEqual(1, self.persona.get_active_profile(NS).ref.record_version)
        self.activate(second)
        self.assertEqual(4, self.revision())
        self.assertEqual(2, self.persona.get_active_profile(NS).ref.record_version)
        self.activate(first)
        self.assertEqual(5, self.revision())
        self.assertEqual(1, self.persona.get_active_profile(NS).ref.record_version)
        self.assertEqual((5, 2, 0), self.watermarks())  # two review events, no epoch

    def test_profile_scope_is_namespace_general(self) -> None:
        first = self.publish()
        self.assertIsNone(first.scope)
        self.assertIsNone(self.store.get_version(NS, first.record_id, 1).scope)


class ProfileReviewTrustTests(DomainTestCase):
    def _assert_nothing_written(self) -> None:
        self.assertEqual(0, self.revision())
        self.assertEqual(0, self.count_rows("records"))
        self.assertEqual(0, len(self.audit_rows()))

    def test_missing_review_event_is_not_found(self) -> None:
        review_ref = {
            "ref_type": "event",
            "namespace": NS,
            "event_id": str(uuid.uuid4()),
        }
        body = self.profile_body(review_ref=review_ref)
        with self.assertRaises(EventNotFound) as caught:
            self.persona.publish_profile(
                namespace=NS,
                profile_id=PROFILE_ID,
                body=body,
                review_ref=review_ref,
                authorization_ref="auth:profile-publish",
            )
        self.assertEqual("NOT_FOUND", caught.exception.code)
        self._assert_nothing_written()

    def test_untrusted_source_kinds_cannot_stand_in_for_a_user_review(self) -> None:
        for source_kind in (
            "synthetic_fixture",
            "model_inference",
            "host_observation",
            "imported_memory",
            "system_internal",
        ):
            with self.subTest(source_kind=source_kind):
                review_ref = self.approve_review(source_kind=source_kind)
                body = self.profile_body(review_ref=review_ref)
                with self.assertRaises(SourceUntrusted) as caught:
                    self.persona.publish_profile(
                        namespace=NS,
                        profile_id=PROFILE_ID,
                        body=body,
                        review_ref=review_ref,
                        authorization_ref="auth:profile-publish",
                    )
                self.assertEqual("SOURCE_UNTRUSTED", caught.exception.code)
                self._assert_nothing_written()

    def test_wrong_review_event_type_is_source_untrusted(self) -> None:
        review_ref = self.approve_review(event_type="persona.review.reject")
        body = self.profile_body(review_ref=review_ref)
        with self.assertRaises(SourceUntrusted) as caught:
            self.persona.publish_profile(
                namespace=NS,
                profile_id=PROFILE_ID,
                body=body,
                review_ref=review_ref,
                authorization_ref="auth:profile-publish",
            )
        self.assertEqual("SOURCE_UNTRUSTED", caught.exception.code)
        self._assert_nothing_written()

    def test_cross_namespace_review_is_source_untrusted(self) -> None:
        review_ref = self.approve_review(namespace=NS_B)
        body = self.profile_body(review_ref=review_ref)
        with self.assertRaises(SourceUntrusted) as caught:
            self.persona.publish_profile(
                namespace=NS,
                profile_id=PROFILE_ID,
                body=body,
                review_ref=review_ref,
                authorization_ref="auth:profile-publish",
            )
        self.assertEqual("SOURCE_UNTRUSTED", caught.exception.code)
        self._assert_nothing_written()

    def test_deleted_review_event_is_redacted(self) -> None:
        review_ref = self.approve_review()
        body = self.profile_body(review_ref=review_ref)
        self.delete_event(review_ref["event_id"])
        self.store.cleanup_deletions()

        with self.assertRaises(Redacted) as caught:
            self.persona.publish_profile(
                namespace=NS,
                profile_id=PROFILE_ID,
                body=body,
                review_ref=review_ref,
                authorization_ref="auth:profile-publish",
            )
        self.assertEqual("REDACTED", caught.exception.code)
        self.assertEqual(1, self.revision())  # only the deletion barrier committed
        self.assertEqual(0, self.count_rows("records"))
        self.assertEqual(0, len(self.audit_rows()))

    def test_review_event_that_is_only_barriered_is_also_redacted(self) -> None:
        review_ref = self.approve_review()
        body = self.profile_body(review_ref=review_ref)
        self.delete_event(review_ref["event_id"])  # Tx1 only, cleanup not run

        with self.assertRaises(Redacted):
            self.persona.publish_profile(
                namespace=NS,
                profile_id=PROFILE_ID,
                body=body,
                review_ref=review_ref,
                authorization_ref="auth:profile-publish",
            )
        self.assertEqual(0, self.count_rows("records"))

    def test_review_ref_must_be_a_typed_event_ref(self) -> None:
        event_id = self.approve_review()["event_id"]
        bad_refs = (
            event_id,
            {"event_id": event_id},
            {"ref_type": "record", "namespace": NS, "event_id": event_id},
            {"ref_type": "event", "namespace": NS, "event_id": event_id, "extra": 1},
            {"ref_type": "event", "namespace": NS, "event_id": "not-a-uuid"},
        )
        for bad in bad_refs:
            with self.subTest(ref=bad):
                with self.assertRaises(InvalidInput) as caught:
                    self.persona.publish_profile(
                        namespace=NS,
                        profile_id=PROFILE_ID,
                        body=self.profile_body(review_ref={"ref_type": "event", "namespace": NS, "event_id": event_id}),
                        review_ref=bad,
                        authorization_ref="auth:profile-publish",
                    )
                self.assertEqual("INVALID_SCHEMA", caught.exception.code)
        self.assertEqual(0, self.count_rows("records"))


class ProfileBodyValidationTests(DomainTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.review_ref = self.approve_review()
        self.valid_body = self.profile_body(review_ref=self.review_ref)

    def _publish_body(self, body: object) -> None:
        self.persona.publish_profile(
            namespace=NS,
            profile_id=PROFILE_ID,
            body=body,  # type: ignore[arg-type]
            review_ref=self.review_ref,
            authorization_ref="auth:profile-publish",
        )

    def test_missing_required_field_is_invalid_schema(self) -> None:
        body = dict(self.valid_body)
        del body["principles"]
        with self.assertRaises(InvalidInput) as caught:
            self._publish_body(body)
        self.assertEqual("INVALID_SCHEMA", caught.exception.code)
        self.assertIn("principles", str(caught.exception))
        self.assertEqual(0, self.count_rows("records"))

    def test_unknown_top_level_field_is_invalid_schema(self) -> None:
        body = dict(self.valid_body)
        body["approved_by"] = "model-scale"
        with self.assertRaises(InvalidInput) as caught:
            self._publish_body(body)
        self.assertEqual("INVALID_SCHEMA", caught.exception.code)
        self.assertIn("approved_by", str(caught.exception))

    def test_body_profile_id_must_match(self) -> None:
        body = dict(self.valid_body)
        body["profile_id"] = PROFILE_ID_B
        with self.assertRaises(InvalidInput):
            self._publish_body(body)

    def test_body_review_ref_must_be_the_verified_event(self) -> None:
        body = dict(self.valid_body)
        body["review_ref"] = self.approve_review()
        with self.assertRaises(InvalidInput):
            self._publish_body(body)

    def test_style_and_examples_are_checked(self) -> None:
        broken_style = dict(self.valid_body)
        broken_style["style"] = {"language": "zh-CN"}
        with self.assertRaises(InvalidInput):
            self._publish_body(broken_style)

        broken_examples = dict(self.valid_body)
        broken_examples["examples"] = [{"example_id": "x"}]
        with self.assertRaises(InvalidInput):
            self._publish_body(broken_examples)

        false_flag = dict(self.valid_body)
        false_flag["examples"] = [
            {
                "example_id": "x",
                "scene": "s",
                "user": "u",
                "assistant": "a",
                "synthetic": "yes",
            }
        ]
        with self.assertRaises(InvalidInput):
            self._publish_body(false_flag)
        self.assertEqual(0, self.count_rows("records"))

    def test_non_json_body_value_is_invalid_schema(self) -> None:
        body = dict(self.valid_body)
        body["identity_summary"] = b"bytes are not JSON"
        with self.assertRaises(InvalidInput):
            self._publish_body(body)


class ProfileAuthorizationTests(DomainTestCase):
    def test_publish_requires_an_explicit_authorization(self) -> None:
        review_ref = self.approve_review()
        body = self.profile_body(review_ref=review_ref)
        for value in ("", "   ", None, 7):
            with self.subTest(value=value):
                with self.assertRaises(AuthorizationRequired) as caught:
                    self.persona.publish_profile(
                        namespace=NS,
                        profile_id=PROFILE_ID,
                        body=body,
                        review_ref=review_ref,
                        authorization_ref=value,  # type: ignore[arg-type]
                    )
                self.assertEqual("SCOPE_VIOLATION", caught.exception.code)
        self.assertEqual(0, self.count_rows("records"))

    def test_activation_requires_an_explicit_authorization(self) -> None:
        first = self.publish()
        with self.assertRaises(AuthorizationRequired) as caught:
            self.persona.activate_profile(
                namespace=NS,
                record_id=first.record_id,
                record_version=1,
                authorization_ref=" ",
            )
        self.assertEqual("SCOPE_VIOLATION", caught.exception.code)
        self.assertEqual(1, self.revision())
        self.assertFalse(self.persona.get_active_profile(NS).active)

    def test_audit_ledger_is_append_only(self) -> None:
        first = self.publish()
        self.activate(first)
        self.assertEqual(2, self.count_rows("authorization_audit"))

        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE authorization_audit SET authorization_ref = 'auth:rewritten'"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute("DELETE FROM authorization_audit")
        self.assertEqual(2, self.count_rows("authorization_audit"))

        # The audit ledger stores references, never a body.
        audit_texts = [
            row[column]
            for row in self.store.connection.execute(
                "SELECT * FROM authorization_audit"
            ).fetchall()
            for column in row.keys()
            if isinstance(row[column], str)
        ]
        self.assertFalse(
            any("Synthetic profile used only" in value for value in audit_texts)
        )


class ActiveProfileSchemaTests(DomainTestCase):
    def test_pointer_to_a_non_profile_record_is_corrupt(self) -> None:
        receipt = self.ingest()
        episode = self.build_episode(receipt.event_id)
        self.store.connection.execute(
            "UPDATE namespace_state SET active_profile_record_id = ?, "
            "active_profile_version = 1 WHERE namespace = ?",
            (episode.record_id, NS),
        )
        with self.assertRaises(CorruptStore) as caught:
            self.persona.get_active_profile(NS)
        self.assertEqual("CORRUPT_STORE", caught.exception.code)

    def test_pointer_columns_move_as_a_pair(self) -> None:
        first = self.publish()
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.connection.execute(
                "UPDATE namespace_state SET active_profile_record_id = ? "
                "WHERE namespace = ?",
                (first.record_id, NS),
            )
        pointer = self.store.connection.execute(
            "SELECT active_profile_record_id, active_profile_version "
            "FROM namespace_state WHERE namespace = ?",
            (NS,),
        ).fetchone()
        self.assertIsNone(pointer["active_profile_record_id"])
        self.assertIsNone(pointer["active_profile_version"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
