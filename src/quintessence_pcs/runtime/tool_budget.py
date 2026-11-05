"""Per-result and per-round budgets for tool results inside the agent loop.

The bounded Context work (J07) capped what the *initial* render may carry, but a
tool result is a second, unbounded way to put bytes into the next request: one
``cognition.belief.read`` with twenty large records returned about 70 KiB in the
J07 real run, and the following request was refused for budget.  This module is
the delivery-side counterpart: a result is shaped *before* it enters the message
history, never after.

Two invariants drive the design:

* **Bound what is delivered, not what is remembered.**  The store keeps the
  full record; only the projection the model sees is limited.  Nothing here
  truncates a JSON string in the middle of a claim, a condition, a negation, a
  scope or a provenance wrapper -- a half record that still looks complete is
  worse than a ref the model can read next turn.
* **Identity is not body.**  A ref-only projection tells the model *that* a
  record exists; it does not hand over the content.  Callers must therefore
  register citable evidence only for results whose body was actually included,
  otherwise "I was told the id" would silently become "I checked the text".

The numbers are host policy, not model input: a model cannot raise its own
allowance by asking for a larger ``limit``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .budget import estimate_units, request_units
from ..foundation.jsonutil import canonical_json_text

#: Reason codes a projection can carry back to the model.
REASON_PAGE_BUDGET = "record_exceeds_page_budget"
REASON_ROUND_BUDGET = "round_budget_exhausted"
REASON_TOO_LARGE = "record_too_large"
REASON_NOT_EXECUTED = "tool_not_executed_budget"
REASON_REQUEST_BUDGET = "request_budget_exceeded"

#: Smallest refusal envelope this module can emit, used as the admission floor.
MIN_ENVELOPE_BYTES = 96
MIN_ENVELOPE_UNITS = 48

#: Tool whose outcome is a single record lookup, and therefore may use the
#: larger exact-read allowance when it is the only call in the round.
EXACT_READ_TOOL = "cognition.belief.read"

#: Room kept back for the model's follow-up request (continued reading, a
#: correction, or the wrap-up answer).  It is part of the request budget, not an
#: addition to it: ``request_units(body) + headroom <= request_budget_units``.
CONTINUATION_HEADROOM_UNITS = 4096


@dataclass(frozen=True)
class ToolBudgetPolicy:
    """Host-configured caps.  Values are bytes and PCS ``U`` -- never tokens."""

    page_max_bytes: int = 8192
    page_max_units: int = 4096
    round_max_bytes: int = 16384
    round_max_units: int = 8192
    exact_max_bytes: int = 32768
    exact_max_units: int = 16384
    single_exact_round_max_bytes: int = 32768
    single_exact_round_max_units: int = 16384
    continuation_headroom_units: int = CONTINUATION_HEADROOM_UNITS

    def __post_init__(self) -> None:
        for name in (
            "page_max_bytes",
            "page_max_units",
            "round_max_bytes",
            "round_max_units",
            "exact_max_bytes",
            "exact_max_units",
            "single_exact_round_max_bytes",
            "single_exact_round_max_units",
            "continuation_headroom_units",
        ):
            value = getattr(self, name)
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True)
class Projection:
    """One result as it will be written into the message history."""

    payload: Mapping[str, Any]
    delivered_bytes: int
    delivered_units: int
    body_included: bool
    truncated: bool
    reason: str | None = None

    def to_mapping(self) -> dict[str, Any]:
        return dict(self.payload)


def _serialize_units(text: str) -> tuple[int, int]:
    return len(text.encode("utf-8")), estimate_units(text)


def _ref_of(payload: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Find the record/event identity a ref-only projection can point at."""

    stack: list[Any] = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, Mapping):
            record_id = item.get("record_id")
            if isinstance(record_id, str) and record_id:
                ref: dict[str, Any] = {"record_id": record_id}
                version = item.get("record_version")
                if type(version) is int:
                    ref["record_version"] = version
                return ref
            event_id = item.get("event_id")
            if isinstance(event_id, str) and event_id:
                return {"event_id": event_id}
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return None


def _caps_of(
    *, page_bytes: int, page_units: int, round_bytes: int, round_units: int
) -> dict[str, int]:
    return {
        "page_bytes": page_bytes,
        "page_units": page_units,
        "round_bytes": round_bytes,
        "round_units": round_units,
    }


class ToolResultBudgeter:
    """Round-scoped accounting shared by one agent loop and its handlers."""

    def __init__(self, policy: ToolBudgetPolicy | None = None) -> None:
        self.policy = policy or ToolBudgetPolicy()
        self._round_bytes = 0
        self._round_units = 0
        self._round_bytes_cap = self.policy.round_max_bytes
        self._round_units_cap = self.policy.round_max_units
        self._round_calls = 0

    # -- round lifecycle ---------------------------------------------------

    def begin_round(self, calls: Sequence[str]) -> None:
        """Open a round, widening the caps for the single exact-read case."""

        self._round_bytes = 0
        self._round_units = 0
        self._round_calls = len(calls)
        exact_only = len(calls) == 1 and calls[0] == EXACT_READ_TOOL
        if exact_only:
            self._round_bytes_cap = self.policy.single_exact_round_max_bytes
            self._round_units_cap = self.policy.single_exact_round_max_units
        else:
            self._round_bytes_cap = self.policy.round_max_bytes
            self._round_units_cap = self.policy.round_max_units

    def can_execute(self) -> bool:
        """Whether a minimal result envelope still fits in this round.

        Checked *before* the call, so an unaffordable tool is never run.  The
        threshold is the smallest envelope this module can emit; a round with
        less room than that cannot report anything but a refusal.
        """

        return (
            self.remaining_bytes >= MIN_ENVELOPE_BYTES
            and self.remaining_units >= MIN_ENVELOPE_UNITS
        )

    @property
    def remaining_bytes(self) -> int:
        return max(0, self._round_bytes_cap - self._round_bytes)

    @property
    def remaining_units(self) -> int:
        return max(0, self._round_units_cap - self._round_units)

    @property
    def round_calls(self) -> int:
        return self._round_calls

    # -- delivery ----------------------------------------------------------

    def project(
        self,
        *,
        tool: str,
        payload: Mapping[str, Any],
        content: str | None = None,
    ) -> Projection:
        """Shape one result for delivery, counting it against the round.

        ``content`` is the exact wall text the tool produced (``ok``/``code``
        plus the payload).  Sizing the payload alone would under-count the
        envelope by a few dozen bytes per call, which is exactly the kind of
        error that turns a passing cap into an exceeded request.
        """

        body = dict(payload)
        full = content if content is not None else canonical_json_text(body)
        body_bytes, body_units = _serialize_units(full)
        page_bytes, page_units = self._page_caps(tool)
        fits_page = body_bytes <= page_bytes and body_units <= page_units
        fits_round = (
            body_bytes <= self.remaining_bytes and body_units <= self.remaining_units
        )
        if fits_page and fits_round:
            self._round_bytes += body_bytes
            self._round_units += body_units
            return Projection(
                payload=body,
                delivered_bytes=body_bytes,
                delivered_units=body_units,
                body_included=True,
                truncated=False,
            )

        if tool == EXACT_READ_TOOL and self._round_calls == 1:
            # A single exact read exists to hand over *that* record.  If the
            # record itself exceeds the exact allowance there is nothing honest
            # to deliver, so say so and let the caller choose a narrower read
            # instead of silently handing back a fragment or a descriptor that
            # looks like the answer.  A *page* of many records takes the branch
            # below instead: one oversized row must not cost the whole page.
            if (
                body_bytes > self.policy.exact_max_bytes
                or body_units > self.policy.exact_max_units
            ) and self._single_record(body):
                return self._reject(
                    REASON_TOO_LARGE, tool=tool, body_bytes=body_bytes
                )

        listed = self._project_items(body, page_bytes, page_units)
        if listed is not None:
            return listed

        ref = _ref_of(body)
        reason = REASON_PAGE_BUDGET if not fits_page else REASON_ROUND_BUDGET
        if ref is None:
            # Nothing stable to point at: refuse rather than emit a fragment.
            return self._reject(REASON_TOO_LARGE, tool=tool, body_bytes=body_bytes)
        descriptor = self._ref_only_descriptor(
            tool=tool,
            ref=ref,
            reason=reason,
            page_bytes=page_bytes,
            page_units=page_units,
            retry_as_single_exact_read=self._retry_as_single_exact(tool),
        )
        text = canonical_json_text(descriptor)
        desc_bytes, desc_units = _serialize_units(text)
        if desc_bytes > self.remaining_bytes or desc_units > self.remaining_units:
            return self._reject(REASON_ROUND_BUDGET, tool=tool, body_bytes=body_bytes)
        self._round_bytes += desc_bytes
        self._round_units += desc_units
        return Projection(
            payload=descriptor,
            delivered_bytes=desc_bytes,
            delivered_units=desc_units,
            body_included=False,
            truncated=True,
            reason=reason,
        )

    def describe_only(self, *, tool: str, payload: Mapping[str, Any]) -> Projection:
        """Re-shape an already produced result as an identity-only descriptor.

        Used when the *request* preview says the round, as assembled, will not
        be sendable: the projection shrinks to the smallest honest thing (the
        ref and the reason) instead of the harness silently dropping a result.
        """

        ref = _ref_of(payload)
        if ref is None:
            return self._reject(REASON_TOO_LARGE, tool=tool, body_bytes=0)
        page_bytes, page_units = self._page_caps(tool)
        descriptor = self._ref_only_descriptor(
            tool=tool,
            ref=ref,
            reason=REASON_REQUEST_BUDGET,
            page_bytes=page_bytes,
            page_units=page_units,
            retry_as_single_exact_read=self._retry_as_single_exact(tool),
        )
        text = canonical_json_text(descriptor)
        size, units = _serialize_units(text)
        if size <= self.remaining_bytes and units <= self.remaining_units:
            self._round_bytes += size
            self._round_units += units
        return Projection(
            payload=descriptor,
            delivered_bytes=size,
            delivered_units=units,
            body_included=False,
            truncated=True,
            reason=REASON_REQUEST_BUDGET,
        )

    # -- internals ---------------------------------------------------------

    def _page_caps(self, tool: str) -> tuple[int, int]:
        if tool == EXACT_READ_TOOL and self._round_calls == 1:
            return self.policy.exact_max_bytes, self.policy.exact_max_units
        return self.policy.page_max_bytes, self.policy.page_max_units

    def _single_record(self, body: Mapping[str, Any]) -> bool:
        """Whether this payload is one record rather than a page of them."""

        result = body.get("result")
        if not isinstance(result, Mapping):
            return True
        items = result.get("items")
        if not isinstance(items, list):
            return True
        return len(items) <= 1

    def _retry_as_single_exact(self, tool: str) -> bool:
        return tool == EXACT_READ_TOOL and self._round_calls > 1

    def _ref_only_descriptor(
        self,
        *,
        tool: str,
        ref: Mapping[str, Any],
        reason: str,
        page_bytes: int,
        page_units: int,
        retry_as_single_exact_read: bool,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "code": "ok",
            "result": {
                "body_included": False,
                "truncated": True,
                "reason": reason,
                "ref": dict(ref),
                "caps": _caps_of(
                    page_bytes=page_bytes,
                    page_units=page_units,
                    round_bytes=self.policy.round_max_bytes,
                    round_units=self.policy.round_max_units,
                ),
                "retry_as_single_exact_read": retry_as_single_exact_read,
                "note": (
                    "full body omitted for budget; read this ref on its own "
                    "next turn to receive the content"
                ),
            },
        }

    def _project_items(
        self, body: Mapping[str, Any], page_bytes: int, page_units: int
    ) -> Projection | None:
        """Deliver whole records while they fit, then describe the rest by ref.

        A page of twenty records is not one indivisible blob: dropping all of it
        because the last record is large hands the model nothing to work with.
        Selection is deterministic (the store's order is already fixed) and
        never splits a record.
        """

        result = body.get("result")
        if not isinstance(result, Mapping):
            return None
        items = result.get("items")
        if not isinstance(items, list) or not items:
            return None

        kept: list[Any] = []
        dropped: list[Mapping[str, Any]] = []
        for item in items:
            candidate = dict(result)
            candidate["items"] = kept + [item]
            candidate["truncated"] = True
            text = canonical_json_text({"ok": body.get("ok", True), "code": body.get("code", "ok"), "result": candidate})
            size, units = _serialize_units(text)
            if size <= self.remaining_bytes and units <= self.remaining_units and size <= page_bytes and units <= page_units:
                kept.append(item)
            else:
                ref = _ref_of({"_": item})
                if ref is None:
                    return None
                dropped.append(
                    {
                        "ref": dict(ref),
                        "body_included": False,
                        "reason": REASON_PAGE_BUDGET,
                    }
                )
        if not dropped:
            return None

        candidate = dict(result)
        candidate["items"] = kept
        candidate["truncated"] = True
        candidate["ref_only_items"] = dropped
        candidate["caps"] = _caps_of(
            page_bytes=page_bytes,
            page_units=page_units,
            round_bytes=self.policy.round_max_bytes,
            round_units=self.policy.round_max_units,
        )
        candidate["note"] = (
            "records kept whole; the rest are identity-only. Read one ref on its "
            "own next turn to receive the body"
        )
        descriptor = {
            "ok": body.get("ok", True),
            "code": body.get("code", "ok"),
            "result": candidate,
        }
        text = canonical_json_text(descriptor)
        size, units = _serialize_units(text)
        if size > self.remaining_bytes or units > self.remaining_units:
            return None
        self._round_bytes += size
        self._round_units += units
        return Projection(
            payload=descriptor,
            delivered_bytes=size,
            delivered_units=units,
            body_included=bool(kept),
            truncated=True,
            reason=REASON_PAGE_BUDGET,
        )

    def _reject(self, reason: str, *, tool: str, body_bytes: int) -> Projection:
        descriptor = {
            "ok": False,
            "code": reason,
            "result": {
                "executed": True,
                "delivered": False,
                "tool": tool,
                "body_bytes": body_bytes,
                "reason": reason,
                "note": "result was produced but not delivered; ask again with a "
                "smaller limit or read a single ref",
            },
        }
        text = canonical_json_text(descriptor)
        size, units = _serialize_units(text)
        if size <= self.remaining_bytes and units <= self.remaining_units:
            self._round_bytes += size
            self._round_units += units
        return Projection(
            payload=descriptor,
            delivered_bytes=size,
            delivered_units=units,
            body_included=False,
            truncated=True,
            reason=reason,
        )

    def refusal(self, *, tool: str, reason: str = REASON_NOT_EXECUTED) -> Projection:
        """A result for a call the budget refused *before* executing it."""

        descriptor = {
            "ok": False,
            "code": reason,
            "result": {
                "executed": False,
                "delivered": False,
                "tool": tool,
                "reason": reason,
                "note": "not executed: the round has no budget left for a "
                "minimal result; ask again with a smaller limit",
            },
        }
        text = canonical_json_text(descriptor)
        size, units = _serialize_units(text)
        if size <= self.remaining_bytes and units <= self.remaining_units:
            self._round_bytes += size
            self._round_units += units
        return Projection(
            payload=descriptor,
            delivered_bytes=size,
            delivered_units=units,
            body_included=False,
            truncated=True,
            reason=reason,
        )


@dataclass(frozen=True)
class PreviewDecision:
    """Whether the next concrete request would fit the whole-request budget."""

    fits: bool
    reason: str | None
    body_units: int
    required_units: int
    budget_units: int | None
    admission_units: int | None


class AgentRequestPreview:
    """Same-source preview of the request the loop is about to send.

    ``build_agent_request_body`` is reused with the manifest, the exact
    messages, the exact tool schemas and the exact ``max_tokens`` the adapter
    will use, so the estimate covers the whole body: history, tool results,
    tool schemas and the current input.  Nothing is sent and no attempt is
    reserved to find out whether it fits.

    The admission limit is ``request_budget_units - continuation_headroom``:
    the headroom is *inside* the budget, so a request that leaves no room for
    the model's follow-up is treated as not fitting rather than allowed to
    consume the whole allowance.
    """

    def __init__(
        self,
        *,
        manifest: Any,
        max_tokens: int,
        output_reserve_units: int,
        request_budget_units: int | None,
        continuation_headroom_units: int = CONTINUATION_HEADROOM_UNITS,
    ) -> None:
        self._manifest = manifest
        self._max_tokens = max_tokens
        self._output_reserve_units = output_reserve_units
        self._request_budget_units = request_budget_units
        self._headroom = continuation_headroom_units

    @property
    def request_budget_units(self) -> int | None:
        return self._request_budget_units

    @property
    def continuation_headroom_units(self) -> int:
        return self._headroom

    def check(
        self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]]
    ) -> PreviewDecision:
        from .agent_protocol import build_agent_request_body

        body = build_agent_request_body(
            self._manifest,
            messages=messages,
            tools=tools,
            max_tokens=self._max_tokens,
        )
        body_units = estimate_units(body)
        required = request_units(body_units, self._output_reserve_units)
        if self._request_budget_units is None:
            # No host budget: the adapter has nothing to compare against, and
            # guessing one here would silently invent a policy.
            return PreviewDecision(True, None, body_units, required, None, None)
        admission = self._request_budget_units - self._headroom
        fits = required <= admission
        return PreviewDecision(
            fits,
            None if fits else REASON_REQUEST_BUDGET,
            body_units,
            required,
            self._request_budget_units,
            admission,
        )


__all__ = [
    "AgentRequestPreview",
    "CONTINUATION_HEADROOM_UNITS",
    "EXACT_READ_TOOL",
    "MIN_ENVELOPE_BYTES",
    "MIN_ENVELOPE_UNITS",
    "PreviewDecision",
    "Projection",
    "REASON_NOT_EXECUTED",
    "REASON_PAGE_BUDGET",
    "REASON_REQUEST_BUDGET",
    "REASON_ROUND_BUDGET",
    "REASON_TOO_LARGE",
    "ToolBudgetPolicy",
    "ToolResultBudgeter",
]
