"""Private, atomic JSON storage shared by settings and connection profiles."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) / "fastfiles" if base else Path.home() / ".config" / "fastfiles"


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Invalid JSON in {path}; the file was left unchanged") from error
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}; the file was left unchanged")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".fastfiles-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            json.dump(value, output, indent=2)
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
