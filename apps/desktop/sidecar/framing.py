"""desktop.v1 frame codec: 4-byte big-endian length + UTF-8 JSON.

The frame boundary is owned by this module, not by a ``read`` return value.
``read_frame`` loops until it has the exact header and the exact body, so a
pipe that delivers one byte, a partial header or two coalesced frames behaves
the same as a pipe that delivers one complete frame per read.

Limits are structural: an empty frame (length 0) and a frame above
``MAX_FRAME_BYTES`` are refused before any allocation proportional to the
claimed length.  A frame that ends at EOF before its declared body is complete
is ``TruncatedFrame`` and the caller must stop; a clean EOF between frames is
``None``.
"""

from __future__ import annotations

import json
import struct
from typing import Any

MAX_FRAME_BYTES = 1024 * 1024
_HEADER = struct.Struct(">I")


class FrameError(Exception):
    """Base class for a frame that cannot be decoded."""


class TruncatedFrame(FrameError):
    """EOF arrived inside the 4-byte header or inside the declared body."""


class InvalidLength(FrameError):
    """The declared length is zero, above the cap, or otherwise unusable."""


class InvalidPayload(FrameError):
    """The body is not valid UTF-8 or not strict JSON."""


def encode_frame(value: Any) -> bytes:
    """Encode one JSON value as a frame; never emit an oversize frame."""

    body = json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if not body or len(body) > MAX_FRAME_BYTES:
        raise InvalidLength(
            f"encoded frame is {len(body)} bytes; limit is {MAX_FRAME_BYTES}"
        )
    return _HEADER.pack(len(body)) + body


def encode_json_text_frame(text: str) -> bytes:
    """Encode already-canonical JSON text as a frame (used by tests)."""

    body = text.encode("utf-8")
    if not body or len(body) > MAX_FRAME_BYTES:
        raise InvalidLength(
            f"encoded frame is {len(body)} bytes; limit is {MAX_FRAME_BYTES}"
        )
    return _HEADER.pack(len(body)) + body


def _read_exact(stream: Any, size: int) -> bytes | None:
    """Read exactly ``size`` bytes; ``None`` means clean EOF at position 0."""

    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            if not chunks:
                return None
            raise TruncatedFrame(
                f"EOF after {size - remaining} of {size} declared byte(s)"
            )
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: Any) -> bytes | None:
    """Return one exact body, or ``None`` for a clean EOF between frames."""

    header = _read_exact(stream, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length == 0:
        raise InvalidLength("zero-length frame is not allowed")
    if length > MAX_FRAME_BYTES:
        raise InvalidLength(
            f"declared frame is {length} bytes; limit is {MAX_FRAME_BYTES}"
        )
    body = _read_exact(stream, length)
    if body is None:  # defensive: header exists, so _read_exact cannot return None
        raise TruncatedFrame("EOF before the declared body")
    return body


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise InvalidPayload(f"duplicate JSON object key {key!r}")
        document[key] = value
    return document


def decode_json(body: bytes) -> Any:
    """Decode strict UTF-8/JSON, rejecting duplicate object keys."""

    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InvalidPayload("frame body is not valid UTF-8") from exc
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except InvalidPayload:
        raise
    except ValueError as exc:
        raise InvalidPayload(f"frame body is not valid JSON: {exc}") from exc


def decode_json_text(text: str) -> Any:
    """Decode strict JSON text, rejecting duplicate object keys."""

    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except InvalidPayload:
        raise
    except ValueError as exc:
        raise InvalidPayload(f"text is not valid JSON: {exc}") from exc


__all__ = [
    "MAX_FRAME_BYTES",
    "FrameError",
    "TruncatedFrame",
    "InvalidLength",
    "InvalidPayload",
    "decode_json",
    "decode_json_text",
    "encode_frame",
    "encode_json_text_frame",
    "read_frame",
]
