#!/usr/bin/env python3
"""Prove that Chrome Web Store packages are minimal, safe, and reproducible."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGER_PATH = REPO_ROOT / "tools" / "package-extension.py"
EXTENSION_DIR = REPO_ROOT / "extension"


def load_packager():
    spec = importlib.util.spec_from_file_location("daemon_package_extension", PACKAGER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError("Could not load package-extension.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


PACKAGER = load_packager()


def assert_true(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_packager(extension_dir: Path, output: Path, expect_success: bool) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [
            sys.executable,
            str(PACKAGER_PATH),
            "--extension-dir",
            str(extension_dir),
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert_true((result.returncode == 0) is expect_success, result.stdout + result.stderr)
    return result


def copy_extension(destination: Path) -> Path:
    copy = destination / "extension"
    shutil.copytree(EXTENSION_DIR, copy)
    return copy


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="daemon-store-package-proof-") as raw_temp:
        temp = Path(raw_temp)
        first = temp / "first.zip"
        second = temp / "second.zip"

        run_packager(EXTENSION_DIR, first, expect_success=True)
        run_packager(EXTENSION_DIR, second, expect_success=True)
        assert_true(first.read_bytes() == second.read_bytes(), "repeated packages must be byte-identical")

        with zipfile.ZipFile(first) as archive:
            names = archive.namelist()
            assert_true(names == sorted(PACKAGER.PACKAGE_FILES), "ZIP inventory must equal the sorted allowlist")
            assert_true(names[0] == "assets/brand/dmn-mark.png", "ZIP entries must be sorted")
            assert_true("manifest.json" in names, "manifest.json must be at the ZIP root")
            assert_true(not any(name.startswith("extension/") for name in names), "ZIP must not wrap files in extension/")
            assert_true(
                all(info.date_time == PACKAGER.FIXED_ZIP_TIME for info in archive.infolist()),
                "ZIP timestamps must be fixed",
            )
            assert_true(
                "assets/brand/dmn-logo-board-source.png" not in names,
                "source artwork must not ship",
            )
            assert_true("assets/icons/dmn-icon-1024.png" not in names, "unused large icons must not ship")

        checksum = first.with_suffix(".zip.sha256").read_text(encoding="utf-8").split()[0]
        assert_true(checksum == sha256(first), "checksum sidecar must match the archive")
        inventory = first.with_suffix(".zip.inventory.txt").read_text(encoding="utf-8")
        assert_true(inventory.count("\n") == len(PACKAGER.PACKAGE_FILES) + 1, "inventory must list every file once")

        symlink_copy = copy_extension(temp / "symlink-case")
        (symlink_copy / "unexpected-link").symlink_to(symlink_copy / "manifest.json")
        run_packager(symlink_copy, temp / "symlink.zip", expect_success=False)

        secret_copy = copy_extension(temp / "secret-case")
        (secret_copy / ".env").write_text("DAEMON_TEST_SECRET=synthetic\n", encoding="utf-8")
        run_packager(secret_copy, temp / "secret.zip", expect_success=False)

        private_path_copy = copy_extension(temp / "private-path-case")
        popup_path = private_path_copy / "popup" / "popup.html"
        popup_path.write_text(
            popup_path.read_text(encoding="utf-8") + "\n<!-- /Users/example/private -->\n",
            encoding="utf-8",
        )
        run_packager(private_path_copy, temp / "private-path.zip", expect_success=False)

        missing_manifest_asset_copy = copy_extension(temp / "missing-manifest-asset-case")
        manifest_path = missing_manifest_asset_copy / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["icons"]["24"] = "assets/icons/not-allowlisted.png"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        run_packager(missing_manifest_asset_copy, temp / "missing-manifest-asset.zip", expect_success=False)

    print("PASS package-extension-proof")
    print(f"files={len(PACKAGER.PACKAGE_FILES)} reproducible=true unsafe-inputs=refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
