"""PCS2-005 acceptance tests for the event journal and Tx A.

``test_01`` .. ``test_11`` map one-to-one to the eleven required behaviours in
the handoff card.  The extra classes cover the structural requirements the card
states in prose (schema-level idempotency, immutability, payload integrity,
source-field validation) and pin the event fields to ``contracts/v2``.

Every test builds its own database under a temporary directory.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from support import REPO_ROOT, SRC_DIR, StoreTestCase  # noqa: E402

from quintessence_pcs.foundation import identifiers  # noqa: E402
from quintessence_pcs.foundation.errors import (  # noqa: E402
    CorruptStore,
    EventNotFound,
    InvalidInput,
    ScopeViolation,
    SourceEventConflict,
)
from quintessence_pcs.foundation.jsonutil import (  # noqa: E402
    canonical_json_text,
    sha256_hex,
)
from quintessence_pcs.store.events import (  # noqa: E402
    OUTCOME_CREATED,
    OUTCOME_DUPLICATE,
    OUTCOMES,
    PENDING_WORK_KIND,
    EventStore,
)

NS = "example.test.events"
NS_B = "example.test.events.other"
INSTANCE = "instance-a"
RECORDED_AT = "2026-10-17T00:00:00Z"
#: A target that is never ingested: causation may arrive before its cause.
ABSENT_EVENT_ID = "12121212-1212-4212-8212-121212121212"

_INSERT_EVENT_SQL = (
    "INSERT INTO event_journal ("
    "namespace, event_id, event_seq, source_kind, source_instance, source_event_id, "
    "source_position, event_type, observed_at, recorded_at, payload_ref, privacy_class"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def event_ref(event_id: str, namespace: str = NS) -> dict[str, str]:
    return {"ref_type": "event", "namespace": namespace, "event_id": event_id}


def record_ref(record_id: str, namespace: str = NS) -> dict[str, object]:
    return {
        "ref_type": "record",
        "namespace": namespace,
        "record_id": record_id,
        "record_version": 1,
    }


class EventStoreTestCase(StoreTestCase):
    """The PCS2-004 harness with the PCS2-005 store."""

    store_class = EventStore

    def ingest(self, **overrides: object):
        kwargs: dict[str, object] = {
            "namespace": NS,
            "source_kind": "synthetic_fixture",
            "source_instance": INSTANCE,
            "source_event_id": "source-1",
            "event_type": "ObservationRecorded",
            "payload": {"text": "first"},
            "recorded_at": RECORDED_AT,
        }
        kwargs.update(overrides)
        return self.store.ingest_event(**kwargs)  # type: ignore[attr-defined]

    def journal_rows(self) -> list[sqlite3.Row]:
        return self.store.connection.execute(
            "SELECT * FROM event_journal ORDER BY event_seq"
        ).fetchall()

    def pending_rows(self) -> list[sqlite3.Row]:
        return self.store.connection.execute(
            "SELECT * FROM pending_work ORDER BY work_id"
        ).fetchall()


class RequiredIngestScenarios(EventStoreTestCase):
    """The eleven scenarios named by the PCS2-005 card."""

    def test_01_first_ingest_creates_event_work_and_sequence(self) -> None:
        receipt = self.ingest(
            source_position=42,
            payload={"text": "first", "nested": {"b": [1, 2], "a": None}},
            privacy_class="restricted",
        )

        self.assertEqual(OUTCOME_CREATED, receipt.outcome)
        self.assertEqual(NS, receipt.namespace)
        self.assertEqual(1, receipt.event_seq)
        uuid.UUID(receipt.event_id)  # canonical UUID text, parseable
        self.assertEqual(1, self.store.last_event_seq(NS))
        self.assertEqual(1, self.count_rows("event_journal"))

        stored = self.store.get_event(NS, receipt.event_id)
        self.assertEqual(receipt.event_id, stored.event_id)
        self.assertEqual(1, stored.event_seq)
        self.assertEqual("synthetic_fixture", stored.source_kind)
        self.assertEqual(INSTANCE, stored.source_instance)
        self.assertEqual("source-1", stored.source_event_id)
        self.assertEqual(42, stored.source_position)
        self.assertEqual("ObservationRecorded", stored.event_type)
        self.assertIsNone(stored.observed_at)
        self.assertEqual(RECORDED_AT, stored.recorded_at)
        self.assertEqual("restricted", stored.privacy_class)
        self.assertEqual({"text": "first", "nested": {"b": [1, 2], "a": None}}, stored.payload)

        work = self.store.pending_work_for(NS, receipt.event_id)
        self.assertEqual(1, len(work))
        self.assertEqual(PENDING_WORK_KIND, work[0].kind)
        self.assertEqual("pending", work[0].status)
        self.assertEqual(receipt.event_id, work[0].event_id)
        self.assertEqual("source-1", work[0].source_event_id)
        self.assertEqual(RECORDED_AT, work[0].created_at)

    def test_02_duplicate_returns_the_first_receipt_and_writes_nothing(self) -> None:
        first = self.ingest(source_event_id="source-1")
        second = self.ingest(source_event_id="source-2", payload={"text": "second"})
        self.assertEqual(1, first.event_seq)
        self.assertEqual(2, second.event_seq)
        journal_before = self.journal_rows()
        payloads_before = self.count_rows("private_payloads")
        work_before = len(self.pending_rows())

        repeat = self.ingest(source_event_id="source-1")

        self.assertEqual(OUTCOME_DUPLICATE, repeat.outcome)
        self.assertEqual(first.event_id, repeat.event_id)
        self.assertEqual(1, repeat.event_seq)
        self.assertEqual(journal_before, self.journal_rows())
        self.assertEqual(payloads_before, self.count_rows("private_payloads"))
        self.assertEqual(work_before, len(self.pending_rows()))
        self.assertEqual(2, self.store.last_event_seq(NS))

        # A duplicate consumes no number: the next new event is 3, not 4.
        third = self.ingest(source_event_id="source-3", payload={"text": "third"})
        self.assertEqual(OUTCOME_CREATED, third.outcome)
        self.assertEqual(3, third.event_seq)
        self.assertEqual([1, 2, 3], [int(row["event_seq"]) for row in self.journal_rows()])

        # The uniqueness is in the schema, not in a Python cache: a raw second
        # row for the same source key is rejected by the database itself.
        payload_ref = self.store.get_event(NS, first.event_id).payload_ref
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.store.connection.execute(
                _INSERT_EVENT_SQL,
                (
                    NS,
                    "99999999-9999-4999-8999-999999999999",
                    99,
                    "synthetic_fixture",
                    INSTANCE,
                    "source-1",
                    None,
                    "ObservationRecorded",
                    None,
                    RECORDED_AT,
                    payload_ref,
                    "private",
                ),
            )
        self.assertIn("UNIQUE constraint failed", str(caught.exception))
        self.assertEqual(3, self.count_rows("event_journal"))

    def test_03_same_key_with_a_different_payload_is_a_conflict(self) -> None:
        first = self.ingest(payload={"text": "original"})
        payloads_before = self.count_rows("private_payloads")

        with self.assertRaises(SourceEventConflict) as caught:
            self.ingest(payload={"text": "changed"})
        self.assertEqual("ID_CONFLICT", caught.exception.code)

        self.assertEqual(1, self.count_rows("event_journal"))
        self.assertEqual(1, self.count_rows("pending_work"))
        self.assertEqual(payloads_before, self.count_rows("private_payloads"))
        self.assertEqual(1, self.store.last_event_seq(NS))
        stored = self.store.get_event(NS, first.event_id)
        self.assertEqual({"text": "original"}, stored.payload)
        self.assertEqual(RECORDED_AT, stored.recorded_at)

        # Neither a different recorded_at nor a different event_type overwrites
        # the first row; the payload alone decides duplicate vs conflict.
        self.assertEqual(
            OUTCOME_DUPLICATE,
            self.ingest(payload={"text": "original"}, event_type="OtherType").outcome,
        )
        with self.assertRaises(SourceEventConflict):
            self.ingest(payload={"text": "Original"})

    def test_04_normalization_makes_key_order_and_whitespace_irrelevant(self) -> None:
        receipt = self.ingest(payload={"b": 1, "a": {"d": 2, "c": 3}})
        stored = self.store.get_event(NS, receipt.event_id)

        expected_text = '{"a":{"c":3,"d":2},"b":1}'
        self.assertEqual(expected_text, stored.payload_text)
        self.assertEqual(sha256_hex(expected_text), stored.payload_sha256)
        # PCS2-006 B1: the reference is opaque, not derived from the digest.
        self.assertTrue(stored.payload_ref.startswith("payload:"))
        self.assertNotEqual(f"sha256:{stored.payload_sha256}", stored.payload_ref)

        # Reordered keys.
        self.assertEqual(
            OUTCOME_DUPLICATE,
            self.ingest(payload={"a": {"c": 3, "d": 2}, "b": 1}).outcome,
        )
        # Same object delivered as JSON text with insignificant whitespace.
        whitespace_text = '{\n  "b" : 1,\n  "a": { "d": 2, "c": 3 }\n}\n'
        duplicate = self.ingest(payload=whitespace_text)
        self.assertEqual(OUTCOME_DUPLICATE, duplicate.outcome)
        self.assertEqual(receipt.event_id, duplicate.event_id)
        self.assertEqual(1, self.count_rows("event_journal"))

        # A different canonical form is a different payload (ID_CONFLICT).
        with self.assertRaises(SourceEventConflict):
            self.ingest(payload={"a": {"c": 3, "d": "2"}, "b": 1})
        self.assertEqual(expected_text, self.store.get_event(NS, receipt.event_id).payload_text)

    def test_05_duplicate_survives_close_and_reopen(self) -> None:
        first = self.ingest()
        self.reopen()

        repeat = self.ingest()
        self.assertEqual(OUTCOME_DUPLICATE, repeat.outcome)
        self.assertEqual(first.event_id, repeat.event_id)
        self.assertEqual(first.event_seq, repeat.event_seq)
        self.assertEqual(1, self.store.last_event_seq(NS))
        self.assertEqual(1, self.count_rows("event_journal"))
        self.assertEqual(1, self.count_rows("private_payloads"))
        self.assertEqual(1, self.count_rows("pending_work"))

    def test_05b_duplicate_survives_a_separate_process(self) -> None:
        first = self.ingest(payload={"text": "restart-check"})
        self.store.close()

        script = "\n".join(
            [
                "import json, sys",
                f"sys.path.insert(0, {str(SRC_DIR)!r})",
                "from quintessence_pcs.store.events import EventStore",
                f"store = EventStore.open({str(self.db_path)!r})",
                "receipt = store.ingest_event(",
                f"    namespace={NS!r}, source_kind='synthetic_fixture',",
                f"    source_instance={INSTANCE!r}, source_event_id='source-1',",
                "    event_type='ObservationRecorded', payload={'text': 'restart-check'},",
                f"    recorded_at={RECORDED_AT!r},",
                ")",
                "row = store.connection.execute(",
                "    'SELECT COUNT(*) FROM event_journal').fetchone()[0]",
                "print(json.dumps({",
                "    'outcome': receipt.outcome,",
                "    'event_id': receipt.event_id,",
                "    'event_seq': receipt.event_seq,",
                "    'last_event_seq': store.last_event_seq(receipt.namespace),",
                "    'events': row,",
                "}))",
                "store.close()",
            ]
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            cwd=str(REPO_ROOT),
        )
        self.assertEqual(0, completed.returncode, completed.stderr)
        observed = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(
            {
                "outcome": OUTCOME_DUPLICATE,
                "event_id": first.event_id,
                "event_seq": 1,
                "last_event_seq": 1,
                "events": 1,
            },
            observed,
        )

        self.reopen()
        self.assertEqual(1, self.store.last_event_seq(NS))
        self.assertEqual(1, self.count_rows("event_journal"))
        self.assertEqual({"text": "restart-check"}, self.store.get_event(NS, first.event_id).payload)

    def test_06_ingesting_events_never_advances_state_revision(self) -> None:
        self.assertEqual(0, self.store.current_state_revision(NS))

        statements: list[str] = []
        self.store.connection.set_trace_callback(statements.append)
        try:
            receipt = self.ingest()
        finally:
            self.store.connection.set_trace_callback(None)

        self.assertEqual(OUTCOME_CREATED, receipt.outcome)
        self.assertEqual(1, self.store.last_event_seq(NS))
        self.assertEqual(0, self.store.current_state_revision(NS))

        # Structural evidence: no statement assigns or increments
        # namespace_state.state_revision.  Only last_event_seq moves.
        assignment = re.compile(r"state_revision\s*=")
        self.assertEqual([], [item for item in statements if assignment.search(item)])
        self.assertTrue(any("last_event_seq" in item for item in statements))

        # A record write still advances the domain revision, and the following
        # event leaves it exactly where the record write put it.
        self.store.append_record_version(
            namespace=NS,
            record_id="22222222-2222-4222-8222-222222222222",
            record_version=1,
            kind="Observation",
            body={"text": "domain change"},
            recorded_at=RECORDED_AT,
        )
        self.assertEqual(1, self.store.current_state_revision(NS))
        self.ingest(source_event_id="source-2", payload={"text": "another"})
        self.assertEqual(1, self.store.current_state_revision(NS))
        self.assertEqual(2, self.store.last_event_seq(NS))

        # And the event path never calls the revision allocator at all.
        guarded = _NoRevisionStore.open(self.tmp_path / "guarded.sqlite")
        self.addCleanup(guarded.close)
        guarded_receipt = guarded.ingest_event(
            namespace=NS,
            source_kind="synthetic_fixture",
            source_instance=INSTANCE,
            source_event_id="source-1",
            event_type="ObservationRecorded",
            payload={"text": "guarded"},
            recorded_at=RECORDED_AT,
        )
        self.assertEqual(OUTCOME_CREATED, guarded_receipt.outcome)
        self.assertEqual(0, guarded.current_state_revision(NS))
        self.assertEqual(1, guarded.last_event_seq(NS))

    def test_07_event_and_pending_work_are_all_or_nothing(self) -> None:
        self.store.close()
        self.store = _FailingPendingWorkStore.open(self.db_path)

        with self.assertRaises(RuntimeError):
            self.ingest()

        self.assertEqual(0, self.count_rows("event_journal"))
        self.assertEqual(0, self.count_rows("private_payloads"))
        self.assertEqual(0, self.count_rows("pending_work"))
        self.assertEqual(0, self.count_rows("event_causes"))
        self.assertEqual(0, self.count_rows("namespace_state"))
        self.assertEqual(0, self.store.last_event_seq(NS))
        self.assertFalse(self.store.connection.in_transaction)

        # The rolled-back attempt consumed no sequence number and left nothing.
        self.store.fail_before_pending_work = False
        receipt = self.ingest()
        self.assertEqual(OUTCOME_CREATED, receipt.outcome)
        self.assertEqual(1, receipt.event_seq)
        self.assertEqual(1, self.count_rows("event_journal"))
        self.assertEqual(1, self.count_rows("pending_work"))
        self.assertEqual(1, self.store.last_event_seq(NS))

    def test_08_late_causation_is_allowed_and_stays_pending(self) -> None:
        cause = self.ingest(source_event_id="cause", payload={"text": "cause"})
        self.assertEqual(1, cause.event_seq)

        effect = self.ingest(
            source_event_id="effect",
            payload={"text": "effect"},
            causation_refs=[event_ref(cause.event_id), event_ref(ABSENT_EVENT_ID)],
        )
        self.assertEqual(2, effect.event_seq)
        self.assertEqual(
            {cause.event_id: "resolved", ABSENT_EVENT_ID: "pending"},
            {edge.to_event_id: edge.status for edge in self.store.causes_of(NS, effect.event_id)},
        )

        # The target may arrive after the edge, and the FK does not block it.
        # This card generates event IDs internally, so the late row is inserted
        # directly here: it stands in for the arrival path PCS2-007 will own.
        payload_ref = self.store.get_event(NS, effect.event_id).payload_ref
        self.store.connection.execute(
            _INSERT_EVENT_SQL,
            (
                NS,
                ABSENT_EVENT_ID,
                3,
                "synthetic_fixture",
                INSTANCE,
                "late-cause",
                None,
                "LateCause",
                None,
                RECORDED_AT,
                payload_ref,
                "private",
            ),
        )
        self.store.connection.execute(
            "UPDATE namespace_state SET last_event_seq = 3 WHERE namespace = ?", (NS,)
        )
        self.assertEqual("LateCause", self.store.get_event(NS, ABSENT_EVENT_ID).event_type)
        self.assertEqual(3, self.store.last_event_seq(NS))

        # No resolution logic exists in this card, so the edge is still
        # 'pending' even though its target is now present.  That is deliberate
        # and documented; the resolving card will flip it in its own migration.
        self.assertEqual(
            "pending",
            {edge.to_event_id: edge.status for edge in self.store.causes_of(NS, effect.event_id)}[
                ABSENT_EVENT_ID
            ],
        )

        # event_seq is receive order only: the effect arrived at 2 and points
        # at a cause that only arrived at 3, so sequence cannot order causes.
        self.assertEqual(2, self.store.get_event(NS, effect.event_id).event_seq)
        self.assertEqual(3, self.store.get_event(NS, ABSENT_EVENT_ID).event_seq)
        self.assertEqual(2, self.count_rows("event_causes"))

    def test_09_source_fields_are_validated(self) -> None:
        invalid_cases = (
            {"source_kind": "model_guess"},
            {"source_kind": "host"},
            {"source_kind": None},
            {"source_instance": ""},
            {"source_instance": "x" * 257},
            {"source_instance": None},
            {"source_event_id": ""},
            {"source_event_id": "x" * 513},
            {"source_event_id": None},
            {"event_type": ""},
            {"event_type": "x" * 129},
            {"privacy_class": "secret"},
            {"source_position": -1},
            {"source_position": True},
            {"source_position": 1.0},
            {"source_position": "1"},
            {"source_position": identifiers.MAX_SAFE_INTEGER + 1},
            {"observed_at": "2026-10-17T00:00:00"},
            {"observed_at": "2026-10-17T00:00:00+00:00"},
            {"payload": []},
            {"payload": "not json"},
            {"payload": {"x": float("nan")}},
        )
        for overrides in invalid_cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(InvalidInput):
                    self.ingest(**overrides)
        self.assertEqual(0, self.count_rows("event_journal"))
        self.assertEqual(0, self.count_rows("namespace_state"))

        # Missing source_instance is a required-keyword error, not a verdict.
        with self.assertRaises(TypeError):
            self.store.ingest_event(  # type: ignore[call-arg]
                namespace=NS,
                source_kind="synthetic_fixture",
                source_event_id="source-1",
                event_type="ObservationRecorded",
                payload={"text": "no instance"},
            )
        with self.assertRaises(TypeError):
            self.store.ingest_event(  # type: ignore[call-arg]
                namespace=NS,
                source_kind="synthetic_fixture",
                source_instance=INSTANCE,
                source_event_id="source-1",
                event_type="ObservationRecorded",
            )

        # An omitted payload becomes None for the same reason and is rejected
        # by the store rather than stored as JSON null.
        with self.assertRaises(InvalidInput):
            self.ingest(payload=None)

        # Every J.2 source kind is accepted and round-trips.
        for index, source_kind in enumerate(identifiers.SOURCE_KINDS):
            with self.subTest(source_kind=source_kind):
                receipt = self.ingest(
                    source_kind=source_kind,
                    source_event_id=f"kind-{index}",
                    payload={"text": source_kind},
                )
                self.assertEqual(
                    source_kind, self.store.get_event(NS, receipt.event_id).source_kind
                )

        # Nullable positional/time fields round-trip.
        bounded = self.ingest(
            source_event_id="bounds",
            payload={"text": "bounds"},
            source_position=identifiers.MAX_SAFE_INTEGER,
            observed_at="2026-10-17T12:34:56.123456Z",
        )
        stored = self.store.get_event(NS, bounded.event_id)
        self.assertEqual(identifiers.MAX_SAFE_INTEGER, stored.source_position)
        self.assertEqual("2026-10-17T12:34:56.123456Z", stored.observed_at)

    def test_10_namespaces_keep_their_own_sequence_and_source_key(self) -> None:
        in_a = self.ingest(namespace=NS)
        in_b = self.ingest(namespace=NS_B, payload={"text": "other namespace"})

        self.assertEqual(1, in_a.event_seq)
        self.assertEqual(1, in_b.event_seq)
        self.assertNotEqual(in_a.event_id, in_b.event_id)
        self.assertEqual(1, self.store.last_event_seq(NS))
        self.assertEqual(1, self.store.last_event_seq(NS_B))

        # The same source key is only a duplicate inside its own namespace.
        self.assertEqual(
            OUTCOME_DUPLICATE,
            self.ingest(namespace=NS_B, payload={"text": "other namespace"}).outcome,
        )
        # With a different payload the same key is a conflict in that namespace,
        # not a duplicate: isolation keeps both facts, it does not merge keys.
        with self.assertRaises(SourceEventConflict):
            self.ingest(namespace=NS_B)

        second = self.ingest(namespace=NS, source_event_id="source-2")
        self.assertEqual(2, second.event_seq)
        self.assertEqual(2, self.store.last_event_seq(NS))
        self.assertEqual(1, self.store.last_event_seq(NS_B))

        rows = self.store.connection.execute(
            "SELECT namespace, COUNT(*) AS n, MAX(event_seq) AS seq FROM event_journal "
            "GROUP BY namespace ORDER BY namespace"
        ).fetchall()
        self.assertEqual(
            [(NS, 2, 2), (NS_B, 1, 1)],
            [(row["namespace"], row["n"], row["seq"]) for row in rows],
        )

    def test_11_chinese_and_long_fields_round_trip_byte_exactly(self) -> None:
        long_path = "X:/synthetic/合成样例/" + "很长的目录名/" * 40 + "样例实体-第七十章.txt"
        payload = {
            "text": "合成中文样例：第一卷第「样例实体」，——中文标点与全角符号。",
            "path": long_path,
            "note": "line one\r\nline two",
        }
        receipt = self.ingest(payload=payload, source_event_id="中文-来源事件-0001")

        stored = self.store.get_event(NS, receipt.event_id)
        self.assertEqual(payload, stored.payload)
        self.assertEqual("中文-来源事件-0001", stored.source_event_id)
        self.assertEqual(canonical_json_text(payload), stored.payload_text)
        # UTF-8 without ASCII escaping and without platform newline translation.
        self.assertIn("合成中文样例", stored.payload_text)
        self.assertNotIn("\\u", stored.payload_text)
        raw = self.store.connection.execute(
            "SELECT body_json, sha256 FROM private_payloads WHERE payload_ref = ?",
            (stored.payload_ref,),
        ).fetchone()
        stored_bytes = raw["body_json"].encode("utf-8")
        self.assertNotIn(b"\r", stored_bytes)
        self.assertEqual(sha256_hex(stored.payload_text), raw["sha256"])
        self.assertEqual(len(stored.payload_text.encode("utf-8")), len(stored_bytes))

        # The payload row is reused, not duplicated, by an identical delivery.
        self.assertEqual(OUTCOME_DUPLICATE, self.ingest(
            payload=payload, source_event_id="中文-来源事件-0001"
        ).outcome)
        self.assertEqual(1, self.count_rows("private_payloads"))


class _NoRevisionStore(EventStore):
    """Proves the event path never reserves a domain revision (J.7)."""

    def next_state_revision(self, namespace: str) -> int:  # pragma: no cover - must not run
        raise AssertionError("ingesting an event must not reserve a state revision")

    def _allocate_state_revision_locked(self, namespace: str) -> int:
        raise AssertionError("ingesting an event must not reserve a state revision")


class _FailingPendingWorkStore(EventStore):
    """Fails right before pending_work, to prove the whole Tx A rolls back."""

    fail_before_pending_work = True

    def _insert_pending_work_locked(
        self, namespace: str, event_id: str, source_event_id: str, created_at: str
    ) -> None:
        if self.fail_before_pending_work:
            raise RuntimeError("injected failure before pending_work")
        super()._insert_pending_work_locked(namespace, event_id, source_event_id, created_at)


class CausationReferenceTests(EventStoreTestCase):
    def test_causation_references_are_validated(self) -> None:
        target = "33333333-3333-4333-8333-333333333333"
        cases = (
            ([{"event_id": target}], InvalidInput),
            ([target], InvalidInput),
            ([event_ref(target, namespace=NS_B)], ScopeViolation),
            ([record_ref(target)], InvalidInput),
            ([{**event_ref(target), "extra": True}], InvalidInput),
            ([event_ref(target), event_ref(target)], InvalidInput),
            ([event_ref("not-a-uuid")], InvalidInput),
            (event_ref(target), InvalidInput),
        )
        for causation_refs, expected in cases:
            with self.subTest(causation_refs=causation_refs):
                with self.assertRaises(expected):
                    self.ingest(causation_refs=causation_refs)  # type: ignore[arg-type]
        self.assertEqual(0, self.count_rows("event_journal"))
        self.assertEqual(0, self.count_rows("event_causes"))

        with self.assertRaises(ScopeViolation) as caught:
            self.ingest(causation_refs=[event_ref(target, namespace=NS_B)])
        self.assertEqual("SCOPE_VIOLATION", caught.exception.code)

        # A self-reference cannot be produced (the event ID is fresh) and a
        # raw self-edge is rejected by the database CHECK.
        receipt = self.ingest()
        with self.assertRaises(sqlite3.IntegrityError) as caught:
            self.store.connection.execute(
                "INSERT INTO event_causes (namespace, from_event_id, to_event_id, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (NS, receipt.event_id, receipt.event_id, RECORDED_AT),
            )
        self.assertIn("CHECK constraint failed", str(caught.exception))


class EventIntegrityTests(EventStoreTestCase):
    def test_event_rows_cannot_be_updated_or_deleted(self) -> None:
        receipt = self.ingest()
        with self.assertRaises(sqlite3.IntegrityError) as updated:
            self.store.connection.execute(
                "UPDATE event_journal SET payload_ref = 'sha256:deadbeef' WHERE event_id = ?",
                (receipt.event_id,),
            )
        self.assertIn("immutable", str(updated.exception))

        with self.assertRaises(sqlite3.IntegrityError) as deleted:
            self.store.connection.execute(
                "DELETE FROM event_journal WHERE event_id = ?", (receipt.event_id,)
            )
        self.assertIn("immutable", str(deleted.exception))

        self.assertEqual({"text": "first"}, self.store.get_event(NS, receipt.event_id).payload)

    def test_event_payload_tampering_is_reported_as_corruption(self) -> None:
        receipt = self.ingest()
        stored = self.store.get_event(NS, receipt.event_id)
        self.store.connection.execute(
            "UPDATE private_payloads SET body_json = ? WHERE payload_ref = ?",
            ('{"text":"tampered"}', stored.payload_ref),
        )
        with self.assertRaises(CorruptStore) as caught:
            self.store.get_event(NS, receipt.event_id)
        self.assertEqual("CORRUPT_STORE", caught.exception.code)

    def test_event_payload_digest_tampering_is_reported_as_corruption(self) -> None:
        receipt = self.ingest()
        stored = self.store.get_event(NS, receipt.event_id)
        self.store.connection.execute(
            "UPDATE private_payloads SET sha256 = ? WHERE payload_ref = ?",
            ("0" * 64, stored.payload_ref),
        )
        with self.assertRaises(CorruptStore):
            self.store.get_event(NS, receipt.event_id)

    def test_missing_event_is_not_found(self) -> None:
        with self.assertRaises(EventNotFound) as caught:
            self.store.get_event(NS, ABSENT_EVENT_ID)
        self.assertEqual("NOT_FOUND", caught.exception.code)
        with self.assertRaises(EventNotFound):
            self.store.causes_of(NS, ABSENT_EVENT_ID)
        with self.assertRaises(EventNotFound):
            self.store.pending_work_for(NS, ABSENT_EVENT_ID)

    def test_events_and_record_versions_share_the_payload_area(self) -> None:
        """The same canonical body is one payload row for both tables."""

        event = self.ingest(payload={"text": "shared text"})
        self.store.append_record_version(
            namespace=NS,
            record_id="22222222-2222-4222-8222-222222222222",
            record_version=1,
            kind="Observation",
            body={"text": "shared text"},
            recorded_at=RECORDED_AT,
        )
        self.assertEqual(1, self.count_rows("private_payloads"))
        self.assertEqual({"text": "shared text"}, self.store.get_event(NS, event.event_id).payload)
        self.assertEqual(
            {"text": "shared text"},
            self.store.get_version(
                NS, "22222222-2222-4222-8222-222222222222", 1
            ).body,
        )


class SourceFieldValidationTests(StoreTestCase):
    store_class = EventStore

    def test_event_constants_match_the_frozen_contract(self) -> None:
        common = json.loads(
            (REPO_ROOT / "contracts" / "v2" / "_common.json").read_text(encoding="utf-8")
        )
        defs = common["$defs"]
        self.assertEqual(defs["source_kind"]["enum"], list(identifiers.SOURCE_KINDS))
        self.assertEqual(defs["instance_id"]["maxLength"], identifiers.INSTANCE_MAX_LENGTH)
        self.assertEqual(
            defs["source_event_id"]["maxLength"], identifiers.SOURCE_EVENT_ID_MAX_LENGTH
        )
        self.assertEqual(defs["event_type"]["maxLength"], identifiers.EVENT_TYPE_MAX_LENGTH)
        self.assertEqual(
            defs["source_position"]["maximum"], identifiers.MAX_SAFE_INTEGER
        )
        self.assertEqual(defs["privacy_class"]["enum"], list(identifiers.PRIVACY_CLASSES))
        self.assertEqual(defs["uuid"]["pattern"], identifiers.UUID_PATTERN.pattern)
        event_schema = json.loads(
            (REPO_ROOT / "contracts" / "v2" / "event.json").read_text(encoding="utf-8")
        )
        # Every field the journal stores is required by the frozen envelope and
        # vice versa, apart from protocol_version (added by the wire layer) and
        # event_seq (generated by the store, deliberately not in the envelope).
        journal_fields = {
            "namespace",
            "source_kind",
            "source_instance",
            "source_event_id",
            "source_position",
            "event_type",
            "observed_at",
            "recorded_at",
            "payload_ref",
            "privacy_class",
            "event_id",
            "causation_refs",
        }
        self.assertEqual(
            journal_fields, set(event_schema["required"]) - {"protocol_version"}
        )

    def test_ingest_outcome_enum_is_closed(self) -> None:
        self.assertEqual(("created", "duplicate"), OUTCOMES)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
