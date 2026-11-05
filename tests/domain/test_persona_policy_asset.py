"""RG08: persona expression policy assets (kind-separated two-state model).

Pure unit tests: a fake record store stands in for the real one, so these run
without a database.  They pin the resolver's precedence rules: a published
asset always wins, a candidate is only visible to a trusted Lab host, and an
invalid published asset never falls back to the candidate.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parents[1] / "src"
for _path in (HERE, SRC):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from quintessence_pcs.domains.persona.policy import (  # noqa: E402
    CANDIDATE_KIND,
    PUBLISHED_KIND,
    PersonaPolicyAsset,
    policy_record_id,
    policy_text_sha256,
    resolve_persona_policy,
    validate_policy_body,
)
from quintessence_pcs.foundation.errors import InvalidInput, RecordNotFound  # noqa: E402
from quintessence_pcs.domains.refs import RecordRef  # noqa: E402

NS = "example.rg08.policy"
PID = "lab.persona.p0"
PV = "14"
PROFILE_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


class FakeRecord:
    def __init__(self, namespace, record_id, record_version, kind, body):
        self.namespace = namespace
        self.record_id = record_id
        self.record_version = record_version
        self.kind = kind
        self.body = body


class FakeStore:
    """Minimal store: exact versions, current pointer, deletion barrier."""

    def __init__(self):
        self.versions: dict[tuple[str, int], FakeRecord] = {}
        self.deleted: set[str] = set()
        self.reads = 0

    def add(self, record):
        self.versions[(record.record_id, record.record_version)] = record

    def get_version(self, namespace, record_id, record_version):
        self.reads += 1
        try:
            return self.versions[(record_id, record_version)]
        except KeyError:
            raise RecordNotFound("missing version") from None

    def get_current(self, namespace, record_id):
        self.reads += 1
        rows = [
            (ver, rec)
            for (rid, ver), rec in self.versions.items()
            if rid == record_id
        ]
        if not rows:
            raise RecordNotFound("missing record")
        return max(rows, key=lambda item: item[0])[1]

    def record_is_deleted(self, namespace, record_id):
        return record_id in self.deleted


def profile_record(store, profile_version=PV):
    rec = FakeRecord(
        NS,
        PROFILE_ID,
        3,
        "persona_profile",
        {"profile_id": PID, "profile_version": profile_version},
    )
    store.add(rec)
    return RecordRef(namespace=NS, record_id=PROFILE_ID, record_version=3)


def policy_body(kind, text="人格表达政策。", review=True):
    status = "published" if kind == PUBLISHED_KIND else "lab_candidate"
    body = {
        "schema_version": "1",
        "status": status,
        "profile_id": PID,
        "profile_version": PV,
        "text": text,
        "text_sha256": policy_text_sha256(text),
        "review_ref": (
            {"ref_type": "event", "namespace": NS, "event_id": "e-1"}
            if review
            else None
        ),
    }
    return body


def add_policy(store, kind, text="人格表达政策。", review=None, version=1):
    if review is None:
        review = kind == PUBLISHED_KIND
    rid = policy_record_id(NS, kind, PID, PV)
    body = policy_body(kind, text=text, review=review)
    rec = FakeRecord(NS, rid, version, kind, body)
    store.add(rec)
    return rid


class RecordIdTests(unittest.TestCase):
    def test_kind_is_part_of_the_identity(self) -> None:
        published = policy_record_id(NS, PUBLISHED_KIND, PID, PV)
        candidate = policy_record_id(NS, CANDIDATE_KIND, PID, PV)
        self.assertNotEqual(published, candidate)

        profile_record(FakeStore())
        store = FakeStore()
        add_policy(store, PUBLISHED_KIND)
        add_policy(store, CANDIDATE_KIND)
        self.assertEqual(2, len(store.versions))

    def test_identity_is_deterministic(self) -> None:
        first = policy_record_id(NS, PUBLISHED_KIND, PID, PV)
        second = policy_record_id(NS, PUBLISHED_KIND, PID, PV)
        self.assertEqual(first, second)

    def test_unknown_kind_is_refused(self) -> None:
        with self.assertRaises(InvalidInput):
            policy_record_id(NS, "persona_policy_bogus", PID, PV)


class BodyValidationTests(unittest.TestCase):
    def test_published_requires_review_ref(self) -> None:
        with self.assertRaises(InvalidInput):
            validate_policy_body(
                policy_body(PUBLISHED_KIND, review=False),
                profile_id=PID,
                profile_version=PV,
                kind=PUBLISHED_KIND,
            )

    def test_candidate_must_not_carry_review_ref(self) -> None:
        with self.assertRaises(InvalidInput):
            validate_policy_body(
                policy_body(CANDIDATE_KIND, review=True),
                profile_id=PID,
                profile_version=PV,
                kind=CANDIDATE_KIND,
            )

    def test_status_must_agree_with_kind(self) -> None:
        body = policy_body(PUBLISHED_KIND)
        body["status"] = "lab_candidate"
        with self.assertRaises(InvalidInput):
            validate_policy_body(
                body, profile_id=PID, profile_version=PV, kind=PUBLISHED_KIND
            )

    def test_digest_must_match_text(self) -> None:
        body = policy_body(PUBLISHED_KIND)
        body["text_sha256"] = "0" * 64
        with self.assertRaises(InvalidInput):
            validate_policy_body(
                body, profile_id=PID, profile_version=PV, kind=PUBLISHED_KIND
            )

    def test_unknown_key_is_refused(self) -> None:
        body = policy_body(PUBLISHED_KIND)
        body["extra"] = 1
        with self.assertRaises(InvalidInput):
            validate_policy_body(
                body, profile_id=PID, profile_version=PV, kind=PUBLISHED_KIND
            )


class ResolverTests(unittest.TestCase):
    def test_no_asset_is_absent(self) -> None:
        store = FakeStore()
        ref = profile_record(store)
        asset = resolve_persona_policy(store, NS, ref, host_is_lab=False)
        self.assertEqual("absent", asset.state)
        self.assertEqual("no_asset", asset.reason)

    def test_published_wins_over_candidate(self) -> None:
        store = FakeStore()
        ref = profile_record(store)
        add_policy(store, PUBLISHED_KIND, text="正式文本。")
        add_policy(store, CANDIDATE_KIND, text="候选文本。")
        asset = resolve_persona_policy(store, NS, ref, host_is_lab=True)
        self.assertEqual("present", asset.state)
        self.assertEqual("正式文本。", asset.text)
        self.assertEqual("approved", asset.review_state)

    def test_invalid_published_does_not_fall_back(self) -> None:
        store = FakeStore()
        ref = profile_record(store)
        rid = add_policy(store, PUBLISHED_KIND)
        store.versions[(rid, 1)].body["text_sha256"] = "0" * 64
        add_policy(store, CANDIDATE_KIND)
        asset = resolve_persona_policy(store, NS, ref, host_is_lab=True)
        self.assertEqual("invalid", asset.state)
        self.assertTrue(asset.reason.startswith("invalid_body:"))
        self.assertEqual("", asset.text)

    def test_candidate_is_invisible_to_a_non_lab_host(self) -> None:
        store = FakeStore()
        ref = profile_record(store)
        add_policy(store, CANDIDATE_KIND)
        asset = resolve_persona_policy(store, NS, ref, host_is_lab=False)
        self.assertEqual("absent", asset.state)
        self.assertEqual("candidate_ignored", asset.reason)

    def test_candidate_is_visible_to_a_lab_host(self) -> None:
        store = FakeStore()
        ref = profile_record(store)
        add_policy(store, CANDIDATE_KIND, text="实验文本。")
        asset = resolve_persona_policy(store, NS, ref, host_is_lab=True)
        self.assertEqual("present", asset.state)
        self.assertEqual("实验文本。", asset.text)
        self.assertEqual("unreviewed", asset.review_state)

    def test_deleted_published_is_invalid(self) -> None:
        store = FakeStore()
        ref = profile_record(store)
        rid = add_policy(store, PUBLISHED_KIND)
        store.deleted.add(rid)
        asset = resolve_persona_policy(store, NS, ref, host_is_lab=True)
        self.assertEqual("invalid", asset.state)
        self.assertEqual("deleted", asset.reason)

    def test_kind_mismatch_is_invalid(self) -> None:
        store = FakeStore()
        ref = profile_record(store)
        rid = add_policy(store, CANDIDATE_KIND)
        store.versions[(rid, 1)].kind = PUBLISHED_KIND  # forged kind on the row
        asset = resolve_persona_policy(store, NS, ref, host_is_lab=True)
        self.assertEqual("invalid", asset.state)
        self.assertEqual("kind_mismatch", asset.reason)


if __name__ == "__main__":
    unittest.main()
