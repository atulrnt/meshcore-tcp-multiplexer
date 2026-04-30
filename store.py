import asyncio
import sqlite3
import time
from pathlib import Path

# Companion → client packet types worth persisting across reconnections.
# 7  = CONTACT_MSG_RECV    (private message, legacy)
# 8  = CHANNEL_MSG_RECV    (channel message, legacy)
# 16 = CONTACT_MSG_RECV_V3 (private message, v3)
# 17 = CHANNEL_MSG_RECV_V3 (channel message, v3)
STORABLE_TYPES: frozenset[int] = frozenset([7, 8, 16, 17])


class MessageStore:
    """
    Async wrapper around a SQLite message store.

    All blocking DB calls are dispatched to a thread via asyncio.to_thread
    so they never stall the event loop.

    Schema
    ------
    packets  — one row per stored frame
    clients  — per-IP watermark (last sync timestamp)
    """

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._setup()

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def store(self, packet_type: int, data: bytes) -> None:
        await asyncio.to_thread(self._store_sync, packet_type, data)

    async def load_since(self, client_ip: str) -> list[bytes]:
        return await asyncio.to_thread(self._load_since_sync, client_ip)

    async def update_client(self, client_ip: str) -> None:
        await asyncio.to_thread(self._update_client_sync, client_ip)

    # ------------------------------------------------------------------
    # Sync helpers (run inside a thread)
    # ------------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
        return con

    def _setup(self) -> None:
        with self._connect() as con:
            con.execute(
                "CREATE TABLE IF NOT EXISTS packets "
                "(id INTEGER PRIMARY KEY, timestamp REAL NOT NULL, "
                " type INTEGER NOT NULL, data BLOB NOT NULL)"
            )
            con.execute(
                "CREATE TABLE IF NOT EXISTS clients "
                "(ip TEXT PRIMARY KEY, last_timestamp REAL NOT NULL DEFAULT 0)"
            )

    def _store_sync(self, packet_type: int, data: bytes) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO packets (timestamp, type, data) VALUES (?, ?, ?)",
                (time.time(), packet_type, data),
            )

    def _load_since_sync(self, client_ip: str) -> list[bytes]:
        with self._connect() as con:
            row = con.execute(
                "SELECT last_timestamp FROM clients WHERE ip = ?", (client_ip,)
            ).fetchone()

            if row is None:
                # First time we see this client — register it with timestamp 0
                con.execute(
                    "INSERT INTO clients (ip, last_timestamp) VALUES (?, 0)",
                    (client_ip,),
                )
                timestamp = 0.0
            else:
                timestamp = row[0]

            rows = con.execute(
                "SELECT data FROM packets WHERE timestamp > ? ORDER BY timestamp",
                (timestamp,),
            ).fetchall()

        return [r[0] for r in rows]

    def _update_client_sync(self, client_ip: str) -> None:
        with self._connect() as con:
            con.execute(
                "INSERT INTO clients (ip, last_timestamp) VALUES (?, ?) "
                "ON CONFLICT(ip) DO UPDATE SET last_timestamp = excluded.last_timestamp",
                (client_ip, time.time()),
            )
