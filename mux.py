import asyncio
import logging

from framing import COMPANION_START, CLIENT_START, read_frame

log = logging.getLogger(__name__)


class MeshCoreMux:
    def __init__(
        self,
        companion_host: str,
        companion_port: int,
        listen_host: str,
        listen_port: int,
        queue_depth: int = 256,
    ):
        self.companion_host = companion_host
        self.companion_port = companion_port
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._clients: set[asyncio.StreamWriter] = set()
        self._write_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=queue_depth)

    async def run(self) -> None:
        server = await asyncio.start_server(
            self._handle_client, self.listen_host, self.listen_port
        )
        addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
        log.info("listening on %s", addrs)
        async with server:
            await asyncio.gather(server.serve_forever(), self._companion_loop())

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

    async def _companion_read_loop(self, reader: asyncio.StreamReader) -> None:
        while True:
            frame = await read_frame(reader, COMPANION_START)
            log.debug("companion → clients: %d bytes", len(frame))
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
        log.info("client connected: %s", addr)
        self._clients.add(writer)
        try:
            while True:
                frame = await read_frame(reader, CLIENT_START)
                log.debug("client %s → companion: %d bytes", addr, len(frame))
                if self._write_queue.full():
                    # Drop oldest to prevent unbounded memory growth
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
