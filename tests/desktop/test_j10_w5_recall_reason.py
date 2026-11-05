"""J10 W5: a failed memory recall keeps a bounded, content-free reason.

The RG09 acceptance turn recorded ``{"status": "unavailable", "reasons": []}``:
the failure was real but undiagnosable, because the adapter's own error token
was dropped while the value was being built.  These tests pin the three things
that fix has to keep: the token survives, success carries no reason, and
nothing but a short code can reach the durable event.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from quintessence_pcs.domains.memory.scope_recall_recall import (
    MemoryRecallMaterial,
    MemoryRecallPacket,
    _bounded_reason,
    bind_recall_sources,
    recall_scope_recall,
)
from quintessence_pcs.domains.memory.scope_recall_search import (
    MemorySearchUnavailable,
    search_scope_recall,
)
from runtime import memory_recall_event_payload

NS = "example.test.j10.w5"
SCOPE = "scope:j10-w5"


def _access() -> SimpleNamespace:
    return SimpleNamespace(
        namespace=NS,
        access_ref="authorization:j10-w5",
        retention="standard",
        can_read=lambda _namespace: True,
    )


def _page(status: str = "unavailable", reason: str = "") -> MemoryRecallPacket:
    return MemoryRecallPacket(
        status=status,
        items=(),
        gaps=(),
        coverage="unknown",
        answerability="unknown",
        memory_epoch=None,
        elapsed_ms=1,
        reason=reason,
    )


class _FailingAdapter:
    def search(self, **_kwargs):
        raise TimeoutError("upstream slow")

    def recall(self, **_kwargs):
        raise TimeoutError("upstream slow")


class RecallReasonPropagationTests(unittest.TestCase):
    def test_the_packet_reason_reaches_the_material(self) -> None:
        material = bind_recall_sources(
            None,  # the early failure path never touches the store
            namespace=NS,
            access=_access(),
            page=_page("unavailable", "adapter_unavailable"),
            scope_id=SCOPE,
        )
        self.assertEqual("unavailable", material.status)
        self.assertEqual("adapter_unavailable", material.reason)

    def test_a_successful_attempt_carries_no_reason(self) -> None:
        material = bind_recall_sources(
            None,
            namespace=NS,
            access=_access(),
            page=_page("no_match", ""),
            scope_id=SCOPE,
        )
        self.assertEqual("", material.reason)

    def test_an_adapter_exception_becomes_a_class_name_token(self) -> None:
        packet = recall_scope_recall(
            _FailingAdapter(),
            query="最近怎么样",
            scope_id=SCOPE,
        )
        self.assertEqual("unavailable", packet.status)
        self.assertEqual("TimeoutError", packet.reason)


class RecallEventPayloadTests(unittest.TestCase):
    def test_a_failed_attempt_records_the_reason_code(self) -> None:
        material = MemoryRecallMaterial(
            status="unavailable",
            items=(),
            reason="adapter_unavailable",
        )
        payload = memory_recall_event_payload(material)
        self.assertEqual("unavailable", payload["status"])
        self.assertEqual("adapter_unavailable", payload["reason_code"])

    def test_a_successful_attempt_records_no_reason_code(self) -> None:
        material = MemoryRecallMaterial(status="ok", items=(), reason="")
        payload = memory_recall_event_payload(material)
        self.assertNotIn("reason_code", payload)

    def test_a_no_match_is_not_a_failure(self) -> None:
        material = MemoryRecallMaterial(status="no_match", items=(), reason="x")
        self.assertNotIn("reason_code", memory_recall_event_payload(material))

    def test_the_payload_never_carries_content(self) -> None:
        material = MemoryRecallMaterial(
            status="unavailable",
            items=(),
            reason="adapter_unavailable",
        )
        payload = memory_recall_event_payload(material)
        text = repr(payload)
        self.assertNotIn("query", text)
        self.assertNotIn("secret", text)


class BoundedReasonTests(unittest.TestCase):
    def test_prose_cannot_ride_into_the_event(self) -> None:
        # A provider sentence collapses to its keyword; the words are dropped.
        self.assertEqual(
            "timeout",
            _bounded_reason(
                "timeout: upstream returned 429 -- do not log the user question"
            ),
        )

    def test_unrecognised_prose_becomes_other(self) -> None:
        self.assertEqual("other", _bounded_reason("the user asked about 蓝鲸-17"))

    def test_a_machine_token_is_kept(self) -> None:
        self.assertEqual("SOURCE_MISSING", _bounded_reason("SOURCE_MISSING"))

    def test_only_token_characters_survive(self) -> None:
        self.assertEqual("adapter_unavailable", _bounded_reason("adapter_unavailable"))
        self.assertEqual("", _bounded_reason("   "))
        self.assertEqual("", _bounded_reason(None))

    def test_a_long_reason_is_truncated(self) -> None:
        self.assertLessEqual(len(_bounded_reason("a" * 200)), 64)


class SearchUnavailableReasonTests(unittest.TestCase):
    class _Adapter:
        def __init__(self, outcome):
            self._outcome = outcome

        def search(self, **_kwargs):
            return self._outcome

    def _reason(self, outcome) -> str:
        with self.assertRaises(MemorySearchUnavailable) as caught:
            search_scope_recall(self._Adapter(outcome), query="q", scope_id=SCOPE)
        return caught.exception.reason

    def test_blocked_adapter_unavailable_keeps_its_token(self) -> None:
        self.assertEqual(
            "adapter_unavailable",
            self._reason({"kind": "blocked", "error": "adapter_unavailable"}),
        )

    def test_unavailable_keeps_the_adapter_error(self) -> None:
        self.assertEqual(
            "instance_not_registered",
            self._reason({"kind": "unavailable", "error": "instance_not_registered"}),
        )

    def test_unknown_timeout_stays_a_timeout(self) -> None:
        self.assertEqual(
            "timeout", self._reason({"kind": "unknown", "error": "timeout"})
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
