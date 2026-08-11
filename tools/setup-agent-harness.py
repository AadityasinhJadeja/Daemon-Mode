#!/usr/bin/env python3
import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ is expected.
    tomllib = None


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVICE_PATH = REPO_ROOT / "tools" / "local-memory-service.py"
SERVER_NAME = "daemon-mode"
CLIENT_IDS = ("claude-code", "codex", "cursor", "claude-desktop", "chatgpt")


class SetupError(RuntimeError):
    pass


def load_service_module():
    spec = importlib.util.spec_from_file_location("daemon_mode_local_memory_service", SERVICE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def detected_python3():
    command = shutil.which("python3")
    if not command:
        raise SetupError("Python 3 is required but was not found on PATH.")
    return str(Path(command).resolve())


def managed_runtime_root(service):
    configured = os.environ.get("DAEMON_MODE_RUNTIME_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(service.DEFAULT_DATA_DIR).expanduser() / "runtime"


def managed_server_path(service):
    return managed_runtime_root(service) / "current" / "tools" / "daemon-mcp-server.py"


def canonical_spec(service, db_path):
    return service.mcp_server_spec(
        db_path,
        python_command=detected_python3(),
        server_path=managed_server_path(service),
    )


def client_spec(client_id, spec):
    if client_id == "claude-desktop":
        return {
            "command": spec["command"],
            "args": list(spec["args"]),
        }
    return {
        "type": "stdio",
        "command": spec["command"],
        "args": list(spec["args"]),
    }


def normalized_spec(value):
    if not isinstance(value, dict):
        return None
    command = value.get("command")
    args = value.get("args")
    server_type = value.get("type", "stdio")
    if not isinstance(command, str) or not isinstance(args, list) or server_type != "stdio":
        return None
    return {
        "type": "stdio",
        "command": command,
        "args": [str(item) for item in args],
    }


def specs_match(current, desired):
    return normalized_spec(current) == normalized_spec(desired)


def safe_existing_daemon_spec(current, db_path):
    if not isinstance(current, dict) or set(current) - {"type", "command", "args"}:
        return False
    normalized = normalized_spec(current)
    if normalized is None:
        return False
    command_path = Path(normalized["command"])
    args = normalized["args"]
    if not command_path.is_absolute() or not re.fullmatch(r"python3(?:\.\d+)*", command_path.name):
        return False
    if len(args) != 3 or args[1] != "--db-path":
        return False
    server_path = Path(args[0])
    if not server_path.is_absolute() or server_path.name != "daemon-mcp-server.py":
        return False
    return args[2] == str(Path(db_path).expanduser().resolve())


def default_config_path(client_id, scope, repo_root=REPO_ROOT, home=None):
    home = Path(home or Path.home()).expanduser()

    if client_id == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", home / ".codex")).expanduser()
        return codex_home / "config.toml"

    if client_id == "cursor":
        return repo_root / ".cursor" / "mcp.json" if scope == "project" else home / ".cursor" / "mcp.json"

    if client_id == "claude-code":
        return repo_root / ".mcp.json" if scope == "project" else home / ".claude.json"

    if client_id == "claude-desktop":
        if sys.platform == "darwin":
            return home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
        if os.name == "nt":
            app_data = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
            return app_data / "Claude" / "claude_desktop_config.json"
        return home / ".config" / "Claude" / "claude_desktop_config.json"

    raise SetupError(f"No local config path exists for {client_id}.")


def validate_scope(client_id, scope):
    allowed = {
        "claude-code": {"local", "user", "project"},
        "codex": {"user"},
        "cursor": {"user", "project"},
        "claude-desktop": {"user"},
    }
    if scope not in allowed.get(client_id, set()):
        choices = ", ".join(sorted(allowed.get(client_id, set()))) or "none"
        raise SetupError(f"{client_id} supports these helper scopes: {choices}.")


def backup_config(path):
    timestamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = path.with_name(f"{path.name}.daemon-backup-{timestamp}")
    shutil.copy2(path, backup_path)
    return backup_path


def atomic_write(path, content, expected_bytes):
    if path.is_symlink():
        raise SetupError(f"Refusing to replace symlinked config: {path}")
    if path.exists() and not path.is_file():
        raise SetupError(f"Config path is not a regular file: {path}")

    current_bytes = path.read_bytes() if path.exists() else None
    if current_bytes != expected_bytes:
        raise SetupError(f"Config changed while Daemon was preparing the update: {path}")

    path.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    backup_path = backup_config(path) if path.exists() else None
    temp_path = None

    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as temp_file:
            temp_path = Path(temp_file.name)
            temp_file.write(content)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        temp_path.chmod(existing_mode)
        os.replace(temp_path, path)
    except Exception:
        if temp_path and temp_path.exists():
            temp_path.unlink()
        raise

    return backup_path


def load_json_config(path):
    if not path.exists():
        return {}, None
    raw = path.read_bytes()
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SetupError(f"Refusing to modify malformed JSON config {path}: {error}") from error
    if not isinstance(payload, dict):
        raise SetupError(f"Refusing to modify non-object JSON config: {path}")
    return payload, raw


def json_server_container(payload, client_id, scope, repo_root=REPO_ROOT):
    if client_id == "claude-code" and scope == "local":
        projects = payload.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise SetupError("Claude Code config has a non-object projects field.")
        project = projects.setdefault(str(repo_root), {})
        if not isinstance(project, dict):
            raise SetupError("Claude Code local project config is not an object.")
        servers = project.setdefault("mcpServers", {})
    else:
        servers = payload.setdefault("mcpServers", {})

    if not isinstance(servers, dict):
        raise SetupError("Config has a non-object mcpServers field.")
    return servers


def apply_json_config(path, client_id, scope, desired, db_path, migrate_existing=False):
    payload, original_bytes = load_json_config(path)
    servers = json_server_container(payload, client_id, scope)
    current = servers.get(SERVER_NAME)

    if current is not None:
        if specs_match(current, desired):
            return {"status": "already_configured", "path": path, "backup": None}
        if not migrate_existing or not safe_existing_daemon_spec(current, db_path):
            raise SetupError(
                f"{SERVER_NAME} already exists with different or ambiguous settings in {path}. "
                "Daemon will not overwrite it automatically."
            )
        servers[SERVER_NAME] = desired
        backup_path = atomic_write(path, json.dumps(payload, indent=2) + "\n", original_bytes)
        return {"status": "migrated", "path": path, "backup": backup_path}

    servers[SERVER_NAME] = desired
    backup_path = atomic_write(path, json.dumps(payload, indent=2) + "\n", original_bytes)
    return {"status": "configured", "path": path, "backup": backup_path}


def toml_string(value):
    return json.dumps(str(value), ensure_ascii=False)


def codex_toml_block(desired):
    args = ", ".join(toml_string(value) for value in desired["args"])
    return (
        f"[mcp_servers.{SERVER_NAME}]\n"
        f"command = {toml_string(desired['command'])}\n"
        f"args = [{args}]\n"
    )


def apply_codex_config(path, desired, db_path, migrate_existing=False):
    if tomllib is None:
        raise SetupError("Python 3.11 or newer is required for safe Codex TOML validation.")

    original_bytes = path.read_bytes() if path.exists() else None
    try:
        raw = original_bytes.decode("utf-8") if original_bytes is not None else ""
    except UnicodeDecodeError as error:
        raise SetupError(f"Refusing to modify non-UTF-8 TOML config {path}: {error}") from error
    try:
        payload = tomllib.loads(raw) if raw.strip() else {}
    except tomllib.TOMLDecodeError as error:
        raise SetupError(f"Refusing to modify malformed TOML config {path}: {error}") from error

    servers = payload.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise SetupError("Codex config has a non-table mcp_servers value.")
    current = servers.get(SERVER_NAME)

    if current is not None:
        if specs_match(current, desired):
            return {"status": "already_configured", "path": path, "backup": None}
        if not migrate_existing or not safe_existing_daemon_spec(current, db_path):
            raise SetupError(
                f"{SERVER_NAME} already exists with different or ambiguous settings in {path}. "
                "Daemon will not overwrite it automatically."
            )
        start_pattern = re.compile(
            rf"(?m)^\[mcp_servers\.{re.escape(SERVER_NAME)}\]\s*$"
        )
        start_match = start_pattern.search(raw)
        if not start_match:
            raise SetupError(f"Cannot safely locate the existing Daemon TOML table in {path}.")
        next_table = re.search(r"(?m)^\[[^\n]+\]\s*$", raw[start_match.end():])
        end = start_match.end() + next_table.start() if next_table else len(raw)
        replacement = codex_toml_block(desired).rstrip()
        content = raw[:start_match.start()] + replacement + "\n" + raw[end:].lstrip("\n")
        try:
            candidate = tomllib.loads(content)
        except tomllib.TOMLDecodeError as error:
            raise SetupError(f"Generated migrated Codex config is invalid: {path}: {error}") from error
        if not specs_match(candidate.get("mcp_servers", {}).get(SERVER_NAME), desired):
            raise SetupError(f"Migrated Codex config did not preserve the expected Daemon entry: {path}")
        backup_path = atomic_write(path, content, original_bytes)
        return {"status": "migrated", "path": path, "backup": backup_path}

    prefix = raw.rstrip()
    content = f"{prefix}\n\n{codex_toml_block(desired)}" if prefix else codex_toml_block(desired)
    try:
        candidate = tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        raise SetupError(
            f"Codex config uses a TOML shape Daemon cannot extend safely: {path}: {error}"
        ) from error
    if not specs_match(candidate.get("mcp_servers", {}).get(SERVER_NAME), desired):
        raise SetupError(f"Generated Codex config did not preserve the expected Daemon entry: {path}")
    backup_path = atomic_write(path, content, original_bytes)
    return {"status": "configured", "path": path, "backup": backup_path}


def apply_client(client_id, scope, db_path, config_path=None, migrate_existing=False):
    if client_id == "chatgpt":
        raise SetupError(
            "ChatGPT does not consume this local stdio server. Its later path is a remote MCP app/connector "
            "that must preserve Daemon's local-first privacy boundary."
        )

    validate_scope(client_id, scope)
    service = load_service_module()
    server_path = managed_server_path(service)
    if not server_path.is_file():
        raise SetupError(
            f"Daemon's managed runtime is not installed at {server_path}. "
            "Run ./tools/install-local-service-launch-agent.sh first."
        )
    desired = client_spec(client_id, canonical_spec(service, db_path))
    path = Path(config_path).expanduser().absolute() if config_path else default_config_path(client_id, scope)

    if client_id == "codex":
        return apply_codex_config(path, desired, db_path, migrate_existing=migrate_existing)
    return apply_json_config(
        path,
        client_id,
        scope,
        desired,
        db_path,
        migrate_existing=migrate_existing,
    )


def render_dry_run(db_path, client_id=None):
    service = load_service_module()
    python_command = detected_python3()
    server_path = managed_server_path(service)
    clients = service.mcp_client_configs(
        db_path,
        python_command=python_command,
        server_path=server_path,
    )
    if client_id:
        clients = [client for client in clients if client["id"] == client_id]

    print("Daemon Mode MCP setup (dry run)")
    print(f"Source checkout: {REPO_ROOT}")
    print(f"Managed server: {server_path}")
    print(f"Managed runtime installed: {'yes' if server_path.is_file() else 'no'}")
    print(f"Memory: {db_path}")
    print(f"Memory file exists: {'yes' if db_path.exists() else 'not yet'}")
    print(f"Python 3: {python_command}")
    print("Boundary: agent actions are non-destructive; calls keep bounded local diagnostics, while humans retain export, deletion, and protection controls.")

    for client in clients:
        print(f"\n[{client['name']}] {client['status']}")
        print(client["description"])
        print(client["snippet"])

    if client_id == "chatgpt":
        print("\nNothing was changed. ChatGPT cannot use this local stdio setup path.")
    else:
        apply_client_id = client_id or "cursor"
        print("\nNothing was changed. To write one config explicitly:")
        print(f'python3 "{Path(__file__).resolve()}" --apply {apply_client_id} --scope user')
    print("The helper refuses conflicts, backs up existing files, and leaves repeat runs unchanged.")


def main():
    service = load_service_module()
    parser = argparse.ArgumentParser(
        description="Print or safely apply local Daemon Mode MCP configuration for supported agent harnesses."
    )
    parser.add_argument("--client", choices=CLIENT_IDS, help="Show one client during dry-run output")
    parser.add_argument("--apply", choices=CLIENT_IDS, help="Explicitly write one supported client config")
    parser.add_argument(
        "--scope",
        choices=("local", "user", "project"),
        default="user",
        help="Client config scope; support differs by client (default: user)",
    )
    parser.add_argument("--db-path", default=str(service.DEFAULT_DB_PATH), help="Explicit local SQLite memory path")
    parser.add_argument("--config-path", help="Advanced/test override for the target config file")
    parser.add_argument(
        "--migrate-existing-daemon",
        action="store_true",
        help="Explicitly replace only a recognized older Daemon stdio entry that uses the same memory file",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()

    try:
        if args.config_path and not args.apply:
            raise SetupError("--config-path is only valid with --apply.")
        if args.migrate_existing_daemon and not args.apply:
            raise SetupError("--migrate-existing-daemon is only valid with --apply.")
        if args.client and args.apply:
            raise SetupError("Use --client for dry run or --apply for one explicit write, not both.")

        if not args.apply:
            render_dry_run(db_path, client_id=args.client)
            return 0

        result = apply_client(
            args.apply,
            args.scope,
            db_path,
            config_path=args.config_path,
            migrate_existing=args.migrate_existing_daemon,
        )
        print(f"{args.apply}: {result['status']}")
        print(f"Config: {result['path']}")
        if result["backup"]:
            print(f"Backup: {result['backup']}")
        else:
            print("Backup: not needed; config already matched")
        print("Agent access remains non-destructive.")
        return 0
    except (SetupError, OSError) as error:
        print(f"setup refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
