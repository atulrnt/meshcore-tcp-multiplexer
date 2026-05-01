import asyncio
import logging
import time

import telemetry as telemetry_mod
from framing import COMPANION_START, CLIENT_START, read_frame
from store import MessageStore, STORABLE_TYPES
from telemetry import MqttPublisher

log = logging.getLogger(__name__)

# Client → companion command types
_CMD_SYNC_NEXT = 10        # SYNC_NEXT_MESSAGE — triggers stored-message replay
_CMD_GET_CHANNEL = 0x1F    # GET_CHANNEL — request channel name/PSK
_CMD_TELEMETRY_REQ = 0x27  # CMD_SEND_TELEMETRY_REQ — request repeater telemetry

# Companion → client response types (consumed by mux, not forwarded)
_RESP_CHANNEL_INFO = 0x12  # CHANNEL_INFO — response to GET_CHANNEL
_RESP_TELEMETRY = 0x8B     # PUSH_CODE_TELEMETRY_RESPONSE — repeater telemetry


class MeshCoreMux:
    def __init__(
        self,
        companion_host: str,
        companion_port: int,
        listen_host: str,
        listen_port: int,
        queue_depth: int = 256,
        store: MessageStore | None = None,
        beacon: float | None = None,
        beacon_channel: int = 0,
        telemetry_pubkey: bytes | None = None,
        telemetry_refresh: int = 300,
        telemetry_csv: str | None = None,
        mqtt_publisher: MqttPublisher | None = None,
    ):
        self.companion_host = companion_host
        self.companion_port = companion_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._store = store
        self._beacon = beacon
        self._beacon_channel = beacon_channel
        self._telemetry_pubkey = telemetry_pubkey
        self._telemetry_refresh = telemetry_refresh
        self._telemetry_csv = telemetry_csv
        self._mqtt_publisher = mqtt_publisher
        self._clients: set[asyncio.StreamWriter] = set()
        self._write_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_depth)
        self._companion_ready: asyncio.Event = asyncio.Event()

    async def run(self) -> None:
        server = await asyncio.start_server(
            self._handle_client, self.listen_host, self.listen_port
        )
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        log.info("listening on %s", addrs)
        async with server:
            tasks: list[asyncio.Coroutine] = [
                server.serve_forever(),
                self._companion_loop(),
            ]
            if self._beacon:
                tasks.append(self._beacon_loop())
            if self._telemetry_pubkey:
                tasks.append(self._telemetry_loop())
            await asyncio.gather(*tasks)

    # ------------------------------------------------------------------
    # Companion connection (with reconnect backoff)
    # ------------------------------------------------------------------

    async def _companion_loop(self) -> None:
        backoff = 1
        while True:
            try:
                reader, writer = await asyncio.open_connection(
                    self.companion_host, self.companion_port
                )
                log.info(
                    "connected to companion %s:%d",
                    self.companion_host,
                    self.companion_port,
                )
                backoff = 1
                await self._run_companion(reader, writer)
            except Exception as exc:
                log.warning("companion disconnected (%s), retry in %ds", exc, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)

    async def _run_companion(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._companion_ready.set()
        try:
            if self._beacon:
                try:
                    self._write_queue.put_nowait(self._build_get_channel_frame())
                except asyncio.QueueFull:
                    log.warning("beacon: write queue full, skipping channel info query")
            tasks = [
                asyncio.create_task(self._companion_read_loop(reader)),
                asyncio.create_task(self._companion_write_loop(writer)),
            ]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            writer.close()
            for t in done:
                exc = t.exception()
                if exc:
                    raise exc
        finally:
            self._companion_ready.clear()

    async def _companion_read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            frame = await read_frame(reader, COMPANION_START)
            log.debug("companion → clients: %d bytes", len(frame))
            if len(frame) >= 4 and frame[3] == _RESP_TELEMETRY:
                self._handle_telemetry_response(frame)
                continue
            if len(frame) >= 4 and frame[3] == _RESP_CHANNEL_INFO:
                self._log_channel_info(frame)
                continue  # consumed by mux; not a live message for clients
            if self._store and len(frame) >= 4 and frame[3] in STORABLE_TYPES:
                await self._store.store(frame[3], frame)
            await self._broadcast(frame)

    async def _companion_write_loop(self, writer: asyncio.StreamWriter) -> None:
        while True:
            frame = await self._write_queue.get()
            writer.write(frame)
            await writer.drain()

    # ------------------------------------------------------------------
    # Client handler
    # ------------------------------------------------------------------

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        addr = writer.get_extra_info("peername")
        client_ip = addr[0]
        log.info("client connected: %s", addr)
        self._clients.add(writer)
        synced = False
        try:
            while True:
                frame = await read_frame(reader, CLIENT_START)
                log.debug("client %s → companion: %d bytes", addr, len(frame))

                if self._store and len(frame) >= 4 and not synced:
                    if frame[3] == _CMD_SYNC_NEXT:
                        await self._replay_to_client(writer, client_ip)
                        synced = True

                if self._write_queue.full():
                    try:
                        self._write_queue.get_nowait()
                        log.warning("write queue full, dropped oldest frame")
                    except asyncio.QueueEmpty:
                        pass
                await self._write_queue.put(frame)
        except Exception as exc:
            log.info("client disconnected: %s (%s)", addr, exc)
        finally:
            self._clients.discard(writer)
            writer.close()

    async def _replay_to_client(
        self, writer: asyncio.StreamWriter, client_ip: str
    ) -> None:
        frames = await self._store.load_since(client_ip)  # type: ignore[union-attr]
        if not frames:
            log.info("store: no missed messages for %s", client_ip)
            return
        log.info("store: replaying %d message(s) to %s", len(frames), client_ip)
        for frame in frames:
            writer.write(frame)
        await writer.drain()
        await self._store.update_client(client_ip)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Broadcast to all clients in parallel
    # ------------------------------------------------------------------

    async def _broadcast(self, frame: bytes) -> None:
        if not self._clients:
            return
        writers = list(self._clients)
        for w in writers:
            w.write(frame)
        results = await asyncio.gather(
            *[w.drain() for w in writers], return_exceptions=True
        )
        for w, result in zip(writers, results):
            if isinstance(result, Exception):
                self._clients.discard(w)
                w.close()

    # ------------------------------------------------------------------
    # Telemetry loop
    # ------------------------------------------------------------------

    async def _telemetry_loop(self) -> None:
        log.info(
            "telemetry: pubkey=%s... refresh=%ds",
            self._telemetry_pubkey.hex()[:12],  # type: ignore[union-attr]
            self._telemetry_refresh,
        )
        while True:
            await self._companion_ready.wait()
            log.debug(
                "telemetry: sending request to %s",
                self._telemetry_pubkey.hex()[:12],  # type: ignore[union-attr]
            )
            frame = self._build_telemetry_req_frame()
            if self._write_queue.full():
                try:
                    self._write_queue.get_nowait()
                    log.warning("telemetry: write queue full, dropped oldest frame")
                except asyncio.QueueEmpty:
                    pass
            await self._write_queue.put(frame)
            await asyncio.sleep(self._telemetry_refresh)

    def _build_telemetry_req_frame(self) -> bytes:
        payload = bytes([_CMD_TELEMETRY_REQ, 0x00, 0x00, 0x00]) + self._telemetry_pubkey  # type: ignore[operator]
        return bytes([CLIENT_START]) + len(payload).to_bytes(2, "little") + payload

    def _handle_telemetry_response(self, frame: bytes) -> None:
        # frame: [START 1B][len 2B][0x8B][reserved 1B][pubkey_prefix 6B][LPP data...]
        if len(frame) < 11:
            log.warning("telemetry: response too short (%d bytes), ignoring", len(frame))
            return
        pubkey_prefix = frame[5:11].hex()
        lpp_data = frame[11:]
        fields = telemetry_mod.parse_lpp(lpp_data)
        log.debug("telemetry: response from %s fields=%r", pubkey_prefix, fields)
        if self._telemetry_csv:
            telemetry_mod.append_row(self._telemetry_csv, time.time(), pubkey_prefix, fields)
        if self._mqtt_publisher:
            self._mqtt_publisher.publish_telemetry(pubkey_prefix, fields)
        log.info("telemetry: stored %d field(s) from %s", len(fields), pubkey_prefix)

    # ------------------------------------------------------------------
    # Beacon loop
    # ------------------------------------------------------------------

    async def _beacon_loop(self) -> None:
        log.info(
            "beacon enabled: channel=%d interval=%.1fs",
            self._beacon_channel,
            self._beacon,
        )
        while True:
            await asyncio.sleep(self._beacon)
            frame = self._build_channel_frame(
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            )
            if self._write_queue.full():
                try:
                    self._write_queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            await self._write_queue.put(frame)
            log.debug("beacon: queued channel %d message", self._beacon_channel)

    def _build_channel_frame(self, text: str) -> bytes:
        ts = int(time.time()).to_bytes(4, "little")
        payload = bytes([0x03, 0x00, self._beacon_channel]) + ts + text.encode()
        return bytes([CLIENT_START]) + len(payload).to_bytes(2, "little") + payload

    def _build_get_channel_frame(self) -> bytes:
        payload = bytes([_CMD_GET_CHANNEL, self._beacon_channel])
        return bytes([CLIENT_START]) + len(payload).to_bytes(2, "little") + payload

    def _log_channel_info(self, frame: bytes) -> None:
        # CHANNEL_INFO payload: [0x12][idx][name 32B null-padded][psk 16B raw]
        # frame: [START][len 2B][payload...] → payload starts at frame[3]
        if len(frame) < 53:
            log.warning("beacon: CHANNEL_INFO response too short (%d bytes)", len(frame))
            return
        idx = frame[4]
        name = frame[5:37].rstrip(b"\x00").decode("utf-8", errors="replace")
        psk = frame[37:53].hex()
        if name:
            log.info("beacon channel %d: name=%r psk=%s", idx, name, psk)
        else:
            log.info("beacon channel %d: name=(none) psk=%s", idx, psk)
