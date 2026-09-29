"""Owner-issued computer keys and independent download/upload folder grants."""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import re
import secrets
import sys
from functools import lru_cache

from .policy import allowed, relative_path, validate_patterns


def validate_computer_permissions(entries: list[dict]) -> None:
    if not isinstance(entries, list):
        raise ValueError("computer_permissions must be a list")
    names: set[str] = set()
    hashes: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "name", "token_hash", "download_patterns", "upload_patterns"
        }:
            raise ValueError("Each computer needs name, token_hash, download_patterns and upload_patterns")
        name = entry["name"]
        digest = entry["token_hash"]
        if (
            not isinstance(name, str) or not name.strip()
            or any(ord(char) < 32 for char in name) or name.casefold() in names
        ):
            raise ValueError("Computer names must be nonempty and unique")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest) or digest in hashes:
            raise ValueError("Computer token_hash must be a unique SHA-256 hex digest")
        validate_patterns(entry["download_patterns"], "download_patterns")
        validate_patterns(entry["upload_patterns"], "upload_patterns")
        names.add(name.casefold())
        hashes.add(digest)


def new_computer_permission(name: str, download_patterns: list[str], upload_patterns: list[str]) -> tuple[dict, str]:
    """Return the serializable grant and its secret, which must only be shown once."""
    token = "ff_" + secrets.token_urlsafe(32)
    entry = {
        "name": name.strip(),
        "token_hash": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "download_patterns": list(download_patterns),
        "upload_patterns": list(upload_patterns),
    }
    validate_computer_permissions([entry])
    return entry, token


def authenticate_computer(token: str, entries: list[dict]) -> dict | None:
    # Never accept short/shared pairing codes as computer keys, even if a config
    # was accidentally populated with the hash of the old six-digit code.
    if not re.fullmatch(r"ff_[A-Za-z0-9_-]{43}", token):
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    for entry in entries:
        if hmac.compare_digest(digest, entry["token_hash"]):
            return entry
    return None


def path_permitted(path: str, patterns: list[str]) -> bool:
    """Match a file/folder grant; root has no implicit write permission."""
    path = relative_path(path)
    if not path:
        return "**" in patterns or "/**" in patterns
    return allowed(path, patterns, [])


def can_traverse(path: str, patterns: list[str]) -> bool:
    """Can this directory lead to a permitted descendant?

    Expose navigation directories for nested folder and file-pattern grants,
    while keeping files hidden unless a download pattern matches them.
    """
    path = relative_path(path)
    if not path:
        return True
    if sys.platform == "win32":
        path = path.casefold()
        patterns = [pattern.casefold() for pattern in patterns]
    parts = tuple(path.split("/"))
    for pattern in patterns:
        pattern_parts = tuple(pattern.rstrip("/").split("/"))

        @lru_cache(maxsize=None)
        def possible(i: int, j: int) -> bool:
            if i == len(parts):
                return True
            if j == len(pattern_parts):
                return False
            if pattern_parts[j] == "**":
                return possible(i, j + 1) or possible(i + 1, j)
            return fnmatch.fnmatchcase(parts[i], pattern_parts[j]) and possible(i + 1, j + 1)

        if possible(0, 0):
            return True
    return False
