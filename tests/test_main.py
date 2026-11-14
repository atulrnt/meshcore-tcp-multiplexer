import os
import sys
import unittest
from unittest.mock import patch


class TestEnvBool(unittest.TestCase):
    def _call(self, name: str) -> bool:
        import main

        return main._env_bool(name)

    def test_truthy_values(self):
        for val in ("1", "true", "yes", "TRUE", "YES", "True"):
            with patch.dict(os.environ, {"_MUX_TEST_FLAG": val}):
                self.assertTrue(
                    self._call("_MUX_TEST_FLAG"), f"expected True for {val!r}"
                )

    def test_falsy_values(self):
        for val in ("0", "false", "no", "", "off", "False"):
            with patch.dict(os.environ, {"_MUX_TEST_FLAG": val}):
                self.assertFalse(
                    self._call("_MUX_TEST_FLAG"), f"expected False for {val!r}"
                )

    def test_missing_env_var(self):
        env = {k: v for k, v in os.environ.items() if k != "_MUX_TEST_FLAG"}
        with patch.dict(os.environ, env, clear=True):
            self.assertFalse(self._call("_MUX_TEST_FLAG"))


class TestArgParsing(unittest.TestCase):
    def test_mqtt_refresh_interval_passed_in_seconds(self):
        import main

        for options, minutes in (([], 5), (["--telemetry-refresh", "7"], 7)):
            with self.subTest(minutes=minutes), patch.dict(
                os.environ, {}, clear=True
            ), patch(
                "sys.argv", ["main.py", "--mqtt-host", "broker.invalid"] + options
            ), patch(
                "main.MqttPublisher"
            ) as publisher, patch(
                "asyncio.run", side_effect=lambda c: c.close()
            ):
                main.main()
                self.assertEqual(
                    publisher.call_args.kwargs["telemetry_refresh"], minutes * 60
                )

    def test_help_exits_zero(self):
        with patch("sys.argv", ["main.py", "--help"]):
            with self.assertRaises(SystemExit) as ctx:
                import main

                main.main()
        self.assertEqual(ctx.exception.code, 0)

    def test_invalid_pubkey_exits_nonzero(self):
        with patch(
            "sys.argv",
            [
                "main.py",
                "--companion-host",
                "localhost",
                "--save-telemetry",
                "NOTAHEXKEY",
            ],
        ):
            with self.assertRaises(SystemExit) as ctx:
                import main

                main.main()
        self.assertNotEqual(ctx.exception.code, 0)

    def test_valid_pubkey_accepted(self):
        pubkey = "ab" * 32  # 64 hex chars
        with patch(
            "sys.argv",
            ["main.py", "--companion-host", "localhost", "--save-telemetry", pubkey],
        ):
            with patch("asyncio.run", side_effect=lambda c: c.close()):
                import main

                main.main()  # must not raise

    def test_env_var_companion_host(self):
        with patch.dict(os.environ, {"COMPANION_HOST": "10.0.0.1"}):
            with patch("sys.argv", ["main.py"]):
                with patch("asyncio.run", side_effect=lambda c: c.close()) as mock_run:
                    import main

                    main.main()
                    self.assertTrue(mock_run.called)

    def test_store_flag_creates_message_store(self):
        import tempfile

        fd, db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(db_path)
        try:
            with patch(
                "sys.argv",
                ["main.py", "--companion-host", "localhost", "--store", db_path],
            ):
                with patch("asyncio.run", side_effect=lambda c: c.close()):
                    import main

                    main.main()
            # MessageStore.__init__ calls _setup which creates the file
            self.assertTrue(os.path.isfile(db_path))
        finally:
            if os.path.isfile(db_path):
                os.unlink(db_path)
