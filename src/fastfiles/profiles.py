from __future__ import annotations

import logging
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .core import normalize_remote_path, validate_host
from .locker import parse_peer_address
from .policy import relative_path
from .storage import config_dir, read_json, write_json

logger = logging.getLogger("fastfiles.profiles")


def _name(value: str) -> None:
    if not isinstance(value, str) or not value.strip() or any(c in value for c in "\r\n\0"):
        raise ValueError("Profile aliases must be nonempty text")


@dataclass(frozen=True)
class DirectProfile:
    name: str
    host: str
    remote_path: str

    def __post_init__(self) -> None:
        _name(self.name)
        if not isinstance(self.host, str) or not isinstance(self.remote_path, str):
            raise ValueError("Host and remote path must be text")
        validate_host(self.host)
        normalize_remote_path(self.remote_path)


@dataclass(frozen=True)
class PeerProfile:
    name: str
    address: str
    port: int
    device_id: str = ""
    folders: dict[str, str] = field(default_factory=dict)
    last_folder: str = ""

    def __post_init__(self) -> None:
        _name(self.name)
        if not isinstance(self.address, str) or type(self.port) is not int:
            raise ValueError("Invalid saved peer address or port")
        address = f"[{self.address}]" if ":" in self.address else self.address
        parsed = parse_peer_address(f"{address}:{self.port}")
        if parsed.address != self.address:
            raise ValueError("Save only the peer hostname or IP in address")
        if not isinstance(self.device_id, str) or any(c in self.device_id for c in "\r\n\0"):
            raise ValueError("Invalid saved computer identity")
        if not isinstance(self.folders, dict):
            raise ValueError("Saved folders must map names to relative locker paths")
        folders = {}
        try:
            for name, path in self.folders.items():
                _name(name)
                folders[name] = relative_path(path)
            last_folder = relative_path(self.last_folder)
        except (ValueError, PermissionError) as error:
            raise ValueError("Saved folders require names and relative locker paths") from error
        object.__setattr__(self, "folders", folders)
        object.__setattr__(self, "last_folder", last_folder)

    @property
    def secret_id(self) -> str:
        return f"peer:{self.address}:{self.port}"


class ProfileStore:
    def __init__(self, path: Path | None = None):
        self.path = path or config_dir() / "connections.json"

    def _read(self) -> dict:
        try:
            value = read_json(self.path)
        except FileNotFoundError:
            return {}
        for key, kind in (("direct", DirectProfile), ("peers", PeerProfile)):
            entries = value.get(key, [])
            if not isinstance(entries, list):
                raise ValueError(f"{key} must be a list in {self.path}")
            try:
                profiles = [kind(**entry) for entry in entries]
            except (TypeError, ValueError) as error:
                raise ValueError(f"Invalid saved connection in {self.path}; file left unchanged") from error
            if len({profile.name for profile in profiles}) != len(profiles):
                raise ValueError(f"Duplicate aliases in {self.path}")
        hidden = value.get("hidden_peers", [])
        if not isinstance(hidden, list) or any(not isinstance(item, str) for item in hidden):
            raise ValueError(f"hidden_peers must be a list of addresses in {self.path}")
        return value

    def hidden_peers(self) -> set[str]:
        return set(self._read().get("hidden_peers", []))

    def forget_peer(self, endpoint: str, name: str | None = None) -> None:
        data = self._read()
        if name is not None:
            data["peers"] = [item for item in data.get("peers", []) if item["name"] != name]
        data["hidden_peers"] = sorted(set(data.get("hidden_peers", [])) | {endpoint})
        self._write(data)

    def restore_hidden_peers(self) -> None:
        data = self._read()
        data["hidden_peers"] = []
        self._write(data)

    def _write(self, value: dict) -> None:
        write_json(self.path, value)

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

    def update_profile(self, old_name: str, profile: DirectProfile | PeerProfile) -> None:
        """Replace an existing profile in one write, preserving unrelated entries."""
        key = "peers" if isinstance(profile, PeerProfile) else "direct"
        data = self._read()
        entries = data.get(key, [])
        if not any(item["name"] == old_name for item in entries):
            raise ValueError("This saved computer no longer exists")
        if any(item["name"] == profile.name and item["name"] != old_name for item in entries):
            raise ValueError("Another saved computer already has that name")
        data[key] = [asdict(profile) if item["name"] == old_name else item for item in entries]
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
    """Use the OS keyring, or an encrypted private vault on headless accounts."""

    service = "FastFiles"

    def __init__(self):
        from .credentials import CredentialVault

        self.vault = CredentialVault()

    @staticmethod
    def available() -> bool:
        return True

    def get(self, key: str) -> str:
        # A fallback copy is newer than any keyring entry left while it was locked.
        try:
            value = self.vault.get(key)
            if value or self.vault.contains(key):
                return value
        except (OSError, ValueError):
            logger.warning("Saved access could not be unlocked")
        try:
            import keyring

            return keyring.get_password(self.service, key) or ""
        except Exception:
            return ""

    def set(self, key: str, value: str) -> None:
        try:
            import keyring

            backend = keyring.get_keyring()
            if backend.priority <= 0 or "plaintext" in type(backend).__name__.lower():
                raise RuntimeError("No secure keyring")
            keyring.set_password(self.service, key, value)
        except Exception:
            self.vault.set(key, value)
        else:
            self.vault.delete(key)

    def delete(self, key: str) -> None:
        # Keep an empty fallback tombstone if the OS keyring is unavailable, so
        # an old credential cannot reappear when the keyring unlocks later.
        try:
            import keyring

            keyring.delete_password(self.service, key)
        except Exception:
            self.vault.set(key, "")
        else:
            self.vault.delete(key)
