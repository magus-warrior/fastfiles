from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


class PrivateRotatingFileHandler(RotatingFileHandler):
    def _open(self):
        fd = os.open(self.baseFilename, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
        if sys.platform != "win32":
            os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding=self.encoding)


def log_path() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local"
        return Path(base) / "FastFiles" / "fastfiles.log"
    base = os.environ.get("XDG_STATE_HOME")
    directory = Path(base) / "fastfiles" if base else Path.home() / ".local" / "state" / "fastfiles"
    return directory / "fastfiles.log"


def configure_logging() -> Path:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if sys.platform != "win32":
        path.parent.chmod(0o700)
    root = logging.getLogger("fastfiles")
    if root.handlers:
        return path
    fd = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.close(fd)
    if sys.platform != "win32":
        path.chmod(0o600)
    handler = PrivateRotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    logging.captureWarnings(True)
    logging.getLogger("fastfiles").info("FastFiles started pid=%s", os.getpid())
    return path
