"""RG08 R-B: automatic recall packet validation and PCS source binding."""

from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from quintessence_pcs.domains.memory.scope_recall_recall import (
    MemoryRecallMaterial,
    MemoryRecallPacket,
    RecallTimeFilter,
    bind_recall_sources,
    recall_scope_recall,
    recall_time_window,
)
from quintessence_pcs.store.experience import ExperienceAccess

NS = "example.test.rg08.rb"
HOST = "host:rg08-rb"
ACCESS_REF = "authorization:rg08-rb"


def item(ref: str, revision: int = 1, content: str = "我喜欢红色。") -> dict:
    return {
        "ref": ref,
        "revision": revision,
        "kind": "event",
        "content": content,
        "content_truncated": False,
        "temporal_status": "current",
        "origin": "user",
        "applicability": "trusted scope",
        "evidence_refs": (),
        "expandable": False,
        "basis": "direct_report",
        "occurred_at": "2026-10-20T10:00:00+08:00",
    }


def page(
    *items: dict,
    status: str = "ok",
    gaps=(),
    coverage="unknown",
    scan_complete: bool = True,
    scan_pages: int = 0,
) -> MemoryRecallPacket:
    return MemoryRecallPacket(
        status=status,
        items=tuple(items),
        gaps=tuple(gaps),
        coverage=coverage,
        answerability="supported",
        memory_epoch=7,
        elapsed_ms=12,
        scan_complete=scan_complete,
        scan_pages=scan_pages,
    )


def access() -> ExperienceAccess:
    return ExperienceAccess(
        namespace=NS,
        host_instance=HOST,
        access_ref=ACCESS_REF,
        privacy_classes=frozenset({"private"}),
        purposes=frozenset({"consult"}),
    )


class FakeStore:
    def __init__(self, mapping, *, readable: bool = True):
        self.mapping = mapping
        self.readable = readable

    def experience_recall_sources(self, namespace, refs):
        return {
            ref: self.mapping[ref]
            for ref in refs
            if ref in self.mapping
        }

    def read_experience(self, access, *, ref, purpose):
        if not self.readable:
            return SimpleNamespace(readability="unavailable", body=None)
        return SimpleNamespace(readability="available", body="synthetic")


def row(
    *,
    event_id: str,
    ref_id: str = "11111111-1111-4111-8111-111111111111",
    ref_version: int = 1,
    revision: int = 1,
    state: str = "applied",
    origin: str = "user_statement",
    ref_type: str = "collab_input",
    observed_at: str | None = None,
    created_at: str | None = None,
    pcs_time: dict | None = None,
) -> dict:
    return {
        "ref_type": ref_type,
        "ref_id": ref_id,
        "ref_version": ref_version,
        "origin_kind": origin,
        "state": state,
        "event_id": event_id,
        "external_revision": revision,
        "observed_at": observed_at,
        "created_at": created_at,
        "request_json": json.dumps({"pcs_time": pcs_time}, ensure_ascii=False)
        if pcs_time is not None
        else "{}",
    }


def time_filter(
    *,
    intent: str,
    windows: tuple[tuple[str, str], ...],
    unsupported: bool = False,
) -> RecallTimeFilter:
    return RecallTimeFilter(
        windows=windows,
        intent=intent,
        anchor="2026-10-30T10:00:00+08:00",
        anchor_source="observed_at",
        unsupported=unsupported,
    )


def occurred_pcs(day: str = "2026-10-29T00:00:00+08:00") -> dict:
    return {
        "event_type": "occurred",
        "event_time": day,
        "planned_for": None,
        "time_precision": "day",
        "time_basis": "relative_day",
        "message_time_basis": "observed_at",
    }


def planned_pcs() -> dict:
    return {
        "event_type": "planned",
        "event_time": None,
        "planned_for": "2026-10-29T00:00:00+08:00",
        "time_precision": "day",
        "time_basis": "relative_day",
        "message_time_basis": "observed_at",
    }


class BindRecallSourcesTests(unittest.TestCase):
    def bind(
        self,
        packet,
        mapping,
        *,
        tool_results=(),
        history_refs=(),
        readable=True,
        time_filter=None,
    ):
        return bind_recall_sources(
            FakeStore(mapping, readable=readable),
            namespace=NS,
            access=access(),
            page=packet,
            scope_id="scope:rg08-rb",
            current_input_ref={
                "ref_type": "collab_input",
                "input_id": "22222222-2222-4222-8222-222222222222",
                "content_version": 1,
            },
            history_refs=history_refs,
            tool_results=tool_results,
            time_filter=time_filter,
        )

    def test_event_maps_to_exact_source_and_subject(self):
        material = self.bind(
            page(item("event-a", 3)),
            {"event-a": (row(event_id="33333333-3333-4333-8333-333333333333", revision=3),)},
        )
        self.assertEqual("ok", material.status)
        self.assertEqual(1, len(material.items))
        hit = material.items[0]
        self.assertEqual("user", hit.subject)
        self.assertEqual("collab_input:11111111-1111-4111-8111-111111111111@1", hit.subject_ref)
        self.assertEqual("33333333-3333-4333-8333-333333333333", hit.source_event_id)
        self.assertEqual(
            "collab_input", hit.source_ref["ref_type"]
        )
        self.assertEqual("scope:rg08-rb", hit.scope_id)
        self.assertEqual(3, hit.upstream_revision)
        self.assertGreater(hit.units, 0)

    def test_assistant_subject_is_not_user(self):
        material = self.bind(
            page(item("event-a")),
            {
                "event-a": (
                    row(
                        event_id="33333333-3333-4333-8333-333333333333",
                        ref_type="collab_artifact",
                        ref_id="44444444-4444-4444-8444-444444444444",
                        origin="model_output",
                    ),
                )
            },
        )
        self.assertEqual("assistant", material.items[0].subject)

    def test_unknown_origin_stays_empty(self):
        material = self.bind(
            page(item("event-a")),
            {
                "event-a": (
                    row(
                        event_id="33333333-3333-4333-8333-333333333333",
                        origin="mystery_origin",
                    ),
                )
            },
        )
        self.assertEqual("", material.items[0].subject)

    def test_claim_is_derived_not_delivered(self):
        claim = dict(item("claim-a"), kind="claim")
        material = self.bind(page(claim), {})
        self.assertEqual("incomplete", material.status)
        self.assertEqual(0, len(material.items))
        self.assertEqual("derived_item", material.dropped[0]["reason"])

    def test_unmapped_source_is_incomplete_not_no_match(self):
        material = self.bind(page(item("event-a")), {})
        self.assertEqual("incomplete", material.status)
        self.assertEqual("unmapped_source", material.dropped[0]["reason"])

    def test_stale_delivery_revision_is_dropped(self):
        material = self.bind(
            page(item("event-a", 2)),
            {"event-a": (row(event_id="33333333-3333-4333-8333-333333333333", revision=1),)},
        )
        self.assertEqual("incomplete", material.status)
        self.assertEqual("stale_revision", material.dropped[0]["reason"])

    def test_unreadable_source_is_dropped(self):
        material = self.bind(
            page(item("event-a")),
            {"event-a": (row(event_id="33333333-3333-4333-8333-333333333333"),)},
            readable=False,
        )
        self.assertEqual("incomplete", material.status)
        self.assertEqual("source_unavailable", material.dropped[0]["reason"])

    def test_history_and_tool_duplicates_are_dropped(self):
        mapping = {"event-a": (row(event_id="33333333-3333-4333-8333-333333333333"),)}
        material = self.bind(
            page(item("event-a")),
            mapping,
            history_refs=(
                SimpleNamespace(
                    ref_kind="collab_input",
                    ref_id="11111111-1111-4111-8111-111111111111",
                    ref_version=1,
                ),
            ),
        )
        self.assertEqual("incomplete", material.status)
        self.assertEqual("history_duplicate", material.dropped[0]["reason"])

    def test_budget_trims_whole_items_and_reports_partial(self):
        long = "喜欢" * 2000
        refs = [f"event-{index}" for index in range(4)]
        mapping = {
            ref: (
                row(
                    event_id=f"{index + 1:08d}-1111-4111-8111-111111111111",
                    ref_id=f"{index + 1:08d}-2222-4222-8222-222222222222",
                ),
            )
            for index, ref in enumerate(refs)
        }
        material = self.bind(
            page(*[item(ref, content=long) for ref in refs]),
            mapping,
        )
        self.assertLessEqual(material.units, 2048)
        self.assertLessEqual(len(material.items), 4)
        self.assertTrue(any(entry["reason"] == "budget" for entry in material.dropped))
        self.assertEqual("partial", material.status)

    def test_no_match_stays_no_match(self):
        material = self.bind(page(status="no_match"), {})
        self.assertEqual("no_match", material.status)

    def test_timeout_page_has_no_items(self):
        material = self.bind(page(status="timeout"), {})
        self.assertEqual("timeout", material.status)
        self.assertEqual((), material.items)

    def test_occurred_event_in_window_is_delivered_with_time_basis(self):
        material = self.bind(
            page(item("event-a")),
            {
                "event-a": (
                    row(
                        event_id="33333333-3333-4333-8333-333333333333",
                        observed_at="2026-10-30T10:00:00+08:00",
                        created_at="2026-10-30T10:00:00+08:00",
                        pcs_time=occurred_pcs(),
                    ),
                )
            },
            time_filter=time_filter(
                intent="event",
                windows=(("2026-10-28T16:00:00+00:00", "2026-10-29T16:00:00+00:00"),),
            ),
        )
        self.assertEqual("ok", material.status)
        self.assertEqual(1, len(material.items))
        delivered = material.items[0]
        self.assertEqual("occurred", delivered.event_type)
        self.assertEqual("2026-10-29T00:00:00+08:00", delivered.event_time)
        self.assertEqual("day", delivered.time_precision)
        self.assertEqual("relative_day", delivered.time_basis)
        self.assertEqual("observed_at", delivered.message_time_basis)
        self.assertEqual("2026-10-30T10:00:00+08:00", delivered.message_time)

    def test_planned_event_never_matches_an_event_window(self):
        material = self.bind(
            page(item("event-a", content="我明天去招聘会。")),
            {
                "event-a": (
                    row(
                        event_id="33333333-3333-4333-8333-333333333333",
                        observed_at="2026-10-28T10:00:00+08:00",
                        pcs_time=planned_pcs(),
                    ),
                )
            },
            time_filter=time_filter(
                intent="event",
                windows=(("2026-10-28T16:00:00+00:00", "2026-10-29T16:00:00+00:00"),),
            ),
        )
        self.assertEqual("incomplete", material.status)
        self.assertEqual((), material.items)
        self.assertEqual(1, material.time_not_occurred)
        self.assertEqual(0, material.time_unknown)

    def test_unknown_event_time_is_incomplete_not_no_match(self):
        material = self.bind(
            page(item("event-a")),
            {
                "event-a": (
                    row(
                        event_id="33333333-3333-4333-8333-333333333333",
                        observed_at="2026-10-30T10:00:00+08:00",
                        pcs_time=None,
                    ),
                )
            },
            time_filter=time_filter(
                intent="event",
                windows=(("2026-10-28T16:00:00+00:00", "2026-10-29T16:00:00+00:00"),),
            ),
        )
        self.assertEqual("incomplete", material.status)
        self.assertEqual(1, material.time_unknown)

    def test_occurred_outside_window_is_a_clean_no_match(self):
        material = self.bind(
            page(item("event-a")),
            {
                "event-a": (
                    row(
                        event_id="33333333-3333-4333-8333-333333333333",
                        observed_at="2026-10-30T10:00:00+08:00",
                        pcs_time=occurred_pcs("2026-10-20T00:00:00+08:00"),
                    ),
                )
            },
            time_filter=time_filter(
                intent="event",
                windows=(("2026-10-28T16:00:00+00:00", "2026-10-29T16:00:00+00:00"),),
            ),
        )
        self.assertEqual("no_match", material.status)
        self.assertEqual(1, material.time_dropped)

    def test_message_intent_uses_observed_at_with_fallback_label(self):
        material = self.bind(
            page(item("event-a")),
            {
                "event-a": (
                    row(
                        event_id="33333333-3333-4333-8333-333333333333",
                        observed_at=None,
                        created_at="2026-10-30T10:00:00+08:00",
                    ),
                )
            },
            time_filter=time_filter(
                intent="message",
                windows=(("2026-10-29T16:00:00+00:00", "2026-10-30T16:00:00+00:00"),),
            ),
        )
        self.assertEqual("ok", material.status)
        self.assertEqual("recorded_at_fallback", material.items[0].message_time_basis)
        self.assertEqual("unknown", material.items[0].event_type)

    def test_incomplete_scan_is_not_no_match(self):
        material = self.bind(
            page(status="ok", scan_complete=False, scan_pages=3),
            {},
            time_filter=time_filter(
                intent="event",
                windows=(("2026-10-28T16:00:00+00:00", "2026-10-29T16:00:00+00:00"),),
            ),
        )
        self.assertEqual("incomplete", material.status)
        self.assertEqual(3, material.scan_pages)


class WindowRecallTests(unittest.TestCase):
    class Adapter:
        def __init__(self, outcome):
            self.outcome = outcome
            self.calls: list[dict] = []

        def time_window(self, **kwargs):
            self.calls.append(kwargs)
            return self.outcome

    def test_window_packet_is_bounded_and_carries_scan_state(self):
        adapter = self.Adapter(
            {
                "kind": "ok",
                "result": {
                    "items": [
                        {
                            "ref": "event-a",
                            "revision": 1,
                            "kind": "event",
                            "content": "我去了招聘会。",
                            "occurred_at": "2026-10-28T10:00:00+08:00",
                        }
                    ],
                    "examined": 3,
                    "scan_complete": False,
                    "stop_reason": "limit",
                    "pages": 1,
                },
            }
        )
        packet = recall_time_window(
            adapter,
            since="2026-10-28T00:00:00+08:00",
            until=None,
            timeout_seconds=1.0,
        )
        self.assertEqual("ok", packet.status)
        self.assertEqual(1, len(packet.items))
        self.assertFalse(packet.scan_complete)
        self.assertEqual(1, packet.scan_pages)
        self.assertEqual(3, packet.examined)
        self.assertIsNone(adapter.calls[0]["until"])

    def test_window_adapter_timeout_is_timeout(self):
        adapter = self.Adapter({"kind": "unknown", "error": "timeout"})
        packet = recall_time_window(
            adapter,
            since="2026-10-28T00:00:00+08:00",
            until=None,
            timeout_seconds=0.4,
        )
        self.assertEqual("timeout", packet.status)


class RecallScopeRecallTests(unittest.TestCase):
    class Adapter:
        def __init__(self, outcome):
            self.outcome = outcome

        def recall(self, **kwargs):
            return self.outcome

    def test_adapter_packet_is_validated(self):
        adapter = self.Adapter(
            {
                "kind": "ok",
                "result": {
                    "status": "ok",
                    "items": [item("event-a")],
                    "gaps": [],
                    "coverage": "unknown",
                    "answerability": "supported",
                    "memory_epoch": 4,
                    "elapsed_ms": 1,
                },
            }
        )
        packet = recall_scope_recall(
            adapter, query="我喜欢什么颜色", scope_id="scope:rg08-rb"
        )
        self.assertEqual("ok", packet.status)
        self.assertEqual(1, len(packet.items))

    def test_over_wide_items_are_invalid_not_truncated(self):
        adapter = self.Adapter(
            {
                "kind": "ok",
                "result": {
                    "status": "ok",
                    "items": [item(f"event-{index}") for index in range(5)],
                    "gaps": [],
                    "coverage": "unknown",
                    "answerability": "unknown",
                    "memory_epoch": 1,
                },
            }
        )
        packet = recall_scope_recall(
            adapter, query="q", scope_id="scope:rg08-rb", max_items=4
        )
        self.assertEqual("invalid", packet.status)
        self.assertEqual((), packet.items)

    def test_timeout_outcome_is_timeout(self):
        adapter = self.Adapter({"kind": "unknown", "error": "timeout"})
        packet = recall_scope_recall(adapter, query="q", scope_id="scope:rg08-rb")
        self.assertEqual("timeout", packet.status)

    def test_unavailable_outcome_is_unavailable(self):
        adapter = self.Adapter({"kind": "blocked", "error": "adapter_unavailable"})
        packet = recall_scope_recall(adapter, query="q", scope_id="scope:rg08-rb")
        self.assertEqual("unavailable", packet.status)

    def test_vector_only_gap_does_not_turn_no_match_into_incomplete(self):
        adapter = self.Adapter(
            {
                "kind": "ok",
                "result": {
                    "status": "no_match",
                    "items": [],
                    "gaps": ["vector_unavailable"],
                    "coverage": "partial",
                    "answerability": "unknown",
                    "memory_epoch": None,
                },
            }
        )
        packet = recall_scope_recall(adapter, query="q", scope_id="scope:rg08-rb")
        self.assertEqual("no_match", packet.status)

    def test_deadline_gap_turns_no_match_into_incomplete(self):
        adapter = self.Adapter(
            {
                "kind": "ok",
                "result": {
                    "status": "no_match",
                    "items": [],
                    "gaps": ["deadline_exceeded_collect"],
                    "coverage": "partial",
                    "answerability": "unknown",
                    "memory_epoch": None,
                },
            }
        )
        packet = recall_scope_recall(adapter, query="q", scope_id="scope:rg08-rb")
        self.assertEqual("incomplete", packet.status)


if __name__ == "__main__":
    unittest.main()
