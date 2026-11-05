"""UI-F02: profile history reads and the review-body digest binding.

The settings list must not infer activation from a version row's own ``status``
(every published profile version stores ``active``); only
``namespace_state.active_profile_*`` says which version is in use.  Publishing
must also refuse a ``persona.review.approve`` event that names a different body
digest, while events written before the binding keep working.
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

from quintessence_pcs.domains.errors import SourceUntrusted  # noqa: E402
from quintessence_pcs.domains.persona.profiles import (  # noqa: E402
    REVIEW_APPROVE_EVENT_TYPE,
    profile_body_digest,
)


class ProfileAdminTests(CognitionTestCase):
    def _approval_with_digest(self, digest: str):
        receipt = self.ingest(
            source_kind="user_report",
            source_instance="ui-f02-test-admin",
            source_event_id=f"ui-f02-approval-{uuid.uuid4()}",
            event_type=REVIEW_APPROVE_EVENT_TYPE,
            payload={
                "decision": "approve",
                "profile_id": "example.synthetic.cognition",
                "body_sha256": digest,
                "surface": "test",
            },
        )
        return self.event_ref(receipt.event_id)

    def test_versions_read_newest_first_and_are_bounded(self) -> None:
        first = self.publish(profile_id="example.test.versions")
        review_ref = self.approve_review()
        body = self.profile_body(
            review_ref=review_ref,
            profile_id="example.test.versions",
            profile_version="0.0.2",
        )
        second = self.profiles.publish_profile(
            namespace=NS,
            profile_id="example.test.versions",
            body=body,
            review_ref=review_ref,
            authorization_ref="auth:cognition-test",
        )
        versions = self.store.versions_of_record(NS, first.record_id)
        self.assertEqual([2, 1], [item.record_version for item in versions])
        self.assertEqual(
            [second.record_version, first.record_version],
            [item.record_version for item in versions],
        )
        # The row status says nothing about activation; it is the write status.
        self.assertEqual({"active"}, {item.status for item in versions})
        limited = self.store.versions_of_record(NS, first.record_id, limit=1)
        self.assertEqual([2], [item.record_version for item in limited])
        self.assertEqual(2, self.store.count_versions_of_record(NS, first.record_id))

    def test_deleted_record_history_reads_empty(self) -> None:
        version = self.publish(profile_id="example.test.deleted")
        self.assertEqual(1, self.store.count_versions_of_record(NS, version.record_id))
        self.delete_record(version.record_id)
        self.assertEqual((), self.store.versions_of_record(NS, version.record_id))
        self.assertEqual(0, self.store.count_versions_of_record(NS, version.record_id))

    def test_body_digest_ignores_only_the_review_reference(self) -> None:
        review_ref = self.approve_review()
        body = self.profile_body(review_ref=review_ref, profile_id="example.test.digest")
        other = dict(body)
        other["review_ref"] = {
            "ref_type": "event",
            "namespace": NS,
            "event_id": str(uuid.uuid4()),
        }
        self.assertEqual(profile_body_digest(body), profile_body_digest(other))
        changed = dict(body)
        changed["identity_summary"] = "A different synthetic summary."
        self.assertNotEqual(profile_body_digest(body), profile_body_digest(changed))

    def test_matching_digest_event_publishes(self) -> None:
        body = self.profile_body(
            review_ref={}, profile_id="example.test.bound"
        )
        digest = profile_body_digest(body)
        review_ref = self._approval_with_digest(digest)
        body["review_ref"] = review_ref
        version = self.profiles.publish_profile(
            namespace=NS,
            profile_id="example.test.bound",
            body=body,
            review_ref=review_ref,
            authorization_ref="auth:cognition-test",
        )
        self.assertEqual(1, version.record_version)

    def test_mismatched_digest_event_is_refused(self) -> None:
        body = self.profile_body(
            review_ref={}, profile_id="example.test.replay"
        )
        other_body = dict(body)
        other_body["identity_summary"] = "Some other synthetic body."
        review_ref = self._approval_with_digest(profile_body_digest(other_body))
        body["review_ref"] = review_ref
        with self.assertRaises(SourceUntrusted):
            self.profiles.publish_profile(
                namespace=NS,
                profile_id="example.test.replay",
                body=body,
                review_ref=review_ref,
                authorization_ref="auth:cognition-test",
            )
        self.assertIsNone(self.store.record_pointer(NS, self._record_id("example.test.replay")))

    def _record_id(self, profile_id: str) -> str:
        from quintessence_pcs.domains.persona.profiles import profile_record_id

        return profile_record_id(NS, profile_id)

    def test_legacy_event_without_digest_still_publishes(self) -> None:
        review_ref = self.approve_review()
        body = self.profile_body(review_ref=review_ref, profile_id="example.test.legacy")
        version = self.profiles.publish_profile(
            namespace=NS,
            profile_id="example.test.legacy",
            body=body,
            review_ref=review_ref,
            authorization_ref="auth:cognition-test",
        )
        self.assertEqual(1, version.record_version)

    def test_activation_pointer_is_independent_of_row_status(self) -> None:
        first = self.publish(profile_id="example.test.pointer")
        review_ref = self.approve_review()
        body = self.profile_body(
            review_ref=review_ref,
            profile_id="example.test.pointer",
            profile_version="0.0.2",
        )
        self.profiles.publish_profile(
            namespace=NS,
            profile_id="example.test.pointer",
            body=body,
            review_ref=review_ref,
            authorization_ref="auth:cognition-test",
        )
        self.activate(first)
        pointer = self.store.active_profile_pointer(NS)
        self.assertEqual(
            (first.record_id, 1), (pointer.record_id, pointer.record_version)
        )
        statuses = {
            item.record_version: item.status
            for item in self.store.versions_of_record(NS, first.record_id)
        }
        self.assertEqual({1: "active", 2: "active"}, statuses)


if __name__ == "__main__":
    unittest.main()
