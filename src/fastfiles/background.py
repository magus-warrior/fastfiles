"""Persistent sharing via a Linux systemd user service (run as your normal user)."""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


def unit_quote(value: str) -> str:
    if any(c in value for c in "\n\r\0"):
        raise ValueError("Service paths cannot contain newlines or NUL")
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$") + '"'


class BackgroundService:
    def __init__(self, config_path: Path):
        self.config_path = config_path.absolute()
        suffix = hashlib.sha256(str(self.config_path).encode()).hexdigest()[:12]
        self.name = f"fastfiles-{suffix}.service"
        base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
        self.path = base / "systemd" / "user" / self.name

    @property
    def supported(self) -> bool:
        return sys.platform.startswith("linux") and shutil.which("systemctl") is not None

    def command(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        if not self.supported:
            raise OSError("Always-on sharing currently requires Linux with systemd")
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=20)
        if check and result.returncode:
            raise OSError(result.stderr.strip() or result.stdout.strip() or "User service command failed")
        return result

    def active(self) -> bool:
        if not self.supported:
            return False
        try:
            return self.command("is-active", "--quiet", self.name, check=False).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def enable(self, *, advertise: bool = True) -> None:
        # Verify the user manager before writing anything.
        self.command("show-environment")
        executable = str(Path(sys.executable).absolute())  # preserve the virtualenv symlink
        command = " ".join(unit_quote(arg) for arg in
                           [executable, "-m", "fastfiles", "--serve", "--config", str(self.config_path)])
        if not advertise:
            command += " --no-discovery"
        content = ("[Unit]\nDescription=FastFiles always-on Locker\nAfter=network.target\n\n"
                   f"[Service]\nType=simple\nExecStart={command}\nRestart=on-failure\nRestartSec=5\n"
                   "UMask=0077\n\n[Install]\nWantedBy=default.target\n")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(content)
        self.path.chmod(0o600)
        self.command("daemon-reload")
        self.command("enable", "--now", self.name)
        self.command("restart", self.name)

    def disable(self) -> None:
        self.command("disable", "--now", self.name)

    def restart(self) -> None:
        self.command("restart", self.name)

    def status(self) -> str:
        return self.command("status", "--no-pager", "--full", self.name, check=False).stdout.strip()

    def enable_at_boot(self) -> None:
        # loginctl may use polkit; use sudo only for this account's linger setting.
        result = subprocess.run(["sudo", "loginctl", "enable-linger", str(os.getuid())], timeout=60)
        if result.returncode:
            raise OSError("Could not enable startup before login. Login startup is still enabled.")
