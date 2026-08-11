#!/usr/bin/env python3
import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "tools" / "setup-agent-harness.py"
PRIVATE_SENTINEL = "PRIVATE_MEMORY_TEXT_MUST_NEVER_APPEAR"
PYTHON_PATH = str(Path(shutil.which("python3")).resolve())
RUNTIME_ROOT = None
SERVER_PATH = None


def print_step(message):
    print(f"[setup-proof] {message}")


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    print_step(f"PASS {message}")


def run_helper(*args, expected_code=0):
    env = os.environ.copy()
    if RUNTIME_ROOT is not None:
        env["DAEMON_MODE_RUNTIME_ROOT"] = str(RUNTIME_ROOT)
    result = subprocess.run(
        [sys.executable, str(HELPER), *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
    )
    if result.returncode != expected_code:
        raise AssertionError(
            f"helper returned {result.returncode}, expected {expected_code}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def backup_files(path):
    return sorted(path.parent.glob(f"{path.name}.daemon-backup-*"))


def assert_server_spec(spec, db_path, message):
    assert_true(spec.get("command") == PYTHON_PATH, f"{message} uses the detected absolute Python 3")
    assert_true(
        spec.get("args") == [str(SERVER_PATH), "--db-path", str(db_path)],
        f"{message} uses exact managed-runtime and memory paths",
    )


def main():
    global RUNTIME_ROOT, SERVER_PATH
    with tempfile.TemporaryDirectory(prefix="daemon-setup-proof-") as temp_dir:
        temp_root = Path(temp_dir).resolve()
        RUNTIME_ROOT = temp_root / "runtime"
        SERVER_PATH = RUNTIME_ROOT / "current" / "tools" / "daemon-mcp-server.py"
        SERVER_PATH.parent.mkdir(parents=True)
        SERVER_PATH.write_text("# isolated managed runtime proof\n", encoding="utf-8")
        db_path = temp_root / "memory.sqlite3"
        db_path.write_text(PRIVATE_SENTINEL, encoding="utf-8")

        dry_run = run_helper("--db-path", db_path)
        assert_true("Daemon Mode MCP setup (dry run)" in dry_run.stdout, "one command renders setup guidance")
        assert_true(str(ROOT) in dry_run.stdout, "dry run detects the source checkout path")
        assert_true(str(SERVER_PATH) in dry_run.stdout, "dry run uses the stable managed runtime path")
        assert_true("Managed runtime installed: yes" in dry_run.stdout, "dry run confirms managed runtime readiness")
        assert_true(str(db_path) in dry_run.stdout, "dry run detects the SQLite path")
        assert_true("Claude Code" in dry_run.stdout and "Codex" in dry_run.stdout, "dry run covers coding agents")
        assert_true("Cursor" in dry_run.stdout and "Claude Desktop" in dry_run.stdout, "dry run covers desktop clients")
        assert_true("later remote MCP path" in dry_run.stdout, "dry run keeps ChatGPT on the honest later path")
        assert_true(PRIVATE_SENTINEL not in dry_run.stdout, "dry run never reads private memory content")

        codex_dry_run = run_helper("--client", "codex", "--db-path", db_path)
        assert_true("--apply codex --scope user" in codex_dry_run.stdout, "single-client dry run recommends that same client")
        assert_true("--apply cursor" not in codex_dry_run.stdout, "single-client dry run does not recommend a different client")

        chatgpt_dry_run = run_helper("--client", "chatgpt", "--db-path", db_path)
        assert_true("ChatGPT cannot use this local stdio setup path" in chatgpt_dry_run.stdout, "ChatGPT dry run does not suggest an invalid apply command")

        fake_bin = temp_root / "fake-bin"
        fake_bin.mkdir()
        fake_args_path = temp_root / "claude-args.json"
        fake_claude = fake_bin / "claude"
        fake_claude.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['DAEMON_FAKE_ARGS']).write_text(json.dumps(sys.argv[1:]))\n",
            encoding="utf-8",
        )
        fake_claude.chmod(0o755)
        claude_command = next(line for line in dry_run.stdout.splitlines() if line.startswith("claude mcp add-json"))
        fake_env = os.environ.copy()
        fake_env["PATH"] = f"{fake_bin}{os.pathsep}{fake_env.get('PATH', '')}"
        fake_env["DAEMON_FAKE_ARGS"] = str(fake_args_path)
        subprocess.run(claude_command, shell=True, check=True, cwd=ROOT, env=fake_env)
        claude_args = json.loads(fake_args_path.read_text(encoding="utf-8"))
        assert_true(claude_args[:3] == ["mcp", "add-json", "daemon-mode"], "Claude command keeps CLI arguments intact")
        claude_spec = json.loads(claude_args[3])
        assert_server_spec(claude_spec, db_path, "Claude shell command")

        cursor_path = temp_root / "cursor" / "mcp.json"
        cursor_path.parent.mkdir(parents=True)
        cursor_path.write_text(
            json.dumps({"mcpServers": {"existing": {"command": "existing", "args": []}}, "theme": "quiet"}),
            encoding="utf-8",
        )
        cursor_first = run_helper(
            "--apply",
            "cursor",
            "--scope",
            "user",
            "--config-path",
            cursor_path,
            "--db-path",
            db_path,
        )
        cursor_payload = json.loads(cursor_path.read_text(encoding="utf-8"))
        assert_true(cursor_first.stdout.startswith("cursor: configured"), "Cursor apply reports a real write")
        assert_true(cursor_payload.get("theme") == "quiet", "Cursor merge preserves unrelated settings")
        assert_true("existing" in cursor_payload["mcpServers"], "Cursor merge preserves unrelated servers")
        assert_server_spec(cursor_payload["mcpServers"]["daemon-mode"], db_path, "Cursor config")
        assert_true(len(backup_files(cursor_path)) == 1, "Cursor apply backs up an existing config")
        cursor_before = cursor_path.read_bytes()
        cursor_second = run_helper(
            "--apply",
            "cursor",
            "--scope",
            "user",
            "--config-path",
            cursor_path,
            "--db-path",
            db_path,
        )
        assert_true("already_configured" in cursor_second.stdout, "Cursor repeat apply is idempotent")
        assert_true(cursor_path.read_bytes() == cursor_before, "Cursor repeat apply leaves bytes unchanged")
        assert_true(len(backup_files(cursor_path)) == 1, "Cursor repeat apply creates no extra backup")

        claude_path = temp_root / "claude.json"
        claude_path.write_text(
            json.dumps({"projects": {str(ROOT): {"permissionMode": "default"}}, "other": True}),
            encoding="utf-8",
        )
        run_helper(
            "--apply",
            "claude-code",
            "--scope",
            "local",
            "--config-path",
            claude_path,
            "--db-path",
            db_path,
        )
        claude_payload = json.loads(claude_path.read_text(encoding="utf-8"))
        claude_project = claude_payload["projects"][str(ROOT)]
        assert_true(claude_project.get("permissionMode") == "default", "Claude local merge preserves project settings")
        assert_server_spec(claude_project["mcpServers"]["daemon-mode"], db_path, "Claude Code config")
        assert_true(len(backup_files(claude_path)) == 1, "Claude Code apply backs up its config")

        desktop_path = temp_root / "claude_desktop_config.json"
        desktop_path.write_text(json.dumps({"ui": {"density": "compact"}}), encoding="utf-8")
        run_helper(
            "--apply",
            "claude-desktop",
            "--scope",
            "user",
            "--config-path",
            desktop_path,
            "--db-path",
            db_path,
        )
        desktop_payload = json.loads(desktop_path.read_text(encoding="utf-8"))
        assert_true(desktop_payload.get("ui", {}).get("density") == "compact", "Desktop merge preserves settings")
        assert_server_spec(desktop_payload["mcpServers"]["daemon-mode"], db_path, "Claude Desktop config")
        assert_true(len(backup_files(desktop_path)) == 1, "Claude Desktop apply backs up its config")

        codex_path = temp_root / "config.toml"
        codex_path.write_text('model = "gpt-5"\n\n[features]\nexample = true\n', encoding="utf-8")
        run_helper(
            "--apply",
            "codex",
            "--scope",
            "user",
            "--config-path",
            codex_path,
            "--db-path",
            db_path,
        )
        codex_payload = tomllib.loads(codex_path.read_text(encoding="utf-8"))
        assert_true(codex_payload.get("model") == "gpt-5", "Codex append preserves unrelated TOML")
        assert_true(codex_payload.get("features", {}).get("example") is True, "Codex append preserves tables")
        assert_server_spec(codex_payload["mcp_servers"]["daemon-mode"], db_path, "Codex config")
        assert_true(len(backup_files(codex_path)) == 1, "Codex apply backs up an existing config")
        codex_before = codex_path.read_bytes()
        codex_second = run_helper(
            "--apply",
            "codex",
            "--scope",
            "user",
            "--config-path",
            codex_path,
            "--db-path",
            db_path,
        )
        assert_true("already_configured" in codex_second.stdout, "Codex repeat apply is idempotent")
        assert_true(codex_path.read_bytes() == codex_before, "Codex repeat apply leaves bytes unchanged")

        relative_codex_path = temp_root / "relative-config.toml"
        relative_codex_path.write_text(
            f'[mcp_servers.daemon-mode]\ncommand = "python3"\n'
            f'args = ["{SERVER_PATH}", "--db-path", "{db_path}"]\n',
            encoding="utf-8",
        )
        relative_before = relative_codex_path.read_bytes()
        relative_result = run_helper(
            "--apply",
            "codex",
            "--config-path",
            relative_codex_path,
            "--db-path",
            db_path,
            expected_code=2,
        )
        assert_true("different or ambiguous settings" in relative_result.stderr, "relative Python config is not called idempotent")
        assert_true(relative_codex_path.read_bytes() == relative_before, "relative Python conflict remains untouched")

        inline_codex_path = temp_root / "inline-config.toml"
        inline_codex_path.write_text(
            'mcp_servers = { existing = { command = "existing", args = [] } }\n',
            encoding="utf-8",
        )
        inline_before = inline_codex_path.read_bytes()
        inline_result = run_helper(
            "--apply",
            "codex",
            "--config-path",
            inline_codex_path,
            "--db-path",
            db_path,
            expected_code=2,
        )
        assert_true("cannot extend safely" in inline_result.stderr, "non-extendable Codex TOML is refused")
        assert_true(inline_codex_path.read_bytes() == inline_before, "refused Codex TOML remains valid and untouched")

        malformed_path = temp_root / "malformed.json"
        malformed_path.write_text("{not valid json", encoding="utf-8")
        malformed_before = malformed_path.read_bytes()
        malformed = run_helper(
            "--apply",
            "cursor",
            "--config-path",
            malformed_path,
            "--db-path",
            db_path,
            expected_code=2,
        )
        assert_true("setup refused" in malformed.stderr, "malformed config fails closed")
        assert_true(malformed_path.read_bytes() == malformed_before, "malformed config remains untouched")
        assert_true(not backup_files(malformed_path), "malformed config creates no misleading backup")

        symlink_target = temp_root / "symlink-target.json"
        symlink_target.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        symlink_path = temp_root / "symlink-config.json"
        symlink_path.symlink_to(symlink_target)
        symlink_before = symlink_target.read_bytes()
        symlink_result = run_helper(
            "--apply",
            "cursor",
            "--config-path",
            symlink_path,
            "--db-path",
            db_path,
            expected_code=2,
        )
        assert_true("symlinked config" in symlink_result.stderr, "symlinked config is refused")
        assert_true(symlink_target.read_bytes() == symlink_before, "symlink target remains untouched")

        legacy_cursor_path = temp_root / "legacy-cursor.json"
        legacy_cursor_path.write_text(
            json.dumps(
                {
                    "theme": "quiet",
                    "mcpServers": {
                        "existing": {"command": "existing", "args": []},
                        "daemon-mode": {
                            "type": "stdio",
                            "command": PYTHON_PATH,
                            "args": [str(ROOT / "tools" / "daemon-mcp-server.py"), "--db-path", str(db_path)],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        legacy_cursor = run_helper(
            "--apply",
            "cursor",
            "--config-path",
            legacy_cursor_path,
            "--db-path",
            db_path,
            "--migrate-existing-daemon",
        )
        legacy_cursor_payload = json.loads(legacy_cursor_path.read_text(encoding="utf-8"))
        assert_true("cursor: migrated" in legacy_cursor.stdout, "explicit Cursor migration reports the path replacement")
        assert_server_spec(legacy_cursor_payload["mcpServers"]["daemon-mode"], db_path, "migrated Cursor config")
        assert_true(legacy_cursor_payload["theme"] == "quiet" and "existing" in legacy_cursor_payload["mcpServers"], "Cursor migration preserves unrelated config")
        assert_true(len(backup_files(legacy_cursor_path)) == 1, "Cursor migration backs up the old repo-pinned config")

        legacy_codex_path = temp_root / "legacy-codex.toml"
        legacy_codex_path.write_text(
            f'model = "gpt-5"\n\n[mcp_servers.daemon-mode]\ncommand = {json.dumps(PYTHON_PATH)}\n'
            f'args = [{json.dumps(str(ROOT / "tools" / "daemon-mcp-server.py"))}, "--db-path", {json.dumps(str(db_path))}]\n\n'
            '[features]\nexample = true\n',
            encoding="utf-8",
        )
        legacy_codex = run_helper(
            "--apply",
            "codex",
            "--config-path",
            legacy_codex_path,
            "--db-path",
            db_path,
            "--migrate-existing-daemon",
        )
        legacy_codex_payload = tomllib.loads(legacy_codex_path.read_text(encoding="utf-8"))
        assert_true("codex: migrated" in legacy_codex.stdout, "explicit Codex migration reports the path replacement")
        assert_server_spec(legacy_codex_payload["mcp_servers"]["daemon-mode"], db_path, "migrated Codex config")
        assert_true(legacy_codex_payload["model"] == "gpt-5" and legacy_codex_payload["features"]["example"] is True, "Codex migration preserves unrelated TOML")
        assert_true(len(backup_files(legacy_codex_path)) == 1, "Codex migration backs up the old repo-pinned config")

        wrong_db_path = temp_root / "wrong-db.json"
        wrong_db_path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "daemon-mode": {
                            "type": "stdio",
                            "command": PYTHON_PATH,
                            "args": [
                                str(ROOT / "tools" / "daemon-mcp-server.py"),
                                "--db-path",
                                str(temp_root / "other.sqlite3"),
                            ],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        wrong_db_before = wrong_db_path.read_bytes()
        wrong_db = run_helper(
            "--apply",
            "cursor",
            "--config-path",
            wrong_db_path,
            "--db-path",
            db_path,
            "--migrate-existing-daemon",
            expected_code=2,
        )
        assert_true("different or ambiguous settings" in wrong_db.stderr, "migration refuses an existing Daemon entry for another database")
        assert_true(wrong_db_path.read_bytes() == wrong_db_before and not backup_files(wrong_db_path), "wrong-database migration refusal leaves config untouched")

        conflict_path = temp_root / "conflict.json"
        conflict_path.write_text(
            json.dumps({"mcpServers": {"daemon-mode": {"command": "other", "args": []}}}),
            encoding="utf-8",
        )
        conflict_before = conflict_path.read_bytes()
        conflict = run_helper(
            "--apply",
            "cursor",
            "--config-path",
            conflict_path,
            "--db-path",
            db_path,
            expected_code=2,
        )
        assert_true("different or ambiguous settings" in conflict.stderr, "conflicting server definition is refused")
        assert_true(conflict_path.read_bytes() == conflict_before, "conflicting config remains untouched")

        chatgpt = run_helper("--apply", "chatgpt", "--db-path", db_path, expected_code=2)
        assert_true("does not consume this local stdio server" in chatgpt.stderr, "ChatGPT apply is honestly refused")

        managed_server_bytes = SERVER_PATH.read_bytes()
        SERVER_PATH.unlink()
        missing_runtime_path = temp_root / "missing-runtime.json"
        missing_runtime = run_helper(
            "--apply",
            "cursor",
            "--config-path",
            missing_runtime_path,
            "--db-path",
            db_path,
            expected_code=2,
        )
        assert_true("managed runtime is not installed" in missing_runtime.stderr, "apply refuses a missing managed runtime")
        assert_true(not missing_runtime_path.exists(), "missing-runtime refusal does not create client config")
        SERVER_PATH.write_bytes(managed_server_bytes)

    print_step("Setup helper proof passed using temporary configs only.")


if __name__ == "__main__":
    main()
