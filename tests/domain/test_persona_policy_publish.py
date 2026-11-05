"""RG08 T-P2: publishing, seeding and promoting persona expression policies.

The three capabilities under test are deliberately asymmetric:

* ``publish_profile`` may write the profile and its expression policy in one
  transaction, so an approval can never end up bound to a profile without the
  text it approved (and vice versa);
* ``seed_lab_policy`` writes a candidate that carries no review reference and
  that a production host refuses to load, so it cannot become an approval
  bypass;
* ``promote_policy`` appends a **new** published record and leaves the
  candidate untouched, which is what lets "assets are immutable" and "a
  candidate can become published" hold at the same time.

Every test uses a fresh temporary database and synthetic text only.
"""

from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from domain_support import NS, PROFILE_ID, DomainTestCase  # noqa: E402

from quintessence_pcs.domains import SourceUntrusted  # noqa: E402
from quintessence_pcs.domains.persona.policy import (  # noqa: E402
    CANDIDATE_KIND,
    PUBLISHED_KIND,
    policy_record_id,
    policy_text_sha256,
    validate_policy_body,
)
from quintessence_pcs.domains.persona.profiles import (  # noqa: E402
    profile_body_digest,
    profile_record_id,
)
from quintessence_pcs.foundation.errors import (  # noqa: E402
    InvalidInput,
    RecordKindMismatch,
    RecordNotFound,
    Redacted,
)

POLICY_TEXT = "Synthetic persona policy used only to test the mechanism."
OTHER_POLICY_TEXT = "A different synthetic policy text, still synthetic."


class PersonaPolicyPublishTests(DomainTestCase):
    # ---------------------------------------------------------------- helpers

    def published_id(self, profile_version: str = "0.0.1") -> str:
        return policy_record_id(NS, PUBLISHED_KIND, PROFILE_ID, profile_version)

    def candidate_id(self, profile_version: str = "0.0.1") -> str:
        return policy_record_id(NS, CANDIDATE_KIND, PROFILE_ID, profile_version)

    def pointer(self, record_id: str):
        return self.store.record_pointer(NS, record_id)

    def body_of(self, record_id: str) -> dict:
        return dict(self.store.get_current(NS, record_id).body)

    def review_for_policy(self, text: str) -> dict:
        """A review event that names both the profile body and the policy text."""

        dummy_ref = {
            "ref_type": "event",
            "namespace": NS,
            "event_id": str(uuid.uuid4()),
        }
        body = self.profile_body(review_ref=dummy_ref)
        digest = profile_body_digest(body)
        return self.approve_review(
            payload={
                "decision": "approve",
                "body_sha256": digest,
                "expression_policy_sha256": policy_text_sha256(text),
            }
        )

    def publish_with_policy(
        self, text: str = POLICY_TEXT, *, review_ref: dict | None = None
    ):
        if review_ref is None:
            review_ref = self.review_for_policy(text)
        body = self.profile_body(review_ref=review_ref)
        return self.persona.publish_profile(
            namespace=NS,
            profile_id=PROFILE_ID,
            body=body,
            review_ref=review_ref,
            authorization_ref="auth:profile-publish",
            expression_policy={"text": text},
        )

    def seed(self, text: str = POLICY_TEXT, *, host_is_lab: bool = True):
        return self.persona.seed_lab_policy(
            namespace=NS,
            profile_id=PROFILE_ID,
            profile_version="0.0.1",
            text=text,
            host_is_lab=host_is_lab,
        )

    def promote(self, *, review_ref: dict | None = None, text: str = POLICY_TEXT):
        if review_ref is None:
            review_ref = self.approve_review(
                payload={
                    "decision": "approve",
                    "expression_policy_sha256": policy_text_sha256(text),
                }
            )
        return self.persona.promote_policy(
            namespace=NS,
            candidate_ref={
                "ref_type": "record",
                "namespace": NS,
                "record_id": self.candidate_id(),
                "record_version": 1,
            },
            review_ref=review_ref,
        )

    # ------------------------------------------- publish_profile (legacy path)

    def test_publish_without_policy_keeps_the_legacy_behaviour(self) -> None:
        version = self.publish()

        self.assertEqual(1, version.record_version)
        with self.assertRaises(RecordNotFound):
            self.store.get_current(NS, self.published_id())
        audit = self.audit_rows()
        self.assertEqual(
            ["persona.profile.publish"], [row["operation"] for row in audit]
        )
        self.assertEqual(1, self.revision())

    def test_publish_without_policy_still_advances_one_revision(self) -> None:
        self.publish()
        self.publish(profile_version="0.0.2")

        self.assertEqual(2, self.store.current_state_revision(NS))
        pointer = self.pointer(profile_record_id(NS, PROFILE_ID))
        self.assertEqual(2, pointer.current_version)

    # ------------------------------------------ publish_profile (with policy)

    def test_publish_with_policy_writes_both_records(self) -> None:
        version = self.publish_with_policy()

        self.assertEqual(1, version.record_version)
        policy = self.store.get_current(NS, self.published_id())
        self.assertEqual(PUBLISHED_KIND, policy.kind)
        self.assertEqual(1, policy.record_version)
        validate_policy_body(
            policy.body,
            profile_id=PROFILE_ID,
            profile_version="0.0.1",
            kind=PUBLISHED_KIND,
        )
        self.assertEqual(POLICY_TEXT, policy.body["text"])
        self.assertEqual(policy_text_sha256(POLICY_TEXT), policy.body["text_sha256"])
        self.assertEqual("published", policy.body["status"])
        self.assertIsInstance(policy.body["review_ref"], dict)

    def test_publish_with_policy_records_both_operations(self) -> None:
        self.publish_with_policy()

        operations = [row["operation"] for row in self.audit_rows()]
        self.assertIn("persona.profile.publish", operations)
        self.assertIn("persona.policy.publish", operations)
        self.assertEqual(2, len(operations))

    def test_publish_with_policy_is_one_commit(self) -> None:
        before = self.revision()
        self.publish_with_policy()

        # The profile version is the domain commit; the policy rides in it.
        self.assertEqual(before + 1, self.revision())

    def test_publish_rejects_mismatched_policy_digest_and_writes_nothing(
        self,
    ) -> None:
        dummy_ref = {
            "ref_type": "event",
            "namespace": NS,
            "event_id": str(uuid.uuid4()),
        }
        body = self.profile_body(review_ref=dummy_ref)
        digest = profile_body_digest(body)
        review_ref = self.approve_review(
            payload={
                "decision": "approve",
                "body_sha256": digest,
                "expression_policy_sha256": "f" * 64,
            }
        )
        body["review_ref"] = review_ref

        with self.assertRaises(SourceUntrusted):
            self.persona.publish_profile(
                namespace=NS,
                profile_id=PROFILE_ID,
                body=body,
                review_ref=review_ref,
                authorization_ref="auth:profile-publish",
                expression_policy={"text": POLICY_TEXT},
            )

        self.assertIsNone(self.pointer(profile_record_id(NS, PROFILE_ID)))
        self.assertIsNone(self.pointer(self.published_id()))
        self.assertEqual(0, self.revision())
        self.assertEqual([], self.audit_rows())

    def test_publish_rejects_a_second_policy_for_the_same_profile_version(
        self,
    ) -> None:
        self.publish_with_policy()

        with self.assertRaises(InvalidInput):
            self.publish_with_policy(text=OTHER_POLICY_TEXT)

        pointer = self.pointer(profile_record_id(NS, PROFILE_ID))
        self.assertEqual(1, pointer.current_version)
        self.assertEqual(
            POLICY_TEXT, self.body_of(self.published_id())["text"]
        )

    def test_publish_rejects_an_empty_or_oversized_policy(self) -> None:
        for text in ("", "   ", "x" * 16385):
            with self.subTest(text=text[:8]):
                review_ref = self.approve_review()
                body = self.profile_body(review_ref=review_ref)
                with self.assertRaises(InvalidInput):
                    self.persona.publish_profile(
                        namespace=NS,
                        profile_id=PROFILE_ID,
                        body=body,
                        review_ref=review_ref,
                        authorization_ref="auth:profile-publish",
                        expression_policy={"text": text},
                    )
        self.assertIsNone(self.pointer(profile_record_id(NS, PROFILE_ID)))

    def test_publish_rejects_unknown_policy_keys(self) -> None:
        review_ref = self.approve_review()
        body = self.profile_body(review_ref=review_ref)

        with self.assertRaises(InvalidInput):
            self.persona.publish_profile(
                namespace=NS,
                profile_id=PROFILE_ID,
                body=body,
                review_ref=review_ref,
                authorization_ref="auth:profile-publish",
                expression_policy={"text": POLICY_TEXT, "status": "published"},
            )

    # ------------------------------------------------------ seed_lab_policy

    def test_seed_requires_a_trusted_lab_host(self) -> None:
        for value in (False, 1, "yes", None):
            with self.subTest(value=value):
                with self.assertRaises(InvalidInput):
                    self.seed(host_is_lab=value)  # type: ignore[arg-type]

        self.assertIsNone(self.pointer(self.candidate_id()))
        self.assertEqual([], self.audit_rows())

    def test_seed_writes_a_candidate_without_a_review_reference(self) -> None:
        version = self.seed()

        self.assertEqual(CANDIDATE_KIND, version.kind)
        self.assertEqual(1, version.record_version)
        body = self.body_of(self.candidate_id())
        validate_policy_body(
            body,
            profile_id=PROFILE_ID,
            profile_version="0.0.1",
            kind=CANDIDATE_KIND,
        )
        self.assertEqual("lab_candidate", body["status"])
        self.assertIsNone(body["review_ref"])
        self.assertEqual(POLICY_TEXT, body["text"])
        self.assertEqual(
            ["persona.policy.seed"],
            [row["operation"] for row in self.audit_rows()],
        )

    def test_seed_rejects_a_duplicate_candidate(self) -> None:
        self.seed()

        with self.assertRaises(InvalidInput):
            self.seed()

        pointer = self.pointer(self.candidate_id())
        self.assertEqual(1, pointer.current_version)

    # -------------------------------------------------------- promote_policy

    def test_promote_rejects_an_unbound_approval(self) -> None:
        self.seed()
        review_ref = self.approve_review(payload={"decision": "approve"})

        with self.assertRaises(SourceUntrusted):
            self.promote(review_ref=review_ref)

        self.assertIsNone(self.pointer(self.published_id()))
        self.assertEqual(
            CANDIDATE_KIND, self.pointer(self.candidate_id()).kind
        )

    def test_promote_rejects_a_different_policy_digest(self) -> None:
        self.seed()
        review_ref = self.approve_review(
            payload={"expression_policy_sha256": policy_text_sha256("other")}
        )

        with self.assertRaises(SourceUntrusted):
            self.promote(review_ref=review_ref)

        self.assertIsNone(self.pointer(self.published_id()))

    def test_promote_creates_the_published_record_and_leaves_the_candidate(
        self,
    ) -> None:
        self.seed()
        candidate_before = self.store.get_current(NS, self.candidate_id())

        published = self.promote()

        self.assertEqual(PUBLISHED_KIND, published.kind)
        self.assertEqual(1, published.record_version)
        body = self.body_of(self.published_id())
        validate_policy_body(
            body,
            profile_id=PROFILE_ID,
            profile_version="0.0.1",
            kind=PUBLISHED_KIND,
        )
        self.assertEqual(POLICY_TEXT, body["text"])
        self.assertEqual(policy_text_sha256(POLICY_TEXT), body["text_sha256"])
        self.assertIsInstance(body["review_ref"], dict)

        candidate_after = self.store.get_current(NS, self.candidate_id())
        self.assertEqual(candidate_before.record_version, candidate_after.record_version)
        self.assertEqual(dict(candidate_before.body), dict(candidate_after.body))
        self.assertEqual(CANDIDATE_KIND, candidate_after.kind)

        operations = [row["operation"] for row in self.audit_rows()]
        # Both rows share one recorded_at, so the query's tie-break is the
        # operation name; compare the sorted set rather than insertion order.
        self.assertEqual(
            ["persona.policy.promote", "persona.policy.seed"], sorted(operations)
        )

    def test_promote_rejects_a_second_promotion(self) -> None:
        self.seed()
        self.promote()

        with self.assertRaises(InvalidInput):
            self.promote()

        self.assertEqual(1, self.pointer(self.published_id()).current_version)

    def test_promote_rejects_a_record_that_is_not_a_candidate(self) -> None:
        profile = self.publish()
        review_ref = self.approve_review(
            payload={"expression_policy_sha256": policy_text_sha256(POLICY_TEXT)}
        )

        with self.assertRaises(RecordKindMismatch):
            self.persona.promote_policy(
                namespace=NS,
                candidate_ref={
                    "ref_type": "record",
                    "namespace": NS,
                    "record_id": profile.record_id,
                    "record_version": profile.record_version,
                },
                review_ref=review_ref,
            )

    def test_promote_refuses_a_deleted_candidate(self) -> None:
        self.seed()
        self.delete_record(self.candidate_id())

        with self.assertRaises(Redacted):
            self.promote()

        self.assertIsNone(self.pointer(self.published_id()))

    def test_promote_rejects_a_foreign_namespace(self) -> None:
        self.seed()
        review_ref = self.approve_review(
            payload={"expression_policy_sha256": policy_text_sha256(POLICY_TEXT)}
        )

        with self.assertRaises(InvalidInput):
            self.persona.promote_policy(
                namespace=NS,
                candidate_ref={
                    "ref_type": "record",
                    "namespace": "example.test.domain.other",
                    "record_id": self.candidate_id(),
                    "record_version": 1,
                },
                review_ref=review_ref,
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
