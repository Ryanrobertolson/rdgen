#!/usr/bin/env python3
"""Daily RustDesk release -> RDGen -> GitHub Release -> CortenDesk sync."""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import hashlib
import json
import os
import pathlib
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


UPSTREAM_LATEST = "https://api.github.com/repos/rustdesk/rustdesk/releases/latest"
REPO = "Ryanrobertolson/rdgen"
PUBLISH_WORKFLOW = "publish-build-release.yml"
TERMINAL_STATES = {"success", "failure", "cancelled", "timed_out", "action_required", "skipped"}
MAX_PLATFORM_BUILD_SECONDS = 5 * 60 * 60
POLL_SECONDS = 30


class SyncError(RuntimeError):
    pass


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    print(f"[{stamp}] {message}", flush=True)


def http_bytes(url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None,
               method: str | None = None, timeout: int = 120) -> bytes:
    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read(1200).decode("utf-8", errors="replace")
        raise SyncError(f"HTTP {exc.code} from {urllib.parse.urlsplit(url).netloc}: {body[:1200]}") from None
    except urllib.error.URLError as exc:
        raise SyncError(f"Could not reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from None


def http_json(url: str, *, headers: dict[str, str] | None = None,
              payload: dict | None = None, timeout: int = 120) -> dict:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {"Accept": "application/vnd.github+json", "User-Agent": "corten-release-sync"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    request_headers.update(headers or {})
    raw = http_bytes(url, headers=request_headers, data=body, timeout=timeout)
    try:
        value = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise SyncError(f"Non-JSON response from {urllib.parse.urlsplit(url).netloc}") from None
    if not isinstance(value, dict):
        raise SyncError(f"Unexpected JSON response from {urllib.parse.urlsplit(url).netloc}")
    return value


def read_json(path: pathlib.Path, *, required: bool = True) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise SyncError(f"Required configuration file is missing: {path}") from None
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"Could not read JSON file {path}: {exc}") from None
    if not isinstance(value, dict):
        raise SyncError(f"Expected a JSON object in {path}")
    return value


def atomic_write_json(path: pathlib.Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def fetch_upstream_release() -> tuple[str, dict]:
    release = http_json(UPSTREAM_LATEST)
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?", tag):
        raise SyncError("RustDesk latest-release API returned an unsupported stable tag")
    if release.get("draft") or release.get("prerelease"):
        raise SyncError("RustDesk latest-release API returned a draft or prerelease")
    return tag, release


def get_repo_releases() -> list[dict]:
    releases: list[dict] = []
    for page in range(1, 11):
        url = f"https://api.github.com/repos/{REPO}/releases?per_page=100&page={page}"
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "corten-release-sync"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                batch = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise SyncError(f"Could not list GitHub Releases (HTTP {exc.code})") from None
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise SyncError(f"Could not list GitHub Releases: {exc}") from None
        if not isinstance(batch, list):
            raise SyncError("GitHub Releases API returned an unexpected response")
        releases.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < 100:
            break
    return releases


def release_manifest(release: dict) -> dict | None:
    asset = next((a for a in release.get("assets", []) if a.get("name") == "manifest.json"), None)
    if not asset or not asset.get("browser_download_url"):
        return None
    raw = http_bytes(asset["browser_download_url"], timeout=90)
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError:
        raise SyncError(f"Release {release.get('tag_name')} has an invalid manifest.json") from None
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise SyncError(f"Release {release.get('tag_name')} has an unsupported manifest")
    return manifest


def version_releases(releases: list[dict], version: str) -> list[tuple[dict, dict]]:
    found = []
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        tag = release.get("tag_name", "")
        if not tag.startswith("cortendesk-rustdesk-"):
            continue
        manifest = release_manifest(release)
        if manifest and manifest.get("rustdesk_version") == version:
            found.append((release, manifest))
    return found


def api_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "corten-release-sync"}


def rdgen_json(base: str, token: str, path: str, *, payload: dict | None = None) -> dict:
    url = urllib.parse.urljoin(base.rstrip("/") + "/", path.lstrip("/"))
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = api_headers(token)
    if body is not None:
        headers["Content-Type"] = "application/json"
    raw = http_bytes(url, headers=headers, data=body, method="POST" if body is not None else None)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise SyncError("RDGen returned a non-JSON API response") from None
    if not isinstance(value, dict):
        raise SyncError("RDGen returned an unexpected API response")
    return value


def image_base64(config: dict, asset_name: str, assets_dir: pathlib.Path) -> str:
    configured = (config.get("assets") or {}).get(asset_name, "")
    if not configured:
        return ""
    name = pathlib.Path(configured).name
    path = assets_dir / name
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SyncError(f"Could not read configured {asset_name} image {name}: {exc}") from None
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise SyncError(f"Configured {asset_name} image {name} is not a PNG")
    return base64.b64encode(data).decode("ascii")


def build_payload(config: dict, version: str, platform: str,
                  permanent_password: str, assets_dir: pathlib.Path) -> dict:
    return {
        "exename": config.get("executable_name", "CortenDesk"),
        "appname": config.get("app_name", "CortenDesk"),
        "platform": platform,
        "version": version,
        "delayFix": config.get("delay_fix", False),
        "direction": config.get("direction", "both"),
        "installation": config.get("installation", "installationY"),
        "settings": config.get("settings_mode", "settingsY"),
        "androidappid": config.get("android_app_id", ""),
        "serverIP": config.get("rustdesk_server", ""),
        "serverPort": str(config.get("server_port", 21116)),
        "apiServer": config.get("api_server", ""),
        "key": config.get("public_key", ""),
        "urlLink": config.get("url_link", ""),
        "downloadLink": config.get("download_link", ""),
        "compname": config.get("company_name", ""),
        "theme": config.get("theme", "system"),
        "themeDorO": config.get("theme_mode", "default"),
        "passApproveMode": config.get("pass_approve_mode", "password-click"),
        "permanentPassword": permanent_password,
        "denyLan": config.get("deny_lan", False),
        "enableDirectIP": config.get("enable_direct_ip", False),
        "autoClose": config.get("auto_close", False),
        "hidecm": config.get("hide_connection_window", False),
        "iconbase64": image_base64(config, "icon", assets_dir),
        "logobase64": image_base64(config, "logo", assets_dir),
        "privacybase64": image_base64(config, "privacy", assets_dir),
        "permissionsDorO": config.get("permissions_mode", "default"),
        "permissionsType": config.get("permissions_type", "custom"),
        "enableKeyboard": config.get("enable_keyboard", True),
        "enableClipboard": config.get("enable_clipboard", True),
        "enableFileTransfer": config.get("enable_file_transfer", True),
        "enableAudio": config.get("enable_audio", True),
        "enableTCP": config.get("enable_tcp", True),
        "enableRemoteRestart": config.get("enable_remote_restart", True),
        "enableRecording": config.get("enable_recording", False),
        "enableBlockingInput": config.get("enable_blocking_input", False),
        "enableRemoteModi": config.get("enable_remote_modification", False),
        "enablePrinter": config.get("enable_printer", True),
        "enableCamera": config.get("enable_camera", True),
        "enableTerminal": config.get("enable_terminal", True),
        "removeWallpaper": config.get("remove_wallpaper", False),
        "defaultManual": config.get("default_manual", ""),
        "overrideManual": config.get("override_manual", ""),
        "xOffline": config.get("x_offline", False),
        "removeNewVersionNotif": config.get("remove_new_version_notification", False),
    }


def status_is_terminal(status: str) -> bool:
    normalized = status.strip().lower().replace(" ", "_")
    return normalized in TERMINAL_STATES


def start_build(config: dict, version: str, platform: str, permanent_password: str,
                assets_dir: pathlib.Path, rdgen_token: str) -> str:
    base = config["rdgen_url"].rstrip("/")
    payload = build_payload(config, version, platform, permanent_password, assets_dir)
    result = rdgen_json(base, rdgen_token, "/api/generate", payload=payload)
    if result.get("success") is False:
        details = result.get("details") or result.get("error") or "request rejected"
        raise SyncError(f"RDGen rejected {platform} build: {str(details)[:1200]}")
    uuid = result.get("uuid")
    if not isinstance(uuid, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", uuid):
        raise SyncError("RDGen did not return a valid build UUID")
    return uuid


def wait_for_build(base: str, token: str, uuid: str) -> dict:
    deadline = time.monotonic() + MAX_PLATFORM_BUILD_SECONDS
    last_status = "unknown"
    while time.monotonic() < deadline:
        result = rdgen_json(base, token, f"/api/status?uuid={urllib.parse.quote(uuid)}")
        status = str(result.get("status", "unknown"))
        if status != last_status:
            log(f"RDGen build {uuid}: {status}")
            last_status = status
        if status_is_terminal(status):
            return result
        time.sleep(POLL_SECONDS)
    raise SyncError(f"RDGen build {uuid} did not finish within five hours; it will be checked again tomorrow")


def trigger_release_workflow(version: str, platform: str, uuid: str,
                             github_token: str, branch: str) -> str:
    tag = f"cortendesk-rustdesk-{version}-{platform}-{uuid[:8]}"
    url = f"https://api.github.com/repos/{REPO}/actions/workflows/{PUBLISH_WORKFLOW}/dispatches"
    payload = {"ref": branch, "inputs": {"uuid": uuid, "version": version, "platform": platform}}
    headers = {"Authorization": f"Bearer {github_token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "corten-release-sync",
               "Content-Type": "application/json"}
    http_bytes(url, headers=headers, data=json.dumps(payload).encode("utf-8"), method="POST")
    return tag


def wait_for_release(tag: str, timeout_seconds: int = 20 * 60) -> dict:
    url = f"https://api.github.com/repos/{REPO}/releases/tags/{urllib.parse.quote(tag, safe='')}"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "corten-release-sync"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                value = json.loads(response.read())
            if isinstance(value, dict) and value.get("id"):
                return value
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise SyncError(f"GitHub Release lookup failed with HTTP {exc.code}") from None
        except (urllib.error.URLError, json.JSONDecodeError) as exc:
            raise SyncError(f"Could not check GitHub Release {tag}: {exc}") from None
        time.sleep(15)
    raise SyncError(f"GitHub Release {tag} was not published within 20 minutes")


def upload_one(page, settings: dict, version: str, platform: str, asset: dict,
               file_path: pathlib.Path) -> None:
    name = asset["name"]
    label = f"{settings.get('app_name', 'CortenDesk')} {version} {platform} - {name}"
    notes = f"RustDesk {version}; {platform}; SHA-256 {asset['sha256']}"
    base = settings["cortendesk_url"].rstrip("/")
    page.goto(base + settings.get("upload_path", "/client-downloads"), wait_until="domcontentloaded", timeout=60000)
    page.get_by_role("button", name=re.compile("upload build", re.I)).first.wait_for(timeout=30000)
    page.get_by_role("button", name=re.compile("upload build", re.I)).first.click()
    page.locator("#cd-file").wait_for(state="visible", timeout=15000)
    page.locator("#cd-file").set_input_files(str(file_path))
    page.wait_for_function(
        """() => {
          const f = document.querySelector('#cd-file');
          if (!f || !f.files || f.files.length !== 1) return false;
          return !Array.from(document.querySelectorAll('*')).some(e => {
            if (!e.hasAttribute('wire:loading')) return false;
            const r = e.getBoundingClientRect();
            return r.width && r.height && getComputedStyle(e).display !== 'none';
          });
        }""",
        timeout=120000,
    )
    page.locator("#cd-label").fill(label)
    page.locator("#cd-version").fill(version)
    page.locator("#cd-notes").fill(notes)
    options = page.locator("#cd-platform option").evaluate_all(
        "els => els.map(e => ({value: e.value, text: e.textContent || ''}))"
    )
    option = next((item for item in options if platform.lower() in (item["value"] + " " + item["text"]).lower()), None)
    if not option:
        raise SyncError(f"CortenDesk has no platform option matching {platform}")
    page.locator("#cd-platform").select_option(option["value"])
    published = page.locator("#cd-published")
    if settings.get("publish_immediately", True):
        published.check()
    else:
        published.uncheck()
    page.get_by_role("button", name="Upload", exact=True).click()
    try:
        page.locator(".modal.show").wait_for(state="detached", timeout=180000)
    except PlaywrightTimeoutError:
        alerts = page.locator("[role=alert], .invalid-feedback, .text-danger").all_inner_texts()
        detail = "; ".join(text.strip() for text in alerts if text.strip())
        raise SyncError(f"CortenDesk did not finish uploading {name}" + (f": {detail[:1200]}" if detail else ""))

    page.goto(base + "/downloads", wait_until="domcontentloaded", timeout=60000)
    body_text = page.locator("body").inner_text(timeout=30000)
    if label not in body_text and name not in body_text:
        raise SyncError(f"CortenDesk accepted {name}, but it did not appear on the public downloads page")


@contextmanager
def make_uploader(settings: dict, credentials: dict):
    base = settings["cortendesk_url"].rstrip("/")
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(30000)
        target = base + settings.get("upload_path", "/client-downloads")
        page.goto(target, wait_until="domcontentloaded", timeout=60000)
        if "/login" in page.url:
            user = page.locator('#username, input[name="username"], input[autocomplete="username"]').first
            password = page.locator('#password, input[name="password"], input[autocomplete="current-password"]').first
            user.fill(credentials["username"])
            password.fill(credentials["password"])
            form = user.locator("xpath=ancestor::form")
            submit = form.locator('button[type="submit"], input[type="submit"]').first
            submit.click()
            try:
                page.wait_for_url(lambda url: "/login" not in url, timeout=45000)
            except PlaywrightTimeoutError:
                if page.locator('input[autocomplete="one-time-code"], input[name*="code"]').count():
                    raise SyncError("CortenDesk requires an interactive verification code for unattended sign-in")
                alerts = page.locator("[role=alert], .alert-danger, .invalid-feedback, .text-danger, .error-message").all_inner_texts()
                detail = "; ".join(text.strip() for text in alerts if text.strip())
                message = "CortenDesk sign-in failed"
                if detail:
                    message += f": {detail[:800]}"
                else:
                    message += "; check the stored login or account security settings"
                raise SyncError(message)
        if "/login" in page.url:
            raise SyncError("CortenDesk still shows the sign-in page after login")
        try:
            yield page
        finally:
            context.close()
            browser.close()


def run_check(config: dict, state: dict, *, check_only: bool) -> tuple[str, list[tuple[dict, dict]], list[str]]:
    version, release = fetch_upstream_release()
    platforms = config.get("platforms") or []
    if not platforms or any(p not in {"windows", "windows-x86", "linux", "macos", "android"} for p in platforms):
        raise SyncError("build-settings.json has no supported target platforms")
    releases = get_repo_releases()
    existing = version_releases(releases, version)
    built = {manifest.get("platform") for _, manifest in existing}
    missing = [platform for platform in platforms if platform not in built]
    log(f"Latest stable RustDesk tag: {version}")
    log(f"RDGen GitHub Releases already contain: {', '.join(sorted(built)) if built else 'no builds for this version'}")
    log(f"Configured platforms: {', '.join(platforms)}")
    if check_only:
        for platform in missing:
            log(f"Check only: a {platform} build would be requested")
        if not missing:
            log("Check only: all configured platforms already have a release")
        return version, existing, missing
    return version, existing, missing


def run_sync(args) -> None:
    config = read_json(pathlib.Path(args.config))
    state_path = pathlib.Path(args.state)
    state = read_json(state_path, required=False)
    version, existing, missing = run_check(config, state, check_only=args.check_only)
    if args.check_only:
        return

    credentials = read_json(pathlib.Path(args.auth_file))
    release_auth = read_json(pathlib.Path(args.rdgen_auth_file))
    for key in ("username", "password"):
        if not credentials.get(key):
            raise SyncError(f"CortenDesk {key} is missing from the encrypted systemd credential")
    for key in ("github_token", "rdgen_api_token"):
        if not release_auth.get(key):
            raise SyncError(f"{key} is missing from the encrypted RDGen credential")

    uploaded = state.setdefault("uploaded", {})
    pending = state.setdefault("pending_builds", {})
    pending_for_version = pending.setdefault(version, {})
    base = config["rdgen_url"].rstrip("/")
    assets_dir = pathlib.Path(args.assets)

    # Reconcile a previously started build before launching another request.
    releases_by_platform = {manifest.get("platform"): (release, manifest) for release, manifest in existing}
    for platform in config["platforms"]:
        if platform in releases_by_platform:
            continue
        build = pending_for_version.get(platform)
        if build:
            uuid = build.get("uuid", "")
            try:
                status = rdgen_json(base, release_auth["rdgen_api_token"], f"/api/status?uuid={urllib.parse.quote(uuid)}")
            except SyncError as exc:
                if "HTTP 404" in str(exc):
                    pending_for_version.pop(platform, None)
                    atomic_write_json(state_path, state)
                    build = None
                else:
                    raise
            if build:
                status_text = str(status.get("status", "unknown"))
                if not status_is_terminal(status_text):
                    log(f"Resuming existing {platform} build {uuid}")
                    status = wait_for_build(base, release_auth["rdgen_api_token"], uuid)
                files = status.get("files") or []
                if files:
                    tag = f"cortendesk-rustdesk-{version}-{platform}-{uuid[:8]}"
                    log(f"Publishing {len(files)} output file(s) from the {platform} build to GitHub Release {tag}")
                    trigger_release_workflow(version, platform, uuid, release_auth["github_token"], config.get("rdgen_branch", "rdgen-auth"))
                    release = wait_for_release(tag)
                    releases_by_platform[platform] = (release, release_manifest(release) or {})
                    pending_for_version.pop(platform, None)
                    atomic_write_json(state_path, state)
                    continue
                if status_is_terminal(status_text):
                    log(f"RDGen {platform} build {uuid} ended without installer files; it will be retried")
                    pending_for_version.pop(platform, None)
                    atomic_write_json(state_path, state)
                    build = None
        if platform in releases_by_platform:
            continue
        log(f"Requesting {platform} installer build for RustDesk {version}")
        uuid = start_build(config, version, platform, credentials.get("permanent_password", ""), assets_dir,
                           release_auth["rdgen_api_token"])
        pending_for_version[platform] = {"uuid": uuid, "started_at": datetime.now(timezone.utc).isoformat()}
        atomic_write_json(state_path, state)
        status = wait_for_build(base, release_auth["rdgen_api_token"], uuid)
        files = status.get("files") or []
        if not files:
            pending_for_version.pop(platform, None)
            atomic_write_json(state_path, state)
            raise SyncError(f"RDGen {platform} build ended as {status.get('status', 'unknown')} with no files")
        tag = f"cortendesk-rustdesk-{version}-{platform}-{uuid[:8]}"
        log(f"Publishing {len(files)} output file(s) from the {platform} build to GitHub Release {tag}")
        trigger_release_workflow(version, platform, uuid, release_auth["github_token"], config.get("rdgen_branch", "rdgen-auth"))
        release = wait_for_release(tag)
        releases_by_platform[platform] = (release, release_manifest(release) or {})
        pending_for_version.pop(platform, None)
        atomic_write_json(state_path, state)

    # Refresh the public release list after any just-published workflows.
    releases = get_repo_releases()
    existing = version_releases(releases, version)
    with make_uploader(config, credentials) as page:
        for release, manifest in sorted(existing, key=lambda pair: pair[0].get("published_at", "")):
            if manifest.get("platform") not in config["platforms"]:
                continue
            assets_by_name = {asset.get("name"): asset for asset in release.get("assets", [])}
            for item in manifest.get("files", []):
                name = item.get("name")
                digest = item.get("sha256")
                asset_key = f"{release.get('id')}:{name}:{digest}"
                if not name or not digest or asset_key in uploaded:
                    continue
                asset = assets_by_name.get(name)
                if not asset or not asset.get("browser_download_url"):
                    raise SyncError(f"GitHub Release {release.get('tag_name')} is missing asset {name}")
                data = http_bytes(asset["browser_download_url"], timeout=300)
                actual = hashlib.sha256(data).hexdigest()
                if actual != digest:
                    raise SyncError(f"SHA-256 mismatch for {name}: GitHub release manifest does not match the downloaded file")
                with tempfile.TemporaryDirectory(prefix="corten-release-") as temporary:
                    file_path = pathlib.Path(temporary) / pathlib.Path(name).name
                    file_path.write_bytes(data)
                    log(f"Uploading {name} from GitHub Release {release.get('tag_name')} to CortenDesk")
                    upload_one(page, config, version, manifest["platform"], item, file_path)
                uploaded[asset_key] = {
                    "release_id": release.get("id"),
                    "filename": name,
                    "sha256": digest,
                    "uploaded_at": datetime.now(timezone.utc).isoformat(),
                }
                atomic_write_json(state_path, state)
                log(f"Published {name} on CortenDesk downloads")
    log(f"Daily sync finished for RustDesk {version}")


def run_login_check(args) -> None:
    config = read_json(pathlib.Path(args.config))
    credentials = read_json(pathlib.Path(args.auth_file))
    if not credentials.get("username") or not credentials.get("password"):
        raise SyncError("CortenDesk username or password is missing from the encrypted systemd credential")
    with make_uploader(config, credentials) as page:
        page.get_by_role("button", name=re.compile("upload build", re.I)).first.wait_for(timeout=30000)
        log("CortenDesk sign-in succeeded; the Client Downloads upload form is available")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=os.environ.get("BUILD_CONFIG", "/config/build-settings.json"))
    parser.add_argument("--assets", default=os.environ.get("BUILD_ASSETS", "/config/assets"))
    parser.add_argument("--state", default=os.environ.get("SYNC_STATE", "/state/state.json"))
    parser.add_argument("--auth-file", default=os.environ.get("CORTEN_AUTH_FILE", "/run/secrets/corten-auth"))
    parser.add_argument("--rdgen-auth-file", default=os.environ.get("RDGEN_AUTH_FILE", "/run/secrets/rdgen-auth"))
    parser.add_argument("--check-only", action="store_true", help="Check release state without building or uploading")
    parser.add_argument("--login-check", action="store_true", help="Check CortenDesk sign-in and upload-page access without uploading")
    args = parser.parse_args()
    try:
        if args.login_check:
            run_login_check(args)
        else:
            run_sync(args)
        return 0
    except SyncError as exc:
        log(f"ERROR: {exc}")
        return 1
    except Exception as exc:
        log(f"ERROR: unexpected {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
