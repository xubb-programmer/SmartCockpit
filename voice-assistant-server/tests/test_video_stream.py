import importlib.util
import sys
import types
import unittest
from pathlib import Path


def load_video_stream_module():
    try:
        import aiohttp  # noqa: F401
    except ModuleNotFoundError:
        aiohttp = types.ModuleType("aiohttp")
        aiohttp.WSMsgType = types.SimpleNamespace(TEXT="TEXT")
        aiohttp.web = types.SimpleNamespace()
        sys.modules["aiohttp"] = aiohttp

    module_path = Path(__file__).parents[1] / "web" / "video_stream.py"
    spec = importlib.util.spec_from_file_location("video_stream_under_test", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


video_stream = load_video_stream_module()


class StreamPlanTests(unittest.TestCase):
    def test_1080p_h264_is_reduced_to_smooth_profile(self):
        plan = video_stream.build_stream_plan({
            "codec_name": "h264",
            "profile": "High",
            "level": 40,
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "60000/1001",
            "r_frame_rate": "60/1",
        })

        self.assertTrue(plan["transcode"])
        self.assertEqual((plan["output_width"], plan["output_height"]), (1280, 720))
        self.assertEqual(plan["output_fps"], 30.0)
        self.assertEqual(plan["codec"], "avc1.42E01F")

    def test_compatible_720p_h264_uses_direct_copy(self):
        plan = video_stream.build_stream_plan({
            "codec_name": "h264",
            "profile": "Main",
            "level": 31,
            "width": 1280,
            "height": 720,
            "avg_frame_rate": "30000/1001",
            "r_frame_rate": "30/1",
        })

        self.assertFalse(plan["transcode"])
        self.assertEqual(plan["codec"], "avc1.4D001F")
        self.assertAlmostEqual(plan["output_fps"], 29.97, places=2)

    def test_non_h264_source_is_transcoded(self):
        plan = video_stream.build_stream_plan({
            "codec_name": "hevc",
            "profile": "Main",
            "level": 120,
            "width": 1280,
            "height": 720,
            "avg_frame_rate": "25/1",
        })

        self.assertTrue(plan["transcode"])
        self.assertEqual(plan["codec"], "avc1.42E01F")
        self.assertEqual(plan["output_fps"], 25.0)

    def test_720p_h264_above_30_fps_is_transcoded(self):
        plan = video_stream.build_stream_plan({
            "codec_name": "h264",
            "profile": "Main",
            "level": 40,
            "width": 1280,
            "height": 720,
            "avg_frame_rate": "60000/1001",
            "r_frame_rate": "60/1",
        })

        self.assertTrue(plan["transcode"])
        self.assertEqual((plan["output_width"], plan["output_height"]), (1280, 720))
        self.assertEqual(plan["output_fps"], 30.0)

    def test_invalid_frame_rate_falls_back_to_25_fps(self):
        plan = video_stream.build_stream_plan({
            "codec_name": "vp9",
            "width": 640,
            "height": 360,
            "avg_frame_rate": "0/0",
            "r_frame_rate": "invalid",
        })

        self.assertEqual(plan["source_fps"], 0.0)
        self.assertEqual(plan["output_fps"], 25.0)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.plan = video_stream.build_stream_plan({
            "codec_name": "h264",
            "profile": "High",
            "level": 40,
            "width": 1920,
            "height": 1080,
            "avg_frame_rate": "30/1",
        })
        self.session = video_stream.VideoSession(None)

    def test_rtsp_command_uses_tcp_jitter_control(self):
        command = self.session._command("rtsp://camera/live", self.plan)

        self.assertIn("-rtsp_transport", command)
        self.assertIn("-reorder_queue_size", command)
        self.assertIn("-max_delay", command)
        self.assertNotIn("-rtmp_buffer", command)

    def test_rtmp_command_uses_live_low_latency_buffer(self):
        command = self.session._command("rtmp://camera/live/stream", self.plan)

        self.assertIn("-rtmp_live", command)
        self.assertIn("-rtmp_buffer", command)
        self.assertNotIn("-rtsp_transport", command)

    def test_transcode_command_caps_resolution_fps_and_fragment_size(self):
        command = self.session._command("rtsp://camera/live", self.plan)

        self.assertIn("scale=1280:720:flags=bilinear,fps=30", command)
        self.assertEqual(command[command.index("-c:v") + 1], "libx264")
        self.assertEqual(command[command.index("-g") + 1], "15")
        self.assertEqual(command[command.index("-crf") + 1], "23")
        self.assertEqual(command[command.index("-maxrate") + 1], "2400k")
        self.assertEqual(command[command.index("-frag_duration") + 1], "150000")
        self.assertEqual(command[-3:], ["-f", "mp4", "pipe:1"])

    def test_direct_copy_does_not_enable_encoder(self):
        direct_plan = video_stream.build_stream_plan({
            "codec_name": "h264",
            "profile": "Main",
            "level": 31,
            "width": 1280,
            "height": 720,
            "avg_frame_rate": "30/1",
        })
        command = self.session._command("rtsp://camera/live", direct_plan)

        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertNotIn("-vf", command)


class UrlTests(unittest.TestCase):
    def test_url_validation_and_password_masking(self):
        url = "rtsp://operator:secret@192.168.10.130:554/live/stream"

        self.assertEqual(video_stream.validate_stream_url(url), url)
        self.assertEqual(
            video_stream.masked_stream_url(url),
            "rtsp://operator:***@192.168.10.130:554/live/stream",
        )

    def test_http_url_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "仅支持 RTSP 或 RTMP"):
            video_stream.validate_stream_url("http://camera/live")


if __name__ == "__main__":
    unittest.main()
