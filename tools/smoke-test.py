#!/usr/bin/env python3
import base64
import datetime as dt
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from urllib import error, parse, request
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / ".daemon-mode" / "smoke-test.sqlite3"
PORT = 4327
BASE_URL = f"http://127.0.0.1:{PORT}"


def print_step(message):
    print(f"[smoke] {message}")


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print_step(f"PASS {message}")


def fetch_json(path, method="GET", payload=None):
    body = None
    headers = {}

    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = request.Request(f"{BASE_URL}{path}", data=body, headers=headers, method=method)

    try:
        with request.urlopen(req, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} for {path}: {details}") from exc


def fetch_text(path):
    req = request.Request(f"{BASE_URL}{path}", method="GET")

    with request.urlopen(req, timeout=3) as response:
        return response.read().decode("utf-8")


def fetch_response(path):
    req = request.Request(f"{BASE_URL}{path}", method="GET")

    with request.urlopen(req, timeout=3) as response:
        return response.info(), response.read()


def fetch_http_status(path, method="GET", headers=None, payload=None):
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = dict(headers or {})
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    req = request.Request(
        f"{BASE_URL}{path}",
        data=body,
        headers=request_headers,
        method=method,
    )

    try:
        with request.urlopen(req, timeout=3) as response:
            return response.status, response.info(), response.read()
    except error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def wait_for_service(process):
    deadline = time.time() + 8

    while time.time() < deadline:
        if process.poll() is not None:
            raise RuntimeError("Local memory service exited before health check passed")

        try:
            payload = fetch_json("/health")
            if payload.get("ok"):
                return
        except Exception:
            time.sleep(0.2)

    raise RuntimeError("Timed out waiting for local memory service")


def cleanup_db():
    for path in [DB_PATH, DB_PATH.with_suffix(".sqlite3-shm"), DB_PATH.with_suffix(".sqlite3-wal")]:
        if path.exists():
            path.unlink()


def previous_los_angeles_evening_utc():
    los_angeles = ZoneInfo("America/Los_Angeles")
    now = dt.datetime.now(los_angeles)
    previous_evening = (now - dt.timedelta(days=1)).replace(hour=20, minute=30, second=0, microsecond=0)
    return previous_evening.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def run_blocklist_checks():
    script = """
      import { isBlockedUrl } from './extension/src/blocklist.js';

      const cases = [
        ['https://developer.chrome.com/docs/extensions/', false],
        ['https://mail.google.com/mail/u/0/#inbox', true],
        ['https://mail.yahoo.com/d/folders/1', true],
        ['https://proton.me/mail/inbox', true],
        ['https://meet.google.com/abc-defg-hij', true],
        ['https://support.google.com/meet/answer/7380413#code', true],
        ['https://zoom.us/j/123456789', true],
        ['https://myaccount.google.com/security', true],
        ['https://example.okta.com/app/UserHome', true],
        ['https://docs.google.com/document/d/example/edit', true],
        ['https://www.notion.so/private-team-page', true],
        ['https://app.slack.com/client/T123/C456', true],
        ['https://web.telegram.org/a/#123', true],
        ['https://chatgpt.com/c/abc123', true],
        ['https://claude.ai/chat/abc123', true],
        ['http://127.0.0.1:4317/', true],
        ['https://example.com/login', true],
        ['https://example.com/oauth/authorize', true],
        ['https://example.com/native/oauth2callback?code=secret&state=opaque', true],
        ['https://example.com/callback?access_token=secret', true],
        ['https://app.notion.com/private-workspace', true],
        ['https://example.com/messages/thread/123', true],
        ['https://example.com/patient/lab-results', true],
        ['https://example.com/billing/invoice/123', true],
        ['https://myaccount.uscis.gov/dashboard', true],
        ['https://www.usps.com/manage/informed-delivery.htm', true],
        ['https://www.capitalone.com/sign-in/', true],
        ['https://www.americanexpress.com/account/summary', true],
        ['https://healthy.example.org/mychart/appointments', true],
        ['https://talentplace.a16z.com/dashboard', true],
        ['https://careers.example.com/jobs/123/candidate?csrf=abc', true],
        ['https://jobs.example.com/role?mode=submit_apply', true],
        ['https://jobs.example.com/role?uploadResume=1', true]
      ];

      for (const [url, expected] of cases) {
        const actual = isBlockedUrl(url).blocked;
        if (actual !== expected) {
          throw new Error(`${url} expected blocked=${expected} but got ${actual}`);
        }
      }
    """
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
    )
    print_step("PASS blocklist expectations")


def run_manifest_checks():
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text())
    assert_true("notifications" not in manifest.get("permissions", []), "extension does not request notification permission")
    assert_true(manifest.get("version") == "0.5.10", "extension version is 0.5.10")
    assert_true(manifest.get("incognito") == "not_allowed", "extension cannot run in incognito")
    assert_true(manifest.get("host_permissions") == ["http://127.0.0.1:4317/*"], "required host access is limited to the local service")
    assert_true(manifest.get("optional_host_permissions") == ["http://*/*", "https://*/*"], "normal page access is optional")
    assert_true("activeTab" not in manifest.get("permissions", []), "extension removes redundant activeTab access")
    assert_true("scripting" in manifest.get("permissions", []), "extension can register its content script only after consent")
    assert_true("content_scripts" not in manifest, "extension does not inject a content script before consent")
    service_source = (ROOT / "tools" / "local-memory-service.py").read_text(encoding="utf-8")
    mcp_source = (ROOT / "tools" / "daemon-mcp-server.py").read_text(encoding="utf-8")
    background_source = (ROOT / "extension" / "src" / "background.js").read_text(encoding="utf-8")
    content_source = (ROOT / "extension" / "src" / "content.js").read_text(encoding="utf-8")
    assert_true('SERVICE_VERSION = "0.5.10"' in service_source, "local service release version matches the extension")
    assert_true('SERVER_VERSION = "0.5.10"' in mcp_source, "MCP server release version matches the extension")
    assert_true('LEGACY_PROTOCOL_VERSION = "2025-06-18"' in mcp_source, "MCP server preserves the legacy protocol era")
    assert_true('MODERN_PROTOCOL_VERSION = "2026-07-28"' in mcp_source, "MCP server declares the modern protocol era")
    assert_true('method == "server/discover"' in mcp_source, "MCP server implements modern protocol discovery")
    assert_true('"supported": SUPPORTED_PROTOCOL_VERSIONS, "requested": requested_version' in mcp_source, "MCP server reports unsupported modern versions honestly")
    assert_true("Chrome's permission state is authoritative" in background_source, "extension repairs stale permission state from Chrome's real grant")
    assert_true("containsOptionalPageAccess().then(async (hostAccessGranted)" in background_source, "permission removal rechecks required page access")
    assert_true("captureContentScriptSync" in background_source, "content script registration is serialized across startup and update events")
    assert_true("registrationsAfterError.length" in background_source, "content script registration races preserve a successful concurrent registration")
    assert_true("finalBlockResult = await getBlockResult(capture.url)" in background_source, "extension rechecks the final page URL before posting text")
    assert_true("settledSensitiveFieldReason = getSensitiveFieldReason()" in content_source, "content script rechecks sensitive fields after content settles")
    assert_true("sensitive_url_reason(url)" in service_source, "service rejects credential-bearing URLs at ingestion")
    assert_true("protection_status_for_url(connection, url)" in service_source, "service reapplies current protection at ingestion")
    public_key = base64.b64decode(manifest.get("key", ""))
    digest = hashlib.sha256(public_key).digest()[:16].hex()
    extension_id = "".join(chr(ord("a") + int(nibble, 16)) for nibble in digest)
    assert_true(extension_id == "hkpoimeiilpfajaikpckmdkpgdijciie", "extension has the stable service-authorized identity")
    assert_true(manifest.get("icons", {}).get("128") == "assets/icons/dmn-icon-128.png", "extension exposes the DMN logo as its app icon")
    assert_true(
        manifest.get("action", {}).get("default_icon", {}).get("32") == "assets/icons/dmn-icon-32.png",
        "extension toolbar action uses the DMN logo",
    )


def run_public_foundation_checks():
    required = [
        "README.md",
        "LICENSE",
        "PRIVACY.md",
        "SECURITY.md",
        "CONTRIBUTING.md",
        "THIRD_PARTY_NOTICES.md",
        ".github/workflows/ci.yml",
        ".github/ISSUE_TEMPLATE/bug.yml",
        ".github/ISSUE_TEMPLATE/feature.yml",
        ".github/pull_request_template.md",
        "extension/assets/fonts/OFL-1.1.txt",
        "site/assets/fonts/OFL-1.1.txt",
    ]
    assert_true(all((ROOT / path).is_file() for path in required), "public launch foundation files are present")
    readme = (ROOT / "README.md").read_text()
    privacy = (ROOT / "PRIVACY.md").read_text()
    security = (ROOT / "SECURITY.md").read_text()
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert_true("Agents cannot delete, clear, export, or change protection rules" in readme, "README states the read-only agent boundary")
    assert_true("No account, cloud service, or AI provider key is required" in readme, "README states provider-free core value")
    assert_true("Captures, protection, the memory river" not in privacy, "privacy policy avoids retired dashboard-first framing")
    assert_true("Broad website access is not used for advertising" in privacy, "privacy policy explains the extension permission boundary")
    assert_true("Store ZIPs are deterministic allowlisted release artifacts" in security, "security policy states the store package integrity boundary")
    assert_true("actions/checkout@34e114" in ci and "permissions:\n  contents: read" in ci, "CI uses pinned actions and least-privilege permissions")


def run_extension_ui_copy_checks():
    background_js = (ROOT / "extension" / "src" / "background.js").read_text()
    popup_html = (ROOT / "extension" / "popup" / "popup.html").read_text()
    popup_css = (ROOT / "extension" / "popup" / "popup.css").read_text()
    popup_js = (ROOT / "extension" / "popup" / "popup.js").read_text()
    content_js = (ROOT / "extension" / "src" / "content.js").read_text()

    assert_true('"SAFE"' not in background_js and '"OK"' not in background_js, "toolbar badge avoids word badges")
    assert_true("formatBadgeCount" in background_js, "toolbar badge uses a quiet numeric memory count")
    assert_true('["active", "captured", "scheduled"].includes(status)' in background_js, "toolbar badge only shows counts for safe active states")
    assert_true('text: ["active", "captured", "scheduled"].includes(status) ? countText : ""' in background_js, "toolbar badge clears counts for protected or trust-critical states")
    assert_true("MEMORY_PULSE_ENDPOINT" in background_js, "toolbar badge reads the local memory pulse count")
    assert_true('text: "•"' not in background_js, "toolbar badge does not use a dot overlay")
    assert_true("chrome.action.setTitle" in background_js, "toolbar badge moves state detail into the Chrome tooltip")
    assert_true('"SKIP"' not in background_js, "toolbar badge no longer uses command-like SKIP copy")
    assert_true('"service-off"' in background_js, "toolbar badge has an explicit service-off state")
    assert_true('!service.connected' in background_js, "toolbar badge checks service health before showing active state")
    assert_true("chrome.tabs.onActivated.addListener" in background_js, "toolbar badge refreshes when active tab changes")
    assert_true("chrome.tabs.onUpdated.addListener" in background_js, "toolbar badge refreshes when active tab URL/status changes")
    assert_true("chrome.windows.onFocusChanged.addListener" in background_js, "toolbar badge refreshes when Chrome window focus changes")
    assert_true('"Protected"' in popup_js, "popup uses protected-state copy")
    assert_true('"Skipping"' not in popup_js, "popup no longer labels protected pages as Skipping")
    assert_true('"Protected already"' in popup_js, "popup disables block action when the page is already protected")
    assert_true('id="page-title"' in popup_html, "popup has the Riff i page title slot")
    assert_true('id="trust-footer"' in popup_html, "popup has the local trust footer")
    assert_true("Local only · no cloud sync" in popup_html, "popup footer promises local storage without sync")
    assert_true("../assets/brand/dmn-mark.png" in popup_html, "popup header uses the selected DMN logo mark")
    assert_true("width: 360px" in popup_css, "popup keeps the accepted fixed 360px width")
    assert_true("min-width: 360px" in popup_css, "popup aligns to Chrome's square action-popup host")
    assert_true(".popup-shell {\n  background: var(--paper-2);\n  border: 0;\n  border-radius: 0;" in popup_css, "popup does not create a competing rounded card shell")
    assert_true("html {\n  background: var(--paper);" in popup_css, "popup paints the Chrome host with paper")
    assert_true("body {\n  background: var(--paper);" in popup_css, "popup body does not expose dark browser chrome")
    assert_true('id="today-line"' in popup_html, "popup includes the gated Riff ii today line")
    assert_true('id="more-activity"' in popup_html, "popup includes the Riff iii activity disclosure")
    assert_true('id="more-activity" class="activity-toggle" type="button"' in popup_html, "popup activity disclosure avoids origin-fill button treatment")
    assert_true("savedToday < 5" in popup_js, "Riff ii today line is gated until enough same-day captures exist")
    assert_true("More activity" in popup_js, "Riff iii is exposed as an expandable activity state")
    assert_true("color-scheme: light;" in popup_css, "popup uses paper-light as the design of record")
    assert_true("prefers-color-scheme" not in popup_css, "popup does not switch to dark mode automatically")
    assert_true("--teal:" in popup_css, "popup uses graphite teal as the primary accent token")
    assert_true('"Daemon UI"' in popup_css, "popup self-hosts the Atkinson-based UI voice")
    assert_true('"Daemon Mono"' in popup_css, "popup self-hosts the Commit Mono data voice")
    assert_true('"Daemon Brief"' in popup_css, "popup self-hosts the italic brief voice")
    assert_true("Geist" not in popup_css, "popup no longer uses the generic Geist font stack")
    assert_true("Local · Capturing" not in popup_js, "popup avoids dense technical status copy")
    assert_true("Ready to save this page if it is allowed." in popup_js, "popup explains the current value in human terms")
    assert_true("Daemon did not read this page." in popup_js, "popup explains protected pages without noisy metadata")
    assert_true("todayMemoryLabel(todayCaptureCount(pulse))" in popup_js, "popup reinforces the same quiet daily memory count")
    assert_true("Latest activity" not in popup_html, "popup no longer leads with a noisy latest-activity block")
    assert_true("Daemon is offline" in popup_js, "popup handles service-off state in user-facing copy")
    assert_true('class="primary origin-action"' in popup_html, "popup primary action uses origin-fill treatment")
    assert_true('class="secondary origin-action"' in popup_html, "popup secondary actions use origin-fill treatment")
    assert_true("grid-template-columns: repeat(3, minmax(0, 1fr));" in popup_css, "popup secondary actions use equal compact columns")
    assert_true(".activity-toggle::after" in popup_css, "popup activity disclosure uses a refined underline transition")
    assert_true("button.secondary {\n  align-items: center;" in popup_css, "popup secondary action labels are vertically centered")
    assert_true("button.origin-action::before" in popup_css, "popup origin-fill button renders a cover layer")
    assert_true("getOriginCoverDiameter" in popup_js, "popup origin-fill button computes cover size without React dependencies")
    assert_true("setOriginButtonLabel(button, label)" in popup_js, "popup dynamic button labels preserve origin-fill markup")
    assert_true("button.textContent = label" not in popup_js, "popup actions avoid replacing origin-fill label markup")
    assert_true("chrome.runtime.lastError" in content_js, "content script handles runtime messaging errors")
    assert_true("Extension context invalidated" in content_js, "content script handles extension reload invalidation")
    assert_true("resolveCaptureProtection" in background_js, "extension resolves user and default protection before page text read")
    assert_true("protection-check-unavailable" in background_js, "extension explains fail-closed protection checks")
    assert_true('id="consent-disclosure"' in popup_html, "popup shows prominent disclosure before capture consent")
    assert_true("URL, page title, capture time, and readable page text" in popup_html, "consent disclosure names captured data")
    assert_true("Chrome profiles enabled under the same macOS user share this local vault" in popup_html, "consent disclosure explains profile sharing")
    assert_true("chrome.permissions.request" in popup_js, "optional page access is requested from the popup user action")
    assert_true("chrome.permissions.request" not in background_js, "background cannot request page access without a user action")


def run_extension_store_readiness_checks():
    result = subprocess.run(
        ["node", "tools/extension-store-readiness-proof.mjs"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert_true("PASS extension store readiness proof" in result.stdout, "extension store consent and protection proof passes")


def run_protection_fail_closed_checks():
    script = """
      import { checkUserBlocklist } from './extension/src/protection.js';

      const endpoint = 'http://127.0.0.1:4317/blocklist/check';
      const unavailable = await checkUserBlocklist('https://example.com/private', endpoint, async () => {
        throw new Error('service offline');
      });
      if (!unavailable.blocked || unavailable.reason !== 'protection-check-unavailable') {
        throw new Error('offline protection check failed open');
      }

      const rejected = await checkUserBlocklist('https://example.com/private', endpoint, async () => ({ ok: false }));
      if (!rejected.blocked || rejected.reason !== 'protection-check-unavailable') {
        throw new Error('non-OK protection check failed open');
      }

      const open = await checkUserBlocklist('https://example.com/open', endpoint, async () => ({
        ok: true,
        json: async () => ({ ok: true, blocked: false })
      }));
      if (open.blocked) {
        throw new Error('confirmed open page was blocked');
      }

      const blocked = await checkUserBlocklist('https://example.com/private', endpoint, async () => ({
        ok: true,
        json: async () => ({ ok: true, blocked: true, reason: 'user-blocked-domain:example.com' })
      }));
      if (!blocked.blocked || blocked.reason !== 'user-blocked-domain:example.com') {
        throw new Error('confirmed user block was not preserved');
      }

      for (const payload of [{}, { ok: false, blocked: false }, { ok: true, blocked: 0 }]) {
        const malformed = await checkUserBlocklist('https://example.com/private', endpoint, async () => ({
          ok: true,
          json: async () => payload
        }));
        if (!malformed.blocked || malformed.reason !== 'protection-check-unavailable') {
          throw new Error(`malformed protection payload failed open: ${JSON.stringify(payload)}`);
        }
      }
    """
    subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, check=True, text=True)
    print_step("PASS unavailable user protection fails closed before page text read")


def run_storage_checks():
    service_py = (ROOT / "tools" / "local-memory-service.py").read_text()
    assert_true("def managed_db" in service_py, "local service uses a managed SQLite connection context")
    assert_true("connection.close()" in service_py, "local service closes SQLite connections after requests")
    assert_true("Application Support" in service_py, "local service defaults to stable OS app-data storage on macOS")
    assert_true("migrate_repo_db_if_needed" in service_py, "local service can migrate existing repo-local memory")
    assert_true("uses_default_db_path" in service_py, "repo-local migration is limited to the default database path")
    assert_true("absolute_db_path(self.db_path)" in service_py, "health endpoint exposes the absolute database path")
    assert_true("send_sqlite_export" in service_py, "local service exposes a human-only SQLite export endpoint")
    assert_true("/export-memory" in service_py, "local service routes memory export outside MCP tools")
    non_loopback = subprocess.run(
        [
            sys.executable,
            "tools/local-memory-service.py",
            "--db-path",
            str(ROOT / ".daemon-mode" / "non-loopback-refusal.sqlite3"),
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "4339",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert_true(non_loopback.returncode != 0 and "only serves on a loopback host" in non_loopback.stderr, "local service refuses non-loopback network exposure")
    assert_true((DB_PATH.parent.stat().st_mode & 0o777) == 0o700, "local memory directory uses owner-only permissions")
    assert_true((DB_PATH.stat().st_mode & 0o777) == 0o600, "local SQLite file uses owner-only permissions")
    spec = importlib.util.spec_from_file_location("daemon_mode_storage_smoke", ROOT / "tools" / "local-memory-service.py")
    service_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(service_module)
    with closing(service_module.open_db(DB_PATH)) as connection:
        secure_delete = connection.execute("PRAGMA secure_delete").fetchone()[0]
        callback_status = service_module.protection_status_for_url(
            connection,
            "https://example.com/native/oauth2callback?code=secret&state=opaque",
        )
    assert_true(secure_delete == 1, "SQLite secure deletion is enabled for local memory")
    assert_true(callback_status.get("protected"), "historical credential-bearing URLs remain protected at read time")
    historical_callback = {
        "id": 99,
        "url": "https://example.com/native/oauth2callback?code=secret&state=opaque",
        "title": "Authentication callback",
        "domain": "example.com",
        "captured_at": "2026-06-25T00:00:00Z",
        "received_at": "2026-06-25T00:00:00Z",
        "text": "historical private callback text",
        "text_length": 32,
        "seen_count": 1,
        "last_seen_at": "2026-06-25T00:00:00Z",
    }
    historical_summary = service_module.row_to_summary(historical_callback)
    historical_detail = service_module.row_to_capture(historical_callback)
    assert_true(
        historical_summary.get("url") == "https://example.com/"
        and historical_summary.get("title") is None
        and historical_summary.get("textPreview") == "",
        "historical sensitive captures are redacted from list responses",
    )
    assert_true(
        historical_detail.get("url") == "https://example.com/"
        and historical_detail.get("title") is None
        and historical_detail.get("text") == "",
        "historical sensitive captures are redacted from detail responses",
    )
    for sidecar in (Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")):
        if sidecar.exists():
            assert_true((sidecar.stat().st_mode & 0o777) == 0o600, f"{sidecar.name} uses owner-only permissions")

    deletion_sentinel = "DAEMON_DELETE_SENTINEL_7F31C9E2"
    sample = {
        "source": "daemon-mode-smoke-test",
        "extensionVersion": "smoke",
        "navigationType": "smoke",
        "url": "https://example.com/daemon-mode-smoke",
        "title": "Daemon Mode Smoke Test",
        "domain": "example.com",
        "capturedAt": "2026-06-25T00:00:00Z",
        "text": f"Daemon Mode smoke test memory with searchable paprika evidence. {deletion_sentinel}",
        "textLength": 64,
    }

    first = fetch_json("/captures", method="POST", payload=sample)
    assert_true(first.get("ok") and not first.get("duplicate"), "first capture is stored")

    sensitive_old_capture = sample | {
        "url": "https://meet.google.com/abc-defg-hij",
        "title": "Meet Smoke",
        "domain": "meet.google.com",
        "text": "This simulates older sensitive data that should not be featured in Memory Pulse.",
        "textLength": 78,
    }
    status, _, _ = fetch_http_status("/captures", method="POST", payload=sensitive_old_capture)
    assert_true(status == 400, "default-protected capture is rejected at service ingestion")
    fetch_json(
        "/default-protection-categories/meetings-calls",
        method="POST",
        payload={"enabled": False},
    )
    fetch_json("/captures", method="POST", payload=sensitive_old_capture)
    fetch_json(
        "/default-protection-categories/meetings-calls",
        method="POST",
        payload={"enabled": True},
    )

    callback_capture = sample | {
        "url": "https://example.com/native/oauth2callback?code=secret&state=opaque",
        "title": "Authentication callback",
        "text": "This authorization callback must never be stored.",
        "textLength": 49,
    }
    status, _, _ = fetch_http_status("/captures", method="POST", payload=callback_capture)
    assert_true(status == 400, "credential-bearing callback URL is rejected at service ingestion")

    duplicate = fetch_json("/captures", method="POST", payload=sample)
    assert_true(duplicate.get("duplicate") and duplicate.get("seenCount") == 2, "duplicate capture increments seen count")

    stats = fetch_json("/stats")
    assert_true(stats.get("capture_count") == 2, "stats count unique active captures")
    assert_true(stats.get("today_capture_count") == 2, "stats count today's remembered captures")

    rebound_headers = {"Host": f"evil.example:{PORT}", "Sec-Fetch-Site": "same-origin"}
    for path in ("/captures?limit=5", "/retrieve?q=paprika", "/connect-summary", "/export-memory", "/answer?q=paprika"):
        status, headers, _ = fetch_http_status(path, headers=rebound_headers)
        assert_true(status == 403, f"DNS-rebound host cannot access {path.split('?')[0]}")
        assert_true(not headers.get("Access-Control-Allow-Origin"), "DNS-rebound responses expose no readable CORS origin")
    status, _, _ = fetch_http_status("/captures", method="DELETE", headers=rebound_headers)
    assert_true(status == 403, "DNS-rebound host cannot clear memory")
    status, _, _ = fetch_http_status(
        "/blocked-domains",
        method="POST",
        headers=rebound_headers,
        payload={"domain": "example.org"},
    )
    assert_true(status == 403, "DNS-rebound host cannot change protection rules")
    same_site_headers = {"Host": f"127.0.0.1:{PORT}", "Sec-Fetch-Site": "same-site"}
    status, _, _ = fetch_http_status("/health", headers=same_site_headers)
    assert_true(status == 403, "no-Origin same-site browser requests fail closed")

    evil_origin = {"Origin": "https://untrusted.example"}
    status, headers, _ = fetch_http_status("/export-memory", headers=evil_origin)
    assert_true(status == 403, "untrusted websites cannot export local memory")
    assert_true(not headers.get("Access-Control-Allow-Origin"), "rejected origins receive no readable CORS response")

    status, _, _ = fetch_http_status("/captures", method="DELETE", headers=evil_origin)
    assert_true(status == 403, "untrusted websites cannot bypass typed clear-all confirmation")
    assert_true(fetch_json("/stats").get("capture_count") == 2, "rejected cross-origin delete preserves local memory")

    status, _, _ = fetch_http_status("/captures", headers={"Sec-Fetch-Site": "cross-site"})
    assert_true(status == 403, "cross-site browser requests without an Origin header fail closed")

    for alias_origin in (f"http://localhost:{PORT}", f"http://[::1]:{PORT}"):
        status, headers, _ = fetch_http_status(
            "/health",
            headers={"Host": f"127.0.0.1:{PORT}", "Origin": alias_origin},
        )
        assert_true(status == 403, f"alternate loopback origin {alias_origin} cannot bypass the canonical origin gate")
        assert_true(not headers.get("Access-Control-Allow-Origin"), "rejected loopback aliases receive no readable CORS response")

    spoofed_extension_origin = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    status, headers, _ = fetch_http_status("/health", headers={"Origin": spoofed_extension_origin})
    assert_true(status == 403, "an unrelated extension cannot read Daemon endpoints")
    assert_true(not headers.get("Access-Control-Allow-Origin"), "a rejected extension receives no readable CORS response")
    status, _, _ = fetch_http_status("/export-memory", headers={"Origin": spoofed_extension_origin})
    assert_true(status == 403, "an unrelated extension cannot export memory")
    status, _, _ = fetch_http_status("/captures", method="DELETE", headers={"Origin": spoofed_extension_origin})
    assert_true(status == 403, "an unrelated extension cannot clear memory")
    status, _, _ = fetch_http_status(
        "/blocked-domains",
        method="POST",
        headers={"Origin": spoofed_extension_origin},
        payload={"domain": "example.org"},
    )
    assert_true(status == 403, "an unrelated extension cannot change protection rules")
    assert_true(fetch_json("/stats").get("capture_count") == 2, "rejected extension requests preserve local memory")

    extension_origin = "chrome-extension://hkpoimeiilpfajaikpckmdkpgdijciie"
    status, headers, _ = fetch_http_status("/health", headers={"Origin": extension_origin})
    assert_true(status == 200, "the local extension origin can still reach Daemon")
    assert_true(headers.get("Access-Control-Allow-Origin") == extension_origin, "allowed extension CORS is origin-scoped")

    export_headers, export_body = fetch_response("/export-memory")
    assert_true(export_headers.get_content_type() == "application/vnd.sqlite3", "memory export returns a SQLite content type")
    assert_true(not export_headers.get("Access-Control-Allow-Origin"), "same-origin export does not advertise wildcard CORS")
    assert_true(
        "attachment;" in export_headers.get("Content-Disposition", "")
        and "daemon-memory-" in export_headers.get("Content-Disposition", ""),
        "memory export downloads as a timestamped attachment",
    )
    assert_true(export_body.startswith(b"SQLite format 3"), "memory export body is a SQLite database")

    listed = fetch_json("/captures?limit=5")
    assert_true(len(listed.get("captures", [])) == 2, "recent captures list stored captures")

    pulse = fetch_json("/memory-pulse")
    assert_true(pulse.get("service", {}).get("status") == "ready", "memory pulse reports local service readiness")
    assert_true(pulse.get("stats", {}).get("today_capture_count") == 2, "memory pulse exposes today's remembered count")
    assert_true(pulse.get("today", {}).get("protectedCount") == 0, "memory pulse starts with an exact zero protected-today count")
    assert_true(pulse.get("today", {}).get("lastExtensionActivityAt"), "memory pulse exposes the latest extension activity time")
    assert_true(len(pulse.get("recentCaptured", [])) == 1, "memory pulse includes recent saved captures")
    assert_true(
        pulse["recentCaptured"][0]["domain"] != "meet.google.com",
        "memory pulse does not feature sensitive-domain captures",
    )
    assert_true(pulse.get("suggestedQuestion"), "memory pulse suggests one local recall question")

    event = fetch_json(
        "/capture-events",
        method="POST",
        payload={
            "status": "skipped",
            "type": "content-script-skipped-before-read",
            "message": "Skipped page before reading text",
            "reason": "sensitive-field:input[type=password]",
            "url": "https://example.com/login?token=secret",
            "title": "Private sign-in",
            "domain": "example.com",
            "occurredAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        },
    )
    assert_true(event.get("ok") and event.get("status") == "skipped", "capture event endpoint stores protected events")

    protected_pulse = fetch_json("/memory-pulse")
    assert_true(len(protected_pulse.get("recentProtected", [])) >= 1, "memory pulse includes protected activity")
    assert_true(protected_pulse.get("today", {}).get("protectedCount") >= 1, "memory pulse reports exact protected activity for today")
    newest_protected = protected_pulse["recentProtected"][0]
    assert_true(newest_protected.get("url") == "https://example.com/", "protected event strips path and credential query data")
    assert_true(newest_protected.get("title") is None, "protected event does not retain a page title")

    results = fetch_json(f"/search?q={parse.quote('paprika')}&limit=5")
    assert_true(len(results.get("results", [])) == 1, "search finds stored text")

    capture_id = results["results"][0]["id"]
    shown = fetch_json(f"/captures/{capture_id}")
    assert_true(shown.get("capture", {}).get("seen_count") == 2, "detail endpoint shows dedupe seen count")

    reader = sqlite3.connect(DB_PATH)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT text FROM captures WHERE id = ?", (capture_id,)).fetchone()
        status, _, _ = fetch_http_status(f"/captures/{capture_id}", method="DELETE")
        assert_true(status == 400, "busy WAL cleanup reports a retryable deletion error after bounded attempts")
    finally:
        reader.close()

    deleted = fetch_json(f"/captures/{capture_id}", method="DELETE")
    assert_true(
        deleted.get("alreadyAbsent") and deleted.get("cleanupCompleted"),
        "repeating delete after a busy WAL finishes privacy cleanup idempotently",
    )

    after_delete = fetch_json(f"/search?q={parse.quote('paprika')}&limit=5")
    assert_true(len(after_delete.get("results", [])) == 0, "deleted capture is absent from search")
    with closing(sqlite3.connect(DB_PATH)) as connection:
        capture_rows = connection.execute("SELECT COUNT(*) FROM captures WHERE id = ?", (capture_id,)).fetchone()[0]
        fts_rows = connection.execute("SELECT COUNT(*) FROM captures_fts WHERE rowid = ?", (capture_id,)).fetchone()[0]
    assert_true(capture_rows == 0 and fts_rows == 0, "individual deletion removes saved page text and its search index row")
    sentinel_bytes = deletion_sentinel.encode("utf-8")
    residual_paths = [path for path in (DB_PATH, Path(f"{DB_PATH}-wal"), Path(f"{DB_PATH}-shm")) if path.exists()]
    assert_true(all(sentinel_bytes not in path.read_bytes() for path in residual_paths), "deleted page text is absent from isolated SQLite, WAL, and SHM bytes")

    second = sample | {
        "url": "https://example.com/daemon-mode-clear-smoke",
        "text": "Second smoke capture to prove clear all deletes captured memory.",
        "textLength": 62,
    }
    fetch_json("/captures", method="POST", payload=second)
    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            """
            INSERT INTO agent_queries (
              agent_name, tool_name, query_text, status, evidence_count, source, created_at
            ) VALUES ('smoke', 'search_memory', 'private reset query', 'hit', 1, 'smoke-reset', ?)
            """,
            (dt.datetime.now(dt.timezone.utc).isoformat(),),
        )
        connection.commit()
    cleared = fetch_json("/captures", method="DELETE")
    assert_true(cleared.get("deleted") and cleared.get("deletedCount") >= 1, "clear all removes captured memory")

    clear_stats = fetch_json("/stats")
    assert_true(clear_stats.get("capture_count") == 0, "stats are empty after clear all")
    with closing(sqlite3.connect(DB_PATH)) as connection:
        query_rows = connection.execute("SELECT COUNT(*) FROM agent_queries").fetchone()[0]
    assert_true(query_rows == 0, "clear all removes local agent query history while protection rules stay")


def run_today_timezone_checks():
    today = {
        "source": "daemon-mode-smoke-test",
        "extensionVersion": "smoke",
        "navigationType": "smoke",
        "url": "https://example.com/local-today",
        "title": "Local Today",
        "domain": "example.com",
        "capturedAt": "2026-06-25T00:00:00Z",
        "text": "This capture should count for the current local day.",
        "textLength": 56,
    }
    previous_evening = today | {
        "url": "https://example.com/local-yesterday-evening",
        "title": "Local Yesterday Evening",
        "text": "This capture has a UTC date that can look like today, but local time says yesterday.",
        "textLength": 83,
    }

    first = fetch_json("/captures", method="POST", payload=today)
    second = fetch_json("/captures", method="POST", payload=previous_evening)

    with closing(sqlite3.connect(DB_PATH)) as connection:
        connection.execute(
            """
            UPDATE captures
            SET received_at = ?
            WHERE id = ?
            """,
            (previous_los_angeles_evening_utc(), second["id"]),
        )
        connection.commit()

    stats = fetch_json("/stats")
    assert_true(stats.get("capture_count") == 2, "timezone smoke stores both local-day fixtures")
    assert_true(
        stats.get("today_capture_count") == 1,
        "today count uses the machine-local day instead of the UTC calendar date",
    )

    cleared = fetch_json("/captures", method="DELETE")
    assert_true(cleared.get("deleted"), "timezone smoke cleanup clears fixture captures")


def run_user_blocklist_checks():
    blocked = fetch_json(
        "/blocked-domains",
        method="POST",
        payload={"domain": "private.example.com", "reason": "smoke-test"},
    )
    assert_true(blocked.get("domain") == "private.example.com", "user blocklist stores a domain")

    check = fetch_json(f"/blocklist/check?url={parse.quote('https://sub.private.example.com/account')}")
    assert_true(
        check.get("blocked") and check.get("reason") == "user-blocked-domain:private.example.com",
        "user blocklist matches subdomains",
    )

    rejected = False
    try:
        fetch_json(
            "/captures",
            method="POST",
            payload={
                "source": "daemon-mode-smoke-test",
                "extensionVersion": "smoke",
                "navigationType": "smoke",
                "url": "https://private.example.com/blocked",
                "title": "Blocked Capture",
                "domain": "private.example.com",
                "capturedAt": "2026-06-25T00:00:00Z",
                "text": "This should not be stored.",
                "textLength": 26,
            },
        )
    except RuntimeError:
        rejected = True

    assert_true(rejected, "service rejects captures from user-blocked domains")

    fetch_json(
        "/capture-events",
        method="POST",
        payload={
            "status": "blocked",
            "type": "blocked-before-read",
            "message": "Skipped blocked page before reading text",
            "reason": "user-blocked-domain:private.example.com",
            "url": "https://sub.private.example.com/account",
            "domain": "sub.private.example.com",
            "occurredAt": "2026-06-25T00:10:00Z",
        },
    )

    protection = fetch_json("/protection-summary")
    assert_true(
        len(protection.get("defaultCategories", [])) == 8,
        "protection summary exposes the starter default categories",
    )
    assert_true(
        [item.get("id") for item in protection.get("defaultCategories", [])[:5]]
        == ["private-messaging", "banking-payments", "password-login", "private-ai-chats", "email-inboxes"],
        "protection summary orders the highest-sensitivity defaults first",
    )
    assert_true(
        all(item.get("examples") and item.get("protectedData") for item in protection.get("defaultCategories", [])),
        "protection summary includes compact examples and protected-data labels",
    )
    assert_true(
        all("enabled" in item for item in protection.get("defaultCategories", [])),
        "protection summary exposes default category preference state",
    )
    assert_true(
        any(item.get("sites") for item in protection.get("defaultCategories", [])),
        "protection summary exposes specific default protected sites",
    )
    assert_true(
        all(any(site.get("enabled") is False for site in item.get("sites", [])) for item in protection.get("defaultCategories", [])),
        "protection summary includes not-protected starter examples in every category",
    )
    default_check = fetch_json(
        f"/default-protection/check?url={parse.quote('https://mail.google.com/mail/u/0/#inbox')}&reason={parse.quote('blocked-domain:mail.google.com')}"
    )
    assert_true(
        default_check.get("categoryId") == "email-inboxes" and default_check.get("siteId") == "email-gmail" and not default_check.get("disabled"),
        "default protection check maps static blocks to enabled sites",
    )
    not_protected_starter_check = fetch_json(
        f"/default-protection/check?url={parse.quote('https://mail.yahoo.com/d/folders/1')}&reason={parse.quote('blocked-domain:mail.yahoo.com')}"
    )
    assert_true(
        not_protected_starter_check.get("siteId") == "email-yahoo"
        and not_protected_starter_check.get("categoryId") == "email-inboxes",
        "not-protected starter sites are still identifiable before preference overrides",
    )
    assert_true(
        not_protected_starter_check.get("disabled"),
        "not-protected starter sites allow capture until the user protects them",
    )
    protected_starter = fetch_json(
        "/default-protection-sites/email-yahoo",
        method="POST",
        payload={"enabled": True},
    )
    assert_true(
        protected_starter.get("siteId") == "email-yahoo" and protected_starter.get("enabled") is True,
        "not-protected starter sites can be switched to protected",
    )
    protected_starter_check = fetch_json(
        f"/default-protection/check?url={parse.quote('https://mail.yahoo.com/d/folders/1')}&reason={parse.quote('blocked-domain:mail.yahoo.com')}"
    )
    assert_true(
        protected_starter_check.get("siteId") == "email-yahoo" and not protected_starter_check.get("disabled"),
        "protected starter override blocks the site again",
    )
    fetch_json(
        "/default-protection-sites/email-yahoo",
        method="POST",
        payload={"enabled": False},
    )
    disabled = fetch_json(
        "/default-protection-sites/email-gmail",
        method="POST",
        payload={"enabled": False},
    )
    assert_true(
        disabled.get("siteId") == "email-gmail" and disabled.get("enabled") is False,
        "specific default protected sites can be turned off",
    )
    disabled_gmail_check = fetch_json(
        f"/default-protection/check?url={parse.quote('https://mail.google.com/mail/u/0/#inbox')}&reason={parse.quote('blocked-domain:mail.google.com')}"
    )
    assert_true(disabled_gmail_check.get("disabled"), "disabled default protected sites allow extension override")
    outlook_check = fetch_json(
        f"/default-protection/check?url={parse.quote('https://outlook.live.com/mail/0/')}&reason={parse.quote('blocked-domain:outlook.live.com')}"
    )
    assert_true(
        outlook_check.get("siteId") == "email-outlook" and not outlook_check.get("disabled"),
        "disabling Gmail does not disable Outlook protection",
    )
    enabled = fetch_json(
        "/default-protection-sites/email-gmail",
        method="POST",
        payload={"enabled": True},
    )
    assert_true(enabled.get("enabled") is True, "specific default protected sites can be turned back on")
    removed_starter_site = fetch_json("/default-protection-sites/banking-paypal", method="DELETE")
    assert_true(
        removed_starter_site.get("removed") and removed_starter_site.get("hidden"),
        "specific default protected sites can be removed from starter lists",
    )
    after_remove = fetch_json("/protection-summary")
    banking_category = next(
        item for item in after_remove.get("defaultCategories", []) if item.get("id") == "banking-payments"
    )
    assert_true(
        all(site.get("id") != "banking-paypal" for site in banking_category.get("sites", [])),
        "removed starter sites disappear from protection summary",
    )
    removed_check = fetch_json(
        f"/default-protection/check?url={parse.quote('https://paypal.com/dashboard')}&reason={parse.quote('blocked-domain:paypal.com')}"
    )
    assert_true(removed_check.get("disabled"), "removed starter sites are no longer protected by default")
    restored_starter_site = fetch_json(
        "/default-protection-sites/banking-paypal",
        method="POST",
        payload={"enabled": True},
    )
    assert_true(
        restored_starter_site.get("enabled") is True,
        "removed starter sites can be restored by enabling them again",
    )
    assert_true(
        any(item.get("domain") == "private.example.com" for item in protection.get("userBlocked", [])),
        "protection summary includes user-blocked domains",
    )
    assert_true(
        any(item.get("lastProtectedAt") for item in protection.get("userBlocked", [])),
        "protection summary links blocked domains to recent protected activity",
    )
    assert_true(
        protection.get("recentProtected", [{}])[0].get("reason") == "user-blocked-domain:private.example.com",
        "protection summary keeps protected activity reason without snippets",
    )
    assert_true(
        "snippet" not in json.dumps(protection.get("recentProtected", [])).lower(),
        "protected activity does not expose captured snippets",
    )

    removed = fetch_json("/blocked-domains/private.example.com", method="DELETE")
    assert_true(removed.get("deleted"), "user blocklist can remove a domain")

    unblocked = fetch_json(f"/blocklist/check?url={parse.quote('https://private.example.com/account')}")
    assert_true(not unblocked.get("blocked"), "removed user-blocked domain no longer blocks future capture")


def run_retrieval_checks():
    captures = [
        {
            "source": "daemon-mode-smoke-test",
            "extensionVersion": "smoke",
            "navigationType": "smoke",
            "url": "https://example.com/product-prototype",
            "title": "Prototype Notes",
            "domain": "example.com",
            "capturedAt": "2026-06-25T00:00:00Z",
            "text": "The product prototype should return compact local evidence before any AI answer is created.",
            "textLength": 88,
        },
        {
            "source": "daemon-mode-smoke-test",
            "extensionVersion": "smoke",
            "navigationType": "smoke",
            "url": "https://example.com/recipe",
            "title": "Recipe Notes",
            "domain": "example.com",
            "capturedAt": "2026-06-25T00:00:00Z",
            "text": "Paprika belongs in this recipe note, not in the product retrieval evidence test.",
            "textLength": 78,
        },
        {
            "source": "daemon-mode-smoke-test",
            "extensionVersion": "smoke",
            "navigationType": "smoke",
            "url": "https://search.example.com/?sourceid=chrome",
            "title": "Tracking Parameter Noise",
            "domain": "search.example.com",
            "capturedAt": "2026-06-25T00:00:00Z",
            "text": "This page has browser tracking parameters but no useful extension privacy content.",
            "textLength": 77,
        },
        {
            "source": "daemon-mode-smoke-test",
            "extensionVersion": "smoke",
            "navigationType": "smoke",
            "url": "https://developer.example.com/chrome-extension-privacy",
            "title": "Chrome Extension Privacy Guide",
            "domain": "developer.example.com",
            "capturedAt": "2026-06-25T00:00:00Z",
            "text": "Chrome extension privacy guidance explains permissions, local capture, and user data review.",
            "textLength": 87,
        },
    ]

    for capture in captures:
        fetch_json("/captures", method="POST", payload=capture)

    retrieval = fetch_json(f"/retrieve?q={parse.quote('prototype evidence')}&limit=2&maxTokens=500")
    assert_true(not retrieval.get("noEvidence"), "retrieve returns local evidence")
    assert_true(len(retrieval.get("evidence", [])) >= 1, "retrieve includes at least one evidence object")
    assert_true(retrieval.get("estimatedTokens") <= retrieval.get("maxTokens"), "retrieve reports a bounded token estimate")

    first = retrieval["evidence"][0]
    assert_true("snippet" in first and "url" in first and "estimatedTokens" in first, "evidence object is compact and citation-ready")

    natural = fetch_json(f"/retrieve?q={parse.quote('where did I read about chrome extension privacy')}&limit=3&maxTokens=700")
    assert_true(not natural.get("noEvidence"), "retrieve handles natural-language recall questions")
    assert_true(natural.get("terms") == ["chrome", "extension", "privacy"], "retrieve removes low-signal question words")
    assert_true(
        natural["evidence"][0]["url"] == "https://developer.example.com/chrome-extension-privacy",
        "retrieve prefers content matches over URL tracking noise",
    )
    assert_true(
        "chrome" in natural["evidence"][0].get("matchedTerms", []),
        "retrieve reports matched terms for evidence inspection",
    )

    weak_match = fetch_json(f"/retrieve?q={parse.quote('chrome nonexistentzzzz')}&limit=3&maxTokens=700")
    assert_true(
        weak_match.get("noEvidence") and len(weak_match.get("evidence", [])) == 0,
        "retrieve rejects weak one-term evidence for multi-term questions",
    )

    missing = fetch_json(f"/retrieve?q={parse.quote('nonexistentzzzz')}&limit=2&maxTokens=500")
    assert_true(missing.get("noEvidence") and len(missing.get("evidence", [])) == 0, "retrieve reports no local evidence")

    answer = fetch_json(f"/answer?q={parse.quote('prototype evidence')}&limit=2&maxTokens=500")
    assert_true(answer.get("status") == "provider_disabled", "answer preserves evidence when provider calls are disabled")
    assert_true(answer.get("usesAi") is False, "answer does not call AI when disabled")
    assert_true(len(answer.get("selectedEvidence", [])) >= 1, "answer contract includes selected evidence")
    assert_true(len(answer.get("citations", [])) >= 1, "answer contract includes citations")
    assert_true(
        answer.get("provider", {}).get("status") == "provider_disabled",
        "answer reports disabled provider status",
    )
    assert_true("selected evidence" in answer.get("answer", {}).get("text", ""), "answer explains retained evidence")

    no_answer = fetch_json(f"/answer?q={parse.quote('chrome nonexistentzzzz')}&limit=3&maxTokens=700")
    assert_true(no_answer.get("status") == "no_evidence", "answer contract refuses weak evidence")
    assert_true(len(no_answer.get("citations", [])) == 0, "no-evidence answer has no citations")
    assert_true(no_answer.get("provider", {}).get("status") == "not_called", "no-evidence answer skips provider calls")


def run_inspector_checks():
    html = fetch_text("/")
    protection_html = fetch_text("/protection")
    connect_html = fetch_text("/connect")
    assert_true('id="pulse-title"' in html, "inspector exposes Memory Pulse")
    assert_true("Memory Pulse" in html, "inspector labels the first-session memory pulse")
    assert_true("/assets/brand/dmn-mark.png" in html, "inspector header uses the selected DMN logo mark")
    assert_true('id="data-location"' in html, "inspector exposes local data location")
    assert_true('class="query-panel"' in html, "inspector exposes the ask and search panel")
    assert_true("color-scheme: light;" in html, "inspector uses paper-light as the design of record")
    assert_true("prefers-color-scheme" not in html, "inspector does not switch to dark mode automatically")
    assert_true('"Daemon UI"' in html, "inspector self-hosts the Atkinson-based UI voice")
    assert_true('"Daemon Mono"' in html, "inspector self-hosts the Commit Mono data voice")
    assert_true('"Daemon Brief"' in html, "inspector self-hosts the italic brief voice")
    assert_true("Geist" not in html, "inspector no longer uses the generic Geist font stack")
    assert_true("button.danger:hover:not(:disabled)" in html, "inspector keeps destructive hover separate from primary teal")
    assert_true(".capture-card:hover:not(:disabled)" in html, "inspector has scoped capture-row hover styles")
    assert_true("border-left-color" not in html, "capture-row hover does not use a colliding left border marker")
    assert_true("inset 2px 0" not in html, "capture-row hover does not use an inset left bar")
    assert_true("font-weight: 400;" in html, "capture rows reset button font weight for readable previews")
    assert_true("className = \"recall-action\"" in html, "memory pulse recall action uses a quiet text treatment")
    assert_true("Open page" in html, "inspector uses user-facing open action copy")
    assert_true("Block site" in html, "inspector uses user-facing block action copy")
    assert_true("Open URL" not in html, "inspector avoids technical open action copy")
    assert_true("Block domain" not in html, "inspector avoids technical block action copy")
    assert_true(".detail-actions .secondary:hover:not(:disabled)" in html, "inspector action buttons have scoped hover styles")
    assert_true("padding: 7px 11px" in html, "inspector action buttons keep text away from borders")
    assert_true('class="workspace-grid"' in html, "inspector exposes the river and workspace layout")
    assert_true('id="retrieve-form"' in html, "inspector exposes the retrieval form")
    assert_true("Ask your memory" in html, "inspector gives recall one primary ask field")
    assert_true('id="ask-submit"' in html, "inspector exposes one smart ask action")
    assert_true("Find evidence" in html, "inspector can fall back to local evidence when AI is not ready")
    assert_true("data-origin-button" in html, "inspector trials the origin-fill treatment on the ask action")
    assert_true("button.origin-action::before" in html, "origin-fill button renders a pointer-origin cover layer")
    assert_true("getOriginCoverDiameter" in html, "origin-fill button computes cover size without React dependencies")
    assert_true("button:hover:not(:disabled):not(.origin-action):not(.capture-card)" in html, "capture rows are not overridden by generic button hover")
    assert_true("button:hover:not(:disabled):not(.origin-action):not(.capture-card):not(.tab-button)" in html, "dashboard tabs do not inherit heavy button hover")
    assert_true("--origin-fill: var(--rose)" in html, "destructive origin-fill uses the danger accent")
    assert_true("--origin-active-color: var(--paper-2)" in html, "destructive origin-fill keeps text readable on red")
    assert_true('button.origin-action.danger[data-origin-active="true"] .origin-label' in html, "destructive origin-fill flips label color immediately")
    assert_true("border-color: var(--origin-active-border, var(--origin-fill, var(--teal)))" in html, "detail destructive buttons share origin-fill contrast variables")
    assert_true('id="refresh" class="secondary origin-action"' in html, "header refresh action uses origin-fill treatment")
    assert_true('id="clear-all" class="danger origin-action"' in html, "destructive header action uses origin-fill treatment")
    assert_true('id="open-url" class="secondary origin-action"' in html, "inspector open action uses origin-fill treatment")
    assert_true('id="delete-capture" class="danger origin-action"' in html, "inspector delete action uses origin-fill treatment")
    assert_true('className = "secondary origin-action"' in html, "generated evidence actions use origin-fill treatment")
    assert_true('prepareOriginButton(button, "Retrieve local evidence")' in html, "memory pulse recall action uses origin-fill treatment")
    assert_true("blockDomain.textContent" not in html, "inspector block action preserves origin-fill label markup")
    assert_true(".retrieve-row input:focus-visible" in html, "ask field has a scoped premium focus state")
    assert_true("0 0 0 3px rgba(41, 105, 113, 0.08)" in html, "ask field focus uses a soft wash instead of a hard teal outline")
    assert_true("#ask-submit.origin-action" in html, "memory ask action matches compact protection form sizing")
    assert_true("#refresh.origin-action" in html and "#clear-all.origin-action" in html, "header actions match the memory ask pill treatment")
    assert_true(".detail-actions .origin-action.secondary" in html, "inspector actions match the memory ask pill treatment")
    assert_true("Exact text search" not in html, "inspector removes exact search from the default dashboard")
    assert_true("Evidence limits" not in html, "inspector hides retrieval tuning from the default dashboard")
    assert_true('/captures?limit=10' in html, "inspector keeps the default recent river to 10 captures")
    assert_true("#${capture.id} ·" not in html, "inspector hides internal capture IDs from the default river")
    assert_true("Delete capture #" not in html, "inspector delete confirmation avoids internal capture IDs")
    assert_true("scrollInspectorIntoView" in html, "inspector scrolls selected lower captures back into view")
    assert_true("resetDashboardToRecent" in html, "inspector can return from ask to the normal dashboard")
    assert_true('addEventListener("search"' in html, "inspector handles native search-field clear buttons")
    assert_true('id="provider-status"' in html, "inspector exposes answer provider readiness")
    assert_true('id="protection-tab"' in html, "inspector exposes the Protection tab")
    assert_true('id="protection-panel"' in html, "inspector exposes the Protection surface")
    assert_true('id="protection-panel"' in protection_html, "inspector serves the Protection route directly")
    assert_true('id="connect-tab"' in html, "inspector exposes the Connect tab")
    assert_true('id="connect-panel"' in html, "inspector exposes the Connect surface")
    assert_true('id="connect-panel"' in connect_html, "inspector serves the Connect route directly")
    assert_true('protection: "/protection"' in html, "inspector defines a stable Protection URL")
    assert_true('connect: "/connect"' in html, "inspector defines a stable Connect URL")
    assert_true("dashboard-surface-in" in html, "dashboard tabs use a subtle panel transition")
    assert_true("DASHBOARD_ORDER" in html, "dashboard tab transitions know navigation direction")
    assert_true("data-transition-direction" in html, "dashboard panels expose transition direction state")
    assert_true("window.history[method]" in html, "inspector updates the URL when tabs change")
    assert_true('addEventListener("popstate"' in html, "inspector follows browser back and forward navigation")
    assert_true("dashboardModeFromPath(window.location.pathname)" in html, "inspector derives the initial tab from the URL")
    assert_true("Block sites Daemon should never read." in html, "Protection tab leads with the user control job")
    assert_true("Sites you blocked" in html, "Protection tab makes user-added blocked domains the main list")
    assert_true("Add site" in html, "Protection tab uses a clear add action")
    assert_true("Protected by default" in html, "Protection tab keeps default protections in the right rail")
    assert_true("Show recent protection log" not in html, "Protection tab removes the recent protection log from the main surface")
    assert_true("Recent protected activity" not in html, "Protection tab no longer shows a tall activity feed by default")
    assert_true("Selected site" not in html, "Protection tab removes selected-site workflow from the main surface")
    assert_true("category-card" not in html, "default protections no longer render as large cards")
    assert_true("category-row" in html, "default protections render as compact rows")
    assert_true("category-examples" not in html and "category-keywords" not in html, "default protection rows omit bulky collapsed metadata")
    assert_true("category-apps" in html and "site.label" in html, "default protection rows show compact app names")
    assert_true("${protectedCount}/${sites.length} protected" in html, "default protection mixed counts use compact status copy")
    assert_true("category-edit" in html, "default protections expose edit controls")
    assert_true("site-toggle" in html, "default protections expose per-site controls")
    assert_true("site-row" in html and "site-domain" in html, "default edit rows expose exact domains")
    assert_true("starter-remove" in html, "default edit rows can remove starter items")
    assert_true("Not protected" in html and "Protected" in html, "default site controls use clear protection-state wording")
    assert_true("toggleLabel.textContent = site.label" not in html, "default site state pills do not repeat app names")
    assert_true("color-mix(in srgb, var(--rose) 7%, var(--paper-2))" in html, "not-protected site pills use a quiet danger tint")
    assert_true("Use Edit to mark starter sites Protected or Not protected" in html, "default rail explains protection wording once")
    assert_true("category-edit origin-action" in html and "site-toggle origin-action" in html, "default controls use origin-fill treatment")
    assert_true("category-custom-add" in html, "default category edit mode can add custom sites")
    assert_true("/default-protection-sites/" in html, "default protected site controls persist preference changes")
    assert_true("domain-empty" in html, "Protection tab has a useful empty state for blocked sites")
    assert_true("add-domain-success" in html, "Protection tab has inline success feedback after blocking")
    assert_true("Existing saved memory stays until you delete it" in html, "Protection tab separates future blocking from deleting old memory")
    assert_true("/protection-summary" in html, "Protection tab loads a bounded summary endpoint")
    assert_true("/connect-summary" in html, "Connect tab loads a bounded setup and query-log endpoint")
    assert_true("Agent memory is connected." in html, "Connect tab leads with connection proof")
    assert_true("Test from your agent" not in html, "Connect tab avoids a bulky permanent test block")
    assert_true("agent-proof-summary" in html and "agent-test-hint" in html, "Connect tab keeps proof and first-run hint in the agent access rail")
    assert_true("Want to test it?" in html, "Connect tab uses an optional compact first-run test hint")
    assert_true("Agent access" in html, "Connect tab exposes owner-visible agent access")
    assert_true("Dogfood proof" not in html, "Connect tab avoids internal dogfood copy")
    assert_true("Connect another agent" in html, "Connect tab keeps setup secondary")
    assert_true("Setup commands are generated for this machine" in html, "Connect tab explains machine-local setup paths")
    assert_true("client-snippet" in html, "Connect tab expands setup inline with the selected client")
    assert_true("selectConnectClient(client.id" in html, "Connect tab uses a single-row setup accordion handler")
    assert_true("updateClientAccordion(nextClientId" in html, "Connect tab updates client setup rows in place")
    assert_true("snippet.inert = !isSelected" in html, "Connect tab keeps collapsed setup snippets out of focus order")
    assert_true("client-snippet-inner" in html and "grid-template-rows 320ms" in html, "Connect tab uses CSS-grid setup animation without measured height")
    assert_true("style.height" not in html and "scrollHeight" not in html, "Connect tab avoids JS height measuring that can cause partial command-card reveals")
    assert_true("overflow-anchor: none" in html, "Connect tab prevents browser scroll anchoring during setup animation")
    assert_true("linear-gradient(90deg, color-mix(in srgb, var(--teal) 4%" not in html, "Connect tab does not paint a cheap open-row background block")
    assert_true("config-smoke" in html, "Connect tab filters internal smoke-check agent access rows")
    assert_true(".slice(0, 3)" in html, "Connect tab limits agent access to a compact recent proof")
    assert_true("agent-query-prompt" in html, "Connect tab clamps long agent questions in the access panel")
    assert_true("provider setup needed" in html, "Connect tab translates provider setup misses into user-facing labels")
    assert_true("before reading page text" in html, "Protection tab states protected pages are skipped before text read")
    assert_true("Data &amp; exit" in html, "Protection tab exposes data and exit controls")
    assert_true('id="export-memory"' in html, "Protection tab exposes a SQLite export action")
    assert_true("Removing the extension does not delete this file" in html, "Protection tab clarifies uninstall behavior")
    assert_true("Protected-site rules stay" in html, "clear-all confirmation clarifies protection rules stay")
    assert_true("DELETE MEMORY" in html, "clear-all requires a typed destructive confirmation")
    assert_true('id="latest-protected-proof"' in html, "Protection tab keeps latest protected proof visible")
    assert_true("No local evidence found. Daemon will not guess." in html, "no-evidence UI fails closed without guessing")
    font_headers, font_body = fetch_response("/assets/fonts/atkinson-hyperlegible-latin-400-normal.woff2")
    assert_true(font_headers.get_content_type() == "font/woff2", "inspector serves packaged font assets")
    assert_true(len(font_body) > 1000, "packaged font asset is not empty")
    logo_headers, logo_body = fetch_response("/assets/brand/dmn-mark.png")
    assert_true(logo_headers.get_content_type() == "image/png", "inspector serves the selected PNG DMN logo")
    assert_true(len(logo_body) > 1000, "selected PNG DMN logo is not empty")
    favicon_headers, favicon_body = fetch_response("/favicon.ico")
    assert_true(favicon_headers.get_content_type() == "image/png", "inspector favicon serves a PNG icon")
    assert_true(len(favicon_body) > 100, "inspector favicon asset is not empty")


def run_provider_status_checks():
    payload = fetch_json("/provider-status")
    provider = payload.get("provider", {})

    assert_true(provider.get("status") == "provider_disabled", "provider status reports disabled AI without a live call")
    assert_true(provider.get("ready") is False, "provider status marks disabled AI as not ready")
    assert_true(provider.get("canCallProvider") is False, "provider status does not allow provider calls when disabled")
    assert_true("OPENAI_API_KEY" not in json.dumps(payload), "provider status does not expose API keys")


def run_connect_summary_checks():
    payload = fetch_json("/connect-summary")
    clients = {client.get("name"): client for client in payload.get("clients", [])}
    tools = payload.get("mcp", {}).get("tools", [])
    tool_names = {tool.get("name") for tool in tools}

    assert_true(payload.get("service", {}).get("status") == "ready", "connect summary reports local MCP readiness")
    assert_true(payload.get("mcp", {}).get("readOnly") is True, "connect summary marks MCP tools read-only")
    assert_true(
        {"search_memory", "retrieve_evidence", "answer_from_evidence", "check_protection_status"}
        <= tool_names,
        "connect summary lists the read-only agent tools",
    )
    assert_true(
        not ({"delete_capture", "clear_captures", "export_memory", "set_blocklist"} & tool_names),
        "connect summary does not advertise mutating agent tools",
    )
    assert_true(
        {"Claude Code", "Codex", "Cursor", "Claude Desktop", "ChatGPT"} <= set(clients),
        "connect summary includes the expected agent clients",
    )
    assert_true("local stdio" in clients["Codex"].get("status", ""), "connect summary treats Codex as a local stdio client")
    assert_true("later remote MCP path" in clients["ChatGPT"].get("status", ""), "connect summary does not overclaim ChatGPT local-stdio support")
    assert_true(payload.get("recentAgentQueries") == [], "connect summary starts with an empty query log")
    assert_true(payload.get("agentProof", {}).get("status") == "waiting", "connect summary starts with waiting agent proof")
    hint = payload.get("firstRunHint", {})
    prompt_text = hint.get("prompt", "")
    assert_true(hint.get("id") == "recent-daemon-pages", "connect summary provides one optional first-run test hint")
    assert_true("Use Daemon" in prompt_text and "local evidence" in prompt_text, "connect summary hint teaches agent-visible Daemon invocation")
    assert_true("raw evidence snippets" in payload.get("logPolicy", {}).get("doesNotStore", ""), "connect summary states query-log privacy boundaries")


def run_dogfood_readout_checks():
    def readout(private=False, db_path=DB_PATH, require_provider=False, owner_confirmed_useful=False):
        command = [
            sys.executable,
            "tools/dogfood-readout.py",
            "--db-path",
            str(db_path),
            "--days",
            "14",
        ]
        if private:
            command.append("--private")
        if require_provider:
            command.append("--require-provider")
        if owner_confirmed_useful:
            command.append("--owner-confirmed-useful")
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout

    def seed_rows(rows):
        with closing(sqlite3.connect(DB_PATH)) as connection:
            connection.execute("DELETE FROM agent_queries")
            connection.executemany(
                """
                INSERT INTO agent_queries (
                  agent_name,
                  tool_name,
                  query_text,
                  status,
                  evidence_count,
                  uses_ai,
                  provider_name,
                  provider_status,
                  source,
                  error,
                  filtered_protected_count,
                  duration_ms,
                  created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'mcp', ?, ?, ?, ?)
                """,
                rows,
            )
            connection.commit()

    def row(
        index,
        tool_name="search_memory",
        status="hit",
        evidence_count=1,
        provider_status="",
        filtered_protected_count=0,
        duration_ms=20,
        day_offset=None,
    ):
        created_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            days=index % 3 if day_offset is None else day_offset
        )
        return (
            "dogfood-test",
            tool_name,
            f"private regression query {index}",
            status,
            evidence_count,
            0,
            "openai" if provider_status else "",
            provider_status,
            "",
            filtered_protected_count,
            duration_ms,
            created_at.isoformat(),
        )

    output = readout()
    assert_true("# Daemon Mode 0.5.8 Dogfood Readout" in output, "dogfood readout renders the upgraded PM report")
    assert_true("Raw queries included: no" in output, "dogfood readout redacts raw queries by default")
    assert_true("## Miss Diagnostics" in output, "dogfood readout explains why recall missed")
    assert_true("Launch recommendation:" in output, "dogfood readout gives an explicit launch recommendation")
    assert_true("Readout: `insufficient_data`" in output, "dogfood readout fails closed when there is no dogfood data")
    assert_true("No non-smoke calls in this window." in output, "dogfood readout explains an empty window")

    rows = [row(index, "check_protection_status", "checked", 0) for index in range(11)]
    rows.append(row(11))
    seed_rows(rows)
    output = readout()
    assert_true("Readout: `keep_dogfooding`" in output, "protection checks cannot inflate repeated recall usage")
    assert_true("Low repeated usage: yes" in output, "dogfood readout names low repeated usage")

    seed_rows([row(index, day_offset=0) for index in range(12)])
    output = readout()
    assert_true("Readout: `keep_dogfooding`" in output, "one-day-only usage cannot produce a launch candidate")
    assert_true("One-day-only usage: yes" in output, "dogfood readout names one-day-only usage")

    seed_rows(
        [row(index, "answer_from_evidence", "provider_error", 1, "provider_error") for index in range(12)]
    )
    output = readout()
    assert_true("Readout: `fix_reliability`" in output, "provider errors fail the dogfood reliability gate")

    retrieval_rows = [row(index, status="miss", evidence_count=0) for index in range(7)]
    retrieval_rows.extend(row(index + 7) for index in range(5))
    seed_rows(retrieval_rows)
    output = readout()
    assert_true("Readout: `fix_retrieval`" in output, "repeated evidence misses route to retrieval work")
    assert_true("No local evidence: 7 call(s)" in output, "dogfood readout separates genuine no-evidence misses")

    seed_rows(
        [
            row(index, status="miss", evidence_count=0, filtered_protected_count=1)
            for index in range(12)
        ]
    )
    output = readout()
    assert_true("Readout: `keep_dogfooding`" in output, "correct protection filtering is not mislabeled as retrieval failure")
    assert_true("Protected evidence filtered: 12 call(s), 12 internal match(es)" in output, "dogfood readout separates protected misses without exposing content")

    seed_rows(
        [
            row(index, "answer_from_evidence", "provider_missing_key", 1, "provider_missing_key")
            for index in range(12)
        ]
    )
    output = readout()
    assert_true("Readout: `public_story_candidate`" in output, "optional provider credentials do not block evidence-only launch readiness")
    assert_true("Provider-backed answers required: no" in output, "dogfood readout defaults to valid evidence-only use")
    provider_required_output = readout(require_provider=True)
    assert_true("Readout: `fix_setup`" in provider_required_output, "explicitly required provider setup routes missing credentials to setup work")

    seed_rows([row(index) for index in range(12)])
    output = readout()
    assert_true("Readout: `public_story_candidate`" in output, "repeated evidence hits can produce a technical story candidate")
    assert_true("owner-confirmed usefulness" in output, "positive readout still requires qualitative usefulness proof")
    confirmed_output = readout(owner_confirmed_useful=True)
    assert_true("Proceed with the scoped technical-preview launch checklist" in confirmed_output, "owner usefulness confirmation clears the qualitative launch hold")
    assert_true("private regression query" not in output, "default dogfood readout keeps query text private")
    assert_true(str(DB_PATH) not in output, "default dogfood readout hides the absolute database path")
    assert_true("20 ms median / 20 ms p95" in output, "dogfood readout summarizes recorded MCP latency")

    activity_rows = [row(index) for index in range(11)]
    activity_rows.append(row(11, "get_activity_summary", "summarized", 547, day_offset=0))
    seed_rows(activity_rows)
    activity_output = readout()
    assert_true("get_activity_summary | summarized | aggregate summary" in activity_output, "activity-summary calls render as aggregates")
    assert_true("547 evidence" not in activity_output, "activity-summary diagnostics are not mislabeled as evidence counts")

    private_output = readout(private=True)
    assert_true("private regression query" in private_output, "private dogfood mode can include owner query text")
    assert_true(str(DB_PATH) in private_output, "private dogfood mode can include the exact database path")
    assert_true("Private mode: yes" in private_output, "private dogfood mode is explicit in the report")

    with closing(sqlite3.connect(DB_PATH)) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_queries)").fetchall()}
    assert_true("filtered_protected_count" in columns, "agent query schema stores aggregate protected-filter diagnostics")
    assert_true("duration_ms" in columns, "agent query schema stores MCP call duration")

    with tempfile.TemporaryDirectory(prefix="daemon-dogfood-legacy-") as temp_dir:
        legacy_db = Path(temp_dir) / "memory.sqlite3"
        with closing(sqlite3.connect(legacy_db)) as connection:
            connection.execute(
                """
                CREATE TABLE agent_queries (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  agent_name TEXT,
                  tool_name TEXT NOT NULL,
                  query_text TEXT,
                  status TEXT NOT NULL,
                  evidence_count INTEGER NOT NULL DEFAULT 0,
                  uses_ai INTEGER NOT NULL DEFAULT 0,
                  provider_name TEXT,
                  provider_status TEXT,
                  source TEXT NOT NULL DEFAULT 'mcp',
                  error TEXT,
                  created_at TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO agent_queries (
                  agent_name, tool_name, query_text, status, evidence_count,
                  uses_ai, provider_name, provider_status, source, error, created_at
                ) VALUES (?, 'search_memory', ?, 'hit', 1, 0, '', '', 'mcp', '', ?)
                """,
                ("legacy-dogfood", "legacy private query", dt.datetime.now(dt.timezone.utc).isoformat()),
            )
            connection.commit()
        legacy_output = readout(db_path=legacy_db)
        assert_true("Recorded latency: not available" in legacy_output, "dogfood readout accepts the pre-0.5.8 query schema")
        assert_true("legacy private query" not in legacy_output, "legacy-schema readout remains redacted")
        with closing(sqlite3.connect(legacy_db)) as connection:
            legacy_columns = {row[1] for row in connection.execute("PRAGMA table_info(agent_queries)").fetchall()}
        assert_true("duration_ms" not in legacy_columns, "dogfood readout does not mutate an older database")

    seed_rows([])


def run_setup_helper_checks():
    result = subprocess.run(
        [sys.executable, "tools/setup-agent-harness-proof.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout
    assert_true("Setup helper proof passed using temporary configs only." in output, "setup helper proof passes")
    assert_true("dry run never reads private memory content" in output, "setup helper keeps private memory out of config output")
    assert_true("repeat apply is idempotent" in output, "setup helper proves idempotent config writes")
    assert_true("ChatGPT apply is honestly refused" in output, "setup helper keeps ChatGPT on the later remote path")


def run_managed_runtime_checks():
    result = subprocess.run(
        [sys.executable, "tools/managed-runtime-proof.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout
    assert_true("repo-pinned LaunchAgent migrates to managed runtime" in output, "managed runtime migrates the legacy checkout-pinned service")
    assert_true("failed update restores the prior active runtime" in output, "managed runtime proves update rollback")
    assert_true("uninstall preserves the SQLite database" in output, "managed runtime uninstall preserves memory")
    assert_true("Managed runtime proof passed entirely inside an isolated temporary home." in output, "managed runtime proof stays isolated")


def run_health_contract_checks():
    payload = fetch_json("/health")
    assert_true(payload.get("serviceVersion") == "0.5.10", "health exposes service version 0.5.10")
    assert_true(payload.get("apiVersion") == 1, "health exposes API contract version 1")
    assert_true(payload.get("minExtensionApiVersion") == 1, "health exposes the minimum compatible extension API")
    assert_true(payload.get("maxExtensionApiVersion") == 1, "health exposes the maximum compatible extension API")


def run_mcp_retrieval_eval_checks():
    result = subprocess.run(
        [sys.executable, "tools/mcp-retrieval-eval.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout
    assert_true("Result: 14/14 passed" in output, "MCP retrieval regression eval passes")
    assert_true("protected_absence_equivalence (protection)" in output, "MCP eval covers protected evidence indistinguishability")
    assert_true("protected_search_absence_equivalence (protection)" in output, "MCP eval covers protected search indistinguishability")
    assert_true("provider_missing_preserves_evidence (provider_setup)" in output, "MCP eval diagnoses provider setup separately from retrieval")
    assert_true("query_log_excludes_evidence (logging)" in output, "MCP eval covers evidence-safe query logging")
    assert_true("exact_local_day_boundaries (activity_summary)" in output, "MCP eval covers exact local-day activity counts")
    assert_true("protected_activity_filtered (protection)" in output, "MCP aggregate counts apply protection first")


def main():
    assert_true(shutil.which("node") is not None, "Node.js is available for extension checks")
    run_public_foundation_checks()
    run_manifest_checks()
    run_extension_ui_copy_checks()
    run_protection_fail_closed_checks()
    run_extension_store_readiness_checks()
    run_managed_runtime_checks()
    cleanup_db()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    service_env = os.environ.copy()
    service_env["DAEMON_MODE_DISABLE_AI"] = "1"
    service_env["TZ"] = "America/Los_Angeles"
    service_env.pop("OPENAI_API_KEY", None)

    process = subprocess.Popen(
        [
            sys.executable,
            "tools/local-memory-service.py",
            "--db-path",
            str(DB_PATH),
            "serve",
            "--port",
            str(PORT),
        ],
        cwd=ROOT,
        env=service_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        text=True,
    )

    try:
        wait_for_service(process)
        print_step(f"PASS isolated service is running on {BASE_URL}")
        run_health_contract_checks()
        run_inspector_checks()
        run_provider_status_checks()
        run_connect_summary_checks()
        run_dogfood_readout_checks()
        run_setup_helper_checks()
        run_mcp_retrieval_eval_checks()
        run_today_timezone_checks()
        run_storage_checks()
        run_retrieval_checks()
        run_user_blocklist_checks()
        run_blocklist_checks()
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        cleanup_db()

    print_step("All smoke checks passed without touching the real memory database.")


if __name__ == "__main__":
    main()
