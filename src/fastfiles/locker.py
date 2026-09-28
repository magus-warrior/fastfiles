from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import fnmatch
import logging
import secrets
import socket
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

SERVICE_TYPE = "_fastfiles._tcp.local."
DEFAULT_PORT = 47832
logger = logging.getLogger("fastfiles.locker")


def _config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) / "fastfiles" if base else Path.home() / ".config" / "fastfiles"


@dataclass
class LockerConfig:
    device_id: str
    device_name: str
    locker_path: str
    access_code_hash: str
    access_code: str = ""
    allow_patterns: list[str] = field(default_factory=lambda: ["**"])
    deny_patterns: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> "LockerConfig":
        config_path = path or _config_dir() / "config.json"
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            return cls(**data)
        except (FileNotFoundError, OSError, ValueError, TypeError):
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

    def save(self, path: Path | None = None) -> None:
        config_path = path or _config_dir() / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        # The code is needed locally so it can be displayed. File permissions keep
        # it private to the current account.
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        try:
            config_path.chmod(0o600)
        except OSError:
            pass


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
        self.allow_patterns = allow_patterns or ["**"]
        self.deny_patterns = deny_patterns or []
        self.root.mkdir(parents=True, exist_ok=True)

    def is_allowed(self, relative: str, *, is_dir: bool = False) -> bool:
        normalized = relative.replace("\\", "/").strip("/")
        if normalized in ("", "."):
            return True
        candidates = [normalized, normalized + "/"] if is_dir else [normalized]
        denied = any(fnmatch.fnmatchcase(value, pattern) for value in candidates for pattern in self.deny_patterns)
        if denied:
            return False
        return any(fnmatch.fnmatchcase(value, pattern) for value in candidates for pattern in self.allow_patterns)

    def resolve(self, relative: str, *, must_exist: bool = True) -> Path:
        relative = urllib.parse.unquote(relative).replace("\\", "/").lstrip("/")
        candidate = (self.root / relative).resolve()
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
            if not self.is_allowed(relative_path, is_dir=entry.is_dir()):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            result.append({
                "name": entry.name,
                "path": relative_path,
                "is_dir": entry.is_dir(),
                "size": 0 if entry.is_dir() else stat.st_size,
                "modified": int(stat.st_mtime),
            })
        return result


class LockerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], locker: Locker, config: LockerConfig):
        super().__init__(address, LockerRequestHandler)
        self.locker = locker
        self.config = config


class LockerRequestHandler(BaseHTTPRequestHandler):
    server: LockerHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *args: object) -> None:
        return

    def _json(self, status: int, value: Any) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        code = self.headers.get("X-FastFiles-Code", "")
        return hmac.compare_digest(hash_access_code(code), self.server.config.access_code_hash)

    def _route(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

    def do_GET(self) -> None:  # noqa: N802
        route, query = self._route()
        if route == "/v1/info":
            self._json(200, {
                "device_id": self.server.config.device_id,
                "device_name": self.server.config.device_name,
                "protocol": 1,
            })
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Pairing code required"})
            return
        relative = query.get("path", [""])[0]
        try:
            target = self.server.locker.resolve(relative)
            if route == "/v1/files":
                self._json(200, {"path": relative, "items": self.server.locker.list(relative)})
            elif route == "/v1/download" and target.is_file():
                size = target.stat().st_size
                logger.info("download accepted client=%s path=%s bytes=%s", self.client_address[0], relative, size)
                self.send_response(200)
                self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
                self.send_header("Content-Length", str(size))
                self.end_headers()
                with target.open("rb") as source:
                    while chunk := source.read(1024 * 1024):
                        self.wfile.write(chunk)
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
        except (FileNotFoundError, NotADirectoryError):
            self._json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
        except PermissionError:
            self._json(HTTPStatus.FORBIDDEN, {"error": "Outside locker"})
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_PUT(self) -> None:  # noqa: N802
        route, query = self._route()
        if route != "/v1/upload":
            self._json(HTTPStatus.NOT_FOUND, {"error": "Unknown endpoint"})
            return
        if not self._authorized():
            self._json(HTTPStatus.UNAUTHORIZED, {"error": "Pairing code required"})
            return
        relative = query.get("path", [""])[0]
        try:
            target = self.server.locker.resolve(relative, must_exist=False)
            target.parent.mkdir(parents=True, exist_ok=True)
            remaining = int(self.headers.get("Content-Length", "0"))
            temporary = target.with_name(target.name + ".fastfiles-upload")
            with temporary.open("wb") as output:
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("Upload ended early")
                    output.write(chunk)
                    remaining -= len(chunk)
            temporary.replace(target)
            logger.info(
                "upload stored client=%s path=%s bytes=%s destination=%s",
                self.client_address[0], relative, target.stat().st_size, target,
            )
            self._json(201, {"path": relative, "size": target.stat().st_size})
        except PermissionError:
            self._json(HTTPStatus.FORBIDDEN, {"error": "Outside locker"})
        except (OSError, ValueError, ConnectionError) as error:
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(error)})


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
    if "://" in value:
        parsed = urllib.parse.urlsplit(value)
    else:
        parsed = urllib.parse.urlsplit("//" + value)
    try:
        host = parsed.hostname
        port = parsed.port or default_port
    except ValueError as error:
        raise ValueError("Use hostname, hostname:port, or [IPv6]:port") from error
    if not host or parsed.path not in ("", "/"):
        raise ValueError("Use hostname, hostname:port, or [IPv6]:port")
    return Peer(f"manual:{host}:{port}", host, host, port)


class PeerClient:
    def __init__(self, peer: Peer, code: str, timeout: float = 15):
        self.peer = peer
        self.code = code
        self.timeout = timeout

    def _request(self, route: str, *, method: str = "GET", data: Any = None) -> urllib.request.Request:
        url = self.peer.base_url + route
        return urllib.request.Request(url, method=method, data=data, headers={"X-FastFiles-Code": self.code})

    def info(self) -> dict[str, Any]:
        with urllib.request.urlopen(self.peer.base_url + "/v1/info", timeout=self.timeout) as response:
            return json.load(response)

    def list(self, path: str = "") -> list[dict[str, Any]]:
        route = "/v1/files?" + urllib.parse.urlencode({"path": path})
        with urllib.request.urlopen(self._request(route), timeout=self.timeout) as response:
            return json.load(response)["items"]

    def download(self, remote_path: str, local_path: Path, progress: Callable[[int, int], None] | None = None) -> None:
        logger.info("download starting peer=%s path=%s destination=%s", self.peer.base_url, remote_path, local_path)
        route = "/v1/download?" + urllib.parse.urlencode({"path": remote_path})
        with urllib.request.urlopen(self._request(route), timeout=self.timeout) as response:
            total = int(response.headers.get("Content-Length", "0"))
            done = 0
            temporary = local_path.with_name(local_path.name + ".fastfiles-download")
            with temporary.open("wb") as output:
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            temporary.replace(local_path)
            logger.info("download complete peer=%s path=%s bytes=%s", self.peer.base_url, remote_path, done)

    def upload(
        self,
        local_path: Path,
        remote_path: str,
        progress: Callable[[int, int], None] | None = None,
    ) -> None:
        """Stream a file to a peer without loading it into memory."""
        route = "/v1/upload?" + urllib.parse.urlencode({"path": remote_path})
        size = local_path.stat().st_size
        connection = http.client.HTTPConnection(self.peer.address, self.peer.port, timeout=self.timeout)
        logger.info("upload starting peer=%s source=%s path=%s bytes=%s", self.peer.base_url, local_path, remote_path, size)
        try:
            connection.putrequest("PUT", route)
            connection.putheader("X-FastFiles-Code", self.code)
            connection.putheader("Content-Length", str(size))
            connection.endheaders()
            sent = 0
            with local_path.open("rb") as source:
                while chunk := source.read(1024 * 1024):
                    connection.send(chunk)
                    sent += len(chunk)
                    if progress:
                        progress(sent, size)
            response = connection.getresponse()
            response.read()
            if response.status >= 400:
                raise OSError(f"Peer rejected upload (HTTP {response.status})")
            logger.info("upload complete peer=%s path=%s bytes=%s status=%s", self.peer.base_url, remote_path, sent, response.status)
        finally:
            connection.close()


class LockerService:
    def __init__(self, config: LockerConfig, port: int = DEFAULT_PORT):
        self.config = config
        self.server = LockerHTTPServer(
            ("", port),
            Locker(config.locker_path, config.allow_patterns, config.deny_patterns),
            config,
        )
        self.port = self.server.server_address[1]
        self._thread: threading.Thread | None = None
        self._zeroconf: Any = None
        self._service_info: Any = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self.server.serve_forever, name="fastfiles-locker", daemon=True)
        self._thread.start()
        try:
            from zeroconf import ServiceInfo, Zeroconf
            self._zeroconf = Zeroconf()
            name = f"{self.config.device_name}-{self.config.device_id[:8]}.{SERVICE_TYPE}"
            self._service_info = ServiceInfo(
                SERVICE_TYPE,
                name,
                port=self.port,
                properties={"id": self.config.device_id, "name": self.config.device_name},
                server=f"{socket.gethostname()}.local.",
            )
            self._zeroconf.register_service(self._service_info)
        except Exception:
            self._zeroconf = None

    def stop(self) -> None:
        if self._zeroconf and self._service_info:
            self._zeroconf.unregister_service(self._service_info)
            self._zeroconf.close()
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
        props = {key.decode(): value.decode() for key, value in info.properties.items()}
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
