"""Frame codec tests: a read boundary is never treated as a frame boundary."""

from __future__ import annotations

import io
import json
import unittest

from tests.desktop.support import framing


class ChunkedStream:
    """A stream that hands out pre-cut chunks, then EOF."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, size: int) -> bytes:
        if not self._chunks:
            return b""
        chunk = self._chunks[0]
        if len(chunk) <= size:
            self._chunks.pop(0)
            return chunk
        self._chunks[0] = chunk[size:]
        return chunk[:size]


def one_frame(**payload: object) -> bytes:
    return framing.encode_frame(payload)


class FramingTests(unittest.TestCase):
    def test_chunked_header_and_body(self) -> None:
        frame = one_frame(value="abcdef")
        stream = ChunkedStream([frame[:2], frame[2:4], frame[4:7], frame[7:]])
        self.assertEqual(json.loads(framing.read_frame(stream)), {"value": "abcdef"})

    def test_body_delivered_one_byte_per_read(self) -> None:
        frame = one_frame(value="0123456789")
        stream = ChunkedStream([bytes([byte]) for byte in frame])
        self.assertEqual(
            json.loads(framing.read_frame(stream)), {"value": "0123456789"}
        )

    def test_two_frames_coalesced_in_one_write(self) -> None:
        first = one_frame(value=1)
        second = one_frame(value=2)
        stream = io.BytesIO(first + second)
        self.assertEqual(json.loads(framing.read_frame(stream)), {"value": 1})
        self.assertEqual(json.loads(framing.read_frame(stream)), {"value": 2})
        self.assertIsNone(framing.read_frame(stream))

    def test_truncated_header_at_eof(self) -> None:
        stream = io.BytesIO(b"\x00\x00")
        with self.assertRaises(framing.TruncatedFrame):
            framing.read_frame(stream)

    def test_truncated_body_at_eof(self) -> None:
        frame = one_frame(value="truncated")
        stream = io.BytesIO(frame[:-3])
        with self.assertRaises(framing.TruncatedFrame):
            framing.read_frame(stream)

    def test_zero_length_frame_is_refused(self) -> None:
        stream = io.BytesIO(b"\x00\x00\x00\x00")
        with self.assertRaises(framing.InvalidLength):
            framing.read_frame(stream)

    def test_oversize_length_is_refused_without_allocating(self) -> None:
        stream = io.BytesIO((framing.MAX_FRAME_BYTES + 1).to_bytes(4, "big"))
        with self.assertRaises(framing.InvalidLength):
            framing.read_frame(stream)

    def test_invalid_json_is_a_payload_error(self) -> None:
        body = b"{not json"
        with self.assertRaises(framing.InvalidPayload):
            framing.decode_json(body)

    def test_invalid_utf8_is_a_payload_error(self) -> None:
        with self.assertRaises(framing.InvalidPayload):
            framing.decode_json(b"\xff\xfe\x00")

    def test_duplicate_object_keys_are_refused(self) -> None:
        with self.assertRaises(framing.InvalidPayload):
            framing.decode_json(b'{"a":1,"a":2}')

    def test_clean_eof_between_frames_is_none(self) -> None:
        self.assertIsNone(framing.read_frame(io.BytesIO(b"")))

    def test_encode_refuses_oversize_and_empty(self) -> None:
        with self.assertRaises(framing.InvalidLength):
            framing.encode_json_text_frame("")
        with self.assertRaises(framing.InvalidLength):
            framing.encode_json_text_frame("x" * (framing.MAX_FRAME_BYTES + 1))

    def test_round_trip_preserves_non_ascii(self) -> None:
        frame = framing.encode_frame({"text": "中文合成内容"})
        self.assertEqual(
            framing.decode_json(frame[4:]), {"text": "中文合成内容"}
        )


if __name__ == "__main__":
    unittest.main()
