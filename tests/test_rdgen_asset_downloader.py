import importlib.util
import os
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "download_rdgen_asset.py"
SPEC = importlib.util.spec_from_file_location("download_rdgen_asset", SCRIPT_PATH)
if SPEC is None or SPEC.loader is None:
    raise ImportError(f"Could not load downloader module at {SCRIPT_PATH}")
downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(downloader)


class AssetHandler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.requests.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/get_png?filename=redirect&"):
            self.send_response(302)
            self.send_header("Location", f"https://localhost:{self.server.server_port}/collect")
            self.end_headers()
            return

        payload = b"test-image-bytes"
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, _format, *_args):
        pass


class RdgenAssetDownloaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.certificate_dir = tempfile.TemporaryDirectory()
        cls.certificate = Path(cls.certificate_dir.name) / "localhost.pem"
        cls.private_key = Path(cls.certificate_dir.name) / "localhost-key.pem"
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(cls.private_key), "-out", str(cls.certificate),
                "-days", "1", "-subj", "/CN=localhost",
                "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            ],
            check=True,
            capture_output=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.certificate_dir.cleanup()

    def setUp(self):
        AssetHandler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), AssetHandler)
        server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_tls.load_cert_chain(self.certificate, self.private_key)
        self.server.socket = server_tls.wrap_socket(self.server.socket, server_side=True)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.base_url = f"https://localhost:{self.server.server_port}"
        self.previous_ssl_cert_file = os.environ.get("SSL_CERT_FILE")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)
        if self.previous_ssl_cert_file is None:
            os.environ.pop("SSL_CERT_FILE", None)
        else:
            os.environ["SSL_CERT_FILE"] = self.previous_ssl_cert_file

    def trust_test_certificate(self):
        os.environ["SSL_CERT_FILE"] = str(self.certificate)

    def test_download_uses_bearer_token_and_verifies_tls(self):
        self.trust_test_certificate()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "icon.png"
            downloader.download_asset(
                self.base_url, "icon.png", "asset-id", output, "test-token"
            )

            self.assertEqual(output.read_bytes(), b"test-image-bytes")
        self.assertEqual(len(AssetHandler.requests), 1)
        request_path, authorization = AssetHandler.requests[0]
        self.assertIn("filename=icon.png", request_path)
        self.assertIn("uuid=asset-id", request_path)
        self.assertEqual(authorization, "Bearer test-token")

    def test_untrusted_certificate_is_rejected_before_request(self):
        os.environ.pop("SSL_CERT_FILE", None)
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "icon.png"
            with self.assertRaises(downloader.DownloadError):
                downloader.download_asset(
                    self.base_url, "icon.png", "asset-id", output, "test-token"
                )
            self.assertFalse(output.exists())
        self.assertEqual(AssetHandler.requests, [])

    def test_redirect_is_rejected_without_forwarding_bearer_token(self):
        self.trust_test_certificate()
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "icon.png"
            with self.assertRaises(downloader.DownloadError):
                downloader.download_asset(
                    self.base_url, "redirect", "asset-id", output, "test-token"
                )
        self.assertEqual(len(AssetHandler.requests), 1)
        self.assertTrue(AssetHandler.requests[0][0].startswith("/get_png?filename=redirect&"))
        self.assertEqual(AssetHandler.requests[0][1], "Bearer test-token")

    def test_rejects_http_urls_and_missing_tokens(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "icon.png"
            with self.assertRaises(downloader.DownloadError):
                downloader.download_asset(
                    self.base_url.replace("https://", "http://"),
                    "icon.png", "asset-id", output, "test-token"
                )
            with self.assertRaises(downloader.DownloadError):
                downloader.download_asset(
                    self.base_url, "icon.png", "asset-id", output, ""
                )
        self.assertEqual(AssetHandler.requests, [])

    def test_macos_icon_and_logo_steps_use_the_secure_downloader(self):
        workflow_path = SCRIPT_PATH.parents[1] / "workflows" / "generator-macos.yml"
        workflow = workflow_path.read_text(encoding="utf-8")
        icon_start = workflow.index("      - name: Magick stuff for macOS")
        logo_start = workflow.index("      - name: logo handling", icon_start)
        sign_start = workflow.index("      - name: Sign macOS app bundle", logo_start)
        icon_step = workflow[icon_start:logo_start]
        logo_step = workflow[logo_start:sign_start]

        for step in (icon_step, logo_step):
            self.assertIn("RDGEN_API_TOKEN: ${{ secrets.RDGEN_API_TOKEN }}", step)
            self.assertIn("python3 .github/scripts/download_rdgen_asset.py", step)
            self.assertNotIn("curl -k", step)
            self.assertNotIn("--no-check-certificate", step)
            run_script = step.split("        run: |", 1)[1]
            self.assertNotIn("${{ secrets.RDGEN_API_TOKEN }}", run_script)


if __name__ == "__main__":
    unittest.main()
