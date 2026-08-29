import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).parents[1]
WEB_ROOT = SERVER_ROOT / "web"


def load_https_gateway_module():
    try:
        import aiohttp
    except ModuleNotFoundError:
        aiohttp = types.ModuleType("aiohttp")
        sys.modules["aiohttp"] = aiohttp
    if not hasattr(aiohttp, "ClientSession"):
        aiohttp.ClientSession = object
    if not hasattr(aiohttp, "ClientTimeout"):
        aiohttp.ClientTimeout = object
    if not hasattr(aiohttp, "WSMsgType"):
        aiohttp.WSMsgType = types.SimpleNamespace()
    if not hasattr(aiohttp, "web"):
        aiohttp.web = types.SimpleNamespace()

    sys.path.insert(0, str(WEB_ROOT))
    try:
        module_path = WEB_ROOT / "https_gateway.py"
        spec = importlib.util.spec_from_file_location(
            "https_gateway_under_test",
            module_path,
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(WEB_ROOT))


https_gateway = load_https_gateway_module()


class VisionFrameGatewayTests(unittest.TestCase):
    def test_valid_jpeg_is_accepted(self):
        frame = b"\xff\xd8\xff\xe0frame-data\xff\xd9"

        https_gateway.validate_vision_frame("image/jpeg", frame)

    def test_non_jpeg_and_invalid_payload_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "JPEG"):
            https_gateway.validate_vision_frame("image/png", b"png")
        with self.assertRaisesRegex(ValueError, "格式无效"):
            https_gateway.validate_vision_frame(
                "image/jpeg",
                b"not-a-jpeg",
            )

    def test_oversized_frame_is_rejected(self):
        oversized = (
            b"\xff\xd8\xff"
            + b"0" * https_gateway.MAX_VISION_FRAME_BYTES
            + b"\xff\xd9"
        )

        with self.assertRaisesRegex(ValueError, "4 MB"):
            https_gateway.validate_vision_frame("image/jpeg", oversized)

    def test_atomic_write_replaces_previous_frame(self):
        first = b"\xff\xd8\xff\xe0first\xff\xd9"
        second = b"\xff\xd8\xff\xe0second\xff\xd9"
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "latest.jpg"

            https_gateway.write_vision_frame_atomic(first, str(destination))
            https_gateway.write_vision_frame_atomic(second, str(destination))

            self.assertEqual(destination.read_bytes(), second)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])


class VisionFrameFrontendContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.controller_source = (
            WEB_ROOT / "js" / "ui" / "video-controller.js"
        ).read_text(encoding="utf-8")
        cls.vision_source = (
            SERVER_ROOT / "plugins_func" / "functions" / "vision_analyze.py"
        ).read_text(encoding="utf-8")
        cls.http_server_source = (
            WEB_ROOT / "http_server.py"
        ).read_text(encoding="utf-8")
        cls.intent_source = (
            SERVER_ROOT / "core" / "handle" / "intentHandler.py"
        ).read_text(encoding="utf-8")
        cls.rknn_provider_source = (
            SERVER_ROOT / "core" / "providers" / "vllm" / "rknn.py"
        ).read_text(encoding="utf-8")

    def test_frontend_uploads_a_jpeg_every_second(self):
        self.assertIn("VISION_FRAME_UPLOAD_INTERVAL_MS = 1000", self.controller_source)
        self.assertIn("toBlob(", self.controller_source)
        self.assertIn("fetch('/video/frame'", self.controller_source)
        self.assertIn("'image/jpeg'", self.controller_source)

    def test_vision_analyzer_rejects_stale_video_frames(self):
        self.assertIn('max_image_age_seconds", 5.0', self.vision_source)
        self.assertIn("当前视频画面未更新", self.vision_source)

    def test_http_server_supports_same_frame_upload_endpoint(self):
        self.assertIn('urlsplit(self.path).path != "/video/frame"', self.http_server_source)
        self.assertIn("validate_vision_frame", self.http_server_source)
        self.assertIn("write_vision_frame_atomic", self.http_server_source)

    def test_clear_vision_commands_skip_text_llm_and_limit_output(self):
        self.assertIn("check_direct_vision_analyze", self.intent_source)
        self.assertIn("识别到明确视觉分析指令，跳过文本模型", self.intent_source)
        self.assertIn('"name": "vision_analyze"', self.intent_source)
        self.assertIn("config.get(\"max_tokens\", 48)", self.rknn_provider_source)
        self.assertIn('"max_tokens": max(16', self.rknn_provider_source)
        self.assertIn("def abort(self)", self.rknn_provider_source)

    def test_vision_abort_and_reconnect_contracts_are_present(self):
        llm_server_source = (
            SERVER_ROOT.parent / "rknn-server" / "llm_server.py"
        ).read_text(encoding="utf-8")
        video_stream_source = (
            WEB_ROOT / "video_stream.py"
        ).read_text(encoding="utf-8")
        player_source = (
            WEB_ROOT / "js" / "core" / "video" / "player.js"
        ).read_text(encoding="utf-8")

        self.assertIn("@app.route('/rkllm_abort'", llm_server_source)
        self.assertIn("_active_call_cancelled", llm_server_source)
        self.assertIn("MAX_RECONNECT_ATTEMPTS", video_stream_source)
        self.assertIn("stream-reconnected", video_stream_source)
        self.assertIn("message.type === 'reconnecting'", player_source)


if __name__ == "__main__":
    unittest.main()
