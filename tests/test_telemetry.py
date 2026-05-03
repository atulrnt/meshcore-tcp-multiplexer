import csv
import json
import os
import tempfile
import unittest

from telemetry import MqttPublisher, append_row, parse_lpp


def _signed_be(value: int, size: int) -> bytes:
    return value.to_bytes(size, "big", signed=True)


class TestParseLpp(unittest.TestCase):
    def test_temperature_positive(self):
        # ch=0, type=0x67, 250 big-endian signed → 25.0°C
        data = bytes([0x00, 0x67]) + _signed_be(250, 2)
        result = parse_lpp(data)
        self.assertAlmostEqual(result["temperature_c_ch0"], 25.0)

    def test_temperature_negative(self):
        # -10.0°C → encoded as -100
        data = bytes([0x00, 0x67]) + _signed_be(-100, 2)
        result = parse_lpp(data)
        self.assertAlmostEqual(result["temperature_c_ch0"], -10.0)

    def test_battery_voltage(self):
        # ch=1, type=0x74, 370 → 3.70V
        data = bytes([0x01, 0x74, 0x01, 0x72])
        result = parse_lpp(data)
        self.assertAlmostEqual(result["battery_v_ch1"], 3.70)

    def test_humidity(self):
        # ch=0, type=0x68, 160 → 80.0%
        data = bytes([0x00, 0x68, 160])
        result = parse_lpp(data)
        self.assertAlmostEqual(result["humidity_pct_ch0"], 80.0)

    def test_pressure(self):
        # ch=0, type=0x73, 10132 big-endian → 1013.2 hPa
        data = bytes([0x00, 0x73]) + (10132).to_bytes(2, "big")
        result = parse_lpp(data)
        self.assertAlmostEqual(result["pressure_hpa_ch0"], 1013.2, places=1)

    def test_digital_input(self):
        data = bytes([0x00, 0x00, 0x01])
        result = parse_lpp(data)
        self.assertEqual(result["digital_in_ch0"], 1)

    def test_analog_input(self):
        # ch=0, type=0x02, 100 big-endian signed → 1.00V
        data = bytes([0x00, 0x02]) + _signed_be(100, 2)
        result = parse_lpp(data)
        self.assertAlmostEqual(result["analog_in_v_ch0"], 1.00)

    def test_multiple_fields(self):
        temp = bytes([0x00, 0x67]) + _signed_be(200, 2)
        hum = bytes([0x00, 0x68, 100])
        result = parse_lpp(temp + hum)
        self.assertIn("temperature_c_ch0", result)
        self.assertIn("humidity_pct_ch0", result)
        self.assertAlmostEqual(result["temperature_c_ch0"], 20.0)
        self.assertAlmostEqual(result["humidity_pct_ch0"], 50.0)

    def test_empty_data(self):
        self.assertEqual(parse_lpp(b""), {})

    def test_unknown_lpp_type_stored_as_raw(self):
        data = bytes([0x00, 0xFF, 0x01, 0x02])
        result = parse_lpp(data)
        self.assertIn("lpp_raw", result)

    def test_truncated_data_skips_field(self):
        # temperature needs 2 bytes but only 1 after header
        data = bytes([0x00, 0x67, 0xAA])
        result = parse_lpp(data)
        self.assertNotIn("temperature_c_ch0", result)

    def test_channel_index_in_key(self):
        data = bytes([0x05, 0x67]) + _signed_be(150, 2)
        result = parse_lpp(data)
        self.assertIn("temperature_c_ch5", result)

    def test_alt_battery_type(self):
        # type=0x86 also maps to battery_v
        data = bytes([0x00, 0x86, 0x01, 0x5E])
        result = parse_lpp(data)
        self.assertIn("battery_v_ch0", result)


class TestAppendRow(unittest.TestCase):
    def setUp(self):
        fd, self.csv_path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        os.unlink(self.csv_path)

    def tearDown(self):
        if os.path.isfile(self.csv_path):
            os.unlink(self.csv_path)

    def test_creates_new_file_with_header(self):
        append_row(self.csv_path, 1000.0, "aabbccdd", {"temperature_c_ch0": 25.0})
        self.assertTrue(os.path.isfile(self.csv_path))
        with open(self.csv_path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pubkey"], "aabbccdd")
        self.assertAlmostEqual(float(rows[0]["temperature_c_ch0"]), 25.0)

    def test_timestamp_stored(self):
        append_row(self.csv_path, 1234567890.0, "aa", {"battery_v_ch0": 3.7})
        with open(self.csv_path) as f:
            rows = list(csv.DictReader(f))
        self.assertAlmostEqual(float(rows[0]["timestamp"]), 1234567890.0)

    def test_appends_second_row(self):
        append_row(self.csv_path, 1000.0, "aa", {"temperature_c_ch0": 20.0})
        append_row(self.csv_path, 2000.0, "aa", {"temperature_c_ch0": 21.0})
        with open(self.csv_path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 2)
        self.assertAlmostEqual(float(rows[1]["temperature_c_ch0"]), 21.0)

    def test_new_fields_in_later_row_are_excluded(self):
        # First row establishes header without "new_field"
        append_row(self.csv_path, 1000.0, "aa", {"temperature_c_ch0": 20.0})
        # Second row has an extra field not in header — should be silently dropped
        append_row(
            self.csv_path,
            2000.0,
            "aa",
            {"temperature_c_ch0": 21.0, "new_field": 99.0},
        )
        with open(self.csv_path) as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)
        self.assertNotIn("new_field", headers)
        self.assertEqual(len(rows), 2)

    def test_missing_field_in_later_row_gets_empty_string(self):
        append_row(
            self.csv_path,
            1000.0,
            "aa",
            {"temperature_c_ch0": 20.0, "humidity_pct_ch0": 60.0},
        )
        # Second row missing humidity
        append_row(self.csv_path, 2000.0, "aa", {"temperature_c_ch0": 21.0})
        with open(self.csv_path) as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[1]["humidity_pct_ch0"], "")


class FakeMqttClient:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


class TestMqttPublisher(unittest.TestCase):
    def test_publish_telemetry_uses_single_json_topic(self):
        publisher = MqttPublisher.__new__(MqttPublisher)
        publisher._client = FakeMqttClient()
        publisher._discovered = set()

        publisher.publish_telemetry(
            "aabbccdd",
            {"temperature_c_ch0": 20.2, "battery_v_ch1": 4.11},
        )

        state_messages = [
            msg
            for msg in publisher._client.published
            if msg[0] == "meshcore/aabbccdd/telemetry"
        ]
        self.assertEqual(len(state_messages), 1)
        topic, payload, qos, retain = state_messages[0]
        self.assertEqual(topic, "meshcore/aabbccdd/telemetry")
        self.assertEqual(
            json.loads(payload),
            {"temperature_c_ch0": 20.2, "battery_v_ch1": 4.11},
        )
        self.assertEqual(qos, 1)
        self.assertTrue(retain)

    def test_discovery_reads_field_from_json_topic(self):
        publisher = MqttPublisher.__new__(MqttPublisher)
        publisher._client = FakeMqttClient()

        publisher._publish_discovery("aabbccdd", "temperature_c_ch0")

        topic, payload, qos, retain = publisher._client.published[0]
        self.assertEqual(topic, "homeassistant/sensor/meshcore_aabbccdd_temperature_c_ch0/config")
        self.assertEqual(qos, 1)
        self.assertTrue(retain)
        discovery = json.loads(payload)
        self.assertEqual(discovery["state_topic"], "meshcore/aabbccdd/telemetry")
        self.assertEqual(discovery["value_template"], "{{ value_json.temperature_c_ch0 }}")
