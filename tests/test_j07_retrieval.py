"""J07 deterministic boundaries, access isolation and real HTTP-port budget gates."""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import types
import unittest
import uuid
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tests.domain.cognition_support import CognitionTestCase, NS
from quintessence_pcs.runtime.context import ContextService
from quintessence_pcs.runtime.belief_workset import ordered_beliefs
from quintessence_pcs.domains.cognition.belief_page import CursorError, read_belief_page
from quintessence_pcs.domains.cognition.query import CognitionQueryService, QueryAccess
from quintessence_pcs.foundation.errors import InvalidInput
from quintessence_pcs.store.errors import ContextTooLarge


def seed(fixture, count, *, claim_chars=120, status="proposed", privacy="private", offset=0, scope=None):
    """Synthetic storage fixtures, never presented as actual model commits."""
    rows = []
    with fixture.store.transaction():
        for index in range(offset, count + offset):
            rows.append(fixture.store.append_record_version(
                namespace=NS, record_id=str(uuid.UUID(int=10000 + index)), record_version=1,
                kind="belief", body=fixture.belief_body(
                    claim=f"部署窗口合成记录 {index}: " + "测" * claim_chars,
                    claim_type="general_hypothesis", strength="limited"),
                status=status, schema_version="1", privacy_class=privacy, scope=scope or {},
            ))
    return rows


class WorksetTests(CognitionTestCase):
    def render(self, *, cap=24576, event=None, **kwargs):
        svc = ContextService(self.store, pcs_context_max_bytes=cap,
                             request_budget_units=200000, **kwargs)
        if event is None:
            event = self.ingest()
        env = svc.build_context(namespace=NS, trigger_ref=self.event_ref(event.event_id))
        return env, json.loads(env.rendered_input)

    def test_default_and_four_caps(self):
        seed(self, 100)
        counts = []
        for cap in (24576, 32768, 49152, 65536, 81920):
            env, doc = self.render(cap=cap)
            self.assertEqual(cap, env.budget_view["limit_bytes"])
            self.assertLessEqual(len(env.rendered_input.encode()), cap)
            counts.append(len(doc["beliefs"]))
            self.assert_receipt(doc)
        self.assertEqual(sorted(counts), counts)
        self.assertGreater(counts[-1], counts[0])

    def assert_receipt(self, doc):
        c = doc["belief_coverage"]
        self.assertEqual(c["observable_total"], c["rendered"] + c["omitted_budget"] + c["omitted_size"])
        self.assertEqual(c["rendered"], len(doc["beliefs"]))
        self.assertEqual(c["more_available"], c["rendered"] < c["observable_total"])
        self.assertNotIn("unavailable", c)
        self.assertLessEqual(len(json.dumps({"belief_coverage": c}).encode()), 256)

    def test_required_overflow_and_all_cut_are_distinct(self):
        with self.assertRaises(ContextTooLarge):
            self.render(cap=1)
        seed(self, 4, claim_chars=8100)
        env, doc = self.render()
        self.assertEqual(0, len(doc["beliefs"]))
        self.assert_receipt(doc)
        self.assertTrue(doc["belief_coverage"]["more_available"])

    def test_zero_remaining_is_not_an_error(self):
        event = self.ingest()
        env, _ = self.render(event=event)
        seed(self, 2)
        # A small reserve may exhaust Belief space while the scaffold fits.
        _, doc = self.render(cap=len(env.rendered_input.encode()) + 50, event=event)
        self.assertEqual([], doc["beliefs"])
        self.assert_receipt(doc)

    def test_oversized_first_is_skipped_and_later_small_record_survives(self):
        seed(self, 1, claim_chars=8100, offset=2)
        small = seed(self, 1, claim_chars=5)[0]
        _, doc = self.render()
        self.assertIn(small.record_id, [item["ref"]["record_id"] for item in doc["beliefs"]])
        self.assertGreater(doc["belief_coverage"]["omitted_size"], 0)

    def test_read_set_and_self_view_match_selected_records(self):
        seed(self, 80)
        env, doc = self.render()
        ids = {item["ref"]["record_id"] for item in doc["beliefs"]}
        self.assertEqual(ids, {ref.record_id for ref in env.belief_refs})
        self.assertTrue(ids.issubset({entry.ref.record_id for entry in env.read_set}))
        self.assertTrue({ref["record_id"] for ref in doc["self_view"].get("belief_refs", [])}.issubset(ids))
        self.assertFalse(any(item.get("reason") == "budget" and item["ref"].get("record_id") in
                             {str(uuid.UUID(int=10000+i)) for i in range(80)} for item in env.omissions))

    def test_unauthorized_presence_does_not_change_receipt(self):
        seed(self, 50, privacy="public-example")
        event = self.ingest()
        _, before = self.render(event=event, pcs_belief_privacy_classes=frozenset({"public-example"}))
        seed(self, 40, offset=100, privacy="private", claim_chars=400)
        _, after = self.render(event=event, pcs_belief_privacy_classes=frozenset({"public-example"}))
        self.assertEqual(json.dumps(before["belief_coverage"], sort_keys=True),
                         json.dumps(after["belief_coverage"], sort_keys=True))
        self.assertEqual(50, after["belief_coverage"]["observable_total"])

    def test_configuration_rejects_disabled_receipt_and_invalid_caps(self):
        for cap in (True, 0, -1, 1.5):
            with self.assertRaises(InvalidInput):
                ContextService(self.store, pcs_context_max_bytes=cap)
        with self.assertRaises(InvalidInput):
            ContextService(self.store, pcs_belief_coverage=False)

    def test_same_capture_is_byte_identical_and_filter_runs_once(self):
        seed(self, 70)
        event = self.ingest()
        with mock.patch("quintessence_pcs.runtime.context.uuid", types.SimpleNamespace(uuid4=lambda: uuid.UUID(int=999))):
            first, _ = self.render(event=event)
            second, _ = self.render(event=event)
        self.assertEqual(first.rendered_input, second.rendered_input)
        self.assertEqual(first.content_hash, second.content_hash)

    def test_count_cap_p1_full_and_p5_zero_log(self):
        rows = seed(self, 220, claim_chars=1)
        event = self.ingest(payload={"refs": [self.record_ref(row) for row in rows[:200]]})
        with self.assertLogs("quintessence_pcs.runtime.context", "INFO") as captured:
            _, doc = self.render(cap=1024*1024, event=event)
        self.assertEqual(200, len(doc["beliefs"]))
        self.assertEqual({row.record_id for row in rows[:200]}, {x["ref"]["record_id"] for x in doc["beliefs"]})
        self.assertIn("P5 rendered=0", " ".join(captured.output))

    def test_real_experiment_fixture_preflight(self):
        seed(self, 10, claim_chars=1800)
        seed(self, 1, claim_chars=1800, offset=10, status="contested")
        seed(self, 1, claim_chars=1800, offset=11, status="supported")
        env, doc = self.render()
        self.assertEqual(12, doc["belief_coverage"]["observable_total"])
        self.assertGreater(len(doc["beliefs"]), 0)
        self.assertLess(len(doc["beliefs"]), 12)
        self.assertLessEqual(len(env.rendered_input.encode()), 24576)

    def test_final_serialization_recheck_updates_receipt(self):
        seed(self, 80)
        original = ContextService._render_document
        def extra(service, **kwargs):
            doc = original(service, **kwargs)
            if 0 < len(kwargs["materials"].beliefs) < 80:
                doc["synthetic_serialization_overhead"] = "x" * 4000
            return doc
        with mock.patch.object(ContextService, "_render_document", extra):
            env, doc = self.render()
        self.assertLessEqual(len(env.rendered_input.encode()), 24576)
        self.assertGreater(doc["belief_coverage"]["omitted_budget"], 0)
        self.assert_receipt(doc)

    def test_capture_filter_is_not_repeated_to_make_receipt(self):
        seed(self, 30)
        event = self.ingest()
        service = ContextService(self.store)
        with mock.patch.object(service, "_read_materials_locked", wraps=service._read_materials_locked) as capture:
            env = service.build_context(namespace=NS, trigger_ref=self.event_ref(event.event_id))
        self.assertEqual(1, capture.call_count)
        self.assert_receipt(json.loads(env.rendered_input))

    def test_oversized_p5_conflict_does_not_starve_later_p5(self):
        oversized = seed(self, 1, claim_chars=8100, status="contested")[0]
        later = seed(self, 69, claim_chars=120, offset=1)
        _, doc = self.render(cap=65536)
        ids = {item["ref"]["record_id"] for item in doc["beliefs"]}
        self.assertNotIn(oversized.record_id, ids)
        self.assertIn(later[0].record_id, ids)
        self.assertGreater(doc["belief_coverage"]["omitted_size"], 0)

    def test_unavailable_belief_audit_cannot_consume_visible_budget(self):
        seed(self, 50)
        unavailable = seed(self, 1, offset=100, status="retracted")[0]
        event = self.ingest()
        service = ContextService(self.store)
        capture = service._read_materials_locked
        audit = {"ref": self.record_ref(unavailable), "reason": "source_redacted"}
        def with_unavailable_audit(*args, **kwargs):
            material = capture(*args, **kwargs)
            return replace(material, omissions=material.omissions + (audit,))
        with mock.patch("quintessence_pcs.runtime.context.uuid", types.SimpleNamespace(uuid4=lambda: uuid.UUID(int=999))):
            before = service.build_context(namespace=NS, trigger_ref=self.event_ref(event.event_id))
            with mock.patch.object(service, "_read_materials_locked", side_effect=with_unavailable_audit):
                after = service.build_context(namespace=NS, trigger_ref=self.event_ref(event.event_id))
        self.assertEqual(before.rendered_input, after.rendered_input)
        self.assertEqual(before.budget_view["used_bytes"], after.budget_view["used_bytes"])
        self.assertIn(audit, after.omissions)
        self.assertNotIn(unavailable.record_id, after.rendered_input)


class PriorityTests(unittest.TestCase):
    def test_proposal_metadata_is_stable_across_process_hash_seeds(self):
        root = Path(__file__).resolve().parents[1]
        code = (f"import sys; sys.path.insert(0, {str(root / 'src')!r}); "
                "from quintessence_pcs.runtime.context import PROPOSAL_CONTRACT; "
                "from quintessence_pcs.foundation.jsonutil import canonical_json_text; "
                "print(canonical_json_text(PROPOSAL_CONTRACT))")
        outputs = []
        for seed_value in ("1", "2", "3"):
            env = os.environ.copy()
            env["PYTHONHASHSEED"] = seed_value
            outputs.append(subprocess.check_output(
                [sys.executable, "-B", "-c", code], cwd=root, env=env))
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(outputs[0], outputs[2])

    def pairs(self, count):
        return [(types.SimpleNamespace(record_id=f"{i:04d}", recorded_at="2026-01-01T00:00:00Z",
                                       scope={}, status="supported"), {}) for i in range(count)]

    def test_high_priority_extremes_and_adaptive_n(self):
        for high, p4, p5 in ((0, 50, 170), (100, 50, 70), (180, 0, 40), (190, 0, 30), (200, 0, 20), (210, 0, 10)):
            pairs = self.pairs(220)
            order = ordered_beliefs(pairs, direct_ids={v.record_id for v, _ in pairs[:high]}, scope={})
            self.assertEqual(p4, sum(level == 4 for level, _ in order))
            self.assertEqual(p5, sum(level == 5 for level, _ in order))
            selected = order[:200]
            self.assertEqual(min(high, 200), sum(level == 1 for level, _ in selected))
            self.assertLessEqual(len(selected), 200)

    def test_scope_priority_and_conflict_priority_are_local(self):
        pairs = self.pairs(80)
        pairs[0][0].scope = {"scene": "x", "entity_ids": ["a"]}
        pairs[1][0].scope = {"scene": "x"}
        pairs[2][0].status = "contested"
        order = ordered_beliefs(pairs, direct_ids={"0003"}, scope={"scene": "x", "entity_ids": ["a"]})
        self.assertEqual(["0003", "0000", "0001"], [p[0].record_id for _, p in order[:3]])
        p5 = [pair for level, pair in order if level == 5]
        self.assertEqual("0002", p5[0][0].record_id)
        self.assertEqual(order, ordered_beliefs(list(reversed(pairs)), direct_ids={"0003"}, scope={"scene": "x", "entity_ids": ["a"]}))


class PaginationTests(CognitionTestCase):
    def page(self, **kwargs):
        return read_belief_page(CognitionQueryService(self.store),
            QueryAccess(NS, frozenset({"private"}), frozenset({"consult"})), **kwargs)

    def test_traversal_total_and_terminal(self):
        rows = seed(self, 23)
        seen, cursor = [], None
        while True:
            page = self.page(limit=5, cursor=cursor)
            self.assertEqual(23, page["total"])
            seen.extend(item["ref"]["record_id"] for item in page["items"])
            cursor = page["next_cursor"]
            self.assertEqual(cursor is not None, page["truncated"])
            if cursor is None:
                break
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual({row.record_id for row in rows}, set(seen))

    def test_cursor_errors_and_filter_binding(self):
        seed(self, 3)
        cursor = self.page(limit=1)["next_cursor"]
        for bad in ("!", "", "e30", "WzFd"):
            with self.assertRaises(CursorError) as caught:
                self.page(cursor=bad)
            self.assertEqual("cursor_invalid", str(caught.exception))
        decoded = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        decoded["v"] = 2
        newer = base64.urlsafe_b64encode(json.dumps(decoded).encode()).decode().rstrip("=")
        with self.assertRaisesRegex(CursorError, "cursor_version_mismatch"):
            self.page(cursor=newer)
        with self.assertRaisesRegex(CursorError, "cursor_filters_mismatch"):
            self.page(cursor=cursor, status="supported")

    def test_deleted_anchor_and_middle_continue_by_key(self):
        seed(self, 9)
        first = self.page(limit=3)
        anchor = first["items"][-1]["ref"]["record_id"]
        self.delete_record(anchor)
        second = self.page(limit=2, cursor=first["next_cursor"])
        self.assertTrue(second["truncated"])
        self.assertEqual(8, second["total"])
        seen = {x["ref"]["record_id"] for x in first["items"]}
        self.assertFalse(seen & {x["ref"]["record_id"] for x in second["items"]})

    def test_every_advertised_state_keeps_identity_and_scope(self):
        for i, status in enumerate(("proposed", "supported", "contested", "contradicted", "stale")):
            seed(self, 1, offset=i, status=status)
            page = self.page(status=status)
            self.assertEqual(1, page["total"])
            self.assertEqual(status, page["items"][0]["status"])
        seed(self, 2, offset=10, privacy="public-example")
        self.assertEqual(5, self.page()["total"])

    def test_filtering_does_not_make_a_false_terminal_short_page(self):
        seed(self, 7)
        seed(self, 18, offset=100, privacy="restricted")
        page = self.page(limit=5)
        self.assertEqual(7, page["total"])
        self.assertEqual(5, len(page["items"]))
        self.assertTrue(page["truncated"])
        tail = self.page(limit=5, cursor=page["next_cursor"])
        self.assertEqual(2, len(tail["items"]))
        self.assertFalse(tail["truncated"])

    def test_middle_deletion_has_no_duplicates_or_missing_survivors(self):
        rows = seed(self, 12)
        first = self.page(limit=3)
        deleted = rows[3].record_id
        self.delete_record(deleted)
        seen = [x["ref"]["record_id"] for x in first["items"]]
        cursor = first["next_cursor"]
        while cursor:
            page = self.page(limit=3, cursor=cursor)
            seen.extend(x["ref"]["record_id"] for x in page["items"])
            cursor = page["next_cursor"]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual({r.record_id for r in rows} - {deleted}, set(seen))


if __name__ == "__main__":
    unittest.main()
