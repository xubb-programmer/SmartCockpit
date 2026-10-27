import http.client
import importlib.util
import sys
import tempfile
import threading
import unittest
from functools import partial
from pathlib import Path


SERVER_ROOT = Path(__file__).parents[1]
WEB_ROOT = SERVER_ROOT / "web"


def load_http_server_module():
    sys.path.insert(0, str(WEB_ROOT))
    try:
        spec = importlib.util.spec_from_file_location(
            "http_server_under_test",
            WEB_ROOT / "http_server.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(WEB_ROOT))


http_server = load_http_server_module()


class FileDownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.web_dir = root / "web"
        self.download_dir = root / "downloads"
        self.web_dir.mkdir()
        self.download_dir.mkdir()
        handler = partial(
            http_server.VoiceAssistantHttpHandler,
            directory=str(self.web_dir),
            download_dir=str(self.download_dir),
        )
        self.server = http_server.http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0),
            handler,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    def request(self, method, path, headers=None):
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=3,
        )
        connection.request(method, path, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.getheaders()), response.read()
        connection.close()
        return result

    def test_empty_directory_returns_ie6_compatible_message(self):
        status, headers, body = self.request("GET", "/download/")

        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(
            headers["Cache-Control"],
            "no-store, no-cache, must-revalidate, max-age=0",
        )
        self.assertEqual(headers["Pragma"], "no-cache")
        self.assertEqual(headers["Expires"], "0")
        self.assertIn("暂无可下载文件".encode("utf-8"), body)

    def test_single_file_is_downloaded_without_filename_in_url(self):
        (self.download_dir / "ZTL_Client.zip").write_bytes(b"0123456789")

        status, headers, body = self.request("GET", "/download/")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"0123456789")
        self.assertEqual(headers["Accept-Ranges"], "bytes")
        self.assertEqual(
            headers["Content-Disposition"],
            'attachment; filename="ZTL_Client.zip"',
        )

    def test_multiple_files_return_download_list(self):
        (self.download_dir / "client_a.zip").write_bytes(b"a")
        (self.download_dir / "client_b.zip").write_bytes(b"b")

        status, _, body = self.request("GET", "/download/")

        self.assertEqual(status, 200)
        self.assertIn(b'/download/client_a.zip', body)
        self.assertIn(b'/download/client_b.zip', body)

    def test_range_and_head_are_supported(self):
        (self.download_dir / "client.zip").write_bytes(b"0123456789")

        status, headers, body = self.request(
            "GET",
            "/download/client.zip",
            {"Range": "bytes=2-5"},
        )
        head_status, head_headers, head_body = self.request(
            "HEAD",
            "/download/client.zip",
        )

        self.assertEqual(status, 206)
        self.assertEqual(headers["Content-Range"], "bytes 2-5/10")
        self.assertEqual(body, b"2345")
        self.assertEqual(head_status, 200)
        self.assertEqual(head_headers["Content-Length"], "10")
        self.assertEqual(head_body, b"")

    def test_directory_traversal_is_not_served(self):
        outside = Path(self.temp_dir.name) / "secret.txt"
        outside.write_text("secret", encoding="utf-8")

        status, _, _ = self.request("GET", "/download/..%2Fsecret.txt")

        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
