#!/usr/bin/env python3
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "tools" / "local-memory-service.py"
DB_PATH = ROOT / ".daemon-mode" / "mcp-proof.sqlite3"


def print_step(message):
    print(f"[mcp-proof] {message}")


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print_step(f"PASS {message}")


def load_service_module():
    spec = importlib.util.spec_from_file_location("daemon_mode_local_memory_service", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cleanup_db():
    for path in [DB_PATH, DB_PATH.with_suffix(".sqlite3-shm"), DB_PATH.with_suffix(".sqlite3-wal")]:
        if path.exists():
            path.unlink()


def seed_fixtures():
    service = load_service_module()
    cleanup_db()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with service.open_db(DB_PATH) as connection:
        service.init_db(connection)
        service.save_capture(
            connection,
            {
                "source": "daemon-mode-mcp-proof",
                "extensionVersion": "proof",
                "navigationType": "fixture",
                "url": "https://example.com/daemon-mcp-proof",
                "title": "Daemon MCP Proof Notes",
                "domain": "example.com",
                "capturedAt": "2026-07-08T10:15:00-07:00",
                "text": (
                    "Daemon MCP loop prototype should let Claude Code retrieve citable local memory. "
                    "The proof keyword is citadel-mcp-loop and the saved page must include URL and timestamp."
                ),
                "textLength": 162,
            },
        )
        # Simulate a historical record captured before inbox protection was
        # enabled. Current ingestion rejects this URL by design.
        service.set_default_category_enabled(connection, "email-inboxes", False)
        service.save_capture(
            connection,
            {
                "source": "daemon-mode-mcp-proof",
                "extensionVersion": "proof",
                "navigationType": "fixture",
                "url": "https://mail.google.com/mail/u/0/#inbox",
                "title": "Protected Mail Fixture",
                "domain": "mail.google.com",
                "capturedAt": "2026-07-08T10:20:00-07:00",
                "text": "SECRET_GMAIL_SENTINEL protected private page text must never reach the MCP client.",
                "textLength": 79,
            },
        )
        service.set_default_category_enabled(connection, "email-inboxes", True)


class McpClient:
    def __init__(self, process):
        self.process = process
        self.next_id = 1

    def request(self, method, params=None):
        message_id = self.next_id
        self.next_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        self.process.stdin.flush()
        response = self.read_response()
        assert_true(response.get("id") == message_id, f"{method} response id matches")
        if "error" in response:
            raise AssertionError(f"{method} returned error: {response['error']}")
        return response["result"]

    def notify(self, method, params=None):
        payload = {
            "jsonrpc": "2.0",
            "method": method,
        }
        if params is not None:
            payload["params"] = params
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
        self.process.stdin.flush()

    def read_response(self):
        header = b""
        while b"\r\n\r\n" not in header:
            chunk = self.process.stdout.read(1)
            if not chunk:
                raise RuntimeError("MCP server closed stdout before responding")
            header += chunk

        header_text = header.decode("ascii", errors="replace")
        length = None
        for line in header_text.split("\r\n"):
            name, _, value = line.partition(":")
            if name.lower() == "content-length":
                length = int(value.strip())
                break

        if length is None:
            raise RuntimeError(f"Missing Content-Length in response header: {header_text!r}")

        body = self.process.stdout.read(length)
        if len(body) != length:
            raise RuntimeError("MCP server response body ended early")
        return json.loads(body.decode("utf-8"))


class ModernMcpClient:
    def __init__(self, process, client_name="daemon-modern-proof"):
        self.process = process
        self.client_name = client_name
        self.next_id = 1

    def request_response(self, method, params=None, protocol_version="2026-07-28", client_name=None):
        message_id = self.next_id
        self.next_id += 1
        request_params = dict(params or {})
        request_params["_meta"] = {
            "io.modelcontextprotocol/protocolVersion": protocol_version,
            "io.modelcontextprotocol/clientInfo": {
                "name": client_name or self.client_name,
                "version": "0.5.11-proof",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        }
        payload = {
            "jsonrpc": "2.0",
            "id": message_id,
            "method": method,
            "params": request_params,
        }
        self.process.stdin.write(json.dumps(payload, separators=(",", ":")).encode("utf-8") + b"\n")
        self.process.stdin.flush()
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("Modern MCP server closed stdout before responding")
        response = json.loads(line.decode("utf-8"))
        assert_true(response.get("id") == message_id, f"{method} modern response id matches")
        return response

    def request(self, method, params=None, protocol_version="2026-07-28", client_name=None):
        response = self.request_response(method, params, protocol_version, client_name)
        if "error" in response:
            raise AssertionError(f"{method} returned modern error: {response['error']}")
        return response["result"]


def tool_payload(result):
    text = result["content"][0]["text"]
    return json.loads(text)


def run_mcp_checks():
    env = os.environ.copy()
    env["DAEMON_MODE_AI_PROVIDER"] = "mock"
    env.pop("DAEMON_MODE_DISABLE_AI", None)
    env.pop("OPENAI_API_KEY", None)

    process = subprocess.Popen(
        [sys.executable, "tools/daemon-mcp-server.py", "--db-path", str(DB_PATH)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    client = McpClient(process)

    try:
        initialized = client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "daemon-mcp-proof", "version": "0.5.0"},
            },
        )
        assert_true(initialized["serverInfo"]["name"] == "daemon-mode-memory", "MCP server initializes")
        assert_true(initialized["protocolVersion"] == "2025-06-18", "legacy MCP negotiates the supported protocol version")
        client.notify("notifications/initialized")

        listed = client.request("tools/list")
        tool_names = {tool["name"] for tool in listed["tools"]}
        assert_true(
            {"search_memory", "retrieve_evidence", "answer_from_evidence", "check_protection_status"} <= tool_names,
            "MCP exposes the read-only 0.5.0 tools",
        )

        searched = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "search_memory",
                    "arguments": {"query": "where did I read about the citadel MCP loop?", "limit": 3},
                },
            )
        )
        assert_true(searched["ok"] and not searched["noEvidence"], "MCP search handles natural-language recall")
        assert_true(searched["results"][0]["url"] == "https://example.com/daemon-mcp-proof", "MCP search returns agent-safe memory")

        retrieved = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "retrieve_evidence",
                    "arguments": {"query": "citadel mcp loop", "limit": 3, "maxTokens": 700},
                },
            )
        )
        assert_true(retrieved["ok"] and not retrieved["noEvidence"], "MCP retrieves seeded local evidence")
        first = retrieved["evidence"][0]
        assert_true(first["url"] == "https://example.com/daemon-mcp-proof", "MCP evidence includes source URL")
        assert_true(first["capturedAt"] == "2026-07-08T10:15:00-07:00", "MCP evidence includes capture timestamp")

        missing = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "retrieve_evidence",
                    "arguments": {"query": "nonexistentzzzz", "limit": 2, "maxTokens": 500},
                },
            )
        )
        assert_true(missing["ok"] and missing["noEvidence"], "MCP no-evidence retrieval fails closed")

        protected = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "retrieve_evidence",
                    "arguments": {"query": "SECRET_GMAIL_SENTINEL", "limit": 3, "maxTokens": 700},
                },
            )
        )
        assert_true(protected["ok"] and protected["noEvidence"], "MCP filters protected captures out of retrieval")
        assert_true("SECRET_GMAIL_SENTINEL" not in json.dumps(protected.get("evidence", [])), "MCP retrieval does not expose protected page text")
        assert_true("mail.google.com" not in json.dumps(protected.get("evidence", [])), "MCP retrieval does not expose protected page URL")

        answer = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "answer_from_evidence",
                    "arguments": {"question": "What did I save about the citadel MCP loop?", "limit": 3, "maxTokens": 700},
                },
            )
        )
        assert_true(answer["ok"] and answer["status"] == "answered", "MCP answer path uses selected evidence")
        assert_true(answer["usesAi"] is True and answer["provider"]["name"] == "mock", "MCP answer path uses the configured provider only after evidence")
        assert_true(len(answer["citations"]) >= 1 and answer["citations"][0]["url"], "MCP answer returns citations")

        no_answer = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "answer_from_evidence",
                    "arguments": {"question": "What did protected mail say about SECRET_GMAIL_SENTINEL?", "limit": 3, "maxTokens": 700},
                },
            )
        )
        assert_true(no_answer["ok"] and no_answer["status"] == "no_evidence", "MCP answer fails closed for protected-only evidence")
        assert_true(no_answer["provider"]["status"] == "not_called", "MCP protected-only answer does not call provider")
        assert_true("SECRET_GMAIL_SENTINEL" not in json.dumps(no_answer.get("selectedEvidence", [])), "MCP answer does not expose protected text")

        protection = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "check_protection_status",
                    "arguments": {"url": "https://mail.google.com/mail/u/0/#inbox"},
                },
            )
        )
        assert_true(protection["ok"], "MCP protection status returns a bounded payload")
        assert_true(protection["urlStatus"]["protected"], "MCP protection status identifies protected URLs")
        assert_true("SECRET_GMAIL_SENTINEL" not in json.dumps(protection), "MCP protection status does not expose protected page text")

        service = load_service_module()
        with service.open_db(DB_PATH) as connection:
            service.init_db(connection)
            queries = service.list_agent_queries(connection, 20)
        assert_true(
            any(query["agentName"] == "daemon-mcp-proof" for query in queries),
            "MCP query log records the initialized agent name",
        )
        assert_true(
            any(query["toolName"] == "retrieve_evidence" and query["status"] == "hit" for query in queries),
            "MCP query log records evidence hits",
        )
        assert_true(
            any(query["toolName"] == "retrieve_evidence" and query["status"] == "miss" for query in queries),
            "MCP query log records no-evidence misses",
        )
        assert_true(
            any(query["toolName"] == "answer_from_evidence" and query["usesAi"] for query in queries),
            "MCP query log records provider-backed answers",
        )
        assert_true(
            "mail.google.com/mail/u/0/#inbox" not in json.dumps([query["query"] for query in queries]),
            "MCP query log does not store protection-check URLs as query text",
        )
        assert_true(
            any(query["filteredProtectedCount"] > 0 for query in queries),
            "MCP query log keeps an internal aggregate for protected-filter diagnostics",
        )
        assert_true(
            all(query["durationMs"] is None or query["durationMs"] >= 0 for query in queries),
            "MCP query log records non-negative call duration",
        )
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
        if process.returncode not in (0, None):
            raise RuntimeError(f"MCP server exited with {process.returncode}: {stderr}")


def run_legacy_fallback_check():
    process = subprocess.Popen(
        [sys.executable, "tools/daemon-mcp-server.py", "--db-path", str(DB_PATH)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    client = McpClient(process)
    try:
        initialized = client.request(
            "initialize",
            {
                "protocolVersion": "2099-01-01",
                "capabilities": {},
                "clientInfo": {"name": "daemon-legacy-fallback-proof", "version": "0.5.11-proof"},
            },
        )
        assert_true(
            initialized["protocolVersion"] == "2025-06-18",
            "legacy MCP falls back honestly instead of echoing an unsupported version",
        )
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def run_modern_mcp_checks():
    env = os.environ.copy()
    env["DAEMON_MODE_AI_PROVIDER"] = "mock"
    env.pop("DAEMON_MODE_DISABLE_AI", None)
    env.pop("OPENAI_API_KEY", None)

    process = subprocess.Popen(
        [sys.executable, "tools/daemon-mcp-server.py", "--db-path", str(DB_PATH)],
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    client = ModernMcpClient(process)
    try:
        listed = client.request("tools/list")
        tool_names = {tool["name"] for tool in listed["tools"]}
        assert_true(listed["resultType"] == "complete", "modern stdio can open inline without initialize")
        assert_true("retrieve_evidence" in tool_names, "modern MCP exposes Daemon retrieval")

        discovered = client.request("server/discover")
        assert_true(discovered["resultType"] == "complete", "modern discovery identifies a complete result")
        assert_true(
            discovered["supportedVersions"] == ["2026-07-28", "2025-06-18"],
            "modern discovery advertises both supported protocol eras",
        )
        assert_true(discovered["capabilities"]["tools"]["listChanged"] is False, "modern discovery advertises read-only tools")
        assert_true(
            discovered["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "daemon-mode-memory",
            "modern discovery stamps server identity",
        )

        unsupported = client.request_response("tools/list", protocol_version="2099-01-01")
        assert_true(unsupported["error"]["code"] == -32022, "modern MCP rejects unsupported protocol versions")
        assert_true(
            unsupported["error"]["data"]
            == {"supported": ["2026-07-28", "2025-06-18"], "requested": "2099-01-01"},
            "modern version error provides an honest retry contract",
        )

        retrieved = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "retrieve_evidence",
                    "arguments": {"query": "citadel mcp loop", "limit": 3, "maxTokens": 700},
                },
                client_name="daemon-modern-retrieval-proof",
            )
        )
        assert_true(retrieved["ok"] and not retrieved["noEvidence"], "modern MCP retrieves seeded local evidence")
        assert_true(retrieved["evidence"][0]["url"] == "https://example.com/daemon-mcp-proof", "modern MCP preserves citations")

        protected = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "retrieve_evidence",
                    "arguments": {"query": "SECRET_GMAIL_SENTINEL", "limit": 3, "maxTokens": 700},
                },
            )
        )
        protected_evidence_blob = json.dumps(protected.get("evidence", []))
        assert_true(protected["ok"] and protected["noEvidence"], "modern MCP filters protected evidence")
        assert_true("SECRET_GMAIL_SENTINEL" not in protected_evidence_blob, "modern MCP does not expose protected text")
        assert_true("mail.google.com" not in protected_evidence_blob, "modern MCP does not expose protected provenance")

        service = load_service_module()
        with service.open_db(DB_PATH) as connection:
            service.init_db(connection)
            queries = service.list_agent_queries(connection, 30)
        assert_true(
            any(query["agentName"] == "daemon-modern-retrieval-proof" for query in queries),
            "modern MCP records client identity from each request",
        )
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        stderr = process.stderr.read().decode("utf-8", errors="replace").strip()
        if process.returncode not in (0, None):
            raise RuntimeError(f"Modern MCP server exited with {process.returncode}: {stderr}")


def main():
    seed_fixtures()
    try:
        run_legacy_fallback_check()
        run_mcp_checks()
        run_modern_mcp_checks()
    finally:
        cleanup_db()

    print_step("MCP loop proof passed without touching the real memory database.")


if __name__ == "__main__":
    main()
