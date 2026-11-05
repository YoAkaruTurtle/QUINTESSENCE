"""RG08: persona policy assets against the real store (custom record kind).

This is the acceptance test the plan asked for before trusting the design: a
non-profile record kind must be writable, readable by its deterministic id and
visible through the deletion barrier query, without any DDL or migration.

Uses a throw-away sqlite database in a temporary directory; no trial data, no
model call.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from cognition_support import NS, CognitionTestCase  # noqa: E402

from quintessence_pcs.domains.persona.policy import (  # noqa: E402
    CANDIDATE_KIND,
    PUBLISHED_KIND,
    POLICY_SCHEMA_VERSION,
    policy_record_id,
    policy_text_sha256,
    resolve_persona_policy,
)
from quintessence_pcs.domains.persona.profiles import profile_record_id  # noqa: E402
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402

TEXT = "这是某个合成人格的表达政策。"


class PolicyAssetStoreTests(CognitionTestCase):
    def setUp(self) -> None:
        super().setUp()
        pointer = self.store.get_current(
            NS, profile_record_id(NS, "example.synthetic.cognition")
        )
        self.profile_ref = RecordRef(
            namespace=NS,
            record_id=pointer.record_id,
            record_version=pointer.record_version,
        )
        self.pid = pointer.body["profile_id"]
        self.pv = str(pointer.body["profile_version"])

    def _body(self, kind: str, review_ref):
        return {
            "schema_version": POLICY_SCHEMA_VERSION,
            "status": "published" if kind == PUBLISHED_KIND else "lab_candidate",
            "profile_id": self.pid,
            "profile_version": self.pv,
            "text": TEXT,
            "text_sha256": policy_text_sha256(TEXT),
            "review_ref": review_ref,
        }

    def _append(self, kind: str, review_ref=None):
        record_id = policy_record_id(NS, kind, self.pid, self.pv)
        self.store.append_record_version(
            namespace=NS,
            record_id=record_id,
            record_version=1,
            kind=kind,
            body=self._body(kind, review_ref),
            schema_version=POLICY_SCHEMA_VERSION,
        )
        return record_id

    def test_custom_kind_is_writable_and_resolvable(self) -> None:
        record_id = self._append(CANDIDATE_KIND)
        stored = self.store.get_current(NS, record_id)
        self.assertEqual(CANDIDATE_KIND, stored.kind)
        self.assertFalse(self.store.record_is_deleted(NS, record_id))

        lab = resolve_persona_policy(
            self.store, NS, self.profile_ref, host_is_lab=True
        )
        self.assertEqual("present", lab.state)
        self.assertEqual(TEXT, lab.text)

        production = resolve_persona_policy(
            self.store, NS, self.profile_ref, host_is_lab=False
        )
        self.assertEqual("absent", production.state)
        self.assertEqual("candidate_ignored", production.reason)

    def test_published_and_candidate_can_coexist(self) -> None:
        review = {"ref_type": "event", "namespace": NS, "event_id": "e-1"}
        published_id = self._append(PUBLISHED_KIND, review)
        candidate_id = self._append(CANDIDATE_KIND)
        self.assertNotEqual(published_id, candidate_id)
        asset = resolve_persona_policy(
            self.store, NS, self.profile_ref, host_is_lab=True
        )
        self.assertEqual("present", asset.state)
        self.assertEqual("approved", asset.review_state)

    def test_a_second_version_is_refused_by_the_resolver(self) -> None:
        # The store itself allows a second version (that is how a profile
        # evolves); the policy contract does not.  The resolver must therefore
        # refuse anything but version 1 rather than follow the pointer.
        record_id = self._append(CANDIDATE_KIND)
        self.store.append_record_version(
            namespace=NS,
            record_id=record_id,
            record_version=2,
            kind=CANDIDATE_KIND,
            body=self._body(CANDIDATE_KIND, None),
            schema_version=POLICY_SCHEMA_VERSION,
        )
        asset = resolve_persona_policy(
            self.store, NS, self.profile_ref, host_is_lab=True
        )
        self.assertEqual("invalid", asset.state)
        self.assertEqual("unexpected_version", asset.reason)


if __name__ == "__main__":
    unittest.main()
