#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "tools" / "local-memory-service.py"
DEFAULT_DB_PATH = ROOT / ".daemon-mode" / "first-run-agent-proof.sqlite3"
SAFE_PROOF_URL = "https://example.com/daemon-first-run-proof"
SAFE_PROOF_CAPTURED_AT = "2026-07-08T11:05:00-07:00"
SAFE_PROOF_QUERY = "What did I just read about lighthouse memory proof?"


def print_step(message):
    print(f"[first-run-proof] {message}")


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print_step(f"PASS {message}")


def load_service_module():
    spec = importlib.util.spec_from_file_location("daemon_mode_local_memory_service", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def cleanup_db(db_path):
    for path in [db_path, db_path.with_suffix(".sqlite3-shm"), db_path.with_suffix(".sqlite3-wal")]:
        if path.exists():
            path.unlink()


def seed_safe_page(db_path):
    service = load_service_module()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with service.open_db(db_path) as connection:
        service.init_db(connection)
        result = service.save_capture(
            connection,
            {
                "source": "daemon-mode-first-run-proof",
                "extensionVersion": "proof",
                "navigationType": "seeded-safe-page",
                "url": SAFE_PROOF_URL,
                "title": "Daemon First-Run Proof Page",
                "domain": "example.com",
                "capturedAt": SAFE_PROOF_CAPTURED_AT,
                "text": (
                    "The lighthouse memory proof shows Daemon can give an AI agent local, citable memory. "
                    "The user's agent should retrieve this safe page, cite the URL, and mention the timestamp. "
                    "This page is intentionally harmless seed data for the first-run proof."
                ),
                "textLength": 246,
            },
        )

    return result


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
        if response.get("id") != message_id:
            raise RuntimeError(f"{method} returned mismatched id: {response!r}")
        if "error" in response:
            raise RuntimeError(f"{method} returned error: {response['error']}")
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


def tool_payload(result):
    if "structuredContent" in result:
        return result["structuredContent"]
    return json.loads(result["content"][0]["text"])


def ask_agent_memory(db_path):
    env = os.environ.copy()
    env["DAEMON_MODE_AI_PROVIDER"] = "mock"
    env.pop("DAEMON_MODE_DISABLE_AI", None)
    env.pop("OPENAI_API_KEY", None)

    process = subprocess.Popen(
        [sys.executable, "tools/daemon-mcp-server.py", "--db-path", str(db_path)],
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
                "clientInfo": {"name": "daemon-first-run-proof", "version": "0.5.1"},
            },
        )
        client.notify("notifications/initialized")
        assert_true(initialized["serverInfo"]["name"] == "daemon-mode-memory", "agent connected to Daemon MCP")

        retrieved = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "retrieve_evidence",
                    "arguments": {"query": SAFE_PROOF_QUERY, "limit": 3, "maxTokens": 700},
                },
            )
        )
        assert_true(retrieved["ok"] and not retrieved["noEvidence"], "agent retrieved first-run evidence")
        assert_true(retrieved["evidence"][0]["url"] == SAFE_PROOF_URL, "evidence includes URL")
        assert_true(retrieved["evidence"][0]["capturedAt"] == SAFE_PROOF_CAPTURED_AT, "evidence includes timestamp")

        answered = tool_payload(
            client.request(
                "tools/call",
                {
                    "name": "answer_from_evidence",
                    "arguments": {"question": SAFE_PROOF_QUERY, "limit": 3, "maxTokens": 700},
                },
            )
        )
        assert_true(answered["ok"] and answered["status"] == "answered", "agent got a cited answer from selected evidence")
        assert_true(answered["provider"]["name"] == "mock", "proof does not need an API key")
        assert_true(answered["citations"][0]["url"] == SAFE_PROOF_URL, "answer includes source citation")
        return retrieved, answered
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


def client_config_examples(db_path):
    service = load_service_module()
    configs = {item["id"]: item for item in service.mcp_client_configs(db_path)}
    return {
        "claude_code": configs["claude-code"]["snippet"],
        "codex_config_toml": configs["codex"]["snippet"],
        "cursor_mcp_json": configs["cursor"]["snippet"],
        "claude_desktop_json": configs["claude-desktop"]["snippet"],
        "chatgpt_note": configs["chatgpt"]["description"],
    }


def print_demo(retrieved, answered, db_path, elapsed_seconds):
    citation = answered["citations"][0]
    print()
    print("=== First-run agent proof ===")
    print(f"Question: {SAFE_PROOF_QUERY}")
    print("Agent answer:")
    print(answered["answer"]["text"])
    print()
    print("Citation:")
    print(f"- {citation['title']} ({citation['url']})")
    print(f"- Captured at: {citation.get('capturedAt') or retrieved['evidence'][0]['capturedAt']}")
    print()
    print("Trust note:")
    print("- Evidence selected locally.")
    print("- Answer generated by mock provider for proof, so no API key or network model call is required.")
    print("- Agent actions are non-destructive; calls keep bounded local diagnostics.")
    print()
    print(f"Elapsed: {elapsed_seconds:.2f}s")
    print(f"Proof database: {db_path}")
    print()
    print("Client setup snippets:")
    examples = client_config_examples(db_path)
    for label, value in examples.items():
        print(f"\n[{label}]")
        print(value)


def build_parser():
    parser = argparse.ArgumentParser(description="Run Daemon Mode's 0.5.1 first-run agent proof")
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH, help="SQLite database path for the proof")
    parser.add_argument("--cleanup", action="store_true", help="Delete the proof database after the script exits")
    return parser


def main():
    args = build_parser().parse_args()
    start = time.time()
    cleanup_db(args.db_path)

    try:
        seed = seed_safe_page(args.db_path)
        assert_true(seed["id"] >= 1, "seeded a known safe page")
        retrieved, answered = ask_agent_memory(args.db_path)
        print_demo(retrieved, answered, args.db_path, time.time() - start)
    finally:
        if args.cleanup:
            cleanup_db(args.db_path)


if __name__ == "__main__":
    main()
