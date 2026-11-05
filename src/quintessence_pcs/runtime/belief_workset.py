"""Deterministic working-set ordering; priorities confer no read authority."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

MAX_BELIEFS_IN_DOC = 200
RESERVE_P5 = 20
RECENT_LIMIT = 50
STATUS_RANK = {"contested": 0, "contradicted": 0, "supported": 1, "proposed": 2}


def scope_overlap(left: Mapping, right: Mapping) -> int:
    return sum(
        bool(set(value) & set(right.get(key, ()))) if key == "entity_ids"
        else key in right and value == right[key]
        for key, value in left.items()
    )


def ordered_beliefs(
    beliefs: Sequence[tuple[Any, dict]], *, direct_ids: set[str], scope: Mapping,
) -> list[tuple[int, tuple[Any, dict]]]:
    """P5's reserve is limited to capacity remaining after P1--P3.

    P2 intentionally stays empty. N counts records, not successful byte fits.
    Callers skip oversized records and continue scanning this complete order.
    """
    newest = sorted(beliefs, key=lambda pair: (pair[0].recorded_at, pair[0].record_id), reverse=True)
    p1, p3, rest = [], [], []
    for pair in newest:
        if pair[0].record_id in direct_ids:
            p1.append(pair)
        elif scope_overlap(pair[0].scope or {}, scope):
            p3.append(pair)
        else:
            rest.append(pair)
    p3.sort(key=lambda pair: scope_overlap(pair[0].scope or {}, scope), reverse=True)
    high = len(p1) + len(p3)
    reserve = min(RESERVE_P5, max(0, MAX_BELIEFS_IN_DOC - high))
    n = min(RECENT_LIMIT, max(0, MAX_BELIEFS_IN_DOC - high - reserve))
    p4, p5 = rest[:n], rest[n:]
    p5.sort(key=lambda pair: STATUS_RANK.get(pair[0].status, 3))
    return [(level, pair) for level, group in ((1, p1), (3, p3), (4, p4), (5, p5)) for pair in group]


def coverage(total: int, rendered: int, omitted_size: int) -> dict[str, Any]:
    return {"rendered": rendered, "observable_total": total,
            "omitted_budget": total - rendered - omitted_size,
            "omitted_size": omitted_size, "more_available": total > rendered}
