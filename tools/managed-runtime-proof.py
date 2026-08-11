#!/usr/bin/env python3
"""Prove managed runtime install/update behavior without touching real user state."""

import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MANAGER = ROOT / "tools" / "manage-local-runtime.py"
LABEL = "com.daemonmode.local-memory-service"
DB_SENTINEL = b"DAEMON_MANAGED_RUNTIME_DB_MUST_SURVIVE"


def report(message):
    print(f"[runtime-proof] {message}")


def assert_true(condition, message):
    if not condition:
        raise AssertionError(message)
    report(f"PASS {message}")


def run_manager(command, source_root, data_dir, runtime_root, launch_agents_dir, *extra, expected=0):
    args = [
        sys.executable,
        str(MANAGER),
        command,
        "--data-dir",
        str(data_dir),
        "--runtime-root",
        str(runtime_root),
        "--launch-agents-dir",
        str(launch_agents_dir),
    ]
    if command == "install":
        args.extend(["--source-root", str(source_root), "--python-bin", sys.executable])
    args.extend(map(str, extra))
    result = subprocess.run(args, cwd=ROOT, capture_output=True, text=True)
    if result.returncode != expected:
        raise AssertionError(
            f"manager returned {result.returncode}, expected {expected}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def copy_runtime_source(destination):
    for relative in ("tools/local-memory-service.py", "tools/daemon-mcp-server.py"):
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    for relative in (
        "extension/assets/fonts",
        "extension/assets/brand",
        "extension/assets/icons",
    ):
        shutil.copytree(ROOT / relative, destination / relative)


def old_repo_plist(data_dir):
    return {
        "Label": LABEL,
        "ProgramArguments": [
            sys.executable,
            str(ROOT / "tools" / "local-memory-service.py"),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "4317",
        ],
        "WorkingDirectory": str(ROOT),
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(data_dir / "local-memory-service.out.log"),
        "StandardErrorPath": str(data_dir / "local-memory-service.err.log"),
    }


def main():
    with tempfile.TemporaryDirectory(prefix="daemon-managed-runtime-") as raw_temp:
        temp = Path(raw_temp)
        home = temp / "home"
        data_dir = home / "Library" / "Application Support" / "Daemon Mode"
        runtime_root = data_dir / "runtime"
        launch_agents_dir = home / "Library" / "LaunchAgents"
        launch_agents_dir.mkdir(parents=True)
        data_dir.mkdir(parents=True)
        db_path = data_dir / "memory.sqlite3"
        db_path.write_bytes(DB_SENTINEL)
        plist_path = launch_agents_dir / f"{LABEL}.plist"
        plist_path.write_bytes(plistlib.dumps(old_repo_plist(data_dir), sort_keys=False))
        plist_path.chmod(0o600)

        first = run_manager(
            "install",
            ROOT,
            data_dir,
            runtime_root,
            launch_agents_dir,
            "--skip-launchctl",
        )
        first_payload = json.loads(first.stdout)
        current = runtime_root / "current"
        managed_service = current / "tools" / "local-memory-service.py"
        managed_mcp = current / "tools" / "daemon-mcp-server.py"
        migrated_plist = plistlib.loads(plist_path.read_bytes())
        backups = sorted(launch_agents_dir.glob(f"{plist_path.name}.daemon-backup-*"))
        original_target = os.readlink(current)
        original_plist = plist_path.read_bytes()

        assert_true(first_payload["status"] == "installed", "repo-pinned LaunchAgent migrates to managed runtime")
        assert_true(len(backups) == 1, "migration backs up the prior LaunchAgent config")
        assert_true(plistlib.loads(backups[0].read_bytes())["WorkingDirectory"] == str(ROOT), "backup preserves the repo-pinned config")
        assert_true(current.is_symlink(), "managed runtime uses an atomic current pointer")
        assert_true(managed_service.is_file() and managed_mcp.is_file(), "managed runtime contains service and MCP entrypoints")
        assert_true(
            migrated_plist["ProgramArguments"][1] == str(managed_service),
            "LaunchAgent points at the stable managed service path",
        )
        assert_true(
            migrated_plist["ProgramArguments"][2:4] == ["--db-path", str(db_path)],
            "LaunchAgent pins the existing managed database path",
        )
        assert_true(migrated_plist["WorkingDirectory"] == str(current), "LaunchAgent working directory is checkout-independent")
        assert_true(
            migrated_plist["EnvironmentVariables"]["DAEMON_MODE_DATA_DIR"] == str(data_dir),
            "LaunchAgent keeps service runtime discovery anchored to the managed data directory",
        )
        assert_true(migrated_plist["RunAtLoad"] is True, "LaunchAgent starts the local service when the user logs in")
        assert_true(migrated_plist["KeepAlive"] is True, "LaunchAgent asks macOS to restart the local service after an exit")
        assert_true(db_path.read_bytes() == DB_SENTINEL, "migration preserves the SQLite database bytes")
        assert_true((data_dir.stat().st_mode & 0o777) == 0o700, "managed data directory is owner-only")
        assert_true((plist_path.stat().st_mode & 0o777) == 0o600, "managed LaunchAgent config is owner-only")

        repeated = run_manager(
            "install",
            ROOT,
            data_dir,
            runtime_root,
            launch_agents_dir,
            "--skip-launchctl",
        )
        repeated_payload = json.loads(repeated.stdout)
        assert_true(repeated_payload["status"] == "already_installed", "repeat install is idempotent")
        assert_true(os.readlink(current) == original_target, "repeat install leaves the active release unchanged")
        assert_true(plist_path.read_bytes() == original_plist, "repeat install leaves LaunchAgent bytes unchanged")
        assert_true(len(list(launch_agents_dir.glob(f"{plist_path.name}.daemon-backup-*"))) == 1, "repeat install creates no extra backup")
        assert_true(len(list((runtime_root / "releases").iterdir())) == 1, "repeat install creates no duplicate release")

        recovery_state = temp / "launchctl-loaded"
        recovery_calls = temp / "launchctl-recovery-calls.log"
        recovering_launchctl = temp / "recovering-launchctl"
        recovering_launchctl.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {str(recovery_calls)!r}\n"
            "if [[ \"${1:-}\" == \"print\" ]]; then\n"
            f"  [[ -f {str(recovery_state)!r} ]]\n"
            "  exit $?\n"
            "fi\n"
            "if [[ \"${1:-}\" == \"bootstrap\" ]]; then\n"
            f"  touch {str(recovery_state)!r}\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        recovering_launchctl.chmod(0o755)
        recovered = run_manager(
            "install",
            ROOT,
            data_dir,
            runtime_root,
            launch_agents_dir,
            "--launchctl-bin",
            recovering_launchctl,
        )
        assert_true(json.loads(recovered.stdout)["status"] == "already_installed", "repeat install repairs an unloaded LaunchAgent")
        assert_true(
            recovery_calls.read_text(encoding="utf-8").splitlines() == [
                f"print gui/{os.getuid()}/{LABEL}",
                f"bootstrap gui/{os.getuid()} {plist_path}",
                f"print gui/{os.getuid()}/{LABEL}",
            ],
            "repeat repair checks, reloads, and rechecks the LaunchAgent",
        )

        launchctl_calls = temp / "launchctl-failure-calls.log"
        detached_launchctl = temp / "detached-launchctl"
        detached_launchctl.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {str(launchctl_calls)!r}\n"
            "if [[ \"${1:-}\" == \"print\" ]]; then\n"
            "  exit 3\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        detached_launchctl.chmod(0o755)
        detached_repair = run_manager(
            "install",
            ROOT,
            data_dir,
            runtime_root,
            launch_agents_dir,
            "--launchctl-bin",
            detached_launchctl,
            expected=2,
        )
        repair_calls = launchctl_calls.read_text(encoding="utf-8").splitlines()
        assert_true(
            repair_calls == [
                f"print gui/{os.getuid()}/{LABEL}",
                f"bootstrap gui/{os.getuid()} {plist_path}",
                f"print gui/{os.getuid()}/{LABEL}",
            ],
            "repeat install attempts to reload an unloaded LaunchAgent and verifies it stayed loaded",
        )
        assert_true(
            "did not remain loaded" in detached_repair.stderr,
            "repeat install reports recovery failure instead of claiming success",
        )
        assert_true(os.readlink(current) == original_target, "failed repeat recovery leaves the active runtime unchanged")
        assert_true(plist_path.read_bytes() == original_plist, "failed repeat recovery leaves LaunchAgent config unchanged")
        assert_true(db_path.read_bytes() == DB_SENTINEL, "failed repeat recovery preserves the database")

        real_data = temp / "symlink-target"
        real_data.mkdir()
        linked_data = temp / "linked-data"
        linked_data.symlink_to(real_data, target_is_directory=True)
        symlink_refusal = run_manager(
            "install",
            ROOT,
            linked_data,
            linked_data / "runtime",
            temp / "linked-launch-agents",
            "--skip-launchctl",
            expected=2,
        )
        assert_true("symlinked data directory" in symlink_refusal.stderr, "installer refuses a symlinked data directory")
        assert_true(not any(real_data.iterdir()), "symlink refusal does not write through the target")

        safe_data = temp / "safe-data"
        safe_data.mkdir()
        real_runtime = temp / "real-runtime"
        real_runtime.mkdir()
        linked_runtime = temp / "linked-runtime"
        linked_runtime.symlink_to(real_runtime, target_is_directory=True)
        runtime_symlink_refusal = run_manager(
            "install",
            ROOT,
            safe_data,
            linked_runtime,
            temp / "safe-launch-agents",
            "--skip-launchctl",
            expected=2,
        )
        assert_true("symlinked runtime root" in runtime_symlink_refusal.stderr, "installer refuses a symlinked runtime root")
        assert_true(not any(real_runtime.iterdir()), "runtime symlink refusal does not write through the target")

        real_launch_agents = temp / "real-launch-agents"
        real_launch_agents.mkdir()
        linked_launch_agents = temp / "linked-launch-agents"
        linked_launch_agents.symlink_to(real_launch_agents, target_is_directory=True)
        launch_symlink_refusal = run_manager(
            "install",
            ROOT,
            safe_data,
            safe_data / "runtime",
            linked_launch_agents,
            "--skip-launchctl",
            expected=2,
        )
        assert_true("symlinked LaunchAgents directory" in launch_symlink_refusal.stderr, "installer refuses a symlinked LaunchAgents directory")
        assert_true(not any(real_launch_agents.iterdir()), "LaunchAgents symlink refusal does not write through the target")

        changed_source = temp / "changed-source"
        copy_runtime_source(changed_source)
        changed_service = changed_source / "tools" / "local-memory-service.py"
        changed_service.write_text(changed_service.read_text(encoding="utf-8") + "\n# isolated update proof\n", encoding="utf-8")
        unload_state = temp / "unload-state"
        launchctl_update_calls = temp / "launchctl-update-calls.log"
        fake_launchctl = temp / "failing-launchctl"
        fake_launchctl.write_text(
            "#!/usr/bin/env bash\n"
            f"printf '%s\\n' \"$*\" >> {str(launchctl_update_calls)!r}\n"
            "if [[ \"${1:-}\" == \"bootout\" ]]; then\n"
            f"  printf '0' > {str(unload_state)!r}\n"
            "  exit 0\n"
            "fi\n"
            "if [[ \"${1:-}\" == \"print\" ]]; then\n"
            f"  if [[ -f {str(unload_state)!r} ]]; then\n"
            f"    count=$(cat {str(unload_state)!r})\n"
            "    if [[ \"$count\" -lt 2 ]]; then\n"
            f"      printf '%s' \"$((count + 1))\" > {str(unload_state)!r}\n"
            "      exit 0\n"
            "    fi\n"
            f"    rm -f {str(unload_state)!r}\n"
            "  fi\n"
            "  exit 3\n"
            "fi\n"
            "if [[ \"${1:-}\" == \"bootstrap\" ]]; then\n"
            "  echo 'simulated bootstrap failure' >&2\n"
            "  exit 19\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_launchctl.chmod(0o755)

        failed_update = run_manager(
            "install",
            changed_source,
            data_dir,
            runtime_root,
            launch_agents_dir,
            "--launchctl-bin",
            fake_launchctl,
            expected=2,
        )
        assert_true("simulated bootstrap failure" in failed_update.stderr, "simulated LaunchAgent start failure is reported")
        update_calls = launchctl_update_calls.read_text(encoding="utf-8").splitlines()
        first_bootstrap = next(index for index, call in enumerate(update_calls) if call.startswith("bootstrap "))
        assert_true(
            update_calls[:first_bootstrap] == [
                f"bootout gui/{os.getuid()}/{LABEL}",
                f"print gui/{os.getuid()}/{LABEL}",
                f"print gui/{os.getuid()}/{LABEL}",
                f"print gui/{os.getuid()}/{LABEL}",
            ],
            "update waits for launchd to finish an asynchronous bootout before bootstrapping",
        )
        assert_true(os.readlink(current) == original_target, "failed update restores the prior active runtime")
        assert_true(plist_path.read_bytes() == original_plist, "failed update restores the prior LaunchAgent config")
        assert_true(db_path.read_bytes() == DB_SENTINEL, "failed update preserves the database")
        assert_true(len(list((runtime_root / "releases").iterdir())) == 2, "failed update preserves both releases for recovery")

        successful_update = run_manager(
            "install",
            changed_source,
            data_dir,
            runtime_root,
            launch_agents_dir,
            "--skip-launchctl",
        )
        update_payload = json.loads(successful_update.stdout)
        assert_true(update_payload["status"] == "updated", "a verified new runtime activates as an update")
        assert_true(os.readlink(current) != original_target, "successful update advances the atomic current pointer")
        assert_true(len(list((runtime_root / "releases").iterdir())) == 2, "successful update preserves the prior release")
        assert_true(db_path.read_bytes() == DB_SENTINEL, "successful update preserves the database")

        stopped = run_manager(
            "uninstall",
            ROOT,
            data_dir,
            runtime_root,
            launch_agents_dir,
            "--skip-launchctl",
        )
        stopped_payload = json.loads(stopped.stdout)
        disabled_plist = Path(stopped_payload["plistPreserved"])
        assert_true(stopped_payload["status"] == "stopped", "uninstall stops the managed LaunchAgent configuration")
        assert_true(disabled_plist.is_file() and not plist_path.exists(), "uninstall preserves rather than deletes LaunchAgent config")
        assert_true(current.is_symlink() and managed_service.is_file(), "uninstall preserves the managed runtime")
        assert_true(db_path.read_bytes() == DB_SENTINEL, "uninstall preserves the SQLite database")

        repeated_stop = run_manager(
            "uninstall",
            ROOT,
            data_dir,
            runtime_root,
            launch_agents_dir,
            "--skip-launchctl",
        )
        assert_true(json.loads(repeated_stop.stdout)["status"] == "not_installed", "repeat uninstall is a safe no-op")
        assert_true(db_path.read_bytes() == DB_SENTINEL, "repeat uninstall still preserves memory")

    report("Managed runtime proof passed entirely inside an isolated temporary home.")


if __name__ == "__main__":
    main()
