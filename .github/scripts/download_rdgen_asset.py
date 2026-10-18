#!/usr/bin/env python3
"""Fetch an authenticated RDGen asset without following redirects."""

import argparse
import os
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, Request, build_opener


class DownloadError(RuntimeError):
    """A safe-to-display download error that never includes request headers."""


class RejectRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "RDGen asset redirects are disabled", headers, fp)


def download_asset(base_url, filename, asset_uuid, output_path, token):
    parts = urlsplit(base_url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise DownloadError("RDGen base URL must be an HTTPS origin without credentials or query parameters")
    if not token:
        raise DownloadError("RDGen API token is missing")
    if not filename or not asset_uuid:
        raise DownloadError("RDGen asset filename and UUID are required")

    endpoint_path = f"{parts.path.rstrip('/')}/get_png"
    asset_url = urlunsplit(
        (parts.scheme, parts.netloc, endpoint_path, urlencode({"filename": filename, "uuid": asset_uuid}), "")
    )
    request = Request(
        asset_url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": "Mozilla/5.0",
        },
    )
    opener = build_opener(
        RejectRedirectHandler(),
        HTTPSHandler(context=ssl.create_default_context()),
    )

    try:
        with opener.open(request, timeout=60) as response:
            payload = response.read()
    except HTTPError as exc:
        raise DownloadError(f"RDGen asset request failed with HTTP {exc.code}") from None
    except URLError as exc:
        raise DownloadError(f"RDGen asset request failed due to a network or TLS error: {exc.reason}") from None

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f"{output.name}.tmp")
    try:
        temporary.write_bytes(payload)
        temporary.replace(output)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise DownloadError(f"Could not save RDGen asset to {output}: {exc.strerror or exc}") from None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--filename", required=True)
    parser.add_argument("--uuid", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    try:
        download_asset(
            os.environ.get("RDGEN_BASE_URL", ""),
            args.filename,
            args.uuid,
            args.output,
            os.environ.get("RDGEN_API_TOKEN", ""),
        )
    except DownloadError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
