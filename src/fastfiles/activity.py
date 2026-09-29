"""Private incoming transfer receipts, readable by the local desktop."""

from __future__ import annotations

import errno
import os
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .storage import read_json, write_json

_locks: dict[Path, threading.RLock] = {}
_locks_guard = threading.Lock()


class ActivityStore:
    def __init__(self, root: str | Path):
        self.path = Path(root).expanduser().resolve() / ".fastfiles-activity.json"
        with _locks_guard:
            self._lock = _locks.setdefault(self.path, threading.RLock())

    @contextmanager
    def _writer_lock(self):
        """Serialize writers in separate desktop/headless processes.

        The lock file stays in place so every process locks the same inode.
        Receipts are still replaced atomically, so readers need no process lock.
        OS locks are released automatically if a writer exits unexpectedly.
        """
        lock_path = self.path.with_suffix(".lock")
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if lock_path.is_symlink():
            raise ValueError("Activity lock cannot be a symbolic link")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(lock_path, flags, 0o600)
        acquired = False
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ValueError("Activity lock must be a regular file")
            if sys.platform == "win32":
                import msvcrt

                def acquire():
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

                def release():
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                def acquire():
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

                def release():
                    fcntl.flock(fd, fcntl.LOCK_UN)

            deadline = time.monotonic() + 5
            while True:
                try:
                    acquire()
                    acquired = True
                    break
                except OSError as error:
                    if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                        raise
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Incoming activity is busy; retry shortly") from error
                    time.sleep(0.01)
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            yield
        finally:
            try:
                if acquired:
                    release()
            finally:
                os.close(fd)

    def recent(self, limit: int = 100) -> list[dict]:
        if type(limit) is not int or limit < 0:
            raise ValueError("Activity limit must be a nonnegative integer")
        with self._lock:
            if self.path.is_symlink():
                raise ValueError("Activity storage cannot be a symbolic link")
            try:
                value = read_json(self.path)
            except FileNotFoundError:
                return []
            entries = value.get("receipts")
            if not isinstance(entries, list) or any(
                not isinstance(entry, dict)
                or not all(isinstance(entry.get(key), str) for key in (
                    "id", "time", "kind", "source", "destination"
                ))
                or any(type(entry.get(key)) is not int or entry[key] < 0 for key in ("files", "bytes"))
                for entry in entries
            ):
                raise ValueError("Invalid incoming activity; file left unchanged")
            return entries[:limit]

    def record(self, kind: str, source: str, destination: str, files: int, bytes: int) -> dict:
        if (
            any(not isinstance(value, str) for value in (kind, source, destination))
            or any(type(value) is not int or value < 0 for value in (files, bytes))
        ):
            raise ValueError("Invalid activity receipt")
        entry = {
            "id": str(uuid.uuid4()),
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
            "source": source,
            "destination": destination,
            "files": files,
            "bytes": bytes,
        }
        with self._lock, self._writer_lock():
            write_json(self.path, {"receipts": [entry, *self.recent(199)]})
        return entry
