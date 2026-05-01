import asyncio
import unittest

from framing import COMPANION_START, CLIENT_START, MAX_PAYLOAD, read_frame


def _build_frame(start: int, payload: bytes) -> bytes:
    return bytes([start]) + len(payload).to_bytes(2, "little") + payload


def _make_reader(data: bytes) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    reader.feed_data(data)
    reader.feed_eof()
    return reader


class TestReadFrame(unittest.IsolatedAsyncioTestCase):
    async def test_valid_companion_frame(self):
        payload = b"hello world"
        raw = _build_frame(COMPANION_START, payload)
        frame = await read_frame(_make_reader(raw), COMPANION_START)
        self.assertEqual(frame, raw)

    async def test_valid_client_frame(self):
        payload = b"\x0a\x01"
        raw = _build_frame(CLIENT_START, payload)
        frame = await read_frame(_make_reader(raw), CLIENT_START)
        self.assertEqual(frame, raw)

    async def test_empty_payload(self):
        raw = _build_frame(COMPANION_START, b"")
        frame = await read_frame(_make_reader(raw), COMPANION_START)
        self.assertEqual(frame, raw)

    async def test_junk_before_start_byte(self):
        payload = b"data"
        valid = _build_frame(COMPANION_START, payload)
        junk = bytes([0x00, 0x01, 0xFF, 0xAA])
        frame = await read_frame(_make_reader(junk + valid), COMPANION_START)
        self.assertEqual(frame, valid)

    async def test_start_byte_in_junk_skipped(self):
        # Junk that contains an isolated companion start byte not followed by valid frame
        # then a valid frame — the first start byte triggers length read which leads to
        # oversized/invalid frame and re-sync.
        payload = b"ok"
        valid = _build_frame(COMPANION_START, payload)
        # bad_len > MAX_PAYLOAD → resync
        bad_len = (MAX_PAYLOAD + 10).to_bytes(2, "little")
        bad = bytes([COMPANION_START]) + bad_len + b"\x00" * (MAX_PAYLOAD + 10)
        frame = await read_frame(_make_reader(bad + valid), COMPANION_START)
        self.assertEqual(frame, valid)

    async def test_max_payload(self):
        payload = b"x" * MAX_PAYLOAD
        raw = _build_frame(COMPANION_START, payload)
        frame = await read_frame(_make_reader(raw), COMPANION_START)
        self.assertEqual(frame, raw)

    async def test_frame_header_has_three_bytes(self):
        payload = b"abc"
        raw = _build_frame(COMPANION_START, payload)
        frame = await read_frame(_make_reader(raw), COMPANION_START)
        self.assertEqual(len(frame), 3 + len(payload))
        self.assertEqual(frame[0], COMPANION_START)
        self.assertEqual(int.from_bytes(frame[1:3], "little"), len(payload))

    async def test_consecutive_frames(self):
        p1 = b"first"
        p2 = b"second"
        data = _build_frame(COMPANION_START, p1) + _build_frame(COMPANION_START, p2)
        reader = _make_reader(data)
        f1 = await read_frame(reader, COMPANION_START)
        f2 = await read_frame(reader, COMPANION_START)
        self.assertEqual(f1[3:], p1)
        self.assertEqual(f2[3:], p2)
