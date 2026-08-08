#!/usr/bin/env python3
"""Sanitize Fortnite logs for safe sharing while preserving useful structure."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path


NAME_AND_ID_RE = re.compile(r"\[([^]\r\n]+)] Id \[(?:MCP|EOS):([^]\r\n]+)]")
ACCOUNT_PATTERNS = [
    re.compile(r"\bAccountId=([^\s,}]+)"),
    re.compile(r"\bPartyMemberAccountIds=([^\s,}]+)"),
    re.compile(r"\b(?:MCP|EOS):([^\]\s,})]+)"),
    re.compile(r'(?i)-epicuserid=([^\s\]"]+)'),
    re.compile(r"(?i)teamAccountIds=([^&\s]+)"),
]
SENSITIVE_REPLACEMENTS = [
    (re.compile(r'(?i)(-AUTH_PASSWORD=)[^\s\]"]+'), r"\1<REDACTED>"),
    (re.compile(r'(?i)(-caldera=)[^\s\]"]+'), r"\1<REDACTED>"),
    (re.compile(r'(?i)(-obfuscationid=)[^\s\]"]+'), r"\1<REDACTED>"),
    (re.compile(r'(?i)(-startup=)[^\s\]"]+'), r"\1<REDACTED>"),
    (re.compile(r'(?i)(EncryptionToken=)[^?\s\]"]+'), r"\1<REDACTED>"),
    (re.compile(r'(?i)(Authorization:\s*Bearer\s+)[^\s\]"]+'), r"\1<REDACTED>"),
    (
        re.compile(r"eyJ[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+\.[a-zA-Z0-9_-]+"),
        "<REDACTED_JWT>",
    ),
    (
        re.compile(r"(?i)(?:[A-Z]:)?(?:[/\\]+)Users(?:[/\\]+)[^/\\\r\n]+"),
        "<USER_HOME>",
    ),
    (re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.])"), "<IP>"),
]


def _label(prefix: str, value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"<{prefix}_{digest}>"


def sanitize_text(text: str) -> str:
    names: set[str] = set()
    account_ids: set[str] = set()

    for match in NAME_AND_ID_RE.finditer(text):
        names.add(match.group(1))
        account_ids.add(match.group(2))
    for pattern in ACCOUNT_PATTERNS:
        account_ids.update(match.group(1) for match in pattern.finditer(text))

    for account_id in sorted(account_ids, key=len, reverse=True):
        if (
            account_id
            and len(account_id) >= 12
            and account_id.lower() != "none"
            and not account_id.startswith("<ACCOUNT_")
        ):
            text = text.replace(account_id, _label("ACCOUNT", account_id))
    for name in sorted(names, key=len, reverse=True):
        if name and not name.startswith("<PLAYER_"):
            text = text.replace(name, _label("PLAYER", name))

    text = re.sub(
        r'(?i)(-epicusername=)[^\s\]"]+',
        lambda match: match.group(1) + "<PLAYER_LOCAL>",
        text,
    )
    for pattern, replacement in SENSITIVE_REPLACEMENTS:
        text = pattern.sub(replacement, text)
    return text


def sanitize_file(path: Path) -> None:
    temporary = path.with_suffix(path.suffix + ".sanitizing")
    try:
        with path.open("r", encoding="utf-8", errors="replace") as source:
            with temporary.open("w", encoding="utf-8", newline="") as output:
                for line in source:
                    output.write(sanitize_text(line))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args()
    files: list[Path] = []
    for path in args.paths:
        files.extend(path.rglob("*.log") if path.is_dir() else [path])
    for path in files:
        sanitize_file(path)
    print(f"Sanitized {len(files)} log file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
