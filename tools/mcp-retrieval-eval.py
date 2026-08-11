#!/usr/bin/env python3
import argparse
import importlib.util
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = ROOT / "tools" / "local-memory-service.py"
MCP_SERVER_PATH = ROOT / "tools" / "daemon-mcp-server.py"
SAFE_URL = "https://example.com/borealis-memory-protocol"
SAFE_CAPTURED_AT = "2026-07-09T09:30:00-07:00"
SAFE_RAW_SENTINEL = "DO_NOT_LOG_THIS_EVIDENCE_SENTINEL"
PROTECTED_URL = "https://mail.google.com/mail/u/0/#inbox"
PROTECTED_RAW_SENTINEL = "PRIVATE_PAYROLL_VALUE_MUST_STAY_HIDDEN"


@dataclass
class EvalCase:
    name: str
    subsystem: str
    passed: bool
    diagnosis: str


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_fixtures(service, db_path):
    captures = [
        {
            "source": "daemon-mode-retrieval-eval",
            "extensionVersion": "eval",
            "navigationType": "fixture",
            "url": SAFE_URL,
            "title": "Borealis Memory Protocol",
            "domain": "example.com",
            "capturedAt": SAFE_CAPTURED_AT,
            "text": (
                "The Borealis memory protocol uses citation-backed recall for agent handoffs. "
                "It requires a source URL and capture timestamp on every recalled page. "
                f"{SAFE_RAW_SENTINEL}"
            ),
            "textLength": 170,
        },
        {
            "source": "daemon-mode-retrieval-eval",
            "extensionVersion": "eval",
            "navigationType": "fixture",
            "url": "https://example.com/garden-trellis",
            "title": "Garden Trellis Notes",
            "domain": "example.com",
            "capturedAt": "2026-07-09T09:35:00-07:00",
            "text": "A cedar garden trellis supports basil and climbing beans. This page is an unrelated retrieval distractor.",
            "textLength": 108,
        },
        {
            "source": "daemon-mode-retrieval-eval",
            "extensionVersion": "eval",
            "navigationType": "fixture",
            "url": PROTECTED_URL,
            "title": "Acorn Payroll Notes",
            "domain": "mail.google.com",
            "capturedAt": "2026-07-09T09:40:00-07:00",
            "text": (
                "Private acorn payroll notes are protected evidence. "
                f"{PROTECTED_RAW_SENTINEL}"
            ),
            "textLength": 98,
        },
    ]

    with service.open_db(db_path) as connection:
        service.init_db(connection)
        for capture in captures:
            if capture["url"] == PROTECTED_URL:
                # Simulate a historical record captured before the current
                # default protection was enabled. Ingestion now rejects it.
                service.set_default_category_enabled(connection, "email-inboxes", False)
            service.save_capture(connection, capture)
            if capture["url"] == PROTECTED_URL:
                service.set_default_category_enabled(connection, "email-inboxes", True)
        connection.execute(
            "UPDATE capture_events SET created_at = '2026-07-13T07:00:00Z' WHERE url = ?",
            (SAFE_URL,),
        )
        connection.execute(
            "UPDATE capture_events SET created_at = '2026-07-14T06:59:59Z' WHERE url = ?",
            ("https://example.com/garden-trellis",),
        )
        connection.execute(
            "UPDATE capture_events SET created_at = '2026-07-13T20:00:00Z' WHERE url = ?",
            (PROTECTED_URL,),
        )
        connection.executemany(
            """
            INSERT INTO capture_events (
              status, event_type, url, domain, occurred_at, created_at
            ) VALUES ('captured', ?, ?, ?, ?, ?)
            """,
            [
                (
                    "duplicate-capture-seen",
                    SAFE_URL,
                    "example.com",
                    "2026-07-13T11:00:00-07:00",
                    "2026-07-13T18:00:00Z",
                ),
                (
                    "capture-stored",
                    "https://outside.example.net/next-day",
                    "outside.example.net",
                    "2026-07-14T00:00:00-07:00",
                    "2026-07-14T07:00:00Z",
                ),
                (
                    "capture-stored",
                    "https://clean.example.net/alpha",
                    "clean.example.net",
                    "2026-07-13T12:00:00-07:00",
                    "2026-07-13T19:00:00Z",
                ),
                (
                    "capture-stored",
                    "https://clean.example.net/beta",
                    "clean.example.net",
                    "2026-07-13T12:01:00-07:00",
                    "2026-07-13T19:01:00Z",
                ),
                *[
                    (
                        "duplicate-capture-seen",
                        "https://noisy.example.net/live",
                        "noisy.example.net",
                        f"2026-07-13T13:0{minute}:00-07:00",
                        f"2026-07-13T20:0{minute}:00Z",
                    )
                    for minute in range(4)
                ],
            ],
        )
        connection.commit()


def normalized_miss(payload):
    return {
        key: value
        for key, value in payload.items()
        if key not in {"query", "terms"}
    }


def evaluate(db_path):
    service = load_module("daemon_mode_retrieval_eval_service", SERVICE_PATH)
    mcp = load_module("daemon_mode_retrieval_eval_mcp", MCP_SERVER_PATH)
    seed_fixtures(service, db_path)
    context = {"agent_name": "daemon-retrieval-eval"}
    cases = []

    def check(name, subsystem, condition, success, failure):
        cases.append(EvalCase(name, subsystem, bool(condition), success if condition else failure))

    natural = mcp.call_tool(
        "retrieve_evidence",
        {"query": "where did I read about the Borealis citation-backed agent handoff?", "limit": 3, "maxTokens": 700},
        db_path,
        context,
    )
    natural_evidence = natural.get("evidence") or []
    first = natural_evidence[0] if natural_evidence else {}
    check(
        "natural_language_recall",
        "retrieval",
        not natural.get("noEvidence") and first.get("url") == SAFE_URL,
        "Natural-language recall selected the seeded safe page.",
        "Retrieval did not rank the intended safe page first.",
    )
    check(
        "citation_provenance",
        "retrieval",
        first.get("url") == SAFE_URL and first.get("capturedAt") == SAFE_CAPTURED_AT,
        "Selected evidence includes the source URL and capture timestamp.",
        "Retrieved evidence is missing exact URL or timestamp provenance.",
    )

    false_positive = mcp.call_tool(
        "retrieve_evidence",
        {"query": "borealis payroll reconciliation", "limit": 3, "maxTokens": 700},
        db_path,
        context,
    )
    check(
        "false_positive_rejection",
        "retrieval",
        false_positive.get("noEvidence") and not false_positive.get("evidence"),
        "A mixed-topic distractor query was rejected.",
        "Retrieval accepted weak partial overlap as evidence.",
    )

    missing = mcp.call_tool(
        "retrieve_evidence",
        {"query": "quasar observability ledger", "limit": 3, "maxTokens": 700},
        db_path,
        context,
    )
    check(
        "no_evidence_fail_closed",
        "insufficient_evidence",
        missing.get("noEvidence") and not missing.get("evidence"),
        "No-evidence retrieval returned an empty, fail-closed result.",
        "A query with no local support returned evidence.",
    )

    protected = mcp.call_tool(
        "retrieve_evidence",
        {"query": "private acorn payroll notes", "limit": 3, "maxTokens": 700},
        db_path,
        context,
    )
    protected_blob = json.dumps(protected, sort_keys=True)
    check(
        "protected_content_filtered",
        "protection",
        protected.get("noEvidence")
        and not protected.get("evidence")
        and PROTECTED_URL not in protected_blob
        and PROTECTED_RAW_SENTINEL not in protected_blob,
        "Protected evidence is absent from the agent response.",
        "Protected evidence text or provenance crossed the agent boundary.",
    )
    check(
        "protected_absence_equivalence",
        "protection",
        normalized_miss(protected) == normalized_miss(missing),
        "A protected-only miss is indistinguishable from ordinary absence.",
        "The agent response reveals that hidden protected evidence exists.",
    )

    search_missing = mcp.call_tool(
        "search_memory",
        {"query": "quasar observability ledger", "limit": 3},
        db_path,
        context,
    )
    search_protected = mcp.call_tool(
        "search_memory",
        {"query": "private acorn payroll notes", "limit": 3},
        db_path,
        context,
    )
    search_protected_blob = json.dumps(search_protected, sort_keys=True)
    check(
        "protected_search_filtered",
        "protection",
        search_protected.get("noEvidence")
        and not search_protected.get("results")
        and PROTECTED_URL not in search_protected_blob
        and PROTECTED_RAW_SENTINEL not in search_protected_blob,
        "Protected memory is absent from agent search results.",
        "Protected text or provenance crossed the search boundary.",
    )
    check(
        "protected_search_absence_equivalence",
        "protection",
        normalized_miss(search_protected) == normalized_miss(search_missing),
        "Protected-only search is indistinguishable from an ordinary miss.",
        "The search response reveals that hidden protected memory exists.",
    )

    activity = mcp.call_tool(
        "get_activity_summary",
        {"date": "2026-07-13", "timezone": "America/Los_Angeles", "topDomainsLimit": 3},
        db_path,
        context,
    )
    activity_blob = json.dumps(activity, sort_keys=True)
    activity_metrics = activity.get("metrics") or {}
    activity_domains = activity.get("topDomains") or []
    check(
        "exact_local_day_boundaries",
        "activity_summary",
        activity.get("exhaustiveAgentSafeDataset") is True
        and activity_metrics.get("capturedPages") == 5
        and activity_metrics.get("captureEvents") == 9
        and activity_metrics.get("uniqueDomains") == 3,
        "The local-day summary includes both boundary instants and excludes the next day exactly.",
        "The summary produced incorrect local-day capture or uniqueness counts.",
    )
    check(
        "repeat_capture_definition",
        "activity_summary",
        len(activity_domains) == 3
        and activity_domains[0].get("domain") == "example.com"
        and activity_domains[0].get("capturedPages") == 2
        and activity_domains[0].get("captureEvents") == 3
        and activity_domains[1].get("domain") == "clean.example.net"
        and activity_domains[1].get("capturedPages") == 2
        and activity_domains[2].get("domain") == "noisy.example.net"
        and activity_domains[2].get("capturedPages") == 1
        and activity_domains[2].get("captureEvents") == 4
        and "report capturedPages" in activity.get("reportingGuidance", "")
        and "not complete browser history" in activity["definitions"]["browserHistoryVisits"].lower(),
        "Repeat captures and unique page URLs are reported separately with an honest browser-history limit.",
        "The summary conflates capture events with unique URLs or browser visits.",
    )
    check(
        "protected_activity_filtered",
        "protection",
        PROTECTED_URL not in activity_blob
        and "mail.google.com" not in activity_blob
        and "outside.example.net" not in activity_blob,
        "Protected and out-of-window activity are absent from aggregate results.",
        "A protected or out-of-window domain crossed the aggregate agent boundary.",
    )

    previous_provider = os.environ.get("DAEMON_MODE_AI_PROVIDER")
    previous_disabled = os.environ.get("DAEMON_MODE_DISABLE_AI")
    previous_key = os.environ.get("OPENAI_API_KEY")
    try:
        os.environ["DAEMON_MODE_AI_PROVIDER"] = "mock"
        os.environ["DAEMON_MODE_DISABLE_AI"] = "1"
        disabled = mcp.call_tool(
            "answer_from_evidence",
            {"question": "What does the Borealis protocol require?", "limit": 3, "maxTokens": 700},
            db_path,
            context,
        )
        check(
            "provider_disabled_preserves_evidence",
            "provider_setup",
            disabled.get("status") == "provider_disabled"
            and not disabled.get("usesAi")
            and bool(disabled.get("selectedEvidence"))
            and bool(disabled.get("citations")),
            "Disabled AI reports setup state while preserving inspectable evidence and citations.",
            "Disabled AI lost evidence/citations or reported the wrong provider state.",
        )

        os.environ["DAEMON_MODE_AI_PROVIDER"] = "openai"
        os.environ.pop("DAEMON_MODE_DISABLE_AI", None)
        os.environ.pop("OPENAI_API_KEY", None)
        missing_key = mcp.call_tool(
            "answer_from_evidence",
            {"question": "What does the Borealis protocol require?", "limit": 3, "maxTokens": 700},
            db_path,
            context,
        )
        check(
            "provider_missing_preserves_evidence",
            "provider_setup",
            missing_key.get("status") == "provider_missing_key"
            and not missing_key.get("usesAi")
            and bool(missing_key.get("selectedEvidence"))
            and bool(missing_key.get("citations")),
            "Missing provider credentials are diagnosed without discarding evidence.",
            "Missing credentials were misdiagnosed or discarded evidence/citations.",
        )
    finally:
        for key, value in {
            "DAEMON_MODE_AI_PROVIDER": previous_provider,
            "DAEMON_MODE_DISABLE_AI": previous_disabled,
            "OPENAI_API_KEY": previous_key,
        }.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    with service.open_db(db_path) as connection:
        service.init_db(connection)
        query_log = service.list_agent_queries(connection, 50)
    log_blob = json.dumps(query_log, sort_keys=True)
    check(
        "query_log_excludes_evidence",
        "logging",
        len(query_log) >= 9
        and SAFE_RAW_SENTINEL not in log_blob
        and PROTECTED_RAW_SENTINEL not in log_blob
        and PROTECTED_URL not in log_blob,
        "Query logs retain outcomes without raw evidence snippets or protected URLs.",
        "Query logs contain evidence content, protected provenance, or missing tool-call outcomes.",
    )

    return cases


def report(cases, output_format):
    passed = sum(case.passed for case in cases)
    payload = {
        "version": "0.5.10",
        "isolatedFixtureDatabase": True,
        "summary": {"passed": passed, "failed": len(cases) - passed, "total": len(cases)},
        "cases": [asdict(case) for case in cases],
    }
    if output_format == "json":
        print(json.dumps(payload, indent=2))
        return

    print("# Daemon Mode 0.5.10 MCP Retrieval Eval")
    print(f"Result: {passed}/{len(cases)} passed; isolated fixture database: yes")
    for case in cases:
        status = "PASS" if case.passed else "FAIL"
        print(f"- [{status}] {case.name} ({case.subsystem}): {case.diagnosis}")


def main():
    parser = argparse.ArgumentParser(description="Run isolated MCP retrieval quality regressions.")
    parser.add_argument("--format", choices=("text", "json"), default="text")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="daemon-mcp-retrieval-eval-") as temp_dir:
        cases = evaluate(Path(temp_dir) / "memory.sqlite3")
    report(cases, args.format)
    if not all(case.passed for case in cases):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
