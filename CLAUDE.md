# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running

```bash
python main.py --companion-host 192.168.1.50 --companion-port 5000 --listen-port 5001
python main.py --companion-host 192.168.1.50 --listen-port 5001 --store messages.db --debug
```

All CLI flags have env-var equivalents: `COMPANION_HOST`, `COMPANION_PORT`, `LISTEN_HOST`, `LISTEN_PORT`, `QUEUE_DEPTH`, `STORE`, `DEBUG`.

No dependencies — stdlib only. Python 3.10+ required.

## Docker

```bash
docker build -t meshcore-tcp-multiplexer .
docker run --rm -p 5001:5001 meshcore-tcp-multiplexer --companion-host 192.168.1.50
```

## Architecture

Four modules, no frameworks:

| File | Responsibility |
|---|---|
| `main.py` | CLI parsing, env-var defaults, wires `MessageStore` + `MeshCoreMux`, calls `asyncio.run()` |
| `mux.py` | `MeshCoreMux` — all multiplexer logic (see below) |
| `framing.py` | `read_frame()` — byte-scans for start byte, reads 2-byte LE length, validates, recurses on bad frames |
| `store.py` | `MessageStore` — async SQLite via `asyncio.to_thread`; parameterized queries only |

### MeshCoreMux data flow

```
Companion TCP ──read──► _companion_read_loop ──► _broadcast() ──► all _clients (parallel drain)
                                                 └──► MessageStore.store() (if STORABLE_TYPES)

_clients ──read──► _handle_client ──► _write_queue ──► _companion_write_loop ──► Companion TCP
                        └──► SYNC_NEXT_MESSAGE detected ──► _replay_to_client()
```

- `_companion_loop` reconnects with exponential backoff (1 s → 30 s)
- `_write_queue` is bounded (`--queue-depth`, default 256); overflow drops oldest frame
- `_clients` is a `set[asyncio.StreamWriter]`; dead writers removed in `_broadcast` and `_handle_client`

### Store & forward

Persists packet types 7, 8, 16, 17 (`CONTACT_MSG_RECV` / `CHANNEL_MSG_RECV`, legacy + v3).

Watermark is per client IP (`clients` table). On first `SYNC_NEXT_MESSAGE` from a client, `_replay_to_client` calls `load_since(client_ip)` then `update_client(client_ip)`. Replay only fires once per connection (`synced` flag).

SQLite uses WAL mode + `asyncio.to_thread` to avoid blocking the event loop. Each sync helper opens its own connection.

### Binary frame format

```
[START 1B][LENGTH 2B little-endian][PAYLOAD 0-300B]
```

- `0x3E` companion → client
- `0x3C` client → companion

`read_frame` discards junk bytes before the start byte (device debug output). `MAX_PAYLOAD = 300`.
