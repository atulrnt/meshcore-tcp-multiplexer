import csv
import logging
import os

log = logging.getLogger(__name__)

# Cayenne LPP type → (column_suffix, byte_size, decoder)
# Column name in CSV: "{suffix}_ch{channel}", e.g. "temperature_c_ch0", "battery_v_ch1"
_LPP_TYPES: dict[int, tuple[str, int, object]] = {
    0x00: ("digital_in",    1, lambda b: b[0]),
    0x01: ("digital_out",   1, lambda b: b[0]),
    0x02: ("analog_in_v",   2, lambda b: int.from_bytes(b, "big", signed=True) * 0.01),
    0x03: ("analog_out_v",  2, lambda b: int.from_bytes(b, "big", signed=True) * 0.01),
    0x65: ("illuminance_lx", 2, lambda b: int.from_bytes(b, "big")),
    0x66: ("presence",      1, lambda b: b[0]),
    0x67: ("temperature_c", 2, lambda b: int.from_bytes(b, "big", signed=True) * 0.1),
    0x68: ("humidity_pct",  1, lambda b: b[0] * 0.5),
    0x73: ("pressure_hpa",  2, lambda b: int.from_bytes(b, "big") * 0.1),
    0x74: ("battery_v",     2, lambda b: int.from_bytes(b, "big") * 0.01),
    0x86: ("battery_v",     2, lambda b: int.from_bytes(b, "big") * 0.01),
}


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
            log.debug("telemetry: new LPP fields not in CSV header, dropping: %s", new_keys)
        with open(path, "a", newline="") as f:
            w = csv.DictWriter(f, fieldnames=existing_headers, extrasaction="ignore", restval="")
            w.writerow(row)
    else:
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(row.keys()))
            w.writeheader()
            w.writerow(row)
