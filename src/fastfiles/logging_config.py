from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


def log_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    directory = Path(base) / "fastfiles" if base else Path.home() / ".local" / "state" / "fastfiles"
    return directory / "fastfiles.log"


def configure_logging() -> Path:
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger("fastfiles")
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(handler)
    logging.captureWarnings(True)
    logging.getLogger("fastfiles").info("FastFiles started pid=%s", os.getpid())
    return path
