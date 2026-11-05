"""RG08: explicit ordered reply bubbles and the one-copy reply body format.

The model may return one to four ordered ``messages`` for a chat ``final``
output.  The host never splits a reply on punctuation, newlines or length: a
bubble exists only because the model emitted one.  The parts are generated
once, persisted once and read back in exactly that order.

Storage is deliberately minimal and compatible:

* one bubble keeps the legacy plain-text artifact body byte for byte;
* two or more bubbles are stored as one canonical JSON envelope carrying the
  versioned schema marker and the ordered parts -- never the joined text and
  the parts side by side;
* the read surface derives the joined compatibility text from the same parts,
  so restart/re-read is deterministic and no second full copy is written.

This module is pure: it never reads the store, calls a model or writes a row.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .foundation.errors import CorruptStore, InvalidInput
from .foundation.jsonutil import canonical_json_text, strict_json_loads

#: The exact schema marker of a multi-bubble artifact body.
REPLY_BODY_SCHEMA = "collab.reply.parts.v1"

#: The protocol bound on one explicit final reply.
MAX_REPLY_BUBBLES = 4
MAX_BUBBLE_CHARS = 4096

#: A legacy ``reply`` (and a one-bubble fallback) keeps the old bound.
MAX_SINGLE_REPLY_CHARS = 16384

#: The compatibility text the DTO and the UI use between bubbles.
BUBBLE_JOINER = "\n\n"


def normalise_bubble_parts(
    parts: Sequence[str],
    *,
    field: str = "messages",
    max_items: int = MAX_REPLY_BUBBLES,
    max_chars: int | None = None,
) -> tuple[str, ...]:
    """Validate and freeze one ordered bubble list.

    A single stored bubble may use the legacy reply bound; protocol
    ``messages`` always use the smaller per-bubble bound.  Whitespace-only
    bubbles are refused because they would render as an empty public message.
    """

    if isinstance(parts, (str, bytes)) or not isinstance(parts, Sequence):
        raise InvalidInput(f"{field} must be an array of strings")
    values = tuple(parts)
    if not 1 <= len(values) <= max_items:
        raise InvalidInput(f"{field} must contain 1..{max_items} items")
    if max_chars is not None:
        limit = max_chars
    else:
        limit = MAX_SINGLE_REPLY_CHARS if len(values) == 1 else MAX_BUBBLE_CHARS
    for index, value in enumerate(values):
        if not isinstance(value, str):
            raise InvalidInput(f"{field}[{index}] must be a string")
        if not value.strip():
            raise InvalidInput(f"{field}[{index}] must not be blank")
        if not 1 <= len(value) <= limit:
            raise InvalidInput(
                f"{field}[{index}] must be 1..{limit} characters"
            )
    return values


def join_bubbles(parts: Sequence[str]) -> str:
    """The joined compatibility text of one already-validated part list."""

    return BUBBLE_JOINER.join(parts)


def encode_reply_body(parts: Sequence[str]) -> str:
    """Serialize the one stored copy of a final reply.

    One part stays a plain string so every pre-RG08 artifact is byte-identical
    in shape.  Two or more parts use one JSON envelope with a schema marker.
    """

    values = normalise_bubble_parts(parts)
    if len(values) == 1:
        return values[0]
    return canonical_json_text(
        {"schema": REPLY_BODY_SCHEMA, "parts": list(values)}
    )


def decode_reply_body(body: str) -> tuple[tuple[str, ...], str]:
    """Read one stored reply body as ``(parts, joined_text)``.

    A legacy string (including anything that is not our exact schema marker)
    reads as one part.  A marked envelope with malformed parts is a corrupt
    store, not a silently reduced reply.
    """

    if not isinstance(body, str) or not body:
        raise CorruptStore("a stored reply body must be non-empty text")
    if body.startswith("{"):
        try:
            document: Any = strict_json_loads(body)
        except ValueError:
            document = None
        if (
            isinstance(document, Mapping)
            and document.get("schema") == REPLY_BODY_SCHEMA
        ):
            if set(document) != {"schema", "parts"}:
                raise CorruptStore("a stored reply envelope has unexpected keys")
            raw_parts = document.get("parts")
            try:
                values = normalise_bubble_parts(raw_parts, field="parts")
            except InvalidInput as exc:
                raise CorruptStore("a stored reply envelope has invalid parts") from exc
            return values, join_bubbles(values)
    return (body,), body


__all__ = [
    "BUBBLE_JOINER",
    "MAX_BUBBLE_CHARS",
    "MAX_REPLY_BUBBLES",
    "MAX_SINGLE_REPLY_CHARS",
    "REPLY_BODY_SCHEMA",
    "decode_reply_body",
    "encode_reply_body",
    "join_bubbles",
    "normalise_bubble_parts",
]
