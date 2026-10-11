"""Check that a Lambda zip targets Linux x86_64 / CPython 3.12 and keeps the expected layout.

Usage: python verify_lambda_package.py <package.zip> <report-dir>

Fails (exit 1) on Windows/macOS binaries, non-x86_64 or non-ELF native extensions,
CPython ABI tags other than 3.12, or missing handlers/migrations/dependencies.
"""

from __future__ import annotations

import re
import sys
import zipfile
from pathlib import Path

ELF_MAGIC = b"\x7fELF"
EM_X86_64 = 0x3E
FORBIDDEN_SUFFIXES = (".pyd", ".dll", ".dylib", ".exe")
CPYTHON_TAG = re.compile(r"\.cpython-(\d+)-([a-z0-9_]+)-linux-gnu\.so$")
NATIVE = re.compile(r"\.so(\.\d+)*$")

REQUIRED_FILES = [
    "backend/handlers/api.py",
    "backend/handlers/process_csv.py",
    "backend/handlers/migrate.py",
    "backend/app/__init__.py",
    "backend/app/config.py",
    "backend/migrations/001_initial_schema.sql",
    "backend/migrations/002_notification_superseded.sql",
]
REQUIRED_PACKAGES = ["fastapi/", "pydantic/", "pydantic_core/", "psycopg/", "psycopg_binary/", "mangum/"]
UNWANTED_PREFIXES = ["backend/tests/", ".env", "boto3/", "botocore/"]


def check_elf(header: bytes) -> str | None:
    if header[:4] != ELF_MAGIC:
        return "not an ELF file"
    if header[4] != 2:
        return "not 64-bit ELF"
    if header[5] != 1:
        return "not little-endian"
    machine = int.from_bytes(header[18:20], "little")
    if machine != EM_X86_64:
        return f"e_machine=0x{machine:x}, expected x86_64 (0x3e)"
    return None


def main(zip_path: str, report_dir: str) -> int:
    out = Path(report_dir)
    out.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    native_lines: list[str] = []

    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        (out / "zip-contents.txt").write_text("\n".join(sorted(names)) + "\n", encoding="utf-8")
        name_set = set(names)

        for name in names:
            lower = name.lower()
            if lower.endswith(FORBIDDEN_SUFFIXES):
                errors.append(f"{name}: Windows/macOS binary")
                continue
            if not NATIVE.search(name):
                continue
            problem = check_elf(zf.read(name)[:20])
            tag = CPYTHON_TAG.search(name)
            if problem is None and tag and (tag.group(1) != "312" or tag.group(2) != "x86_64"):
                problem = f"CPython tag cpython-{tag.group(1)}-{tag.group(2)}, expected cpython-312-x86_64"
            native_lines.append(f"{'FAIL' if problem else 'ok  '} {name}{' - ' + problem if problem else ''}")
            if problem:
                errors.append(f"{name}: {problem}")

        for required in REQUIRED_FILES:
            if required not in name_set:
                errors.append(f"missing {required}")
        for package in REQUIRED_PACKAGES:
            if not any(n.startswith(package) for n in names):
                errors.append(f"missing dependency package {package}")
        for unwanted in UNWANTED_PREFIXES:
            hits = [n for n in names if n.startswith(unwanted)]
            if hits:
                errors.append(f"unexpected content {unwanted} ({len(hits)} files)")

    if not native_lines:
        errors.append("no native extensions found; dependencies were probably not packaged")
    summary = [
        f"package: {zip_path}",
        f"files: {len(names)}",
        f"native extensions checked: {len(native_lines)}",
        f"result: {'FAIL' if errors else 'PASS'}",
        *(f"error: {e}" for e in errors),
        "",
        *native_lines,
    ]
    (out / "native-extensions.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary[: 4 + len(errors)]))
    return 1 if errors else 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1], sys.argv[2]))
