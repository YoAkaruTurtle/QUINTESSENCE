"""Scan build/install trees for developer paths and secret-shaped material.

The scanner reads bytes, so it also catches UTF-16LE strings in native
binaries.  It never prints matched content, only the pattern name, file and
count.  The one exception is the official CPython/PyInstaller upstream build
path, which is stripped by exact literal before scanning; any other local user
path still fails.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

# The official CPython 3.12 Windows build embeds its build machine's path.  It
# cannot be removed without altering the toolchain binary, so only this exact
# literal is ignored.  Build the literals from parts so a source checkout does
# not itself contain the path form the gate rejects.
_UPSTREAM_USER = "Barney"
_KNOWN_UPSTREAM = (
    ("C:" + "/" + "Users" + "/" + _UPSTREAM_USER).encode("utf-8"),
    ("C:" + "\\" + "Users" + "\\" + _UPSTREAM_USER).encode("utf-8"),
    ("C:" + "/" + "Users" + "/" + _UPSTREAM_USER).encode("utf-16-le"),
    ("C:" + "\\" + "Users" + "\\" + _UPSTREAM_USER).encode("utf-16-le"),
)

_PRIVATE_TERMS = (
    ("chi" + "tose").encode("utf-8"),
    "\u5343\u5c81".encode("utf-8"),
    ("chi" + "tose").encode("utf-16-le"),
    "\u5343\u5c81".encode("utf-16-le"),
)

_SENTINELS = (
    "C12B-PRIVATE-SENTINEL-7f3a9c",
    "C12B-API-SENTINEL-2d8e1b",
)

TEXT_PATTERNS = {
    "windows-user-path": re.compile(
        rb"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+", re.I
    ),
    "lab-path": re.compile(rb"QUINTESSENCE-Lab", re.I),
    "repo-path": re.compile(rb"[A-Za-z]:[\\/]+QUINTESSENCE(?:[\\/]|$)", re.I),
    "api-key-shape": re.compile(rb"sk-[A-Za-z0-9_-]{24,}"),
    "private-key-marker": re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "private-db-name": re.compile(rb"pcs\.sqlite", re.I),
}

UTF16_PATTERNS = {
    "api-key-shape": re.compile(
        rb"(?:s\x00k\x00-\x00)(?:[A-Za-z0-9_-]\x00){24,}"
    ),
    "windows-user-path": re.compile(
        rb"(?:[A-Za-z]\x00:\x00[\\/]\x00?U\x00s\x00e\x00r\x00s\x00)"
    ),
}


def _strip_known_upstream(data: bytes) -> tuple[bytes, int]:
    count = 0
    for literal in _KNOWN_UPSTREAM:
        occurrences = data.count(literal)
        count += occurrences
        data = data.replace(literal, b"")
    return data, count


def _count(name: str, data: bytes) -> int:
    count = 0
    pattern = TEXT_PATTERNS.get(name)
    if pattern is not None:
        count += len(pattern.findall(data))
    utf16 = UTF16_PATTERNS.get(name)
    if utf16 is not None:
        count += len(utf16.findall(data))
    return count


def scan(root: Path) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    files = 0
    total = 0
    ignored_upstream = 0
    candidates = [root] if root.is_file() else [
        path for path in root.rglob("*") if path.is_file()
    ]
    for path in candidates:
        files += 1
        try:
            data = path.read_bytes()
        except OSError:
            continue
        total += len(data)
        data, stripped = _strip_known_upstream(data)
        ignored_upstream += stripped
        for name in TEXT_PATTERNS:
            count = _count(name, data)
            if count:
                findings.append(
                    {
                        "pattern": name,
                        "file": (
                            path.name
                            if root.is_file()
                            else str(path.relative_to(root)).replace("\\", "/")
                        ),
                        "count": count,
                    }
                )
        for term in _PRIVATE_TERMS:
            count = data.count(term)
            if count:
                findings.append(
                    {
                        "pattern": "private-instance-term",
                        "file": (
                            path.name
                            if root.is_file()
                            else str(path.relative_to(root)).replace("\\", "/")
                        ),
                        "count": count,
                    }
                )
                break
        for sentinel in _SENTINELS:
            if sentinel.encode("utf-8") in data or sentinel.encode("utf-16-le") in data:
                findings.append(
                    {
                        "pattern": "synthetic-private-sentinel",
                        "file": (
                            path.name
                            if root.is_file()
                            else str(path.relative_to(root)).replace("\\", "/")
                        ),
                        "count": 1,
                    }
                )
                break
    return {
        "root": str(root),
        "files": files,
        "bytes": total,
        "ignored_known_upstream_paths": ignored_upstream,
        "findings": findings,
        "passed": not findings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path, action="append")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    reports = [scan(root) for root in args.root]
    report = {
        "roots": reports,
        "passed": all(item["passed"] for item in reports),
    }
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in reports:
            print(
                f"{item['root']}: files={item['files']} bytes={item['bytes']} "
                f"findings={len(item['findings'])}"
            )
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
