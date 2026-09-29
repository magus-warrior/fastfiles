from __future__ import annotations

import glob
import ipaddress
import re
import shlex
import shutil
import subprocess
import sys
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
        validate_host(self.host)
        normalize_remote_path(self.remote_path)
        if not self.local_paths:
            noun = "destination folder" if self.direction is Direction.RECEIVE else "file or folder"
            raise ValueError(f"Choose a local {noun}.")
        if self.direction is Direction.RECEIVE and len(self.local_paths) != 1:
            raise ValueError("Choose exactly one local destination folder.")
        if any(not path or any(c in path for c in "\r\n\0") for path in self.local_paths):
            raise ValueError("A local path contains invalid characters.")


def validate_host(host: str) -> None:
    host = host.strip()
    if not host:
        raise ValueError("Enter a remote host or SSH config alias.")
    if "@" in host:
        user, host = host.rsplit("@", 1)
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", user):
            raise ValueError("Use user@hostname or an SSH alias for the remote host.")
    if host.startswith("[") and host.endswith("]"):
        try:
            ipaddress.IPv6Address(host[1:-1])
            return
        except ValueError:
            pass
    elif re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", host):
        return
    raise ValueError("Use a hostname, SSH alias, or user@[IPv6]. Set custom ports in SSH config.")


def normalize_remote_path(path: str) -> str:
    path = path.strip()
    if not path or any(c in path for c in "\r\n\0"):
        raise ValueError("Enter a remote path without control characters.")
    if path.startswith("~") and path != "~" and not path.startswith("~/"):
        raise ValueError("Use ~/ for your SSH home or an absolute path; ~otheruser is not supported.")
    if path == "~":
        return "."
    if path.startswith("~/"):
        return "./" + path[2:]
    # Prefix relative operands so ':' cannot opt into the rsync daemon protocol.
    return path if path.startswith(("/", "./", "../")) else "./" + path


def remote_spec(host: str, path: str) -> str:
    """Build an rsync remote operand without invoking a shell.

    rsync interprets the first colon as the host/path separator. Bracketed IPv6
    addresses and user@host syntax are passed through unchanged.
    """
    validate_host(host)
    host = host.strip()
    # With --protect-args, rsync deliberately prevents the remote shell from
    # expanding "~". A dot is the safe rsync spelling for the SSH user's home
    # directory, so avoid accidentally creating a literal directory named "~".
    return f"{host}:{normalize_remote_path(path)}"


def build_rsync_command(request: TransferRequest) -> list[str]:
    request.validate()
    args = [
        "rsync",
        "--human-readable",
        "--info=progress2",
        "--protect-args",
        "--itemize-changes",
        "--stats",
        "--rsh=ssh -oBatchMode=yes -oConnectTimeout=15 -oServerAliveInterval=15 -oServerAliveCountMax=3",
    ]
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

    path = request.remote_path.strip()
    if request.direction is Direction.SEND and not path.endswith("/"):
        path += "/"  # The send form always selects a destination folder.
    remote = remote_spec(request.host, path)
    local_paths = [rsync_local_path(path) for path in request.local_paths]
    if request.direction is Direction.SEND:
        args.extend(local_paths)
        args.append(remote)
    else:
        args.extend([remote, local_paths[0]])
    return args


def rsync_local_path(path: str) -> str:
    absolute = str(Path(path).expanduser().absolute())
    if sys.platform == "win32":
        converter = shutil.which("cygpath")
        if not converter:
            raise ValueError("Direct SSH on Windows requires Cygwin rsync, openssh, and cygpath on PATH. "
                             "Locker transfers do not require these tools.")
        try:
            absolute = subprocess.check_output(
                [converter, "-u", "--", absolute], text=True, encoding="utf-8", timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError(f"Could not convert the local path using Cygwin: {error}") from error
        if not absolute.startswith("/") or any(c in absolute for c in "\r\n\0"):
            raise ValueError("Cygwin returned an invalid local path")
    separators = ("/", "\\") if sys.platform == "win32" else ("/",)
    return absolute.rstrip("/") + "/" if path.endswith(separators) else absolute


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
        try:
            parts = shlex.split(raw_line, comments=True)
        except ValueError:
            continue
        if not parts:
            continue
        if "=" in parts[0]:
            key, value = parts.pop(0).split("=", 1)
            parts = [key, value, *parts]
        keyword = parts[0]
        if keyword.lower() != "host":
            continue
        for host in parts[1:]:
            try:
                validate_host(host)
            except ValueError:
                continue
            aliases.add(host)
    return sorted(aliases, key=str.casefold)


def read_ssh_aliases(path: Path | None = None) -> list[str]:
    config = path or Path.home() / ".ssh" / "config"
    visited: set[Path] = set()

    def read(current: Path) -> set[str]:
        current = current.expanduser().resolve()
        if current in visited or len(visited) >= 100:
            return set()
        visited.add(current)
        try:
            text = current.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return set()
        aliases = set(parse_ssh_aliases(text))
        for line in text.splitlines():
            try:
                parts = shlex.split(line, comments=True)
            except ValueError:
                continue
            if parts and parts[0].lower() == "include":
                for pattern in parts[1:]:
                    expanded = Path(pattern).expanduser()
                    if not expanded.is_absolute():
                        expanded = config.parent / expanded
                    for match in sorted(glob.glob(str(expanded))):
                        aliases.update(read(Path(match)))
        return aliases

    return sorted(read(config), key=str.casefold)
