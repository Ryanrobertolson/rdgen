import hmac
import os
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpResponse


class BearerTokenMiddleware:
    """Require the configured shared token on every non-static request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if self._is_static_path(request.path_info):
            return self.get_response(request)

        expected = os.environ.get("RDGEN_API_TOKEN", "")
        if not expected:
            return HttpResponse(
                "Service unavailable\n", status=503, content_type="text/plain"
            )

        authorization = request.META.get("HTTP_AUTHORIZATION", "")
        scheme, separator, presented = authorization.partition(" ")
        if (
            scheme.casefold() != "bearer"
            or not separator
            or not presented
            or presented != presented.strip()
            or any(character.isspace() for character in presented)
            or not hmac.compare_digest(
                presented.encode("utf-8"), expected.encode("utf-8")
            )
        ):
            return HttpResponse(
                "Unauthorized\n",
                status=401,
                content_type="text/plain",
                headers={"WWW-Authenticate": "Bearer"},
            )

        return self.get_response(request)

    @staticmethod
    def _is_static_path(request_path):
        static_path = urlsplit(getattr(settings, "STATIC_URL", "") or "").path
        static_prefix = "/" + static_path.strip("/")
        if static_prefix == "/":
            return False
        return request_path == static_prefix or request_path.startswith(
            static_prefix + "/"
        )
