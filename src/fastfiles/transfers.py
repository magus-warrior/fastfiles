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

from .access import path_permitted
from .locker import Locker, PeerClient, check_cancelled, commit_new_file
from .policy import relative_path

ProgressCallback = Callable[[int, int], None]


@dataclass(frozen=True)
class CopyResult:
    files: int
    directories: int
    bytes: int
    destination: str


def _manifest(
    listing, root: str, is_dir: bool, cancel: threading.Event | None, *, require_download: bool = False,
) -> list[dict]:
    entries: list[dict] = []
    root_entry: dict | None = None

    def visit(path: str, directory: bool, size: int = 0, depth: int = 0) -> None:
        check_cancelled(cancel)
        if depth > 128 or len(entries) >= 100_000:
            raise ValueError("Folder is too large or too deeply nested for a Locker transfer; use Direct SSH")
        entries.append({"path": path, "is_dir": directory, "size": size})
        if directory:
            for child in listing(path):
                visit(child["path"], child["is_dir"], child["size"], depth + 1)

    if require_download:
        check_cancelled(cancel)
        root_entry = next((item for item in listing(posixpath.dirname(root)) if item["path"] == root), None)
        if root_entry is None:
            raise FileNotFoundError(f"Item is missing or excluded by sharing policy: {root}")
        if root_entry.get("can_download") is False:
            raise PermissionError(f"Downloading this item is not permitted: {root}. Open it to select permitted files.")
        if root_entry["is_dir"] != is_dir:
            raise ValueError(f"The selected item changed; refresh and select it again: {root}")
    if is_dir:
        visit(root, True)
    else:
        parent = posixpath.dirname(root)
        entry = root_entry or next((item for item in listing(parent) if item["path"] == root and not item["is_dir"]), None)
        if entry is None:
            raise FileNotFoundError(f"File is missing or excluded by sharing policy: {root}")
        visit(root, False, entry["size"])
    return entries


def _selected_paths(sources: list[str]) -> list[str]:
    if not sources:
        raise ValueError("Select at least one file or folder")
    selected: list[str] = []
    names: set[str] = set()
    for value in sources:
        source = relative_path(value)
        if not source:
            raise ValueError("Select a file or folder, not the locker root")
        if source in selected:
            continue
        name = posixpath.basename(source)
        if name in names:
            raise ValueError(f"Selected items have the same destination name: {name}")
        names.add(name)
        selected.append(source)
    return selected


def _peer_destination(client: PeerClient, directory: str) -> str:
    return f"{client.peer.base_url}/{directory}"


def _check_manifest_size(entries: list) -> None:
    if len(entries) > 100_000:
        raise ValueError("Selection is too large for a Locker transfer; use Direct SSH")


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
    result = copy_many_to_peer(
        locker, client, [source], remote_directory, progress, cancel, overwrite=overwrite
    )
    target = posixpath.join(relative_path(remote_directory), posixpath.basename(relative_path(source)))
    return CopyResult(result.files, result.directories, result.bytes, _peer_destination(client, target))


def copy_many_to_peer(
    locker: Locker,
    client: PeerClient,
    sources: list[str],
    remote_directory: str,
    progress: ProgressCallback,
    cancel: threading.Event,
    *,
    overwrite: bool = False,
) -> CopyResult:
    """Copy selected locker items into one peer folder, preserving originals."""
    directory = relative_path(remote_directory)
    entries: list[tuple[Path, str, bool, int]] = []
    for source in _selected_paths(sources):
        check_cancelled(cancel)
        target = posixpath.join(directory, posixpath.basename(source))
        for entry in _manifest(locker.list, source, locker.resolve(source).is_dir(), cancel):
            destination = relative_path(target + entry["path"][len(source):])
            entries.append((locker.resolve(entry["path"]), destination, entry["is_dir"], entry["size"]))
        _check_manifest_size(entries)
    return _upload_entries(client, entries, directory, progress, cancel, overwrite, locker)


def _upload_entries(
    client: PeerClient,
    entries: list[tuple[Path, str, bool, int]],
    directory: str,
    progress: ProgressCallback,
    cancel: threading.Event,
    overwrite: bool,
    locker: Locker | None = None,
) -> CopyResult:
    check_cancelled(cancel)
    capabilities = client.info()
    if capabilities.get("read_only", False) or not capabilities.get("can_upload", True):
        raise PermissionError("This computer does not permit uploads with your access key")
    patterns = capabilities.get("upload_patterns", ["**"])
    for _, destination, _, _ in entries:
        check_cancelled(cancel)
        if not path_permitted(destination, patterns):
            raise PermissionError(f"Uploading to this destination is not permitted: {destination}")
    total = sum(size for _, _, _, size in entries)
    done = files = directories = 0
    progress(0, total)
    for source, destination, is_dir, size in entries:
        check_cancelled(cancel)
        if locker is not None:
            locker.resolve(source.relative_to(locker.root).as_posix())
        metadata = _source_metadata(source)
        if is_dir != stat.S_ISDIR(metadata.st_mode) or (not is_dir and metadata.st_size != size):
            raise OSError(f"Source changed before transfer: {source}")
        if is_dir:
            client.mkdir(destination)
            directories += 1
        else:
            client.upload(
                source,
                destination,
                lambda count, _total: progress(done + count, total),
                cancel=cancel,
                overwrite=overwrite,
            )
            done += size
            files += 1
    progress(done, total)
    return CopyResult(files, directories, done, _peer_destination(client, directory))


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
    result = copy_many_from_peer(
        locker, client, [(source, is_dir)], local_directory, progress, cancel, overwrite=overwrite
    )
    target = posixpath.join(relative_path(local_directory), posixpath.basename(relative_path(source)))
    return CopyResult(result.files, result.directories, result.bytes, str(locker.root / target))


def copy_many_from_peer(
    locker: Locker,
    client: PeerClient,
    sources: list[tuple[str, bool]],
    local_directory: str,
    progress: ProgressCallback,
    cancel: threading.Event,
    *,
    overwrite: bool = False,
) -> CopyResult:
    """Receive a selection using only entries visible to this client's permissions."""
    directory = relative_path(local_directory)
    destination = locker.resolve(directory, must_exist=False)
    if destination.exists() and not destination.is_dir():
        raise NotADirectoryError(str(destination))
    selected = _selected_paths([source for source, _ in sources])
    kinds: dict[str, bool] = {}
    for source, is_dir in sources:
        if type(is_dir) is not bool:
            raise ValueError("Selected item type must be a boolean")
        source = relative_path(source)
        if source in kinds and kinds[source] != is_dir:
            raise ValueError(f"Conflicting types for selected item: {source}")
        kinds[source] = is_dir
    entries: list[tuple[str, Path, bool, int]] = []
    for source in selected:
        target = posixpath.join(directory, posixpath.basename(source))
        for entry in _manifest(client.list, source, kinds[source], cancel, require_download=True):
            local = locker.resolve(target + entry["path"][len(source):], must_exist=False)
            if local.exists() and (
                entry["is_dir"] != local.is_dir() or (not entry["is_dir"] and not overwrite)
            ):
                raise FileExistsError(
                    f"Destination already exists: {local}. Enable Replace existing files to overwrite."
                )
            entries.append((entry["path"], local, entry["is_dir"], entry["size"]))
        _check_manifest_size(entries)
    total = sum(size for _, _, _, size in entries)
    done = files = directories = 0
    progress(0, total)
    for source, local, is_dir, size in entries:
        check_cancelled(cancel)
        locker.resolve(local.relative_to(locker.root).as_posix(), must_exist=False)
        if is_dir:
            local.mkdir(parents=True, exist_ok=True)
            directories += 1
        else:
            received_size = size

            def download_progress(count: int, actual_size: int) -> None:
                nonlocal total, received_size
                # A file can change between browsing and downloading it. The
                # response length describes the bytes actually being copied.
                total += actual_size - received_size
                received_size = actual_size
                progress(done + count, total)

            client.download(
                source,
                local,
                download_progress,
                cancel=cancel,
                overwrite=overwrite,
            )
            done += received_size
            files += 1
    progress(done, total)
    return CopyResult(files, directories, done, str(destination))


def _source_metadata(source: Path):
    # Inspect ancestors too: a regular file reached through a linked folder is
    # still outside the dropped tree's literal path.
    for candidate in reversed((source, *source.parents)):
        metadata = candidate.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError(f"Links and junctions cannot be copied: {candidate}")
    if not stat.S_ISDIR(metadata.st_mode) and not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"Only regular files and folders can be copied: {source}")
    return metadata


def upload_paths_to_peer(
    client: PeerClient,
    sources: list[str],
    remote_directory: str,
    progress: ProgressCallback,
    cancel: threading.Event,
    *,
    overwrite: bool = False,
) -> CopyResult:
    """Send dropped files directly to a computer without staging them in a locker."""
    directory = relative_path(remote_directory)
    if not sources:
        raise ValueError("Drop at least one file or folder")
    entries: list[tuple[Path, str, bool, int]] = []
    seen: set[str] = set()

    def visit(source: Path, target: str, depth: int = 0) -> None:
        check_cancelled(cancel)
        if depth > 128 or len(entries) >= 100_000:
            raise ValueError("Dropped folder is too large or too deeply nested")
        target = relative_path(target)
        if target in seen:
            raise ValueError(f"Multiple dropped items have the same destination: {target}")
        seen.add(target)
        metadata = _source_metadata(source)
        is_dir = stat.S_ISDIR(metadata.st_mode)
        entries.append((source, target, is_dir, 0 if is_dir else metadata.st_size))
        if is_dir:
            for child in sorted(source.iterdir()):
                visit(child, posixpath.join(target, child.name), depth + 1)

    for value in dict.fromkeys(sources):
        source = Path(value).absolute()
        if not source.name:
            raise ValueError("Drop files or folders, not a filesystem root")
        visit(source, posixpath.join(directory, source.name))
    return _upload_entries(client, entries, directory, progress, cancel, overwrite)


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
