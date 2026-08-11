#!/usr/bin/env python3
"""Build a deterministic Chrome Web Store ZIP from an explicit runtime allowlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import sys
import zipfile
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXTENSION_DIR = REPO_ROOT / "extension"
FIXED_ZIP_TIME = (2020, 1, 1, 0, 0, 0)

# Keep this list intentionally boring. Store packages are release artifacts, not
# source archives. Add a file only when the shipped extension actually needs it.
PACKAGE_FILES = (
    "manifest.json",
    "popup/popup.css",
    "popup/popup.html",
    "popup/popup.js",
    "src/background.js",
    "src/blocklist.js",
    "src/content.js",
    "src/protection.js",
    "src/readiness.js",
    "assets/brand/dmn-mark.png",
    "assets/fonts/OFL-1.1.txt",
    "assets/fonts/atkinson-hyperlegible-latin-400-normal.woff2",
    "assets/fonts/atkinson-hyperlegible-latin-700-normal.woff2",
    "assets/fonts/commit-mono-latin-400-normal.woff2",
    "assets/fonts/commit-mono-latin-600-normal.woff2",
    "assets/fonts/literata-latin-400-italic.woff2",
    "assets/icons/dmn-icon-16.png",
    "assets/icons/dmn-icon-32.png",
    "assets/icons/dmn-icon-48.png",
    "assets/icons/dmn-icon-128.png",
)

FORBIDDEN_FILE_SUFFIXES = {
    ".db",
    ".key",
    ".pem",
    ".sqlite",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
}
FORBIDDEN_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "secrets.json",
}
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".txt"}
PRIVATE_CONTENT_PATTERNS = (
    (re.compile(r"/Users/[^/\s]+/"), "macOS personal absolute path"),
    (re.compile(r"[A-Za-z]:\\\\Users\\\\[^\\\s]+\\\\"), "Windows personal absolute path"),
    (re.compile(r"Daemon-Mode-Private", re.IGNORECASE), "private repository name"),
    (re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"\b(?:ghp|github_pat)_[A-Za-z0-9_]{20,}\b"), "GitHub token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "provider API key"),
)


class PackageError(RuntimeError):
    """Raised when packaging would be unsafe or non-reproducible."""


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_relative_path(raw_path: str) -> Path:
    pure_path = PurePosixPath(raw_path)
    if pure_path.is_absolute() or ".." in pure_path.parts:
        raise PackageError(f"Unsafe package path: {raw_path}")
    return Path(*pure_path.parts)


def normalize_local_reference(owner: str, raw_reference: str) -> str | None:
    """Resolve a local runtime reference relative to its referring file."""
    reference = raw_reference.strip().split("#", 1)[0].split("?", 1)[0]
    if not reference or reference.startswith(("data:", "http:", "https:", "//", "#")):
        return None

    if reference.startswith("/"):
        normalized = posixpath.normpath(reference.lstrip("/"))
    else:
        normalized = posixpath.normpath(posixpath.join(posixpath.dirname(owner), reference))

    if normalized == ".." or normalized.startswith("../"):
        raise PackageError(f"Runtime reference escapes extension root: {owner} -> {raw_reference}")
    return normalized


def manifest_runtime_references(manifest: dict) -> set[str]:
    references: set[str] = set()

    def add(value: object) -> None:
        if isinstance(value, str):
            normalized = normalize_local_reference("manifest.json", value)
            if normalized:
                references.add(normalized)

    for value in (manifest.get("icons") or {}).values():
        add(value)
    action = manifest.get("action") or {}
    add(action.get("default_popup"))
    for value in (action.get("default_icon") or {}).values():
        add(value)
    add((manifest.get("background") or {}).get("service_worker"))
    add(manifest.get("options_page"))
    add((manifest.get("options_ui") or {}).get("page"))
    add(manifest.get("devtools_page"))
    add((manifest.get("side_panel") or {}).get("default_path"))
    for value in (manifest.get("chrome_url_overrides") or {}).values():
        add(value)
    for content_script in manifest.get("content_scripts") or []:
        for value in (*content_script.get("js", []), *content_script.get("css", [])):
            add(value)
    for resource_group in manifest.get("web_accessible_resources") or []:
        for value in resource_group.get("resources", []):
            if not any(character in value for character in "*?["):
                add(value)
    return references


def source_runtime_references(relative: str, text: str) -> set[str]:
    """Find local JS, HTML, and CSS assets loaded by packaged runtime files."""
    raw_references: set[str] = set()
    suffix = PurePosixPath(relative).suffix.lower()
    if suffix == ".js":
        raw_references.update(
            re.findall(r"(?:from\s+|import\s*\(\s*)[\"'](\.[^\"']+)[\"']", text)
        )
    elif suffix == ".html":
        raw_references.update(re.findall(r"(?:href|src)=[\"']([^\"']+)[\"']", text, re.IGNORECASE))
    elif suffix == ".css":
        raw_references.update(re.findall(r"url\(\s*[\"']?([^\"')]+)", text, re.IGNORECASE))

    return {
        normalized
        for raw in raw_references
        if (normalized := normalize_local_reference(relative, raw)) is not None
    }


def validate_runtime_references(package: dict[str, bytes], manifest: dict) -> None:
    references = manifest_runtime_references(manifest)
    for relative, data in package.items():
        if PurePosixPath(relative).suffix.lower() in {".css", ".html", ".js"}:
            references.update(source_runtime_references(relative, data.decode("utf-8")))

    missing = sorted(reference for reference in references if reference not in package)
    if missing:
        raise PackageError("Runtime references absent from package allowlist: " + ", ".join(missing))


def validate_extension_tree(extension_dir: Path) -> None:
    if not extension_dir.is_dir():
        raise PackageError(f"Extension directory does not exist: {extension_dir}")

    for path in sorted(extension_dir.rglob("*")):
        relative = path.relative_to(extension_dir).as_posix()
        if path.is_symlink():
            raise PackageError(f"Refusing symlink in extension tree: {relative}")
        if not path.is_file():
            continue

        lower_name = path.name.lower()
        lower_suffix = "".join(path.suffixes).lower()
        if lower_name in FORBIDDEN_FILE_NAMES or any(
            lower_suffix.endswith(suffix) for suffix in FORBIDDEN_FILE_SUFFIXES
        ):
            raise PackageError(f"Refusing secret or private-data file: {relative}")


def read_package_files(extension_dir: Path) -> dict[str, bytes]:
    validate_extension_tree(extension_dir)
    package: dict[str, bytes] = {}

    for raw_relative in PACKAGE_FILES:
        relative = safe_relative_path(raw_relative)
        path = extension_dir / relative
        if path.is_symlink() or not path.is_file():
            raise PackageError(f"Missing or unsafe allowlisted file: {raw_relative}")

        data = path.read_bytes()
        if path.suffix.lower() in TEXT_SUFFIXES:
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as error:
                raise PackageError(f"Text file is not UTF-8: {raw_relative}") from error
            for pattern, label in PRIVATE_CONTENT_PATTERNS:
                if pattern.search(text):
                    raise PackageError(f"Refusing {label} in {raw_relative}")

        package[raw_relative] = data

    try:
        manifest = json.loads(package["manifest.json"].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageError("manifest.json is not valid UTF-8 JSON") from error

    if manifest.get("manifest_version") != 3:
        raise PackageError("Chrome Web Store package must use Manifest V3")
    if not manifest.get("name") or not manifest.get("version"):
        raise PackageError("manifest.json must include name and version")

    validate_runtime_references(package, manifest)

    return package


def inventory_text(package: dict[str, bytes]) -> str:
    lines = ["sha256  bytes  path"]
    for relative in sorted(package):
        data = package[relative]
        lines.append(f"{sha256_bytes(data)}  {len(data)}  {relative}")
    return "\n".join(lines) + "\n"


def write_deterministic_zip(output_path: Path, package: dict[str, bytes]) -> None:
    if output_path.exists() and output_path.is_symlink():
        raise PackageError(f"Refusing symlinked output: {output_path}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(
        output_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        strict_timestamps=True,
    ) as archive:
        for relative in sorted(package):
            info = zipfile.ZipInfo(relative, FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits = 0
            archive.writestr(info, package[relative], compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def default_output_path(extension_dir: Path) -> Path:
    manifest = json.loads((extension_dir / "manifest.json").read_text(encoding="utf-8"))
    version = str(manifest["version"])
    return REPO_ROOT / "dist" / f"daemon-mode-chrome-{version}.zip"


def package_extension(extension_dir: Path, output_path: Path) -> dict[str, str | int]:
    package = read_package_files(extension_dir)
    write_deterministic_zip(output_path, package)

    inventory_path = output_path.with_suffix(output_path.suffix + ".inventory.txt")
    inventory_path.write_text(inventory_text(package), encoding="utf-8", newline="\n")

    archive_hash = sha256_bytes(output_path.read_bytes())
    checksum_path = output_path.with_suffix(output_path.suffix + ".sha256")
    checksum_path.write_text(f"{archive_hash}  {output_path.name}\n", encoding="utf-8", newline="\n")

    return {
        "archive": str(output_path),
        "archiveSha256": archive_hash,
        "fileCount": len(package),
        "inventory": str(inventory_path),
        "checksum": str(checksum_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--extension-dir",
        type=Path,
        default=DEFAULT_EXTENSION_DIR,
        help="Extension source directory (defaults to this repository's extension directory).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output ZIP path (defaults to dist/daemon-mode-chrome-<version>.zip).",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    extension_dir = args.extension_dir.expanduser().resolve()
    output_path = (args.output or default_output_path(extension_dir)).expanduser().resolve()

    try:
        result = package_extension(extension_dir, output_path)
    except (OSError, PackageError, KeyError, json.JSONDecodeError) as error:
        print(f"package-extension: {error}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
