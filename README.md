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
- **Repeater telemetry** — periodic telemetry polling from a repeater node; results written to CSV and/or published to MQTT for Home Assistant
- **Async I/O** — single-process, no thread contention, scales to many clients

## Requirements

- Python 3.10+, **or** Docker
- A MeshCore companion radio with WiFi firmware and TCP enabled
- `paho-mqtt>=2.0` (only needed when using `--mqtt-host`)

## Development

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Tests cover framing, store-and-forward, mux routing and shutdown, telemetry parsing, MQTT discovery, and CLI argument handling. Integration tests use real in-process TCP.

### Code style

```bash
black .
```

[Black](https://github.com/psf/black) is enforced at 88-char line length (the default).

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

# With telemetry pushed to Home Assistant via MQTT
docker run --rm \
    -p 5001:5001 \
    ghcr.io/atulrnt/meshcore-tcp-multiplexer \
    --companion-host 192.168.1.50 \
    --save-telemetry <64-hex-pubkey> \
    --mqtt-host 192.168.1.10 \
    --mqtt-user meshcore \
    --mqtt-pass secret
```

### From source

```bash
pip install -r requirements.txt
python main.py [OPTIONS]
```

### Options

| Flag | Env var | Default | Description |
|---|---|---|---|
| `--companion-host HOST` | `COMPANION_HOST` | `127.0.0.1` | IP address of the MeshCore companion |
| `--companion-port PORT` | `COMPANION_PORT` | `5000` | TCP port of the companion |
| `--listen-host HOST` | `LISTEN_HOST` | `0.0.0.0` | IP to listen on for client connections |
| `--listen-port PORT` | `LISTEN_PORT` | `5001` | TCP port to listen on |
| `--queue-depth N` | `QUEUE_DEPTH` | `256` | Max queued frames before oldest is dropped |
| `--store FILE` | `STORE` | off | Enable store-and-forward using FILE as SQLite DB |
| `--beacon SECONDS` | `BEACON` | off | Send a channel message every SECONDS seconds |
| `--beacon-channel INDEX` | `BEACON_CHANNEL` | `0` | Channel slot (0–7) to beacon on; 0 = public |
| `--save-telemetry PUBKEY` | `SAVE_TELEMETRY` | off | Poll telemetry from the repeater with this public key (64 hex chars) |
| `--telemetry-refresh MINUTES` | `TELEMETRY_REFRESH` | `5` | How often to request telemetry, in minutes |
| `--telemetry-csv FILE` | `TELEMETRY_CSV` | off | Append telemetry rows to FILE in CSV format |
| `--mqtt-host HOST` | `MQTT_HOST` | off | MQTT broker hostname; enables publishing telemetry to MQTT |
| `--mqtt-port PORT` | `MQTT_PORT` | `1883` | MQTT broker port |
| `--mqtt-user USER` | `MQTT_USER` | — | MQTT username |
| `--mqtt-pass PASS` | `MQTT_PASS` | — | MQTT password |
| `--debug` | `DEBUG` | off | Enable verbose frame-level logging |

### Example

```bash
# Companion on 192.168.1.50:5000, clients connect to :5001
python main.py --companion-host 192.168.1.50 --companion-port 5000 --listen-port 5001

# With store-and-forward
python main.py --companion-host 192.168.1.50 --listen-port 5001 --store messages.db

# Poll repeater telemetry every 10 minutes, push to MQTT
python main.py \
    --companion-host 192.168.1.50 \
    --save-telemetry a1b2c3d4e5f6...  \
    --telemetry-refresh 10 \
    --mqtt-host 192.168.1.10 \
    --mqtt-user meshcore \
    --mqtt-pass secret
```

## Repeater telemetry

When `--save-telemetry` is set, the multiplexer periodically sends a telemetry request to the specified repeater (identified by its 32-byte public key, hex-encoded). The repeater's response contains [Cayenne LPP](https://docs.mydevices.com/docs/lorawan/cayenne-lpp) sensor data — typically battery voltage and temperature.

Results are stored in whichever outputs are configured:
- **CSV** — when `--telemetry-csv FILE` is set, one row appended per fetch
- **MQTT** — when `--mqtt-host` is set, each field published as a separate sensor (see below)

Telemetry responses (`0x8B`) are processed locally **and forwarded unchanged to all TCP clients**, including malformed responses and responses from other nodes. A CSV or MQTT output error is logged without interrupting forwarding or the companion connection; the other output is still attempted. When a polling target is configured, only responses with its six-byte public-key prefix are written to local outputs. This prefix is the identity available in the legacy response, not full-key verification.

If no matching response arrives within one refresh interval after a request is queued and the companion is connected, a warning is logged. This is a diagnostic only: requests are not retried early, and responses do not carry a request ID, so a client-triggered response from the same node also counts as received. Polling still uses `CMD_SEND_TELEMETRY_REQ` (`0x27`); binary replies (`0x8C`) are passed through without local parsing.

### Home Assistant integration

The multiplexer implements [MQTT Discovery](https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery), so entities appear automatically in Home Assistant without any YAML configuration.

#### 1. Enable MQTT in Home Assistant

Go to **Settings → Devices & Services → Add Integration** and search for **MQTT**. Enter your Mosquitto broker details:

- **Broker**: IP or hostname of your Mosquitto server (e.g. `192.168.1.10` or `homeassistant.local`)
- **Port**: `1883`
- **Username / Password**: as configured in Mosquitto

#### 2. Run the multiplexer with MQTT options

```bash
python main.py \
    --companion-host 192.168.1.50 \
    --save-telemetry <64-hex-pubkey> \
    --mqtt-host 192.168.1.10 \
    --mqtt-user meshcore \
    --mqtt-pass secret
```

#### 3. Entities appear automatically

On the first telemetry response, the multiplexer publishes retained discovery messages to `homeassistant/sensor/meshcore_<pubkey>_<field>/config`. Home Assistant picks these up and creates a **MeshCore Repeater** device with individual sensor entities:

| Sensor | Unit | HA device class |
|---|---|---|
| Battery | V | `voltage` |
| Temperature | °C | `temperature` |
| Humidity | % | `humidity` |
| Pressure | hPa | `atmospheric_pressure` |

Find the device under **Settings → Devices & Services → MQTT → Devices**.

#### MQTT topics

| Topic | Content |
|---|---|
| `homeassistant/sensor/meshcore_<pubkey>_<field>/config` | HA discovery payload (retained) |
| `meshcore/<pubkey>/telemetry` | JSON object containing the latest telemetry fields (not retained, QoS 1) |

Discovery includes `expire_after` set to **three times the configured refresh interval in seconds**. With the default five-minute interval this is 900 seconds; after that long without an update, Home Assistant marks the sensors unavailable. This describes telemetry freshness, not companion or repeater connectivity. Direct `MqttPublisher` callers may pass `telemetry_refresh` in seconds; omitting it keeps the five-minute default.

Discovery stays retained, but telemetry state is deliberately **not retained**: replaying an old retained measurement when Home Assistant reconnects would restart its expiry timer and make stale data look fresh. After Home Assistant restarts or reconnects, sensors may therefore have no current value until the next live telemetry response.

#### Upgrading from retained telemetry state

Non-retained publishing does **not** remove state already retained by the broker. As a one-time migration, remove the old retained message for each exact `meshcore/<pubkey>/telemetry` topic (using its six-byte prefix, 12 hex characters), for example with your broker's authenticated client:

```bash
mosquitto_pub -h BROKER -t 'meshcore/PUBKEY_PREFIX/telemetry' -q 1 -r -n
```

Substitute the actual broker and prefix and supply authentication through your normal secure client configuration. Do not delete the retained discovery topics. Older installations using per-field state topics under `meshcore/telemetry/<pubkey>/<field>` should clear those obsolete retained state topics too. Restart/reload the Home Assistant MQTT integration after clearing stale state if it has already consumed it. Updated discovery replaces the existing sensor config on the next live response after the multiplexer restarts; entity unique IDs are unchanged. A node that never responds again will not get updated discovery until it does, so update its discovery manually if immediate expiry is needed.

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
telemetry.py     LPP parser, CSV writer, MqttPublisher (HA Discovery)
```

### MeshCoreMux internals

- `_companion_loop()` — connects to companion with exponential backoff; restarts on disconnect
- `_companion_read_loop()` — reads frames from companion, stores messages if enabled, calls `_broadcast()`
- `_companion_write_loop()` — drains the write queue to the companion
- `_handle_client()` — per-client coroutine; reads client frames, triggers replay on `SYNC_NEXT_MESSAGE`, enqueues for companion
- `_replay_to_client()` — loads missed messages from store and writes to client before marking sync done
- `_broadcast()` — writes frame to all clients in parallel; removes dead clients on error
- `_telemetry_loop()` — sends `CMD_SEND_TELEMETRY_REQ` (0x27) periodically; waits for companion to be connected
- `_handle_telemetry_response()` — parses Cayenne LPP from `PUSH_CODE_TELEMETRY_RESPONSE` (0x8B), writes CSV, publishes MQTT

### Store & forward

When `--store FILE` is set, all private and channel messages received from the companion (packet types `CONTACT_MSG_RECV`, `CHANNEL_MSG_RECV`, and their v3 variants) are written to a SQLite database.

When a client sends `SYNC_NEXT_MESSAGE` (the standard MeshCore command to page through unread messages), the multiplexer replays any stored messages newer than that client's last session watermark before resuming live forwarding. The watermark is keyed by client IP, so it persists across reconnections.

All DB operations run in a thread pool via `asyncio.to_thread` to avoid blocking the event loop. All queries use parameterized statements.

## License

See [LICENSE](LICENSE).
