"""Locker copy operations independent of the GUI, with one fixed destination."""

from __future__ import annotations

import posixpath
import threading
from dataclasses import dataclass
from typing import Callable

from .locker import Locker, PeerClient, check_cancelled
from .policy import relative_path

ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class CopyResult:
    files: int
    directories: int
    bytes: int
    destination: str


def _manifest(listing, root: str, is_dir: bool, cancel: threading.Event | None) -> list[dict]:
    entries: list[dict] = []

    def visit(path: str, directory: bool, size: int = 0, depth: int = 0) -> None:
        check_cancelled(cancel)
        if depth > 128 or len(entries) >= 100_000:
            raise ValueError("Folder is too large or too deeply nested for a Locker transfer; use Direct SSH")
        entries.append({"path": path, "is_dir": directory, "size": size})
        if directory:
            for child in listing(path):
                visit(child["path"], child["is_dir"], child["size"], depth + 1)

    if is_dir:
        visit(root, True)
    else:
        parent = posixpath.dirname(root)
        entry = next((item for item in listing(parent) if item["path"] == root and not item["is_dir"]), None)
        if entry is None:
            raise FileNotFoundError(f"File is missing or excluded by sharing policy: {root}")
        visit(root, False, entry["size"])
    return entries


def copy_to_peer(
    locker: Locker,
    client: PeerClient,
    source: str,
    remote_directory: str,
    progress: ProgressCallback,
    cancel: threading.Event,
    *,
    overwrite: bool = False,
) -> CopyResult:
    source = relative_path(source)
    if not source:
        raise ValueError("Select a file or folder, not the locker root")
    target = posixpath.join(relative_path(remote_directory), posixpath.basename(source))
    entries = _manifest(locker.list, source, locker.resolve(source).is_dir(), cancel)
    total = sum(entry["size"] for entry in entries)
    done = files = directories = 0
    for entry in entries:
        check_cancelled(cancel)
        local = locker.resolve(entry["path"])
        destination = target + entry["path"][len(source) :]
        if entry["is_dir"]:
            client.mkdir(destination)
            directories += 1
        else:
            client.upload(
                local,
                destination,
                lambda count, _total: progress(done + count, total),
                cancel=cancel,
                overwrite=overwrite,
            )
            done += entry["size"]
            files += 1
    progress(done, total)
    return CopyResult(files, directories, done, f"{client.peer.address}:{client.peer.port}/{target}")


def copy_from_peer(
    locker: Locker,
    client: PeerClient,
    source: str,
    is_dir: bool,
    local_directory: str,
    progress: ProgressCallback,
    cancel: threading.Event,
    *,
    overwrite: bool = False,
) -> CopyResult:
    source = relative_path(source)
    if not source:
        raise ValueError("Select a file or folder, not the locker root")
    target = posixpath.join(relative_path(local_directory), posixpath.basename(source))
    entries = _manifest(client.list, source, is_dir, cancel)
    # Validate the entire destination tree before creating any files.
    destinations = [
        locker.resolve(target + entry["path"][len(source) :], must_exist=False) for entry in entries
    ]
    if not overwrite:
        for entry, local in zip(entries, destinations):
            if not entry["is_dir"] and local.exists():
                raise FileExistsError(
                    f"File already exists: {local}. Enable Replace existing files to overwrite."
                )
    total = sum(entry["size"] for entry in entries)
    done = files = directories = 0
    for entry, local in zip(entries, destinations):
        check_cancelled(cancel)
        locker.resolve(local.relative_to(locker.root).as_posix(), must_exist=False)
        if entry["is_dir"]:
            local.mkdir(parents=True, exist_ok=True)
            directories += 1
        else:
            client.download(
                entry["path"],
                local,
                lambda count, _total: progress(done + count, total),
                cancel=cancel,
                overwrite=overwrite,
            )
            done += entry["size"]
            files += 1
    progress(done, total)
    return CopyResult(files, directories, done, str(locker.root / target))
