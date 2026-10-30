import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("startup_health", ROOT / "scripts/deploy/startup_health.py")
health = importlib.util.module_from_spec(spec)
spec.loader.exec_module(health)


def response(url):
    if url.endswith("18080/health") or url.endswith("/voice_assistant/health"):
        return b'{"status":"ok"}'
    if url.endswith("18000/"):
        return b"Server is running\n"
    return b"<!doctype html><html>voice</html>"


class StartupHealthTests(unittest.TestCase):
    def test_wakeup_mode_does_not_require_web_services(self):
        with patch.object(health, "http_get", side_effect=response) as get, patch.object(health, "headless_error", return_value=None):
            self.assertEqual(health.check_services("wakeup"), [])
        self.assertFalse(any("18007" in call.args[0] for call in get.call_args_list))

    def test_web_mode_does_not_require_headless_client(self):
        with patch.object(health, "http_get", side_effect=response), patch.object(health, "headless_error") as headless:
            self.assertEqual(health.check_services("web"), [])
        headless.assert_not_called()

    def test_open_model_port_with_unhealthy_response_is_not_ready(self):
        def get(url):
            return b'{"status":"not_ready"}' if "18080" in url else response(url)
        with patch.object(health, "http_get", side_effect=get):
            self.assertEqual([key for key, _ in health.check_services("web")], ["rkllm"])

    def test_unresponsive_voice_endpoint_is_not_ready(self):
        def get(url):
            if "18000" in url:
                raise TimeoutError("event loop unavailable")
            return response(url)
        with patch.object(health, "http_get", side_effect=get):
            self.assertEqual([key for key, _ in health.check_services("web")], ["voice"])

    def test_static_page_does_not_hide_failed_api_proxy(self):
        def get(url):
            return b'{"status":"failed"}' if url.endswith("/voice_assistant/health") else response(url)
        with patch.object(health, "http_get", side_effect=get):
            self.assertEqual([key for key, _ in health.check_services("web")], ["web"])

    def test_vehicle_check_accepts_lan_bind_without_opening_connection(self):
        listing = subprocess.CompletedProcess([], 0, "LISTEN 0 128 192.168.0.201:9000 0.0.0.0:*\n", "")
        with patch.object(health, "http_get", side_effect=response), patch.object(health.subprocess, "run", return_value=listing) as run:
            self.assertEqual(health.check_services("web", vehicle_enabled=True), [])
        self.assertEqual(run.call_args.args[0], ["ss", "-H", "-ltn"])

    def test_disabled_vehicle_service_is_not_checked(self):
        with patch.object(health, "http_get", side_effect=response), patch.object(health.subprocess, "run") as run:
            self.assertEqual(health.check_services("web"), [])
        run.assert_not_called()

    def test_enabled_vehicle_entrance_missing_is_anomaly(self):
        listing = subprocess.CompletedProcess([], 0, "", "")
        with patch.object(health, "http_get", side_effect=response), patch.object(health.subprocess, "run", return_value=listing):
            self.assertEqual([key for key, _ in health.check_services("web", True)], ["vehicle_control"])

    def test_hybrid_requires_live_capture_and_server_connection(self):
        with patch.object(health, "http_get", side_effect=response), patch.object(health, "headless_error", return_value="capture unavailable"):
            self.assertEqual([key for key, _ in health.check_services("hybrid")], ["headless"])

    def status(self, **changes):
        value = dict(pid=123, updated_monotonic=100.0, last_audio_monotonic=99.0,
                     capture_alive=True, connected=True, server_ready=True, state="wait_wake")
        value.update(changes)
        return value

    def check_status(self, value, alive=True):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            path.write_text(json.dumps(value), encoding="utf-8")
            with patch.object(health, "process_matches", return_value=alive):
                return health.headless_error(path, now=101.0)

    def test_silent_pcm_is_valid_capture_not_a_disconnected_microphone(self):
        self.assertIsNone(self.check_status(self.status()))

    def test_stale_process_and_stale_heartbeat_are_rejected(self):
        self.assertIsNotNone(self.check_status(self.status(), alive=False))
        self.assertIsNotNone(self.check_status(self.status(updated_monotonic=80)))

    def test_live_process_without_audio_or_websocket_is_rejected(self):
        for update in [dict(capture_alive=False), dict(last_audio_monotonic=80), dict(connected=False), dict(server_ready=False)]:
            with self.subTest(update=update):
                self.assertIsNotNone(self.check_status(self.status(**update)))

    def test_malformed_status_and_wrong_boot_timestamp_are_rejected(self):
        for value in [None, [], {}, self.status(updated_monotonic=1000)]:
            with self.subTest(value=value):
                self.assertIsNotNone(self.check_status(value))
