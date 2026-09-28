from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) / "fastfiles" if base else Path.home() / ".config" / "fastfiles"


@dataclass(frozen=True)
class DirectProfile:
    name: str
    host: str
    remote_path: str


@dataclass(frozen=True)
class PeerProfile:
    name: str
    address: str
    port: int

    @property
    def secret_id(self) -> str:
        return f"peer:{self.address}:{self.port}"


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "connections.json"

    def _read(self) -> dict:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (FileNotFoundError, OSError, ValueError):
            return {}

    def _write(self, value: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.path)

    def direct(self) -> list[DirectProfile]:
        result = []
        for value in self._read().get("direct", []):
            try:
                result.append(DirectProfile(**value))
            except (TypeError, ValueError):
                continue
        return sorted(result, key=lambda profile: profile.name.casefold())

    def peers(self) -> list[PeerProfile]:
        result = []
        for value in self._read().get("peers", []):
            try:
                result.append(PeerProfile(**value))
            except (TypeError, ValueError):
                continue
        return sorted(result, key=lambda profile: profile.name.casefold())

    def save_direct(self, profile: DirectProfile) -> None:
        data = self._read()
        profiles = [item for item in self.direct() if item.name != profile.name]
        profiles.append(profile)
        data["direct"] = [asdict(item) for item in sorted(profiles, key=lambda item: item.name.casefold())]
        self._write(data)

    def delete_direct(self, name: str) -> None:
        data = self._read()
        data["direct"] = [asdict(item) for item in self.direct() if item.name != name]
        self._write(data)

    def save_peer(self, profile: PeerProfile) -> None:
        data = self._read()
        profiles = [item for item in self.peers() if item.name != profile.name]
        profiles.append(profile)
        data["peers"] = [asdict(item) for item in sorted(profiles, key=lambda item: item.name.casefold())]
        self._write(data)

    def delete_peer(self, name: str) -> None:
        data = self._read()
        data["peers"] = [asdict(item) for item in self.peers() if item.name != name]
        self._write(data)


class SecretStore:
    """Store connection secrets in the desktop keyring, never in profile JSON."""

    service = "FastFiles"

    @staticmethod
    def available() -> bool:
        try:
            import keyring
            keyring.get_keyring().priority
            return True
        except Exception:
            return False

    def get(self, key: str) -> str:
        try:
            import keyring
            return keyring.get_password(self.service, key) or ""
        except Exception:
            return ""

    def set(self, key: str, value: str) -> None:
        import keyring
        keyring.set_password(self.service, key, value)

    def delete(self, key: str) -> None:
        try:
            import keyring
            keyring.delete_password(self.service, key)
        except Exception:
            pass
