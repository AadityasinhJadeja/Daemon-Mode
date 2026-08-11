#!/usr/bin/env python3
import argparse
import datetime as dt
import math
import os
import sqlite3
import statistics
import sys
from collections import Counter
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def default_data_dir():
    configured_dir = os.environ.get("DAEMON_MODE_DATA_DIR")

    if configured_dir:
        return Path(configured_dir).expanduser()

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Daemon Mode"

    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Daemon Mode"

    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "daemon-mode"


DEFAULT_DB_PATH = default_data_dir() / "memory.sqlite3"
EVIDENCE_TOOLS = {"search_memory", "retrieve_evidence", "answer_from_evidence", "get_activity_summary"}
HIT_STATUSES = {"hit", "answered", "provider_disabled", "provider_missing_key", "summarized"}
MISS_STATUSES = {"miss", "no_evidence"}
SETUP_STATUSES = {"provider_missing_key"}
RELIABILITY_STATUSES = {"error", "provider_error"}


def parse_time(value):
    if not value:
        return None

    text = str(value)
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"

    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)

    return parsed.astimezone(dt.timezone.utc)


def iso_day(value):
    parsed = parse_time(value)
    return parsed.date().isoformat() if parsed else "unknown"


def table_exists(connection, table_name):
    row = connection.execute(
        """
        SELECT name
        FROM sqlite_master
        WHERE type = 'table'
          AND name = ?
        """,
        (table_name,),
    ).fetchone()
    return row is not None


def table_columns(connection, table_name):
    return {
        row["name"]
        for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def load_rows(db_path, days):
    if not db_path.exists():
        return []

    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row

        if not table_exists(connection, "agent_queries"):
            return []

        columns = table_columns(connection, "agent_queries")
        filtered_select = (
            "filtered_protected_count"
            if "filtered_protected_count" in columns
            else "0 AS filtered_protected_count"
        )
        duration_select = "duration_ms" if "duration_ms" in columns else "NULL AS duration_ms"

        rows = connection.execute(
            f"""
            SELECT
              id,
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
              {filtered_select},
              {duration_select},
              created_at
            FROM agent_queries
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()

    visible_rows = []
    for row in rows:
        created_at = parse_time(row["created_at"])
        if created_at and created_at < since:
            continue
        agent_name = str(row["agent_name"] or "")
        source = str(row["source"] or "")
        if "config-smoke" in agent_name or "smoke" in source:
            continue
        visible_rows.append(dict(row))

    return visible_rows


def is_hit(row):
    status = str(row.get("status") or "")
    evidence_count = int(row.get("evidence_count") or 0)
    return evidence_count > 0 or status in HIT_STATUSES


def is_miss(row):
    status = str(row.get("status") or "")
    evidence_count = int(row.get("evidence_count") or 0)
    return row.get("tool_name") in EVIDENCE_TOOLS and evidence_count == 0 and status in MISS_STATUSES


def filtered_protected_count(row):
    return max(0, int(row.get("filtered_protected_count") or 0))


def evidence_breakdown(rows):
    evidence_rows = [row for row in rows if row.get("tool_name") in EVIDENCE_TOOLS]
    hits = [row for row in evidence_rows if is_hit(row)]
    misses = [row for row in evidence_rows if is_miss(row)]
    protected_misses = [row for row in misses if filtered_protected_count(row) > 0]
    no_evidence_misses = [row for row in misses if filtered_protected_count(row) == 0]
    return evidence_rows, hits, misses, protected_misses, no_evidence_misses


def classify_readout(rows, require_provider=False):
    if not rows:
        return (
            "insufficient_data",
            "No non-smoke agent calls were recorded in the window. Keep dogfooding before any launch story.",
        )

    evidence_rows, hits, _misses, protected_misses, no_evidence_misses = evidence_breakdown(rows)
    errors = [
        row
        for row in rows
        if row.get("status") in RELIABILITY_STATUSES
        or row.get("provider_status") in RELIABILITY_STATUSES
        or row.get("error")
    ]
    evidence_active_days = {
        day
        for row in evidence_rows
        if (day := iso_day(row.get("created_at"))) != "unknown"
    }

    if errors:
        return (
            "fix_reliability",
            "Agent access produced errors. Fix reliability before expanding public setup.",
        )

    if len(evidence_rows) < 12 or len(evidence_active_days) < 3:
        gaps = []
        if len(evidence_rows) < 12:
            gaps.append("low repeated usage")
        if len(evidence_active_days) == 1:
            gaps.append("one-day-only usage")
        elif len(evidence_active_days) < 3:
            gaps.append("fewer than three evidence-active days")
        return (
            "keep_dogfooding",
            "There is signal, but not enough repeated recall use yet"
            + (f" ({'; '.join(gaps)})" if gaps else "")
            + ". Keep collecting evidence-seeking agent calls.",
        )

    retrieval_opportunities = len(hits) + len(no_evidence_misses)
    hit_rate = len(hits) / retrieval_opportunities if retrieval_opportunities else 0
    answer_attempts = [row for row in rows if row.get("tool_name") == "answer_from_evidence"]
    setup_friction = [
        row
        for row in answer_attempts
        if row.get("status") in SETUP_STATUSES or row.get("provider_status") in SETUP_STATUSES
    ]

    if not retrieval_opportunities and protected_misses:
        return (
            "keep_dogfooding",
            "Recent evidence-seeking calls only found protected memory. Protection worked, but there is not enough agent-usable recall to judge retrieval.",
        )

    if hit_rate < 0.45 or len(no_evidence_misses) > len(hits):
        return (
            "fix_retrieval",
            "Agents are asking, but useful evidence is too inconsistent. Improve retrieval before launch.",
        )

    if require_provider and setup_friction and len(setup_friction) / len(answer_attempts) > 0.4:
        return (
            "fix_setup",
            "Retrieval works, but provider/setup friction dominates the readout.",
        )

    return (
        "public_story_candidate",
        "Repeated local agent recall is producing evidence. This is a technical candidate only; owner-confirmed usefulness is still required before public work.",
    )


def percent(part, total):
    if not total:
        return "0%"
    return f"{round((part / total) * 100)}%"


def compact_query(row, include_queries):
    query = str(row.get("query_text") or "").strip()
    if include_queries:
        return query or "no query"
    if not query:
        return "no query"
    return f"redacted query ({len(query)} chars)"


def latency_summary(rows):
    durations = [
        max(0, int(row.get("duration_ms")))
        for row in rows
        if row.get("duration_ms") is not None
    ]
    if not durations:
        return None
    ordered = sorted(durations)
    p95_index = max(0, math.ceil(len(ordered) * 0.95) - 1)
    return {
        "count": len(ordered),
        "median": round(statistics.median(ordered)),
        "p95": ordered[p95_index],
    }


def launch_recommendation(decision, owner_confirmed_useful=False):
    if decision == "public_story_candidate":
        if owner_confirmed_useful:
            return "Proceed with the scoped technical-preview launch checklist; keep claims evidence-backed and local-first."
        return "Hold public launch until the owner confirms that cited recall improved real work."
    return "Do not start public launch polish from this readout. Follow the named fix or keep dogfooding."


def render_markdown(rows, db_path, days, include_queries, require_provider=False, owner_confirmed_useful=False):
    now = dt.datetime.now(dt.timezone.utc)
    evidence_rows, hits, misses, protected_misses, no_evidence_misses = evidence_breakdown(rows)
    protection_checks = [row for row in rows if row.get("tool_name") == "check_protection_status"]
    ai_rows = [row for row in rows if int(row.get("uses_ai") or 0)]
    setup_rows = [
        row
        for row in rows
        if row.get("status") in SETUP_STATUSES or row.get("provider_status") in SETUP_STATUSES
    ]
    errors = [
        row
        for row in rows
        if row.get("status") in RELIABILITY_STATUSES
        or row.get("provider_status") in RELIABILITY_STATUSES
        or row.get("error")
    ]
    active_days = sorted({day for row in rows if (day := iso_day(row.get("created_at"))) != "unknown"})
    evidence_active_days = sorted(
        {day for row in evidence_rows if (day := iso_day(row.get("created_at"))) != "unknown"}
    )
    protected_filtered_calls = [row for row in evidence_rows if filtered_protected_count(row) > 0]
    protected_filtered_matches = sum(filtered_protected_count(row) for row in protected_filtered_calls)
    latency = latency_summary(rows)
    tool_counts = Counter(row.get("tool_name") or "unknown" for row in rows)
    status_counts = Counter(row.get("status") or "unknown" for row in rows)
    decision, reason = classify_readout(rows, require_provider=require_provider)

    lines = [
        "# Daemon Mode 0.5.8 Dogfood Readout",
        "",
        f"- Generated: {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- Window: last {days} day(s)",
        f"- Database: {'`' + str(db_path) + '`' if include_queries else 'local Daemon memory (path hidden)'}",
        f"- Raw queries included: {'yes' if include_queries else 'no'}",
        f"- Private mode: {'yes' if include_queries else 'no'}",
        f"- Provider-backed answers required: {'yes' if require_provider else 'no'}",
        f"- Owner-confirmed usefulness: {'yes' if owner_confirmed_useful else 'no'}",
        "",
        "## Scorecard",
        "",
        f"- Agent calls: {len(rows)}",
        f"- Active days: {len(active_days)}",
        f"- Evidence-active days: {len(evidence_active_days)}",
        f"- Evidence-seeking calls: {len(evidence_rows)}",
        f"- Evidence hits: {len(hits)} ({percent(len(hits), len(evidence_rows))})",
        f"- Evidence misses: {len(misses)} ({percent(len(misses), len(evidence_rows))})",
        f"- Protection checks: {len(protection_checks)}",
        f"- AI-backed answers: {len(ai_rows)}",
        f"- Provider/setup friction: {len(setup_rows)}",
        f"- Errors: {len(errors)}",
        f"- Recorded latency: {latency['median']} ms median / {latency['p95']} ms p95 ({latency['count']} calls)" if latency else "- Recorded latency: not available for these rows",
        "",
        "## Miss Diagnostics",
        "",
        f"- No local evidence: {len(no_evidence_misses)} call(s)",
        f"- Protected evidence filtered: {len(protected_filtered_calls)} call(s), {protected_filtered_matches} internal match(es)",
        f"- Provider missing: {len(setup_rows)} answer attempt(s)",
        f"- Reliability errors: {len(errors)} call(s)",
        f"- Low repeated usage: {'yes' if len(evidence_rows) < 12 else 'no'} ({len(evidence_rows)}/12 evidence-seeking calls)",
        f"- One-day-only usage: {'yes' if len(evidence_active_days) == 1 else 'no'}",
        "",
        "## Tool Mix",
        "",
    ]

    if tool_counts:
        lines.extend(f"- {name}: {count}" for name, count in tool_counts.most_common())
    else:
        lines.append("- No tool calls recorded.")

    lines.extend(["", "## Status Mix", ""])

    if status_counts:
        lines.extend(f"- {name}: {count}" for name, count in status_counts.most_common())
    else:
        lines.append("- No statuses recorded.")

    lines.extend(["", "## Latest Calls", ""])

    for row in rows[-8:][::-1]:
        created = row.get("created_at") or "unknown time"
        query = compact_query(row, include_queries)
        result_summary = (
            "aggregate summary"
            if row.get("tool_name") == "get_activity_summary"
            else f"{row.get('evidence_count') or 0} evidence"
        )
        lines.append(
            f"- {created} | {row.get('agent_name') or 'MCP client'} | {row.get('tool_name')} | "
            f"{row.get('status')} | {result_summary} | {query}"
        )

    if not rows:
        lines.append("- No non-smoke calls in this window.")

    lines.extend(
        [
            "",
            "## Decision Gate",
            "",
            f"- Readout: `{decision}`",
            f"- Why: {reason}",
            f"- Launch recommendation: {launch_recommendation(decision, owner_confirmed_useful)}",
            "",
            "## PM Notes",
            "",
            "- Treat this as a product readout, not analytics theater.",
            "- A good signal is repeated agent-assisted recall with cited evidence, not a high query count by itself.",
            "- A positive technical signal still requires the owner to confirm that recalled evidence improved real work.",
            "- Provider-backed prose is optional; retrieval-only evidence and exact local summaries are valid product use.",
            "- Public README/GitHub work should wait until the readout shows repeated value or the owner explicitly chooses a portfolio-story path.",
        ]
    )

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Summarize local MCP dogfood usage without exposing evidence snippets.")
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="Path to Daemon Mode memory.sqlite3")
    parser.add_argument("--days", type=int, default=14, help="Dogfood window in days")
    parser.add_argument(
        "--private",
        "--include-queries",
        dest="include_queries",
        action="store_true",
        help="Private owner mode: include raw query text and the exact database path",
    )
    parser.add_argument(
        "--require-provider",
        action="store_true",
        help="Treat missing provider credentials as setup friction; off by default for evidence-only use",
    )
    parser.add_argument(
        "--owner-confirmed-useful",
        action="store_true",
        help="Record the owner's qualitative confirmation when generating the launch recommendation",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser()
    rows = load_rows(db_path, max(1, args.days))
    print(
        render_markdown(
            rows,
            db_path,
            max(1, args.days),
            args.include_queries,
            require_provider=args.require_provider,
            owner_confirmed_useful=args.owner_confirmed_useful,
        ),
        end="",
    )


if __name__ == "__main__":
    main()
