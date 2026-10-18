import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, override_settings

from rdgenerator.middleware import BearerTokenMiddleware


class BearerTokenMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.response_body = "view reached"
        self.middleware = BearerTokenMiddleware(
            lambda request: HttpResponse(self.response_body)
        )

    @override_settings(STATIC_URL="/static/")
    def test_missing_server_token_fails_closed_with_503(self):
        with patch.dict(os.environ, {"RDGEN_API_TOKEN": ""}):
            response = self.middleware(self.factory.get("/api/status"))

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("token", response.content.decode().lower())

    @override_settings(STATIC_URL="/static/")
    def test_missing_malformed_and_wrong_credentials_get_401(self):
        bad_headers = [
            {},
            {"HTTP_AUTHORIZATION": "Basic credentials"},
            {"HTTP_AUTHORIZATION": "Bearer"},
            {"HTTP_AUTHORIZATION": "Bearer "},
            {"HTTP_AUTHORIZATION": "Bearer wrong"},
            {"HTTP_AUTHORIZATION": "Bearer expected extra"},
        ]
        with patch.dict(os.environ, {"RDGEN_API_TOKEN": "expected"}):
            for path in ("/api/status", "/download"):
                for headers in bad_headers:
                    with self.subTest(path=path, headers=headers):
                        response = self.middleware(self.factory.get(path, **headers))
                        self.assertEqual(response.status_code, 401)

    @override_settings(STATIC_URL="/static/")
    def test_correct_bearer_token_reaches_view(self):
        with patch.dict(os.environ, {"RDGEN_API_TOKEN": "expected"}):
            response = self.middleware(
                self.factory.get(
                    "/api/status", HTTP_AUTHORIZATION="Bearer expected"
                )
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), self.response_body)

    @override_settings(STATIC_URL="/static/")
    def test_static_prefix_is_available_without_credentials(self):
        with patch.dict(os.environ, {"RDGEN_API_TOKEN": "expected"}):
            response = self.middleware(self.factory.get("/static/site.css"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode(), self.response_body)

    @override_settings(STATIC_URL="/static/")
    def test_static_prefix_does_not_match_similarly_named_routes(self):
        with patch.dict(os.environ, {"RDGEN_API_TOKEN": "expected"}):
            response = self.middleware(self.factory.get("/static-private/page"))

        self.assertEqual(response.status_code, 401)


class DatabasePathConfigurationTests(SimpleTestCase):
    def test_database_path_can_be_moved_to_persistent_storage(self):
        with TemporaryDirectory() as temp_dir:
            expected = str(Path(temp_dir) / "rdgen.sqlite3")
            child_env = os.environ.copy()
            child_env["RDGEN_DATABASE_PATH"] = expected
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from django.conf import settings; "
                    "print(settings.DATABASES['default']['NAME'])",
                ],
                cwd=Path(__file__).resolve().parents[1],
                env=child_env,
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.stdout.strip(), expected)
