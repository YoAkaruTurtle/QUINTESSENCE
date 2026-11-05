"""Read-only action-set compatibility for a configured Scope Recall entry.

The host must not discover a Memory-bridge version mismatch by calling a
missing action against real data: the old adapter answers ``invalid_request``
only after the request has been read, and a newer adapter could execute the
action.  This module therefore reads the entry's declared ``_ACTIONS`` tuple
with :mod:`ast` and never imports, executes or opens anything beside the entry
source file itself.

The check is deliberately conservative:

* a declared tuple that lacks a required action is a hard incompatibility;
* an entry with no declaration (for example a wrapper, a packaged binary or a
  test double) is reported as ``checked=false`` and is **not** treated as a
  mismatch, because the host cannot prove a negative without running it.

That keeps the diagnosis honest for the known PCS adapter while leaving the
ordinary wrapper case working.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Sequence

#: One bridge entry is a small Python shim; this bound only stops a mistaken
#: configuration from making one ``app.info`` call read an arbitrarily large
#: file.  It is not a trust boundary (the file is already configured by the
#: host owner).
MAX_ENTRY_BYTES = 2 * 1024 * 1024


def required_actions(*, recall_enabled: bool, time_enabled: bool) -> tuple[str, ...]:
    """The frozen actions this host needs for the configured surfaces."""

    actions = ["capture", "search"]
    if recall_enabled:
        actions.append("recall")
        if time_enabled:
            actions.append("time_window")
    return tuple(actions)


def read_declared_actions(entry: str | Path) -> tuple[tuple[str, ...] | None, str]:
    """Return ``(actions, reason)`` from the entry's top-level ``_ACTIONS``.

    ``actions`` is ``None`` when no literal declaration can be proven.  The
    reason is a short fixed label, never a path or source excerpt.
    """

    path = Path(entry)
    try:
        raw = path.read_bytes()
    except OSError:
        return None, "entry_unreadable"
    if len(raw) > MAX_ENTRY_BYTES:
        return None, "entry_too_large"
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None, "entry_not_utf8"
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return None, "entry_unparsed"

    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "_ACTIONS"
            for target in node.targets
        ):
            continue
        value = node.value
        if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            return None, "actions_not_literal"
        actions: list[str] = []
        for item in value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return None, "actions_not_literal"
            actions.append(item.value)
        return tuple(actions), "declared"
    return None, "actions_not_declared"


def check_entry_compatibility(
    entry: str | Path, required: Sequence[str]
) -> dict[str, Any]:
    """Describe whether a declared entry action set covers *required*."""

    actions, reason = read_declared_actions(entry)
    wanted = tuple(dict.fromkeys(str(action) for action in required))
    if actions is None:
        return {
            "checked": False,
            "actions": [],
            "required": list(wanted),
            "missing": [],
            "compatible": True,
            "reason": reason,
        }
    missing = [action for action in wanted if action not in actions]
    return {
        "checked": True,
        "actions": list(actions),
        "required": list(wanted),
        "missing": missing,
        "compatible": not missing,
        "reason": "ok" if not missing else "actions_missing",
    }


__all__ = [
    "MAX_ENTRY_BYTES",
    "check_entry_compatibility",
    "read_declared_actions",
    "required_actions",
]
