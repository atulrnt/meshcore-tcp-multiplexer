# MeshCore TCP Multiplexer

An async TCP multiplexer that lets multiple client applications share a single [MeshCore](https://meshcore.co.uk) WiFi companion radio simultaneously.

Inspired by [do6uk/meshcore_multitcp](https://github.com/do6uk/meshcore_multitcp).

## Problem

MeshCore companion radios connected over TCP accept only one client at a time. This multiplexer sits between the companion and your clients, forwarding frames in both directions — any number of clients can connect concurrently.

```
[Client A] ─┐
[Client B] ─┤── [Multiplexer] ── [MeshCore Companion Radio]
[Client C] ─┘
```

## Features

- **Multi-client** — unlimited simultaneous TCP clients
- **Automatic reconnection** — exponential backoff (1 s → 30 s) when the companion drops
- **Correct framing** — dedicated frame parser with resync on junk bytes; safe against partial TCP reads
- **Backpressure** — bounded write queue (default 256 frames); oldest frame dropped on overflow instead of unbounded memory growth
- **Parallel broadcast** — all clients receive companion frames concurrently via `asyncio.gather`
- **Store & forward** — optional SQLite persistence (`--store FILE`); clients receive missed messages on reconnect
- **Async I/O** — single-process, no thread contention, scales to many clients
- **Zero dependencies** — stdlib only (Python 3.10+)

## Comparison with do6uk/meshcore_multitcp

| | **This project** | do6uk/meshcore_multitcp |
|---|---|---|
| Architecture | `asyncio` (single process) | `threading` (one thread/client) |
| Framing | Dedicated parser, resync on junk, partial-read safe | Single `recv(1024)` — breaks on partial reads |
| Error handling | Typed exceptions, structured cleanup | 7 bare `except:` blocks |
| Thread safety | N/A (async) | No locks on shared state |
| Backpressure | Bounded queue, drop-oldest | None |
| Type hints | 100% | ~10% |
| Store & forward | Yes (SQLite, async, no SQL injection) | Yes (SQLite, sync, f-string queries) |
| Systemd unit | Included | Included |

## Requirements

- Python 3.10+, **or** Docker
- A MeshCore companion radio with WiFi firmware and TCP enabled

## Usage

### Docker (recommended)

Pre-built multi-arch images (`linux/amd64`, `linux/arm64`) are published to the GitHub Container Registry on every push to `main` and on version tags.

```bash
# Pull and run (replace args as needed)
docker run --rm ghcr.io/atulrnt/meshcore-tcp-multiplexer \
    --companion-host 192.168.1.50 \
    --companion-port 5000 \
    --listen-port 5001

# With store-and-forward (mount a volume so the DB persists)
docker run --rm \
    -v $(pwd)/data:/data \
    -p 5001:5001 \
    ghcr.io/atulrnt/meshcore-tcp-multiplexer \
    --companion-host 192.168.1.50 \
    --listen-port 5001 \
    --store /data/messages.db
```

### From source

```bash
python main.py [OPTIONS]
```

### Options

| Flag | Default | Description |
|---|---|---|
| `--companion-host HOST` | `127.0.0.1` | IP address of the MeshCore companion |
| `--companion-port PORT` | `5000` | TCP port of the companion |
| `--listen-host HOST` | `0.0.0.0` | IP to listen on for client connections |
| `--listen-port PORT` | `5001` | TCP port to listen on |
| `--queue-depth N` | `256` | Max queued frames before oldest is dropped |
| `--store FILE` | off | Enable store-and-forward using FILE as SQLite DB |
| `--debug` | off | Enable verbose frame-level logging |

### Example

```bash
# Companion on 192.168.1.50:5000, clients connect to :5001
python main.py --companion-host 192.168.1.50 --companion-port 5000 --listen-port 5001

# With store-and-forward
python main.py --companion-host 192.168.1.50 --listen-port 5001 --store messages.db
```

## Protocol

MeshCore uses a simple binary framing protocol over TCP:

```
[START_BYTE 1B][LENGTH 2B little-endian][PAYLOAD 0-300B]
```

- `0x3E` — companion → client frames
- `0x3C` — client → companion frames

The frame parser scans for the start byte, discarding any junk (e.g. device debug output), and validates payload length before reading. Invalid frames trigger a resync rather than a crash.

## Deployment

### systemd

```ini
[Unit]
Description=MeshCore TCP Multiplexer
After=network.target

[Service]
ExecStart=/usr/bin/python3 /opt/meshcore-tcp-multiplexer/main.py \
    --companion-host 192.168.1.50 \
    --companion-port 5000 \
    --listen-port 5001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

## Architecture

```
main.py          CLI entry point — argument parsing, logging setup
mux.py           MeshCoreMux class — core multiplexer logic
framing.py       Frame parser — reads and validates MeshCore binary frames
store.py         MessageStore — async SQLite store-and-forward
```

### MeshCoreMux internals

- `_companion_loop()` — connects to companion with exponential backoff; restarts on disconnect
- `_companion_read_loop()` — reads frames from companion, stores messages if enabled, calls `_broadcast()`
- `_companion_write_loop()` — drains the write queue to the companion
- `_handle_client()` — per-client coroutine; reads client frames, triggers replay on `SYNC_NEXT_MESSAGE`, enqueues for companion
- `_replay_to_client()` — loads missed messages from store and writes to client before marking sync done
- `_broadcast()` — writes frame to all clients in parallel; removes dead clients on error

### Store & forward

When `--store FILE` is set, all private and channel messages received from the companion (packet types `CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV`, and their v3 variants) are written to a SQLite database.

When a client sends `SYNC_NEXT_MESSAGE` (the standard MeshCore command to page through unread messages), the multiplexer replays any stored messages newer than that client's last session watermark before resuming live forwarding. The watermark is keyed by client IP, so it persists across reconnections.

All DB operations run in a thread pool via `asyncio.to_thread` to avoid blocking the event loop. All queries use parameterized statements.

## License

See [LICENSE](LICENSE).
