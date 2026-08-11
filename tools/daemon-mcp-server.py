#!/usr/bin/env python3
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "tools" / "local-memory-service.py"
SERVER_NAME = "daemon-mode-memory"
SERVER_VERSION = "0.5.10"
LEGACY_PROTOCOL_VERSION = "2025-06-18"
MODERN_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = [MODERN_PROTOCOL_VERSION, LEGACY_PROTOCOL_VERSION]
SERVER_INFO_META_KEY = "io.modelcontextprotocol/serverInfo"
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_META_KEY = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META_KEY = "io.modelcontextprotocol/clientCapabilities"

SERVER_CAPABILITIES = {"tools": {"listChanged": False}}
SERVER_INSTRUCTIONS = (
    "Daemon Mode exposes local browsing memory to agents through non-destructive, citation-backed data-access tools. "
    "Each call records bounded local diagnostics for the owner, but agents cannot change memory or protection settings. "
    "Protection rules are applied before any result is returned."
)


def load_service_module():
    spec = importlib.util.spec_from_file_location("daemon_mode_local_memory_service", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SERVICE = load_service_module()


TOOLS = [
    {
        "name": "search_memory",
        "description": "Search saved Daemon Mode memory without changing it. Results are filtered through protection rules before they are returned.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms or a natural-language recall question."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "retrieve_evidence",
        "description": "Retrieve compact saved evidence with source URL and timestamp. No-evidence queries fail closed.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Question or topic to retrieve evidence for."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": SERVICE.DEFAULT_RETRIEVAL_LIMIT},
                "maxTokens": {"type": "integer", "minimum": 100, "maximum": 6000, "default": SERVICE.DEFAULT_RETRIEVAL_MAX_TOKENS},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "answer_from_evidence",
        "description": "Answer using only selected agent-safe evidence. If no evidence is found, the provider is not called.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "Question to answer from saved Daemon Mode evidence."},
                "limit": {"type": "integer", "minimum": 1, "maximum": 10, "default": SERVICE.DEFAULT_RETRIEVAL_LIMIT},
                "maxTokens": {"type": "integer", "minimum": 100, "maximum": 6000, "default": SERVICE.DEFAULT_RETRIEVAL_MAX_TOKENS},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_activity_summary",
        "description": (
            "Return exact aggregate Daemon capture activity for one local calendar day. "
            "Use distinct captured page URLs as the user-facing page total and keep repeat capture events secondary. "
            "This is exhaustive for retained agent-safe capture activity, not complete browser history; protected activity is excluded before aggregation."
        ),
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "Local calendar date in YYYY-MM-DD format."},
                "timezone": {"type": "string", "description": "IANA timezone such as America/Los_Angeles."},
                "topDomainsLimit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 3},
            },
            "required": ["date", "timezone"],
            "additionalProperties": False,
        },
    },
    {
        "name": "check_protection_status",
        "description": "Check Daemon Mode protection status without exposing protected page text.",
        "annotations": {"readOnlyHint": True},
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Optional URL to check against user and default protection rules."},
            },
            "additionalProperties": False,
        },
    },
]


class StdioTransport:
    def __init__(self):
        self.buffer = b""
        self.output_framing = "newline"

    def _read_more(self):
        chunk = sys.stdin.buffer.read1(4096)
        if not chunk:
            return False
        self.buffer += chunk
        return True

    def read_message(self):
        while True:
            if self.buffer.startswith(b"Content-Length:"):
                header_end = self.buffer.find(b"\r\n\r\n")
                if header_end == -1:
                    if self._read_more():
                        continue
                    return None

                header = self.buffer[:header_end].decode("ascii", errors="replace")
                length = None
                for line in header.split("\r\n"):
                    name, _, value = line.partition(":")
                    if name.lower() == "content-length":
                        length = int(value.strip())
                        break

                if length is None:
                    raise ValueError("Missing Content-Length header")

                frame_start = header_end + 4
                frame_end = frame_start + length
                if len(self.buffer) < frame_end:
                    if self._read_more():
                        continue
                    return None

                body = self.buffer[frame_start:frame_end]
                self.buffer = self.buffer[frame_end:]
                self.output_framing = "content-length"
                return json.loads(body.decode("utf-8"))

            newline_index = self.buffer.find(b"\n")
            if newline_index != -1:
                line = self.buffer[:newline_index].strip()
                self.buffer = self.buffer[newline_index + 1:]
                if not line:
                    continue
                self.output_framing = "newline"
                return json.loads(line.decode("utf-8"))

            if not self._read_more():
                return None

    def write_message(self, message):
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if self.output_framing == "content-length":
            sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii"))
            sys.stdout.buffer.write(body)
        else:
            sys.stdout.buffer.write(body + b"\n")
        sys.stdout.buffer.flush()


def clamp_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def require_text(arguments, key):
    value = str(arguments.get(key) or "").strip()
    if not value:
        raise ValueError(f"Missing required argument: {key}")
    return value


def evidence_count_for_tool(name, payload):
    if name == "search_memory":
        return len(payload.get("results") or [])
    if name == "retrieve_evidence":
        return len(payload.get("evidence") or [])
    if name == "answer_from_evidence":
        return len(payload.get("selectedEvidence") or [])
    if name == "get_activity_summary":
        metrics = payload.get("metrics") or {}
        return int(metrics.get("capturedPages") or 0)
    return 0


def status_for_tool(name, payload):
    if name in {"search_memory", "retrieve_evidence"}:
        return "miss" if payload.get("noEvidence") else "hit"
    if name == "answer_from_evidence":
        return payload.get("status") or "unknown"
    if name == "get_activity_summary":
        return "summarized"
    if name == "check_protection_status":
        url_status = payload.get("urlStatus") or {}
        if "protected" in url_status:
            return "checked_protected" if url_status.get("protected") else "checked_open"
        return "checked"
    return "unknown"


def query_for_tool(name, arguments):
    if name == "answer_from_evidence":
        return str(arguments.get("question") or "").strip()
    if name == "check_protection_status":
        return "Protection status check"
    if name == "get_activity_summary":
        return f"Activity summary: {arguments.get('date', '')} in {arguments.get('timezone', '')}".strip()
    return str(arguments.get("query") or "").strip()


def record_tool_call(connection, context, name, arguments, payload=None, error=None, diagnostics=None, duration_ms=None):
    provider = (payload or {}).get("provider") or {}
    diagnostics = diagnostics or {}
    SERVICE.record_agent_query(
        connection,
        {
            "agentName": context.get("agent_name") or "MCP client",
            "toolName": name or "unknown_tool",
            "query": query_for_tool(name, arguments or {}),
            "status": "error" if error else status_for_tool(name, payload or {}),
            "evidenceCount": 0 if error else evidence_count_for_tool(name, payload or {}),
            "usesAi": bool((payload or {}).get("usesAi")),
            "provider": provider,
            "source": "mcp",
            "error": str(error or ""),
            "filteredProtectedCount": diagnostics.get("filteredProtectedCount", 0),
            "durationMs": duration_ms,
        },
    )


def call_tool_with_connection(name, arguments, connection, include_diagnostics=False):
    arguments = arguments or {}

    if name == "search_memory":
        query = require_text(arguments, "query")
        limit = clamp_int(arguments.get("limit"), 10, 1, 20)
        return SERVICE.agent_search_captures(
            connection,
            query,
            limit,
            _include_diagnostics=include_diagnostics,
        )

    if name == "retrieve_evidence":
        query = require_text(arguments, "query")
        limit = clamp_int(arguments.get("limit"), SERVICE.DEFAULT_RETRIEVAL_LIMIT, 1, 10)
        max_tokens = clamp_int(arguments.get("maxTokens"), SERVICE.DEFAULT_RETRIEVAL_MAX_TOKENS, 100, 6000)
        return SERVICE.agent_retrieve_evidence(
            connection,
            query,
            limit,
            max_tokens,
            _include_diagnostics=include_diagnostics,
        )

    if name == "answer_from_evidence":
        question = require_text(arguments, "question")
        limit = clamp_int(arguments.get("limit"), SERVICE.DEFAULT_RETRIEVAL_LIMIT, 1, 10)
        max_tokens = clamp_int(arguments.get("maxTokens"), SERVICE.DEFAULT_RETRIEVAL_MAX_TOKENS, 100, 6000)
        return SERVICE.answer_question_contract_for_agent(
            connection,
            question,
            limit,
            max_tokens,
            _include_diagnostics=include_diagnostics,
        )

    if name == "check_protection_status":
        raw_url = str(arguments.get("url") or "").strip()
        payload = SERVICE.agent_protection_summary(connection, raw_url)
        if include_diagnostics:
            return payload, {"filteredProtectedCount": 0}
        return payload

    if name == "get_activity_summary":
        local_date = require_text(arguments, "date")
        timezone = require_text(arguments, "timezone")
        top_domains_limit = clamp_int(arguments.get("topDomainsLimit"), 3, 1, 20)
        return SERVICE.agent_activity_summary(
            connection,
            local_date,
            timezone,
            top_domains_limit,
            _include_diagnostics=include_diagnostics,
        )

    raise ValueError(f"Unknown tool: {name}")


def call_tool(name, arguments, db_path, context):
    with SERVICE.managed_db(db_path) as connection:
        started_at = time.perf_counter()
        try:
            payload, diagnostics = call_tool_with_connection(
                name,
                arguments,
                connection,
                include_diagnostics=True,
            )
        except Exception as exc:
            duration_ms = round((time.perf_counter() - started_at) * 1000)
            try:
                record_tool_call(
                    connection,
                    context,
                    name,
                    arguments,
                    error=exc,
                    duration_ms=duration_ms,
                )
            except Exception as log_error:
                print(f"[Daemon Mode MCP] Agent query log failed: {log_error}", file=sys.stderr)
            raise

        duration_ms = round((time.perf_counter() - started_at) * 1000)
        try:
            record_tool_call(
                connection,
                context,
                name,
                arguments,
                payload=payload,
                diagnostics=diagnostics,
                duration_ms=duration_ms,
            )
        except Exception as log_error:
            print(f"[Daemon Mode MCP] Agent query log failed: {log_error}", file=sys.stderr)
        return payload


def tool_result(payload):
    return {
        "structuredContent": payload,
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, sort_keys=True),
            }
        ]
    }


def error_response(message_id, code, message, data=None):
    response = {
        "jsonrpc": "2.0",
        "id": message_id,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if data is not None:
        response["error"]["data"] = data
    return response


def modern_result(message_id, result):
    return {
        "jsonrpc": "2.0",
        "id": message_id,
        "result": {
            "resultType": "complete",
            **result,
            "_meta": {
                **(result.get("_meta") or {}),
                SERVER_INFO_META_KEY: {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        },
    }


def modern_request_context(message, fallback_context):
    params = message.get("params")
    if not isinstance(params, dict):
        return None, error_response(message.get("id"), -32602, "Modern MCP requests require params._meta")

    metadata = params.get("_meta")
    if not isinstance(metadata, dict):
        return None, error_response(message.get("id"), -32602, "Modern MCP requests require params._meta")

    requested_version = metadata.get(PROTOCOL_VERSION_META_KEY)
    if not isinstance(requested_version, str) or not requested_version:
        return None, error_response(
            message.get("id"),
            -32602,
            f"Modern MCP requests require _meta.{PROTOCOL_VERSION_META_KEY}",
        )
    if requested_version != MODERN_PROTOCOL_VERSION:
        return None, error_response(
            message.get("id"),
            -32022,
            "Unsupported protocol version",
            {"supported": SUPPORTED_PROTOCOL_VERSIONS, "requested": requested_version},
        )

    if not isinstance(metadata.get(CLIENT_CAPABILITIES_META_KEY), dict):
        return None, error_response(
            message.get("id"),
            -32602,
            f"Modern MCP requests require _meta.{CLIENT_CAPABILITIES_META_KEY}",
        )

    request_context = dict(fallback_context)
    client_info = metadata.get(CLIENT_INFO_META_KEY)
    if client_info is not None:
        if not isinstance(client_info, dict):
            return None, error_response(message.get("id"), -32602, "Modern MCP clientInfo must be an object")
        client_name = str(client_info.get("name") or "").strip()
        client_version = str(client_info.get("version") or "").strip()
        if not client_name or not client_version:
            return None, error_response(
                message.get("id"),
                -32602,
                "Modern MCP clientInfo requires name and version",
            )
        request_context["agent_name"] = client_name
    else:
        request_context["agent_name"] = "MCP client"
    return request_context, None


def handle_request(message, db_path, context):
    method = message.get("method")
    message_id = message.get("id")
    params = message.get("params") or {}

    if method == "initialize":
        if context.get("protocol_era") == "modern":
            return error_response(message_id, -32601, "Method not found: initialize")
        client_info = params.get("clientInfo") or {}
        client_name = str(client_info.get("name") or "").strip()
        if client_name:
            context["agent_name"] = client_name
        requested_version = params.get("protocolVersion")
        negotiated_version = (
            requested_version
            if requested_version == LEGACY_PROTOCOL_VERSION
            else LEGACY_PROTOCOL_VERSION
        )
        context["protocol_era"] = "legacy"
        return {
            "jsonrpc": "2.0",
            "id": message_id,
            "result": {
                "protocolVersion": negotiated_version,
                "capabilities": SERVER_CAPABILITIES,
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                "instructions": SERVER_INSTRUCTIONS,
            },
        }

    if method in {"notifications/initialized", "notifications/cancelled"}:
        return None

    metadata = params.get("_meta") if isinstance(params, dict) else None
    is_modern = context.get("protocol_era") == "modern" or method == "server/discover" or (
        isinstance(metadata, dict) and PROTOCOL_VERSION_META_KEY in metadata
    )

    if is_modern:
        if context.get("protocol_era") == "legacy":
            return error_response(message_id, -32601, f"Method not found: {method}")
        request_context, protocol_error = modern_request_context(message, context)
        if protocol_error is not None:
            return protocol_error
        context["protocol_era"] = "modern"

        if method == "server/discover":
            return modern_result(
                message_id,
                {
                    "supportedVersions": SUPPORTED_PROTOCOL_VERSIONS,
                    "capabilities": SERVER_CAPABILITIES,
                    "instructions": SERVER_INSTRUCTIONS,
                    "ttlMs": 0,
                    "cacheScope": "private",
                },
            )

        if method == "tools/list":
            return modern_result(message_id, {"tools": TOOLS})

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                payload = call_tool(name, arguments, db_path, request_context)
            except Exception as exc:
                return modern_result(
                    message_id,
                    {
                        **tool_result({"ok": False, "error": str(exc)}),
                        "isError": True,
                    },
                )
            return modern_result(message_id, tool_result({"ok": True, **payload}))

        return error_response(message_id, -32601, f"Method not found: {method}")

    if context.get("protocol_era") != "legacy":
        return error_response(message_id, -32600, "Legacy MCP requests require initialize first")

    if method == "ping":
        return {"jsonrpc": "2.0", "id": message_id, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": message_id, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name")
        arguments = params.get("arguments") or {}
        try:
            payload = call_tool(name, arguments, db_path, context)
        except Exception as exc:
            return {
                "jsonrpc": "2.0",
                "id": message_id,
                "result": {
                    **tool_result({"ok": False, "error": str(exc)}),
                    "isError": True,
                },
            }
        return {"jsonrpc": "2.0", "id": message_id, "result": tool_result({"ok": True, **payload})}

    if method in {"resources/list", "prompts/list"}:
        key = "resources" if method == "resources/list" else "prompts"
        return {"jsonrpc": "2.0", "id": message_id, "result": {key: []}}

    return error_response(message_id, -32601, f"Method not found: {method}")


def serve(db_path):
    SERVICE.migrate_repo_db_if_needed(db_path)
    transport = StdioTransport()
    context = {"agent_name": "MCP client"}
    print(f"[Daemon Mode MCP] Serving non-destructive memory tools from {SERVICE.absolute_db_path(db_path)}", file=sys.stderr)

    while True:
        try:
            message = transport.read_message()
        except Exception as exc:
            transport.write_message(error_response(None, -32700, f"Parse error: {exc}"))
            continue

        if message is None:
            break

        if "id" not in message:
            response = handle_request(message, db_path, context)
            if response is not None:
                transport.write_message(response)
            continue

        response = handle_request(message, db_path, context)
        if response is not None:
            transport.write_message(response)


def build_parser():
    parser = argparse.ArgumentParser(description="Run the Daemon Mode non-destructive MCP server")
    parser.add_argument("--db-path", type=Path, default=SERVICE.DEFAULT_DB_PATH, help="SQLite memory database path")
    return parser


def main():
    args = build_parser().parse_args()
    serve(args.db_path)


if __name__ == "__main__":
    main()
