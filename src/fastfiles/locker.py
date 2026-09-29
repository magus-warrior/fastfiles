from __future__ import annotations

import hashlib
import hmac
import http.client
import ipaddress
import json
import logging
import mimetypes
import os
import re
import secrets
import socket
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from .access import authenticate_computer, can_traverse, path_permitted, validate_computer_permissions
from .activity import ActivityStore
from .policy import allowed, relative_path, validate_patterns
from .storage import config_dir, read_json, write_json

SERVICE_TYPE = "_fastfiles._tcp.local."
DEFAULT_PORT = 47832
PROTOCOL_VERSION = 2
logger = logging.getLogger("fastfiles.locker")


@dataclass
class LockerConfig:
    device_id: str
    device_name: str
    locker_path: str
    access_code_hash: str
    access_code: str = ""
    allow_patterns: list[str] = field(default_factory=lambda: ["**"])
    deny_patterns: list[str] = field(default_factory=list)
    bind_address: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    read_only: bool = False
    max_upload_bytes: int = 50 * 1024**3
    computer_permissions: list[dict] = field(default_factory=list)
    computer_access_enabled: bool = False

    def __post_init__(self) -> None:
        for name in ("device_id", "device_name", "locker_path"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} must be a nonempty string")
        if not Path(self.locker_path).expanduser().is_absolute():
            raise ValueError("locker_path must be an absolute path")
        if not isinstance(self.access_code_hash, str) or not re.fullmatch(
            r"[a-f0-9]{64}", self.access_code_hash
        ):
            raise ValueError("access_code_hash must be a SHA-256 hex digest")
        if not isinstance(self.access_code, str) or (
            self.access_code
            and (
                not re.fullmatch(r"[0-9]{6}", self.access_code)
                or hash_access_code(self.access_code) != self.access_code_hash
            )
        ):
            raise ValueError("access_code must be six digits matching access_code_hash")
        validate_patterns(self.allow_patterns, "allow_patterns")
        validate_patterns(self.deny_patterns, "deny_patterns")
        if not isinstance(self.bind_address, str):
            raise ValueError("bind_address must be an IP address")
        ipaddress.ip_address(self.bind_address)
        if type(self.port) is not int or not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if type(self.read_only) is not bool:
            raise ValueError("read_only must be true or false")
        if type(self.max_upload_bytes) is not int or self.max_upload_bytes < 1:
            raise ValueError("max_upload_bytes must be a positive integer")
        validate_computer_permissions(self.computer_permissions)
        if type(self.computer_access_enabled) is not bool:
            raise ValueError("computer_access_enabled must be true or false")
        if not hasattr(self, "_access_lock"):
            self._access_lock = threading.RLock()

    @property
    def uses_computer_keys(self) -> bool:
        return self.computer_access_enabled or bool(self.computer_permissions)

    def access_snapshot(self) -> tuple[bool, list[dict]]:
        """Freeze authorization for one request while the owner updates grants."""
        with self._access_lock:
            return self.uses_computer_keys, deepcopy(self.computer_permissions)

    def save_computer_permissions(self, entries: list[dict], path: Path | None = None) -> None:
        """Persist and apply grants together; failures preserve the old access.

        Computer-key mode stays enabled even when the final key is revoked.
        Requests already authorized may finish using their captured grant.
        """
        entries = deepcopy(entries)
        validate_computer_permissions(entries)
        with self._access_lock:
            previous = self.computer_permissions, self.computer_access_enabled
            self.computer_permissions = entries
            self.computer_access_enabled = True
            try:
                self.save(path)
            except Exception:
                self.computer_permissions, self.computer_access_enabled = previous
                raise

    @classmethod
    def load(cls, path: Path | None = None) -> "LockerConfig":
        config_path = path or config_dir() / "config.json"
        try:
            data = read_json(config_path)
        except FileNotFoundError:
            code = f"{secrets.randbelow(1_000_000):06d}"
            config = cls(
                device_id=str(uuid.uuid4()),
                device_name=socket.gethostname(),
                locker_path=str(Path.home() / "FastFiles Locker"),
                access_code_hash=hash_access_code(code),
                access_code=code,
            )
            config.save(config_path)
            return config
        try:
            return cls(**data)
        except (TypeError, ValueError) as error:
            raise ValueError(
                f"Invalid sharing configuration in {config_path}: {error}. File left unchanged."
            ) from error

    def save(self, path: Path | None = None) -> None:
        with self._access_lock:
            self.__post_init__()
            write_json(path or config_dir() / "config.json", asdict(self))


def hash_access_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


class Locker:
    def __init__(
        self,
        root: str | Path,
        allow_patterns: list[str] | None = None,
        deny_patterns: list[str] | None = None,
    ):
        self.root = Path(root).expanduser().resolve()
        self.allow_patterns = ["**"] if allow_patterns is None else list(allow_patterns)
        self.deny_patterns = [] if deny_patterns is None else list(deny_patterns)
        validate_patterns(self.allow_patterns, "allow_patterns")
        validate_patterns(self.deny_patterns, "deny_patterns")
        self.root.mkdir(parents=True, exist_ok=True)

    def is_allowed(self, relative: str, *, is_dir: bool = False) -> bool:
        try:
            return allowed(relative_path(relative), self.allow_patterns, self.deny_patterns)
        except (ValueError, PermissionError):
            return False

    def resolve(self, relative: str, *, must_exist: bool = True) -> Path:
        relative = relative_path(relative)
        candidate = self.root
        for part in relative.split("/") if relative else []:
            candidate = candidate / part
            if candidate.is_symlink() or (
                sys.platform == "win32" and candidate.exists()
                and candidate.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise PermissionError("Symbolic links are not shared by Locker")
        candidate = candidate.resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError("Path leaves the approved locker") from exc
        if must_exist and not candidate.exists():
            raise FileNotFoundError(relative)
        normalized = candidate.relative_to(self.root).as_posix()
        if not self.is_allowed(normalized, is_dir=candidate.is_dir() if candidate.exists() else False):
            raise PermissionError("Path is blocked by the locker sharing policy")
        return candidate

    def list(self, relative: str = "") -> list[dict[str, Any]]:
        directory = self.resolve(relative)
        if not directory.is_dir():
            raise NotADirectoryError(relative)
        result = []
        for entry in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name.casefold())):
            relative_path = entry.relative_to(self.root).as_posix()
            try:
                self.resolve(relative_path)
                stat = entry.stat()
                if not entry.is_dir() and not entry.is_file():
                    continue
            except (OSError, ValueError):
                continue
            result.append(
                {
                    "name": entry.name,
                    "path": relative_path,
                    "is_dir": entry.is_dir(),
                    "size": 0 if entry.is_dir() else stat.st_size,
                    "modified": int(stat.st_mtime),
                }
            )
        return result


def commit_new_file(temporary: Path, destination: Path) -> None:
    """Publish a closed temporary file without replacing an existing destination."""
    if sys.platform == "win32":
        # Windows rename fails if the destination exists; works on exFAT too.
        temporary.rename(destination)
    else:
        os.link(temporary, destination)


class LockerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], locker: Locker, config: LockerConfig):
        super().__init__(address, LockerRequestHandler)
        self.locker = locker
        self.config = config
        self.auth_failures: dict[str, tuple[int, float]] = {}
        self.auth_lock = threading.Lock()
        self.activity = ActivityStore(locker.root)


class IPv6LockerHTTPServer(LockerHTTPServer):
    address_family = socket.AF_INET6


class LockerRequestHandler(BaseHTTPRequestHandler):
    server: LockerHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _json(self, status: int, value: Any) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        self.wfile.write(data)

    def _authorized(self) -> bool:
        self.computer_permission: dict | None = None
        key_mode, computer_permissions = self.server.config.access_snapshot()
        address = self.client_address[0]
        now = time.monotonic()
        with self.server.auth_lock:
            self.server.auth_failures = {
                key: value for key, value in self.server.auth_failures.items() if now - value[1] < 60
            }
            attempts, since = self.server.auth_failures.get(address, (0, now))
            if attempts >= 10:
                self._json(
                    HTTPStatus.TOO_MANY_REQUESTS, {"error": "Too many incorrect codes; retry in one minute"}
                )
                return False
            code = self.headers.get("X-FastFiles-Code", "")
            if key_mode:
                self.computer_permission = authenticate_computer(code, computer_permissions)
                accepted = self.computer_permission is not None
            else:
                accepted = hmac.compare_digest(hash_access_code(code), self.server.config.access_code_hash)
            if accepted:
                self.server.auth_failures.pop(address, None)
                return True
            self.server.auth_failures[address] = (attempts + 1, since)
        message = "Incorrect or revoked computer key" if key_mode else "Incorrect pairing code"
        self._json(HTTPStatus.UNAUTHORIZED, {"error": message})
        return False

    def _patterns(self, operation: str) -> list[str]:
        if operation == "upload" and self.server.config.read_only:
            return []
        if self.computer_permission is None:
            return ["**"]
        return self.computer_permission[f"{operation}_patterns"]

    def _permitted(self, relative: str, operation: str) -> bool:
        return path_permitted(relative, self._patterns(operation))

    def _may_browse(self, relative: str) -> bool:
        return can_traverse(relative, self._patterns("download") + self._patterns("upload"))

    def _listing(self, relative: str) -> list[dict]:
        result = []
        for entry in self.server.locker.list(relative):
            path = entry["path"]
            downloadable = self._permitted(path, "download")
            if not (self._may_browse(path) if entry["is_dir"] else downloadable):
                continue
            result.append({
                **entry,
                "can_download": downloadable,
                "can_upload": self._permitted(path, "upload"),
            })
        return result

    def _write_target(self, relative: str) -> Path:
        relative = relative_path(relative)
        target = self.server.locker.resolve(relative, must_exist=False)
        if not relative or not self._permitted(relative, "upload"):
            raise PermissionError("Uploads are not permitted in this folder")
        # Each newly created parent must also be a permitted upload directory.
        # An upload of Inbox/nested/photo.jpg must not create a private parent
        # as a side effect of a narrow grant for the leaf filename.
        for parent in reversed(target.parents):
            if parent == self.server.locker.root or self.server.locker.root not in parent.parents:
                continue
            parent_relative = parent.relative_to(self.server.locker.root).as_posix()
            self.server.locker.resolve(parent_relative, must_exist=False)
            if not parent.exists() and not self._permitted(parent_relative, "upload"):
                raise PermissionError("Creating this parent folder is not permitted")
        return target

    def _record_received(self, target: Path, *, files: int, size: int) -> None:
        source = self.computer_permission["name"] if self.computer_permission else self.client_address[0]
        try:
            self.server.activity.record("received", source, str(target), files, size)
        except (OSError, ValueError):
            logger.exception("Could not record incoming activity destination=%s", target)

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    def do_GET(self) -> None:  # noqa: N802
        route, query = self._route()
        if route == "/v1/info" and not self.headers.get("X-FastFiles-Code"):
            self._json(
                200,
                {
                    "device_id": self.server.config.device_id,
                    "device_name": self.server.config.device_name,
                    "protocol": PROTOCOL_VERSION,
                },
            )
            return
        if not self._authorized():
            return
        relative = query.get("path", [""])[0]
        try:
            if route == "/v1/info":
                self._json(
                    200,
                    {
                        "device_id": self.server.config.device_id,
                        "device_name": self.server.config.device_name,
                        "protocol": PROTOCOL_VERSION,
                        "locker_path": str(self.server.locker.root),
                        "read_only": not bool(self._patterns("upload")),
                        "computer_name": self.computer_permission["name"] if self.computer_permission else "",
                        "can_download": bool(self._patterns("download")) and bool(self.server.config.allow_patterns),
                        "can_upload": bool(self._patterns("upload")) and bool(self.server.config.allow_patterns),
                        "download_patterns": self._patterns("download"),
                        "upload_patterns": self._patterns("upload"),
                    },
                )
                return
            if route == "/v1/files":
                if not self._may_browse(relative):
                    raise PermissionError("Browsing this folder is not permitted")
                self._json(200, {
                    "path": relative,
                    "items": self._listing(relative),
                    "can_download": self._permitted(relative, "download"),
                    "can_upload": self._permitted(relative, "upload"),
                })
            elif route == "/v1/download":
                if not self._permitted(relative, "download"):
                    raise PermissionError("Downloading this file is not permitted")
                target = self.server.locker.resolve(relative)
                if not target.is_file():
                    self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return
                size = target.stat().st_size
                logger.info(
                    "download accepted client=%s path=%s bytes=%s", self.client_address[0], relative, size
                )
                self.send_response(200)
                self.send_header(
                    "Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream"
                )
                self.send_header("Content-Length", str(size))
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                with target.open("rb") as source:
                    remaining = size
                    while remaining and (chunk := source.read(min(1024 * 1024, remaining))):
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
        except (FileNotFoundError, NotADirectoryError):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except PermissionError:
            self._json(HTTPStatus.FORBIDDEN, {"error": "Path blocked by sharing policy"})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "Could not read the requested path"})

    def do_PUT(self) -> None:  # noqa: N802
        route, query = self._route()
        if route not in ("/v1/upload", "/v1/directory"):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
            return
        if not self._authorized():
            return
        if self.server.config.read_only:
            self._json(HTTPStatus.FORBIDDEN, {"error": "This locker is read-only"})
            return
        relative = query.get("path", [""])[0]
        temporary: Path | None = None
        try:
            length = self.headers.get("Content-Length", "")
            if not length.isascii() or not length.isdigit() or self.headers.get("Transfer-Encoding"):
                raise ValueError("A nonnegative Content-Length is required")
            remaining = int(length)
            if remaining > self.server.config.max_upload_bytes:
                self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "File exceeds max_upload_bytes"})
                return
            target = self._write_target(relative)
            if route == "/v1/directory":
                if remaining:
                    raise ValueError("Directory requests must be empty")
                existed = target.exists()
                target.mkdir(parents=True, exist_ok=True)
                if not existed:
                    self._record_received(target, files=0, size=0)
                self._json(201, {"path": relative})
                return
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=".fastfiles-", dir=target.parent)
            temporary = Path(name)
            digest = hashlib.sha256()
            with os.fdopen(fd, "wb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("Upload ended early")
                    output.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
                output.flush()
                os.fsync(output.fileno())
            self.server.locker.resolve(relative, must_exist=False)
            if query.get("overwrite", ["0"])[0] == "1":
                temporary.replace(target)
            else:
                commit_new_file(temporary, target)
            logger.info(
                "upload stored client=%s path=%s bytes=%s destination=%s",
                self.client_address[0],
                relative,
                target.stat().st_size,
                target,
            )
            self._record_received(target, files=1, size=int(length))
            self._json(201, {"path": relative, "size": int(length), "sha256": digest.hexdigest()})
        except PermissionError:
            self._json(HTTPStatus.FORBIDDEN, {"error": "Path blocked by sharing policy"})
        except FileExistsError:
            self._json(
                HTTPStatus.CONFLICT,
                {"error": "File already exists; enable Replace existing files to overwrite"},
            )
        except (OSError, ValueError, ConnectionError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class Peer:
    device_id: str
    name: str
    address: str
    port: int

    @property
    def base_url(self) -> str:
        host = f"[{self.address}]" if ":" in self.address else self.address
        return f"http://{host}:{self.port}"


def parse_peer_address(value: str, default_port: int = DEFAULT_PORT) -> Peer:
    """Parse a VPN/LAN hostname, IPv4 address, or bracketed IPv6 endpoint."""
    value = value.strip()
    if not value:
        raise ValueError("Enter a peer hostname or IP address")
    try:
        parsed = urllib.parse.urlsplit(value if "://" in value else "//" + value)
        host = parsed.hostname
        port = parsed.port if parsed.port is not None else default_port
    except ValueError as error:
        raise ValueError("Use hostname, hostname:port, or [IPv6]:port") from error
    if (
        not host
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.scheme not in ("", "http")
        or not 1 <= port <= 65535
        or any(c.isspace() or c in "\0\\" for c in host)
    ):
        raise ValueError("Use hostname, hostname:port, or [IPv6]:port")
    if ":" in host:
        ipaddress.IPv6Address(host)
    elif not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", host):
        raise ValueError("Invalid peer hostname")
    return Peer(f"manual:{host}:{port}", host, host, port)


class TransferCancelled(Exception):
    pass


def check_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise TransferCancelled("Transfer cancelled; completed files were kept")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise OSError("Peer redirected the request; connection refused")


class PeerClient:
    def __init__(self, peer: Peer, code: str, timeout: float = 15):
        self.peer = peer
        self.code = code
        self.timeout = timeout
        # Do not send pairing codes through a configured HTTP proxy or redirect.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    def _request(self, route: str, *, method: str = "GET", data: Any = None) -> urllib.request.Request:
        url = self.peer.base_url + route
        return urllib.request.Request(url, method=method, data=data, headers={"X-FastFiles-Code": self.code})

    def _open(self, request):
        try:
            return self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            try:
                detail = json.loads(error.read(4096)).get("error", error.reason)
            except (ValueError, AttributeError):
                detail = error.reason
            finally:
                error.close()
            raise OSError(f"Peer returned HTTP {error.code}: {detail}") from error

    def _json(self, route: str, *, method: str = "GET") -> dict:
        with self._open(
            self._request(route, method=method, data=b"" if method == "PUT" else None)
        ) as response:
            if response.status not in (200, 201):
                raise OSError(f"Unexpected peer response: HTTP {response.status}")
            data = response.read(8 * 1024 * 1024 + 1)
            if len(data) > 8 * 1024 * 1024:
                raise OSError("Peer listing is too large")
            value = json.loads(data)
            if not isinstance(value, dict):
                raise OSError("Invalid peer response")
            return value

    def info(self) -> dict[str, Any]:
        value = self._json("/v1/info")
        for key in ("read_only", "can_download", "can_upload"):
            if key in value and type(value[key]) is not bool:
                raise OSError(f"Invalid peer {key} capability")
        if "computer_name" in value and not isinstance(value["computer_name"], str):
            raise OSError("Invalid peer computer name")
        for key in ("download_patterns", "upload_patterns"):
            if key in value:
                try:
                    validate_patterns(value[key], key)
                except (ValueError, PermissionError) as error:
                    raise OSError(f"Invalid peer {key}") from error
        return value

    def list(self, path: str = "") -> list[dict[str, Any]]:
        path = relative_path(path)
        items = self._json("/v1/files?" + urllib.parse.urlencode({"path": path})).get("items")
        if not isinstance(items, list):
            raise OSError("Invalid peer file listing")
        seen: set[str] = set()
        for entry in items:
            if not isinstance(entry, dict):
                raise OSError("Invalid peer file entry")
            name = entry.get("name")
            if (
                not isinstance(name, str)
                or not name
                or name in (".", "..")
                or any(c in name for c in "/\\\r\n\0")
                or name in seen
                or entry.get("path") != (path + "/" if path else "") + name
                or type(entry.get("is_dir")) is not bool
                or type(entry.get("size")) is not int
                or entry["size"] < 0
                or any(key in entry and type(entry[key]) is not bool for key in ("can_download", "can_upload"))
            ):
                raise OSError("Unsafe or invalid path in peer file listing")
            relative_path(entry["path"])
            seen.add(name)
        return items

    def mkdir(self, path: str) -> None:
        self._json("/v1/directory?" + urllib.parse.urlencode({"path": relative_path(path)}), method="PUT")

    def download(
        self,
        remote_path: str,
        local_path: Path,
        progress: Callable[[int, int], None] | None = None,
        *,
        cancel: threading.Event | None = None,
        overwrite: bool = False,
    ) -> None:
        check_cancelled(cancel)
        logger.info(
            "download starting peer=%s path=%s destination=%s", self.peer.base_url, remote_path, local_path
        )
        route = "/v1/download?" + urllib.parse.urlencode({"path": remote_path})
        temporary: Path | None = None
        if local_path.is_symlink() or (local_path.exists() and not overwrite):
            raise FileExistsError(f"Destination already exists: {local_path}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = self._open(self._request(route))
            with response:
                if response.status != 200 or not response.headers.get("Content-Length", "").isdigit():
                    raise OSError("Invalid download response")
                total = int(response.headers["Content-Length"])
                fd, name = tempfile.mkstemp(prefix=".fastfiles-", dir=local_path.parent)
                temporary = Path(name)
                done = 0
                with os.fdopen(fd, "wb") as output:
                    while chunk := response.read(1024 * 1024):
                        check_cancelled(cancel)
                        output.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                    output.flush()
                    os.fsync(output.fileno())
                check_cancelled(cancel)
                if done != total:
                    raise OSError(f"Incomplete download: expected {total} bytes, received {done}")
                if overwrite:
                    temporary.replace(local_path)
                else:
                    commit_new_file(temporary, local_path)
                if progress:
                    progress(total, total)
                logger.info(
                    "download complete peer=%s path=%s bytes=%s", self.peer.base_url, remote_path, done
                )
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        progress: Callable[[int, int], None] | None = None,
        *,
        cancel: threading.Event | None = None,
        overwrite: bool = False,
    ) -> None:
        """Stream a file to a peer without loading it into memory."""
        check_cancelled(cancel)
        if local_path.is_symlink() or not local_path.is_file():
            raise ValueError("Only regular files can be uploaded")
        route = "/v1/upload?" + urllib.parse.urlencode(
            {"path": relative_path(remote_path), "overwrite": int(overwrite)}
        )
        size = local_path.stat().st_size
        connection = http.client.HTTPConnection(self.peer.address, self.peer.port, timeout=self.timeout)
        logger.info(
            "upload starting peer=%s source=%s path=%s bytes=%s",
            self.peer.base_url,
            local_path,
            remote_path,
            size,
        )
        try:
            connection.putrequest("PUT", route)
            connection.putheader("X-FastFiles-Code", self.code)
            connection.putheader("Content-Length", str(size))
            connection.endheaders()
            sent = 0
            digest = hashlib.sha256()
            with local_path.open("rb") as source:
                while sent < size and (chunk := source.read(min(1024 * 1024, size - sent))):
                    check_cancelled(cancel)
                    connection.send(chunk)
                    digest.update(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, size)
            if sent != size or local_path.stat().st_size != size:
                raise OSError(
                    "Source file changed during the transfer; retry when it is no longer being edited"
                )
            check_cancelled(cancel)
            response = connection.getresponse()
            receipt = json.loads(response.read(65536))
            if response.status != 201:
                raise OSError(
                    f"Peer rejected upload (HTTP {response.status}): {receipt.get('error', 'unknown error')}"
                )
            if receipt.get("size") != size or receipt.get("sha256") != digest.hexdigest():
                raise OSError("Peer did not confirm the uploaded bytes; update both FastFiles installations")
            logger.info(
                "upload complete peer=%s path=%s bytes=%s status=%s",
                self.peer.base_url,
                remote_path,
                sent,
                response.status,
            )
        finally:
            connection.close()


class LockerService:
    def __init__(self, config: LockerConfig, port: int | None = None):
        self.config = config
        server_class = IPv6LockerHTTPServer if ":" in config.bind_address else LockerHTTPServer
        self.server = server_class(
            (config.bind_address, config.port if port is None else port),
            Locker(config.locker_path, config.allow_patterns, config.deny_patterns),
            config,
        )
        if not config.read_only:
            try:
                self.server.locker.resolve("Inbox", must_exist=False).mkdir(exist_ok=True)
            except (OSError, ValueError):
                # Custom policies may deliberately expose no receiving folder.
                pass
        self.port = self.server.server_address[1]
        self._thread: threading.Thread | None = None
        self._zeroconf: Any = None
        self._service_info: Any = None

    def start(self, *, advertise: bool = True) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(
            target=self.server.serve_forever, name="fastfiles-locker", daemon=True
        )
        self._thread.start()
        logger.info(
            "locker listening bind=%s port=%s root=%s read_only=%s",
            self.config.bind_address,
            self.port,
            self.server.locker.root,
            self.config.read_only,
        )
        if not advertise:
            return
        try:
            import ifaddr
            from zeroconf import ServiceInfo, Zeroconf

            self._zeroconf = Zeroconf()
            name = f"{self.config.device_name}-{self.config.device_id[:8]}.{SERVICE_TYPE}"
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                name,
                port=self.port,
                properties={"id": self.config.device_id, "name": self.config.device_name},
                server=f"{socket.gethostname()}.local.",
                addresses=[
                    ipaddress.ip_address(address).packed
                    for address in (
                        [self.config.bind_address]
                        if self.config.bind_address not in ("0.0.0.0", "::")
                        else sorted(
                            {
                                ip.ip
                                for adapter in ifaddr.get_adapters()
                                for ip in adapter.ips
                                if isinstance(ip.ip, str) and not ipaddress.ip_address(ip.ip).is_loopback
                            }
                        )
                    )
                ],
            )
            self._zeroconf.register_service(self._service_info)
        except Exception as error:
            logger.warning("LAN discovery advertisement unavailable: %s", error)
            if self._zeroconf:
                self._zeroconf.close()
            self._zeroconf = None

    def stop(self) -> None:
        if self._zeroconf and self._service_info:
            self._zeroconf.unregister_service(self._service_info)
            self._zeroconf.close()
        if self._thread:
            self.server.shutdown()
        self.server.server_close()
        if self._thread:
            self._thread.join(timeout=2)


class PeerDiscovery:
    def __init__(self, own_id: str, on_change: Callable[[list[Peer]], None]):
        self.own_id = own_id
        self.on_change = on_change
        self.peers: dict[str, Peer] = {}
        from zeroconf import ServiceBrowser, Zeroconf

        self.zeroconf = Zeroconf()
        self.browser = ServiceBrowser(self.zeroconf, SERVICE_TYPE, self)

    def add_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        self.update_service(zeroconf, service_type, name)

    def update_service(self, zeroconf: Any, service_type: str, name: str) -> None:
        info = zeroconf.get_service_info(service_type, name)
        if not info or not info.parsed_addresses():
            return
        props = {
            key.decode(errors="replace"): value.decode(errors="replace")
            for key, value in info.properties.items()
            if isinstance(value, bytes)
        }
        device_id = props.get("id", name)
        if device_id == self.own_id:
            return
        self.peers[name] = Peer(device_id, props.get("name", name), info.parsed_addresses()[0], info.port)
        self.on_change(list(self.peers.values()))

    def remove_service(self, _zeroconf: Any, _service_type: str, name: str) -> None:
        self.peers.pop(name, None)
        self.on_change(list(self.peers.values()))

    def close(self) -> None:
        self.browser.cancel()
        self.zeroconf.close()
