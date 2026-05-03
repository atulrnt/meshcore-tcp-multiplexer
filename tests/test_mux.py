import asyncio
import os
import socket
import tempfile
import unittest

from framing import CLIENT_START, COMPANION_START, read_frame
from mux import MeshCoreMux
from store import MessageStore


def _get_free_port() -> int:
    with socket.socket() as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def _companion_frame(packet_type: int, body: bytes = b"") -> bytes:
    payload = bytes([packet_type]) + body
    return bytes([COMPANION_START]) + len(payload).to_bytes(2, "little") + payload


def _client_frame(cmd: int, body: bytes = b"") -> bytes:
    payload = bytes([cmd]) + body
    return bytes([CLIENT_START]) + len(payload).to_bytes(2, "little") + payload


def _make_mux(**kwargs) -> MeshCoreMux:
    defaults = dict(
        companion_host="127.0.0.1",
        companion_port=5000,
        listen_host="127.0.0.1",
        listen_port=5001,
    )
    defaults.update(kwargs)
    return MeshCoreMux(**defaults)


# ---------------------------------------------------------------------------
# Unit tests — frame builders and pure response handlers
# ---------------------------------------------------------------------------


class TestFrameBuilders(unittest.TestCase):
    def test_build_channel_frame_structure(self):
        mux = _make_mux(beacon_channel=2)
        frame = mux._build_channel_frame("2024-01-01T00:00:00Z")
        self.assertEqual(frame[0], CLIENT_START)
        length = int.from_bytes(frame[1:3], "little")
        self.assertEqual(len(frame), 3 + length)
        self.assertEqual(frame[3], 0x03)  # send-channel-message cmd
        self.assertEqual(frame[5], 2)  # beacon_channel index

    def test_build_channel_frame_text_encoded(self):
        mux = _make_mux(beacon_channel=0)
        text = "hello"
        frame = mux._build_channel_frame(text)
        # payload: [0x03][0x00][ch][ts 4B][text...]
        self.assertIn(text.encode(), frame)

    def test_build_get_channel_frame(self):
        mux = _make_mux(beacon_channel=3)
        frame = mux._build_get_channel_frame()
        self.assertEqual(frame[0], CLIENT_START)
        self.assertEqual(frame[3], 0x1F)  # GET_CHANNEL
        self.assertEqual(frame[4], 3)  # channel index

    def test_build_telemetry_req_frame(self):
        pubkey = bytes(range(32))
        mux = _make_mux(telemetry_pubkey=pubkey)
        frame = mux._build_telemetry_req_frame()
        self.assertEqual(frame[0], CLIENT_START)
        self.assertEqual(frame[3], 0x27)  # CMD_SEND_TELEMETRY_REQ
        self.assertEqual(frame[7:], pubkey)

    def test_build_telemetry_req_frame_length(self):
        pubkey = bytes(32)
        mux = _make_mux(telemetry_pubkey=pubkey)
        frame = mux._build_telemetry_req_frame()
        length = int.from_bytes(frame[1:3], "little")
        # payload = 4 header bytes + 32 pubkey bytes
        self.assertEqual(length, 36)


class TestResponseHandlers(unittest.TestCase):
    def test_log_channel_info_too_short(self):
        mux = _make_mux()
        mux._log_channel_info(bytes(10))  # must not raise

    def test_log_channel_info_valid(self):
        mux = _make_mux()
        name = b"TestChan\x00" + b"\x00" * 24
        psk = b"\xab" * 16
        payload = bytes([0x12, 5]) + name + psk
        frame = bytes([COMPANION_START]) + len(payload).to_bytes(2, "little") + payload
        mux._log_channel_info(frame)  # must not raise

    def test_log_channel_info_empty_name(self):
        mux = _make_mux()
        name = b"\x00" * 32
        psk = b"\x00" * 16
        payload = bytes([0x12, 0]) + name + psk
        frame = bytes([COMPANION_START]) + len(payload).to_bytes(2, "little") + payload
        mux._log_channel_info(frame)  # must not raise

    def test_handle_telemetry_response_too_short(self):
        mux = _make_mux()
        mux._handle_telemetry_response(bytes(5))  # must not raise

    def test_handle_telemetry_response_valid(self):
        mux = _make_mux()
        pubkey = b"\x01\x02\x03\x04\x05\x06"
        # temperature ch0 = 25.0°C
        lpp = bytes([0x00, 0x67, 0x00, 0xFA])
        payload = bytes([0x8B, 0x00]) + pubkey + lpp
        frame = bytes([COMPANION_START]) + len(payload).to_bytes(2, "little") + payload
        mux._handle_telemetry_response(frame)  # must not raise

    def test_write_queue_overflow_drops_oldest(self):
        mux = _make_mux(queue_depth=2)
        mux._write_queue.put_nowait(b"frame1")
        mux._write_queue.put_nowait(b"frame2")
        self.assertTrue(mux._write_queue.full())
        # Simulate the overflow behavior in _handle_client
        if mux._write_queue.full():
            try:
                mux._write_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        mux._write_queue.put_nowait(b"frame3")
        items = []
        while not mux._write_queue.empty():
            items.append(mux._write_queue.get_nowait())
        self.assertNotIn(b"frame1", items)
        self.assertIn(b"frame2", items)
        self.assertIn(b"frame3", items)


# ---------------------------------------------------------------------------
# Fake companion server
# ---------------------------------------------------------------------------


class FakeCompanion:
    def __init__(self):
        self.received_frames: list[bytes] = []
        self._server = None
        self._writers: list[asyncio.StreamWriter] = []
        self._connected = asyncio.Event()

    async def start(self, host: str = "127.0.0.1") -> int:
        self._server = await asyncio.start_server(self._handle, host, 0)
        return self._server.sockets[0].getsockname()[1]

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._writers.append(writer)
        self._connected.set()
        try:
            while True:
                frame = await read_frame(reader, CLIENT_START)
                self.received_frames.append(frame)
        except Exception:
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)

    async def send(self, frame: bytes) -> None:
        for w in list(self._writers):
            w.write(frame)
            await w.drain()

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def wait_connected(self, timeout: float = 2.0) -> None:
        await asyncio.wait_for(self._connected.wait(), timeout)


# ---------------------------------------------------------------------------
# Integration tests — basic message routing
# ---------------------------------------------------------------------------


class TestMuxIntegration(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.companion = FakeCompanion()
        companion_port = await self.companion.start()

        self.listen_port = _get_free_port()
        self.mux = MeshCoreMux(
            companion_host="127.0.0.1",
            companion_port=companion_port,
            listen_host="127.0.0.1",
            listen_port=self.listen_port,
            queue_depth=64,
        )
        self.mux_task = asyncio.create_task(self.mux.run())
        await asyncio.wait_for(self.mux._companion_ready.wait(), timeout=2.0)

    async def asyncTearDown(self):
        self.mux_task.cancel()
        try:
            await asyncio.wait_for(self.mux_task, timeout=1.0)
        except (asyncio.CancelledError, Exception):
            pass
        await self.companion.stop()

    async def _connect_client(self):
        r, w = await asyncio.open_connection("127.0.0.1", self.listen_port)
        # Wait until mux has registered this client
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if len(self.mux._clients) > 0:
                break
            await asyncio.sleep(0.005)
        return r, w

    async def test_companion_frame_reaches_client(self):
        reader, writer = await self._connect_client()
        try:
            frame = _companion_frame(8, b"hello client")
            await self.companion.send(frame)
            received = await asyncio.wait_for(
                read_frame(reader, COMPANION_START), timeout=2.0
            )
            self.assertEqual(received, frame)
        finally:
            writer.close()

    async def test_client_frame_reaches_companion(self):
        reader, writer = await self._connect_client()
        try:
            frame = _client_frame(0x01, b"from client")
            writer.write(frame)
            await writer.drain()

            deadline = asyncio.get_event_loop().time() + 2.0
            while asyncio.get_event_loop().time() < deadline:
                if self.companion.received_frames:
                    break
                await asyncio.sleep(0.005)

            self.assertTrue(len(self.companion.received_frames) > 0)
            self.assertEqual(self.companion.received_frames[-1], frame)
        finally:
            writer.close()

    async def test_broadcast_reaches_all_clients(self):
        r1, w1 = await self._connect_client()

        r2, w2 = await asyncio.open_connection("127.0.0.1", self.listen_port)
        deadline = asyncio.get_event_loop().time() + 2.0
        while asyncio.get_event_loop().time() < deadline:
            if len(self.mux._clients) >= 2:
                break
            await asyncio.sleep(0.005)

        try:
            frame = _companion_frame(7, b"broadcast")
            await self.companion.send(frame)

            recv1 = await asyncio.wait_for(read_frame(r1, COMPANION_START), timeout=2.0)
            recv2 = await asyncio.wait_for(read_frame(r2, COMPANION_START), timeout=2.0)
            self.assertEqual(recv1, frame)
            self.assertEqual(recv2, frame)
        finally:
            w1.close()
            w2.close()

    async def test_telemetry_response_not_forwarded_to_client(self):
        reader, writer = await self._connect_client()
        try:
            pubkey = b"\x01\x02\x03\x04\x05\x06"
            lpp = bytes([0x00, 0x67, 0x00, 0xFA])  # temp 25°C
            payload = bytes([0x8B, 0x00]) + pubkey + lpp
            telem_frame = (
                bytes([COMPANION_START]) + len(payload).to_bytes(2, "little") + payload
            )
            regular_frame = _companion_frame(8, b"regular msg")

            await self.companion.send(telem_frame)
            await asyncio.sleep(0.05)
            await self.companion.send(regular_frame)

            received = await asyncio.wait_for(
                read_frame(reader, COMPANION_START), timeout=2.0
            )
            self.assertEqual(received, regular_frame)
        finally:
            writer.close()

    async def test_channel_info_forwarded_to_client_when_beacon_disabled(self):
        reader, writer = await self._connect_client()
        try:
            name = b"TestChan\x00" + b"\x00" * 24
            psk = b"\xab" * 16
            payload = bytes([0x12, 0]) + name + psk
            chan_frame = (
                bytes([COMPANION_START]) + len(payload).to_bytes(2, "little") + payload
            )

            await self.companion.send(chan_frame)

            received = await asyncio.wait_for(
                read_frame(reader, COMPANION_START), timeout=2.0
            )
            self.assertEqual(received, chan_frame)
        finally:
            writer.close()

    async def test_channel_info_not_forwarded_to_client_when_beacon_enabled(self):
        self.mux._beacon = 60.0
        reader, writer = await self._connect_client()
        try:
            name = b"TestChan\x00" + b"\x00" * 24
            psk = b"\xab" * 16
            payload = bytes([0x12, 0]) + name + psk
            chan_frame = (
                bytes([COMPANION_START]) + len(payload).to_bytes(2, "little") + payload
            )
            regular_frame = _companion_frame(8, b"follow-up")

            await self.companion.send(chan_frame)
            await asyncio.sleep(0.05)
            await self.companion.send(regular_frame)

            received = await asyncio.wait_for(
                read_frame(reader, COMPANION_START), timeout=2.0
            )
            self.assertEqual(received, regular_frame)
        finally:
            writer.close()

    async def test_dead_client_removed_on_broadcast(self):
        reader, writer = await self._connect_client()
        writer.close()
        await asyncio.sleep(0.05)

        # Broadcast should not raise even with a dead writer
        frame = _companion_frame(8, b"after disconnect")
        await self.companion.send(frame)
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.mux._clients), 0)


# ---------------------------------------------------------------------------
# Integration tests — store-and-forward
# ---------------------------------------------------------------------------


class TestMuxStoreAndForward(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.companion = FakeCompanion()
        companion_port = await self.companion.start()

        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.store = MessageStore(self.db_path)

        self.listen_port = _get_free_port()
        self.mux = MeshCoreMux(
            companion_host="127.0.0.1",
            companion_port=companion_port,
            listen_host="127.0.0.1",
            listen_port=self.listen_port,
            queue_depth=64,
            store=self.store,
        )
        self.mux_task = asyncio.create_task(self.mux.run())
        await asyncio.wait_for(self.mux._companion_ready.wait(), timeout=2.0)

    async def asyncTearDown(self):
        self.mux_task.cancel()
        try:
            await asyncio.wait_for(self.mux_task, timeout=1.0)
        except (asyncio.CancelledError, Exception):
            pass
        await self.companion.stop()
        try:
            os.unlink(self.db_path)
        except FileNotFoundError:
            pass

    async def test_storable_messages_persisted(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.listen_port)
        try:
            frame = _companion_frame(7, b"persist me")
            await self.companion.send(frame)
            # Wait for broadcast + store to complete
            await asyncio.wait_for(read_frame(reader, COMPANION_START), timeout=2.0)
            await asyncio.sleep(0.05)

            stored = await self.store.load_since("new_client")
            self.assertIn(frame, stored)
        finally:
            writer.close()

    async def test_sync_next_message_triggers_replay(self):
        msg = _companion_frame(7, b"missed message")
        await self.store.store(7, msg)

        reader, writer = await asyncio.open_connection("127.0.0.1", self.listen_port)
        try:
            sync_frame = _client_frame(10)  # SYNC_NEXT_MESSAGE = 10
            writer.write(sync_frame)
            await writer.drain()

            received = await asyncio.wait_for(
                read_frame(reader, COMPANION_START), timeout=2.0
            )
            self.assertEqual(received, msg)
        finally:
            writer.close()

    async def test_sync_only_fires_once_per_connection(self):
        msg = _companion_frame(7, b"stored")
        await self.store.store(7, msg)

        reader, writer = await asyncio.open_connection("127.0.0.1", self.listen_port)
        try:
            sync_frame = _client_frame(10)
            writer.write(sync_frame)
            await writer.drain()
            await asyncio.wait_for(read_frame(reader, COMPANION_START), timeout=2.0)

            # Second SYNC_NEXT_MESSAGE should NOT trigger another replay
            writer.write(sync_frame)
            await writer.drain()
            await asyncio.sleep(0.1)

            # Queue should be empty — no second replay in flight
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(read_frame(reader, COMPANION_START), timeout=0.3)
        finally:
            writer.close()

    async def test_non_storable_type_not_persisted(self):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.listen_port)
        try:
            # Type 1 is not in STORABLE_TYPES
            frame = _companion_frame(1, b"ephemeral")
            await self.companion.send(frame)
            await asyncio.wait_for(read_frame(reader, COMPANION_START), timeout=2.0)
            await asyncio.sleep(0.05)

            stored = await self.store.load_since("new_client")
            self.assertNotIn(frame, stored)
        finally:
            writer.close()
