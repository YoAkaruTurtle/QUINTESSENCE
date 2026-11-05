"""RG03: the in-progress turn event cache (a read cache, never the truth).

Every public event is committed to ``collab_turn_events`` first.  This ring is
only a way for the reader thread to answer ``turn.events`` while the single
worker is busy with a turn: it holds the already-committed, already-filtered
public DTOs for at most two in-progress turns, plus the transient live extras
(commentary text, tool arguments/result excerpt) that are deliberately never
persisted.  The ring is not consulted for any state decision and is gone after
a restart.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

TERMINAL_EVENT_TYPES = frozenset(
    {
        "turn_completed",
        "turn_failed",
        "turn_cancelled",
        "turn_interrupted",
    }
)

DEFAULT_MAX_TURNS = 2
DEFAULT_MAX_EVENTS = 256


@dataclass
class _TurnBuffer:
    events: list[dict[str, Any]] = field(default_factory=list)
    terminal: bool = False
    truncated_from_seq: int | None = None


class TurnEventRing:
    """Thread-safe, bounded, per-turn event cache."""

    def __init__(
        self,
        *,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_events: int = DEFAULT_MAX_EVENTS,
    ) -> None:
        if max_turns < 1 or max_events < 1:
            raise ValueError("ring bounds must be positive")
        self._max_turns = max_turns
        self._max_events = max_events
        self._lock = threading.Lock()
        self._turns: dict[str, _TurnBuffer] = {}
        self._order: list[str] = []

    # ------------------------------------------------------------- writing

    def append(self, turn_id: str, event: Mapping[str, Any]) -> None:
        """Append one committed public DTO (called only after the DB commit)."""

        dto = dict(event)
        seq = dto.get("seq")
        if type(seq) is not int:
            raise ValueError("a ring event needs an integer seq")
        with self._lock:
            buffer = self._turns.get(turn_id)
            if buffer is None:
                buffer = _TurnBuffer()
                self._turns[turn_id] = buffer
                self._order.append(turn_id)
                while len(self._order) > self._max_turns:
                    oldest = self._order.pop(0)
                    self._turns.pop(oldest, None)
            buffer.events.append(dto)
            while len(buffer.events) > self._max_events:
                dropped = buffer.events.pop(0)
                buffer.truncated_from_seq = int(dropped.get("seq", 0)) + 1
            if dto.get("type") in TERMINAL_EVENT_TYPES:
                buffer.terminal = True

    def discard(self, turn_id: str) -> None:
        with self._lock:
            self._turns.pop(turn_id, None)
            if turn_id in self._order:
                self._order.remove(turn_id)

    # ------------------------------------------------------------- reading

    def has(self, turn_id: str) -> bool:
        with self._lock:
            return turn_id in self._turns

    def read(
        self, turn_id: str, *, after_seq: int = 0, limit: int = 100
    ) -> tuple[list[dict[str, Any]], int, bool, int | None]:
        """Return ``(events, next_seq, terminal, truncated_from_seq)``."""

        with self._lock:
            buffer = self._turns.get(turn_id)
            if buffer is None:
                return [], after_seq, False, None
            events = [
                dict(event)
                for event in buffer.events
                if int(event.get("seq", 0)) > after_seq
            ][:limit]
            next_seq = events[-1]["seq"] if events else after_seq
            return (
                events,
                next_seq,
                buffer.terminal,
                buffer.truncated_from_seq,
            )


__all__ = [
    "DEFAULT_MAX_EVENTS",
    "DEFAULT_MAX_TURNS",
    "TERMINAL_EVENT_TYPES",
    "TurnEventRing",
]
