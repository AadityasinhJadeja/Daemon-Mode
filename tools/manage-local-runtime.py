#!/usr/bin/env python3
"""Install, update, or stop Daemon's managed local runtime without touching memory."""

import argparse
import datetime as dt
import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


SERVICE_VERSION = "0.5.10"
LABEL = "com.daemonmode.local-memory-service"
LAUNCHCTL_UNLOAD_TIMEOUT_SECONDS = 5.0
LAUNCHCTL_POLL_INTERVAL_SECONDS = 0.05
RUNTIME_FILES = (
    "tools/local-memory-service.py",
    "tools/daemon-mcp-server.py",
)
RUNTIME_DIRECTORIES = (
    "extension/assets/fonts",
    "extension/assets/brand",
    "extension/assets/icons",
)


class RuntimeInstallError(RuntimeError):
    pass


def default_data_dir(home=None):
    configured = os.environ.get("DAEMON_MODE_DATA_DIR")
    if configured:
        return Path(configured).expanduser()
    home = Path(home or Path.home()).expanduser()
    return home / "Library" / "Application Support" / "Daemon Mode"


def timestamp():
    return dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def ensure_private_directory(path):
    if path.is_symlink():
        raise RuntimeInstallError(f"Refusing symlinked managed directory: {path}")
    if path.exists() and not path.is_dir():
        raise RuntimeInstallError(f"Managed path is not a directory: {path}")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def source_entries(source_root):
    entries = []
    for relative in RUNTIME_FILES:
        path = source_root / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeInstallError(f"Missing or unsafe runtime source file: {path}")
        entries.append((relative, path))

    for relative_directory in RUNTIME_DIRECTORIES:
        directory = source_root / relative_directory
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeInstallError(f"Missing or unsafe runtime asset directory: {directory}")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise RuntimeInstallError(f"Refusing symlinked runtime asset: {path}")
            if path.is_file():
                entries.append((str(path.relative_to(source_root)), path))
    return sorted(entries)


def source_manifest(source_root):
    files = {}
    combined = hashlib.sha256()
    for relative, path in source_entries(source_root):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[relative] = digest
        combined.update(relative.encode("utf-8"))
        combined.update(b"\0")
        combined.update(digest.encode("ascii"))
        combined.update(b"\0")
    return {
        "serviceVersion": SERVICE_VERSION,
        "sourceDigest": combined.hexdigest(),
        "files": files,
    }


def verify_release(release_path, manifest):
    manifest_path = release_path / "runtime-manifest.json"
    if not manifest_path.is_file():
        raise RuntimeInstallError(f"Existing runtime release has no manifest: {release_path}")
    try:
        installed = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeInstallError(f"Existing runtime manifest is invalid: {manifest_path}: {error}") from error
    if installed != manifest:
        raise RuntimeInstallError(f"Existing runtime release does not match its expected content: {release_path}")
    for relative, expected_digest in manifest["files"].items():
        path = release_path / relative
        if path.is_symlink() or not path.is_file():
            raise RuntimeInstallError(f"Installed runtime file is missing or unsafe: {path}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected_digest:
            raise RuntimeInstallError(f"Installed runtime file failed integrity verification: {path}")


def install_release(source_root, runtime_root):
    releases = runtime_root / "releases"
    ensure_private_directory(runtime_root)
    ensure_private_directory(releases)
    manifest = source_manifest(source_root)
    release_id = f"{SERVICE_VERSION}-{manifest['sourceDigest'][:12]}"
    release_path = releases / release_id

    if release_path.exists():
        if release_path.is_symlink() or not release_path.is_dir():
            raise RuntimeInstallError(f"Runtime release path is unsafe: {release_path}")
        verify_release(release_path, manifest)
        return release_id, release_path, manifest, False

    staging = releases / f".staging-{release_id}-{uuid.uuid4().hex}"
    staging.mkdir(mode=0o700)
    try:
        for relative, source in source_entries(source_root):
            destination = staging / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            destination.parent.chmod(0o700)
            shutil.copyfile(source, destination)
            destination.chmod(0o600)
        manifest_path = staging / "runtime-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)
        verify_release(staging, manifest)
        os.replace(staging, release_path)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise

    verify_release(release_path, manifest)
    return release_id, release_path, manifest, True


def current_target(current_path):
    if current_path.is_symlink():
        return os.readlink(current_path)
    if current_path.exists():
        raise RuntimeInstallError(f"Refusing to replace non-symlink runtime pointer: {current_path}")
    return None


def set_current_target(current_path, target):
    if current_path.exists() and not current_path.is_symlink():
        raise RuntimeInstallError(f"Refusing to replace non-symlink runtime pointer: {current_path}")
    temporary = current_path.with_name(f".{current_path.name}.tmp-{uuid.uuid4().hex}")
    temporary.symlink_to(target, target_is_directory=True)
    try:
        os.replace(temporary, current_path)
    finally:
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()


def restore_current_target(current_path, previous_target):
    if previous_target is None:
        if current_path.is_symlink():
            current_path.unlink()
        return
    set_current_target(current_path, previous_target)


def plist_payload(runtime_root, data_dir, python_bin):
    current = runtime_root / "current"
    service_path = current / "tools" / "local-memory-service.py"
    db_path = data_dir / "memory.sqlite3"
    return {
        "Label": LABEL,
        "ProgramArguments": [
            str(python_bin),
            str(service_path),
            "--db-path",
            str(db_path),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            "4317",
        ],
        "WorkingDirectory": str(current),
        "EnvironmentVariables": {
            "DAEMON_MODE_DATA_DIR": str(data_dir),
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(data_dir / "local-memory-service.out.log"),
        "StandardErrorPath": str(data_dir / "local-memory-service.err.log"),
    }


def validate_existing_plist(path):
    if path.is_symlink():
        raise RuntimeInstallError(f"Refusing symlinked LaunchAgent config: {path}")
    if not path.exists():
        return None
    if not path.is_file():
        raise RuntimeInstallError(f"LaunchAgent config is not a regular file: {path}")
    raw = path.read_bytes()
    try:
        payload = plistlib.loads(raw)
    except Exception as error:
        raise RuntimeInstallError(f"Refusing malformed LaunchAgent config {path}: {error}") from error
    if payload.get("Label") != LABEL:
        raise RuntimeInstallError(f"Refusing to replace LaunchAgent config owned by another service: {path}")
    return raw


def atomic_write(path, content, mode=0o600, expected_bytes=None, check_expected=False):
    ensure_private_directory(path.parent)
    if check_expected:
        current_bytes = path.read_bytes() if path.exists() else None
        if current_bytes != expected_bytes:
            raise RuntimeInstallError(f"Config changed while Daemon was preparing the update: {path}")
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(mode)
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def backup_file(path):
    backup = path.with_name(f"{path.name}.daemon-backup-{timestamp()}")
    shutil.copy2(path, backup)
    backup.chmod(stat.S_IMODE(path.stat().st_mode))
    return backup


def run_launchctl(launchctl_bin, *arguments, check=True):
    return subprocess.run(
        [str(launchctl_bin), *map(str, arguments)],
        check=check,
        capture_output=True,
        text=True,
    )


def launch_domain():
    return f"gui/{os.getuid()}"


def bootstrap_launch_agent(launchctl_bin, plist_path):
    service_target = f"{launch_domain()}/{LABEL}"
    result = run_launchctl(
        launchctl_bin,
        "bootstrap",
        launch_domain(),
        plist_path,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeInstallError(
            f"LaunchAgent failed to start: {result.stderr.strip() or result.stdout.strip() or 'unknown launchctl error'}"
        )

    check = run_launchctl(launchctl_bin, "print", service_target, check=False)
    if check.returncode != 0:
        raise RuntimeInstallError("LaunchAgent did not remain loaded after installation.")


def ensure_launch_agent_loaded(launchctl_bin, plist_path):
    service_target = f"{launch_domain()}/{LABEL}"
    status = run_launchctl(launchctl_bin, "print", service_target, check=False)
    if status.returncode != 0:
        bootstrap_launch_agent(launchctl_bin, plist_path)


def bootout_launch_agent(launchctl_bin):
    service_target = f"{launch_domain()}/{LABEL}"
    result = run_launchctl(launchctl_bin, "bootout", service_target, check=False)
    deadline = time.monotonic() + LAUNCHCTL_UNLOAD_TIMEOUT_SECONDS

    while True:
        status = run_launchctl(launchctl_bin, "print", service_target, check=False)
        if status.returncode != 0:
            return
        if time.monotonic() >= deadline:
            detail = result.stderr.strip() or result.stdout.strip()
            suffix = f": {detail}" if detail else ""
            raise RuntimeInstallError(f"LaunchAgent did not finish unloading before update{suffix}")
        time.sleep(LAUNCHCTL_POLL_INTERVAL_SECONDS)


def install(args):
    source_root = Path(args.source_root).expanduser().resolve()
    data_dir = Path(args.data_dir).expanduser().absolute()
    runtime_root = Path(args.runtime_root).expanduser().absolute()
    launch_agents_dir = Path(args.launch_agents_dir).expanduser().absolute()
    plist_path = launch_agents_dir / f"{LABEL}.plist"
    python_bin = Path(args.python_bin).expanduser().resolve()

    if not python_bin.is_file():
        raise RuntimeInstallError(f"Python executable was not found: {python_bin}")
    for label, path in (
        ("data directory", data_dir),
        ("runtime root", runtime_root),
        ("LaunchAgents directory", launch_agents_dir),
    ):
        if path.is_symlink():
            raise RuntimeInstallError(f"Refusing symlinked {label}: {path}")

    ensure_private_directory(data_dir)
    release_id, release_path, manifest, created = install_release(source_root, runtime_root)
    target = str(Path("releases") / release_id)
    current = runtime_root / "current"
    previous_target = current_target(current)
    previous_plist = validate_existing_plist(plist_path)
    desired_plist = plistlib.dumps(plist_payload(runtime_root, data_dir, python_bin), sort_keys=False)
    already_current = previous_target == target
    already_configured = previous_plist == desired_plist
    backup = None
    plist_written = False

    if already_current and already_configured:
        if not args.skip_launchctl:
            ensure_launch_agent_loaded(args.launchctl_bin, plist_path)
        return {
            "status": "already_installed",
            "serviceVersion": SERVICE_VERSION,
            "release": str(release_path),
            "current": str(current),
            "plist": str(plist_path),
            "backup": None,
            "database": str(data_dir / "memory.sqlite3"),
        }

    if previous_plist is not None and previous_plist != desired_plist:
        backup = backup_file(plist_path)

    try:
        set_current_target(current, target)
        if previous_plist != desired_plist:
            atomic_write(
                plist_path,
                desired_plist,
                expected_bytes=previous_plist,
                check_expected=True,
            )
            plist_written = True

        if not args.skip_launchctl:
            bootout_launch_agent(args.launchctl_bin)
            bootstrap_launch_agent(args.launchctl_bin, plist_path)
    except Exception:
        failed_path = None
        if plist_written and plist_path.exists():
            failed_path = plist_path.with_name(f"{plist_path.name}.failed-{timestamp()}")
            os.replace(plist_path, failed_path)
        if plist_written and previous_plist is not None:
            atomic_write(plist_path, previous_plist, expected_bytes=None, check_expected=True)
        restore_current_target(current, previous_target)
        if not args.skip_launchctl and previous_plist is not None:
            bootout_launch_agent(args.launchctl_bin)
            run_launchctl(args.launchctl_bin, "bootstrap", launch_domain(), plist_path, check=False)
        raise

    return {
        "status": "installed" if previous_target is None else "updated",
        "serviceVersion": SERVICE_VERSION,
        "release": str(release_path),
        "current": str(current),
        "plist": str(plist_path),
        "backup": str(backup) if backup else None,
        "database": str(data_dir / "memory.sqlite3"),
        "releaseCreated": created,
        "sourceDigest": manifest["sourceDigest"],
    }


def uninstall(args):
    data_dir = Path(args.data_dir).expanduser().absolute()
    runtime_root = Path(args.runtime_root).expanduser().absolute()
    launch_agents_dir = Path(args.launch_agents_dir).expanduser().absolute()
    for label, path in (
        ("data directory", data_dir),
        ("runtime root", runtime_root),
        ("LaunchAgents directory", launch_agents_dir),
    ):
        if path.is_symlink():
            raise RuntimeInstallError(f"Refusing symlinked {label}: {path}")
    plist_path = launch_agents_dir / f"{LABEL}.plist"
    validate_existing_plist(plist_path)

    if not args.skip_launchctl:
        run_launchctl(args.launchctl_bin, "bootout", f"{launch_domain()}/{LABEL}", check=False)

    preserved = None
    if plist_path.exists():
        preserved = plist_path.with_name(f"{plist_path.name}.disabled-{timestamp()}")
        os.replace(plist_path, preserved)

    return {
        "status": "stopped" if preserved else "not_installed",
        "plistPreserved": str(preserved) if preserved else None,
        "runtimePreserved": str(runtime_root),
        "databasePreserved": str(data_dir / "memory.sqlite3"),
    }


def build_parser():
    home = Path.home()
    data_dir = default_data_dir(home)
    parser = argparse.ArgumentParser(description="Safely install, update, or stop Daemon's managed local runtime.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser):
        subparser.add_argument("--data-dir", default=str(data_dir))
        subparser.add_argument("--runtime-root", default=str(data_dir / "runtime"))
        subparser.add_argument("--launch-agents-dir", default=str(home / "Library" / "LaunchAgents"))
        subparser.add_argument("--launchctl-bin", default="launchctl")
        subparser.add_argument("--skip-launchctl", action="store_true", help=argparse.SUPPRESS)

    install_parser = subparsers.add_parser("install", help="Install or safely update the managed runtime")
    add_common(install_parser)
    install_parser.add_argument("--source-root", default=str(Path(__file__).resolve().parents[1]))
    install_parser.add_argument("--python-bin", default=os.environ.get("PYTHON_BIN", sys.executable))
    install_parser.set_defaults(func=install)

    uninstall_parser = subparsers.add_parser("uninstall", help="Stop Daemon while preserving runtime and memory")
    add_common(uninstall_parser)
    uninstall_parser.set_defaults(func=uninstall)
    return parser


def main():
    args = build_parser().parse_args()
    try:
        result = args.func(args)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, RuntimeInstallError, subprocess.SubprocessError) as error:
        print(f"runtime operation refused: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
