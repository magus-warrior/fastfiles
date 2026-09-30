"""Encrypted fallback for accounts without an unlocked OS keyring.

The vault and its local encryption key live in a private user directory. This
protects copied vault files, not against someone who controls the user account.
"""
from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .storage import config_dir, read_json, write_json

_vault_lock = threading.RLock()


class CredentialVault:
    def __init__(self, directory: Path | None = None):
        self.directory = directory or config_dir() / "private"
        self.path = self.directory / "credentials.json"
        self.key_path = self.directory / "credentials.key"
        self.lock_path = self.directory / "credentials.lock"

    def _prepare(self) -> None:
        if any(path.is_symlink() for path in (self.directory, self.path, self.key_path, self.lock_path)):
            raise OSError("Credential storage must not be a symbolic link")
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            self.directory.chmod(0o700)

    @contextmanager
    def _locked(self):
        with _vault_lock:
            self._prepare()
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            locked = False
            try:
                if os.name == "nt":
                    import msvcrt

                    if os.fstat(fd).st_size == 0:
                        os.write(fd, b"\0")
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX)
                locked = True
                yield
            finally:
                if locked:
                    if os.name == "nt":
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _read(self) -> dict[str, str]:
        self._prepare()
        try:
            data = read_json(self.path)
        except FileNotFoundError:
            return {}
        if not all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()):
            raise ValueError("Invalid credential storage; existing data was left unchanged")
        if os.name != "nt":
            self.path.chmod(0o600)
        return data

    def _cipher(self, *, create: bool = False) -> Fernet:
        self._prepare()
        if create and not self.key_path.exists():
            try:
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as output:
                    output.write(Fernet.generate_key())
                    output.flush()
                    os.fsync(output.fileno())
        if os.name != "nt":
            self.key_path.chmod(0o600)
        return Fernet(self.key_path.read_bytes())

    def contains(self, key: str) -> bool:
        return key in self._read()

    def get(self, key: str) -> str:
        data = self._read()
        if key not in data:
            return ""
        try:
            return self._cipher().decrypt(data[key].encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeError) as error:
            raise ValueError("Saved access could not be unlocked; credential files were left unchanged") from error

    def set(self, key: str, value: str) -> None:
        with self._locked():
            data = self._read()
            # A missing master key must never strand already-saved credentials.
            cipher = self._cipher(create=not data)
            data[key] = cipher.encrypt(value.encode("utf-8")).decode("ascii")
            write_json(self.path, data)

    def delete(self, key: str) -> None:
        with self._locked():
            data = self._read()
            if key in data:
                del data[key]
                write_json(self.path, data)
