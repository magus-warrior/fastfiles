"""Locker paths use POSIX globs: * stays in one segment; ** spans segments."""

from __future__ import annotations

import fnmatch
import re
import sys
from functools import lru_cache


def relative_path(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\0\r\n\\"):
        raise ValueError("Use a relative locker path with forward slashes")
    if value.startswith("/") or ".." in value.split("/"):
        raise PermissionError("Path leaves the approved locker")
    parts = [part for part in value.split("/") if part not in ("", ".")]
    if sys.platform == "win32":
        for part in parts:
            validate_windows_name(part)
    if any(
        part.lower().startswith(".fastfiles-") or part.lower().endswith((".fastfiles-upload", ".fastfiles-download"))
        for part in parts
    ):
        raise PermissionError("Transfer temporary files are private")
    return "/".join(parts)


def validate_windows_name(part: str) -> None:
    # Reject device names, alternate data streams, and names Win32 normalizes.
    if (
        any(ord(c) < 32 or c in '<>:"|?*' for c in part)
        or part.endswith((" ", "."))
        or re.fullmatch(r"(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])",
                        part.split(".")[0].rstrip(" "), re.IGNORECASE)
    ):
        raise ValueError(f"Filename is not supported on Windows: {part}")


def validate_patterns(patterns: list[str], name: str) -> None:
    if not isinstance(patterns, list) or any(not isinstance(p, str) or not p for p in patterns):
        raise ValueError(f"{name} must be a JSON list of nonempty path patterns")
    for pattern in patterns:
        # Globs contain wildcard characters that are not literal filenames.
        relative_path(pattern.replace("*", "x").replace("?", "x"))


def matches(path: str, pattern: str) -> bool:
    path_parts = tuple(path.split("/")) if path else ()
    pattern_parts = tuple(pattern.rstrip("/").split("/"))

    @lru_cache(maxsize=None)
    def match(i: int, j: int) -> bool:
        if j == len(pattern_parts):
            return i == len(path_parts)
        if pattern_parts[j] == "**":
            return match(i, j + 1) or (i < len(path_parts) and match(i + 1, j))
        return (
            i < len(path_parts)
            and fnmatch.fnmatchcase(path_parts[i], pattern_parts[j])
            and match(i + 1, j + 1)
        )

    return match(0, 0)


def allowed(path: str, allow: list[str], deny: list[str]) -> bool:
    if not path:
        return True  # Root can always be listed, including an empty allow list.
    if sys.platform == "win32":
        path = path.casefold()
        allow = [pattern.casefold() for pattern in allow]
        deny = [pattern.casefold() for pattern in deny]
    parts = path.split("/")
    # Denying a directory also denies every descendant, even direct API access.
    ancestors = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    if any(matches(item, pattern) for item in ancestors for pattern in deny):
        return False
    return any(matches(path, pattern) for pattern in allow)
