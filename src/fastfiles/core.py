from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Iterable


class Direction(str, Enum):
    SEND = "send"
    RECEIVE = "receive"


@dataclass(frozen=True)
class TransferRequest:
    direction: Direction
    host: str
    remote_path: str
    local_paths: tuple[str, ...]
    archive: bool = True
    compress: bool = True
    partial: bool = True
    dry_run: bool = False

    def validate(self) -> None:
        if not self.host.strip():
            raise ValueError("Enter a remote host or SSH config alias.")
        if any(c in self.host for c in "\r\n\0"):
            raise ValueError("The host contains invalid characters.")
        if not self.remote_path.strip():
            raise ValueError("Enter a remote path.")
        if not self.local_paths:
            noun = "destination folder" if self.direction is Direction.RECEIVE else "file or folder"
            raise ValueError(f"Choose a local {noun}.")


def remote_spec(host: str, path: str) -> str:
    """Build an rsync remote operand without invoking a shell.

    rsync interprets the first colon as the host/path separator. Bracketed IPv6
    addresses and user@host syntax are passed through unchanged.
    """
    host = host.strip()
    path = path.strip()
    # With --protect-args, rsync deliberately prevents the remote shell from
    # expanding "~". A dot is the safe rsync spelling for the SSH user's home
    # directory, so avoid accidentally creating a literal directory named "~".
    if path == "~":
        path = "."
    elif path.startswith("~/"):
        path = "./" + path[2:]
    return f"{host}:{path}"


def build_rsync_command(request: TransferRequest) -> list[str]:
    request.validate()
    args = ["rsync", "--human-readable", "--info=progress2", "--protect-args"]
    if request.archive:
        args.append("--archive")
    else:
        args.append("--recursive")
    if request.compress:
        args.append("--compress")
    if request.partial:
        args.extend(["--partial", "--partial-dir=.fastfiles-partial"])
    if request.dry_run:
        args.append("--dry-run")
    # Everything after this point is an operand, even if a local filename starts
    # with a dash.
    args.append("--")

    remote = remote_spec(request.host, request.remote_path)
    if request.direction is Direction.SEND:
        args.extend(request.local_paths)
        args.append(remote)
    else:
        args.extend([remote, request.local_paths[0]])
    return args


def display_command(args: Iterable[str]) -> str:
    return shlex.join(args)


@dataclass(frozen=True)
class Progress:
    percent: int
    transferred: str
    speed: str = ""
    eta: str = ""


# progress2 examples vary slightly among rsync releases. Anchoring around the
# percentage makes the parser tolerant of spaces and decimal units.
_PROGRESS = re.compile(
    r"^\s*(?P<bytes>[\d,.]+(?:[kKMGTP](?:i?B)?)?)\s+"
    r"(?P<percent>\d{1,3})%\s+"
    r"(?P<speed>[\d,.]+(?:[kKMGTP](?:i?B)?)/s)\s+"
    r"(?P<eta>\d+:\d{2}(?::\d{2})?)"
)


def parse_progress(line: str) -> Progress | None:
    match = _PROGRESS.search(line.replace("\r", "").strip())
    if not match:
        return None
    return Progress(
        percent=min(100, int(match.group("percent"))),
        transferred=match.group("bytes"),
        speed=match.group("speed"),
        eta=match.group("eta"),
    )


def parse_ssh_aliases(config_text: str) -> list[str]:
    aliases: set[str] = set()
    for raw_line in config_text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        keyword = parts[0]
        if keyword.lower() != "host":
            continue
        value = parts[1] if len(parts) == 2 else ""
        for host in value.split():
            if not any(mark in host for mark in "*!?"):
                aliases.add(host)
    return sorted(aliases, key=str.casefold)


def read_ssh_aliases(path: Path | None = None) -> list[str]:
    config = path or Path.home() / ".ssh" / "config"
    try:
        return parse_ssh_aliases(config.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeError):
        return []
