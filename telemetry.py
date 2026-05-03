import csv
import json
import logging
import os
import re

log = logging.getLogger(__name__)

# Cayenne LPP type → (column_suffix, byte_size, decoder)
# Column name in CSV: "{suffix}_ch{channel}", e.g. "temperature_c_ch0", "battery_v_ch1"
_LPP_TYPES: dict[int, tuple[str, int, object]] = {
    0x00: ("digital_in", 1, lambda b: b[0]),
    0x01: ("digital_out", 1, lambda b: b[0]),
    0x02: ("analog_in_v", 2, lambda b: int.from_bytes(b, "big", signed=True) * 0.01),
    0x03: ("analog_out_v", 2, lambda b: int.from_bytes(b, "big", signed=True) * 0.01),
    0x65: ("illuminance_lx", 2, lambda b: int.from_bytes(b, "big")),
    0x66: ("presence", 1, lambda b: b[0]),
    0x67: ("temperature_c", 2, lambda b: int.from_bytes(b, "big", signed=True) * 0.1),
    0x68: ("humidity_pct", 1, lambda b: b[0] * 0.5),
    0x73: ("pressure_hpa", 2, lambda b: int.from_bytes(b, "big") * 0.1),
    0x74: ("battery_v", 2, lambda b: int.from_bytes(b, "big") * 0.01),
    0x86: ("battery_v", 2, lambda b: int.from_bytes(b, "big") * 0.01),
}

# field type prefix → (HA friendly name, device_class, unit, state_class)
_HA_META: dict[str, tuple[str, str | None, str | None, str | None]] = {
    "battery_v": ("Battery", "voltage", "V", "measurement"),
    "temperature_c": ("Temperature", "temperature", "°C", "measurement"),
    "humidity_pct": ("Humidity", "humidity", "%", "measurement"),
    "pressure_hpa": ("Pressure", "atmospheric_pressure", "hPa", "measurement"),
    "illuminance_lx": ("Illuminance", "illuminance", "lx", "measurement"),
    "analog_in_v": ("Analog In", "voltage", "V", "measurement"),
    "analog_out_v": ("Analog Out", "voltage", "V", "measurement"),
    "digital_in": ("Digital In", None, None, None),
    "digital_out": ("Digital Out", None, None, None),
    "presence": ("Presence", None, None, None),
}


def _field_type(field_name: str) -> str:
    return re.sub(r"_ch\d+$", "", field_name)


def parse_lpp(data: bytes) -> dict[str, float]:
    out: dict[str, float] = {}
    i = 0
    while i + 1 < len(data):
        ch = data[i]
        typ = data[i + 1]
        i += 2
        if typ not in _LPP_TYPES:
            remaining = data[i - 2 :]
            log.warning("telemetry: unknown LPP type 0x%02x at offset %d", typ, i - 2)
            out["lpp_raw"] = remaining.hex()
            break
        name, size, decode = _LPP_TYPES[typ]
        if i + size > len(data):
            log.warning("telemetry: LPP data truncated for type 0x%02x", typ)
            break
        out[f"{name}_ch{ch}"] = decode(data[i : i + size])
        i += size
    return out


def append_row(path: str, ts: float, pubkey_prefix: str, fields: dict) -> None:
    row = {"timestamp": ts, "pubkey": pubkey_prefix, **fields}
    exists = os.path.isfile(path)

    if exists:
        with open(path, "r", newline="") as f:
            existing_headers = next(csv.reader(f), [])
        new_keys = [k for k in row if k not in existing_headers]
        if new_keys:
            log.debug(
                "telemetry: new LPP fields not in CSV header, dropping: %s", new_keys
            )
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(
                f, fieldnames=existing_headers, extrasaction="ignore", restval=""
            )
            w.writerow(row)
    else:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            w.writeheader()
            w.writerow(row)


class MqttPublisher:
    def __init__(
        self,
        host: str,
        port: int = 1883,
        user: str | None = None,
        password: str | None = None,
    ) -> None:
        import paho.mqtt.client as mqtt

        self._host = host
        self._port = port
        self._discovered: set[str] = set()

        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if user:
            self._client.username_pw_set(user, password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect

    def connect(self) -> None:
        self._client.connect_async(self._host, self._port)
        self._client.loop_start()
        log.info("mqtt: connecting to %s:%d", self._host, self._port)

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    def publish_telemetry(self, pubkey_prefix: str, fields: dict) -> None:
        for field_name in fields:
            disc_key = f"{pubkey_prefix}/{field_name}"
            if disc_key not in self._discovered:
                self._publish_discovery(pubkey_prefix, field_name)
                self._discovered.add(disc_key)
        state_topic = f"meshcore/{pubkey_prefix}/telemetry"
        self._client.publish(state_topic, json.dumps(fields), qos=1, retain=True)
        log.debug("mqtt: published %s = %r", state_topic, fields)

    def _publish_discovery(self, pubkey_prefix: str, field_name: str) -> None:
        ft = _field_type(field_name)
        human_name, device_class, unit, state_class = _HA_META.get(
            ft, (field_name, None, None, None)
        )

            unique_id = f"meshcore_{pubkey_prefix}_{field_name}"
        state_topic = f"meshcore/{pubkey_prefix}/telemetry"
        disc_topic = f"homeassistant/sensor/{unique_id}/config"

        payload: dict = {
            "name": human_name,
            "unique_id": unique_id,
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{field_name} }}}}",
            "device": {
                "identifiers": [f"meshcore_{pubkey_prefix}"],
                "name": f"MeshCore Repeater {pubkey_prefix}",
                "manufacturer": "MeshCore",
            },
        }
        if device_class:
            payload["device_class"] = device_class
        if unit:
            payload["unit_of_measurement"] = unit
        if state_class:
            payload["state_class"] = state_class

        self._client.publish(disc_topic, json.dumps(payload), qos=1, retain=True)
        log.debug("mqtt: published HA discovery for %s", field_name)

    def _on_connect(self, client, userdata, connect_flags, reason_code, properties):
        if reason_code == 0:
            log.info("mqtt: connected to broker at %s:%d", self._host, self._port)
            self._discovered.clear()  # re-announce all sensors after (re)connect
        else:
            log.warning("mqtt: broker rejected connection, reason=%s", reason_code)

    def _on_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties
    ):
        if reason_code != 0:
            log.warning(
                "mqtt: disconnected unexpectedly (reason=%s), will retry", reason_code
            )
