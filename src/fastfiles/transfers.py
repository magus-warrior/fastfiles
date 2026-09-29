"""Locker copy operations independent of the GUI, with one fixed destination."""

from __future__ import annotations

import os
import posixpath
import stat
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .locker import Locker, PeerClient, check_cancelled, commit_new_file
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


def import_to_locker(
    locker: Locker,
    sources: list[str],
    directory: str,
    progress: ProgressCallback,
    cancel: threading.Event,
    *,
    overwrite: bool = False,
) -> CopyResult:
    """Copy dropped items without moving originals or following links."""
    destination = locker.resolve(directory)
    entries: list[tuple[Path, Path, bool, int]] = []
    seen: set[Path] = set()

    def visit(source: Path, target: Path, depth: int = 0) -> None:
        check_cancelled(cancel)
        if depth > 128 or len(entries) >= 100_000:
            raise ValueError("Dropped folder is too large or too deeply nested")
        metadata = source.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Links and junctions cannot be imported: {source}")
        is_dir = stat.S_ISDIR(metadata.st_mode)
        if not is_dir and not stat.S_ISREG(metadata.st_mode):
            raise ValueError(f"Only regular files and folders can be imported: {source}")
        target = locker.resolve(target.relative_to(locker.root).as_posix(), must_exist=False)
        if target in seen:
            raise ValueError(f"Multiple dropped items have the same destination: {target}")
        seen.add(target)
        if target.exists() and (is_dir != target.is_dir() or (not is_dir and not overwrite)):
            raise FileExistsError(f"Destination already exists: {target}. Enable Replace existing files to overwrite.")
        entries.append((source, target, is_dir, 0 if is_dir else metadata.st_size))
        if is_dir:
            for child in sorted(source.iterdir()):
                visit(child, target / child.name, depth + 1)

    for value in dict.fromkeys(sources):
        source = Path(value).absolute()
        target = destination / source.name
        if source.resolve() == target.resolve() or target.resolve().is_relative_to(source.resolve()):
            raise ValueError(f"Cannot copy a folder into itself or import an item already here: {source}")
        visit(source, target)
    total = sum(size for _, _, _, size in entries)
    done = files = directories = 0
    for source, target, is_dir, size in entries:
        check_cancelled(cancel)
        locker.resolve(target.relative_to(locker.root).as_posix(), must_exist=False)
        if is_dir:
            target.mkdir(parents=True, exist_ok=True)
            directories += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".fastfiles-", dir=target.parent)
        temporary = Path(name)
        try:
            with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
                copied = 0
                while chunk := input_file.read(1024 * 1024):
                    check_cancelled(cancel)
                    output.write(chunk)
                    copied += len(chunk)
                    progress(done + copied, total)
                output.flush()
                os.fsync(output.fileno())
            check_cancelled(cancel)
            if copied != size:
                raise OSError(f"Source changed during import: {source}")
            locker.resolve(target.relative_to(locker.root).as_posix(), must_exist=False)
            if overwrite:
                temporary.replace(target)
            else:
                commit_new_file(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        done += size
        files += 1
    progress(done, total)
    return CopyResult(files, directories, done, str(destination))
