"""Probe Qt in a child process: platform plugin failures abort the interpreter."""
from __future__ import annotations

import os
import subprocess
import sys


def desktop_error() -> str | None:
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY") or os.environ.get("QT_QPA_PLATFORM")
    ):
        return "No desktop display is available."
    script = """
import sys
if sys.platform != 'win32':
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
from PySide6.QtWidgets import QApplication
from qt_material import apply_stylesheet
app = QApplication([])
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"Desktop check failed: {error}"
    if result.returncode:
        return ("The desktop runtime could not open. Run ./install.sh to repair desktop dependencies.\n"
                + result.stderr.strip())
    return None
