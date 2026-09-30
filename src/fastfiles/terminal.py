"""Interactive, Qt-free sharing and transfer workflow for ordinary terminals."""
from __future__ import annotations

import getpass
import posixpath
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

from .access import new_computer_permission
from .activity import ActivityStore
from .background import BackgroundService
from .core import Direction, TransferRequest, build_rsync_command
from .locker import (
    PROTOCOL_VERSION,
    Locker,
    LockerConfig,
    LockerService,
    Peer,
    PeerClient,
    PeerDiscovery,
    parse_peer_address,
)
from .profiles import PeerProfile, ProfileStore, SecretStore
from .transfers import copy_many_from_peer, import_to_locker, upload_paths_to_peer


def clean(value: object) -> str:
    """Do not interpret control sequences in peer names, filenames or errors."""
    return "".join(c if c.isprintable() else "?" for c in str(value))


def ask(label: str, default: str = "") -> str:
    suffix = f" [{clean(default)}]" if default else ""
    return input(f"{label}{suffix}: ").strip() or default


def yes(label: str) -> bool:
    return ask(label + " (y/N)").casefold() in {"y", "yes"}


def paths() -> list[str]:
    print("Enter one file or folder path per line (spaces are fine, no quotes needed).")
    print("An empty line finishes the selection.")
    selected = []
    while value := ask("Path"):
        path = Path(value).expanduser().absolute()
        if not path.exists():
            print("That path does not exist. Try again.")
            continue
        selected.append(str(path))
    if not selected:
        raise ValueError("No files or folders selected")
    return selected


def browse(listing, *, select: bool = False) -> list[dict]:
    directory = ""
    while True:
        entries = listing(directory)
        print(f"\nFolder: /{clean(directory)}")
        for index, entry in enumerate(entries, 1):
            detail = "folder" if entry["is_dir"] else f"{entry['size']:,} bytes"
            print(f"  {index:>3}  {clean(entry['name'])}{'/' if entry['is_dir'] else ''}  ({detail})")
        if not entries:
            print("  (empty)")
        print("Enter a folder number to open it; .. goes up; q returns.")
        if select:
            print("To receive items: s 1,2,3 selects numbers; a selects everything in this folder.")
        choice = ask("Browse", "q").casefold()
        if choice == "q":
            return []
        if choice == "..":
            directory = posixpath.dirname(directory)
            continue
        try:
            if select and choice == "a":
                return entries
            if select and choice.startswith("s "):
                numbers = [int(value.strip()) for value in choice[2:].split(",")]
                if not numbers or any(n < 1 or n > len(entries) for n in numbers):
                    raise ValueError
                return [entries[n - 1] for n in dict.fromkeys(numbers)]
            index = int(choice)
            if not 1 <= index <= len(entries):
                raise ValueError
            entry = entries[index - 1]
            if entry["is_dir"]:
                directory = entry["path"]
            else:
                print(f"File: {clean(entry['path'])} ({entry['size']:,} bytes)")
        except ValueError:
            print("Choose one of the displayed item numbers.")


class TerminalMenu:
    def __init__(self, config: LockerConfig, config_path: Path, *, advertise: bool = True):
        self.config = config
        self.config_path = config_path
        self.advertise = advertise
        self.service: LockerService | None = None
        self.discovery: PeerDiscovery | None = None
        self.peers = []
        self.client: PeerClient | None = None
        self.last_progress = 0.0
        self.background = BackgroundService(config_path)
        self.profiles = ProfileStore(config_path.parent / "connections.json")
        self.secrets = SecretStore()

    def locker(self) -> Locker:
        return Locker(self.config.locker_path, self.config.allow_patterns, self.config.deny_patterns)

    def start(self) -> None:
        if self.background.active():
            print("Always-on sharing is already running. Manage it under Always-on sharing.")
            return
        if self.service:
            print("Sharing is already running.")
            return
        service = LockerService(self.config)
        try:
            service.start(advertise=self.advertise)
        except BaseException:
            service.stop()
            raise
        self.service = service
        print(f"Sharing started on {self.config.bind_address}:{service.port}.")
        print("Keep this menu open to let other computers access your Locker.")
        self.details()

    def stop(self) -> None:
        if self.service:
            self.service.stop()
            self.service = None
            print("Sharing stopped.")

    def details(self) -> None:
        print(f"Device: {clean(self.config.device_name)}\nShared folder: {clean(self.config.locker_path)}")
        print(f"Listen address: {self.config.bind_address}; port: "
              f"{self.service.port if self.service else self.config.port}")
        print("Use this computer's LAN/VPN IP or hostname on the other computer.")
        if self.config.uses_computer_keys:
            print(f"Computer keys enabled ({len(self.config.computer_permissions)} grants).")
        else:
            print(f"Pairing code: {self.config.access_code or '(not stored; use computer keys)'}")
        print(f"Allow: {clean(self.config.allow_patterns)}; deny: {clean(self.config.deny_patterns)}")
        print(f"Uploads: {'disabled' if self.config.read_only else 'enabled'}")

    def connect(self) -> None:
        if self.advertise and self.discovery is None:
            try:
                self.discovery = PeerDiscovery(self.config.device_id, self.update_peers)
            except OSError as error:
                print(f"Discovery unavailable: {clean(error)}. You can enter an address.")
        saved = self.profiles.peers()
        peers = [Peer(f"saved:{entry.name}", entry.name, entry.address, entry.port) for entry in saved]
        endpoints = {(peer.address, peer.port) for peer in peers}
        peers += [peer for peer in self.peers if (peer.address, peer.port) not in endpoints]
        print("\nSaved and nearby computers (choose Connect again to refresh):")
        for number, peer in enumerate(peers, 1):
            suffix = " · saved" if peer.device_id.startswith("saved:") else ""
            print(f"  {number}. {clean(peer.name)}{suffix}")
        if not peers:
            print("  None yet. Enter an address once; the computer and access code will be saved together.")
        address = ask("Computer number or hostname/IP[:port] (blank cancels)")
        if not address:
            return
        peer = peers[int(address) - 1] if address.isdigit() and 1 <= int(address) <= len(peers) else parse_peer_address(address)
        profile = next((entry for entry in saved if (entry.address, entry.port) == (peer.address, peer.port)), None)
        secret_id = f"peer:{peer.address}:{peer.port}"
        code = self.secrets.get(secret_id)
        if not code:
            code = getpass.getpass("Access key / pairing code (hidden; saved for next time): ").strip()
        client = PeerClient(peer, code)
        try:
            info = client.info()
        except OSError:
            if not self.secrets.get(secret_id):
                raise
            print("Saved access did not connect. Enter a new code if it changed, or leave blank to cancel.")
            code = getpass.getpass("New access key / pairing code (hidden): ").strip()
            if not code:
                return
            client = PeerClient(peer, code)
            info = client.info()
        if info.get("protocol") != PROTOCOL_VERSION:
            raise ValueError("Update FastFiles on both computers to use the same protocol")
        if profile and profile.device_id and info.get("device_id") != profile.device_id:
            raise ValueError("This address belongs to a different computer; the saved connection was left unchanged")
        if profile is None:
            reported = info.get("device_name")
            base = reported.strip() if isinstance(reported, str) and reported.strip() else peer.name
            name, number = base, 2
            while any(entry.name.casefold() == name.casefold() for entry in saved):
                name = f"{base} ({number})"
                number += 1
            profile = PeerProfile(name, peer.address, peer.port, info.get("device_id", ""))
        self.secrets.set(secret_id, code)
        self.profiles.save_peer(profile)
        client.peer = Peer(f"saved:{profile.name}", profile.name, peer.address, peer.port)
        self.client = client
        print(f"Connected to {clean(profile.name)}. Computer and access saved for next time.")

    def update_peers(self, peers) -> None:
        self.peers = peers

    def remote(self) -> PeerClient:
        if self.client is None:
            self.connect()
        if self.client is None:
            raise ValueError("No computer selected")
        return self.client

    def progress(self, done: int, total: int) -> None:
        now = time.monotonic()
        if done != total and now - self.last_progress < 0.2:
            return
        self.last_progress = now
        percent = min(100, done * 100 // total) if total else 100
        filled = percent // 5
        print(f"\r  [{'#' * filled}{'-' * (20 - filled)}] {percent:3}%  {done:,} / {total:,} bytes",
              end="", flush=True)

    def transfer(self, operation, *args, overwrite: bool = False) -> None:
        print("Copying… Ctrl+C cancels; completed files are kept.")
        cancel = threading.Event()
        try:
            result = operation(*args, self.progress, cancel, overwrite=overwrite)
        except KeyboardInterrupt:
            cancel.set()
            print("\nCopy cancelled; completed files were kept.")
            return
        finally:
            print()
        print(f"Done: {result.files} files, {result.directories} folders, {result.bytes:,} bytes.")
        print(f"Destination: {clean(result.destination)}")

    def add(self) -> None:
        sources = paths()
        directory = ask("Destination inside your Locker (blank = root)")
        overwrite = yes("Replace existing files")
        self.transfer(import_to_locker, self.locker(), sources, directory, overwrite=overwrite)
        if not self.service:
            print("Files are ready. Choose Start sharing to make them available.")

    def send(self) -> None:
        client = self.remote()
        sources = paths()
        directory = ask("Destination folder inside the other Locker", "Inbox")
        overwrite = yes("Replace existing remote files")
        self.transfer(upload_paths_to_peer, client, sources, directory, overwrite=overwrite)

    def receive(self) -> None:
        client = self.remote()
        selected = browse(client.list, select=True)
        if not selected:
            return
        destination = Path(ask("Local destination folder", self.config.locker_path)).expanduser().absolute()
        overwrite = yes("Replace existing local files")
        self.transfer(copy_many_from_peer, Locker(destination), client,
                      [(entry["path"], entry["is_dir"]) for entry in selected], "", overwrite=overwrite)

    def settings(self) -> None:
        print("\nSharing settings — Enter keeps the current value. Changes stop sharing; start it again when ready.")
        name = ask("Device name", self.config.device_name)
        root = str(Path(ask("Shared folder", self.config.locker_path)).expanduser().absolute())
        if not Path(root).is_dir():
            raise ValueError("Choose an existing folder")
        bind = ask("Listen IP (0.0.0.0 = all IPv4 interfaces)", self.config.bind_address)
        port = int(ask("Port (0 = automatic)", str(self.config.port)))
        mode = ask("Uploads: on / off", "off" if self.config.read_only else "on").casefold()
        if mode not in {"on", "off"}:
            raise ValueError("Uploads must be on or off")
        print("Rules are relative to the shared folder. ** = everything; Photos/** = a folder tree.")
        print("Separate patterns with commas; - means no patterns.")
        allow = ask("Allow patterns", ",".join(self.config.allow_patterns) or "-")
        deny = ask("Deny patterns", ",".join(self.config.deny_patterns) or "-")
        config = replace(self.config, device_name=name, locker_path=root, bind_address=bind, port=port,
                         read_only=mode == "off", allow_patterns=self.patterns(allow), deny_patterns=self.patterns(deny))
        background_active = self.background.active()
        config.save(self.config_path)
        self.stop()
        self.config = config
        if background_active:
            self.background.command("stop", self.background.name)
        print("Settings saved. Sharing stopped; choose Start sharing or enable Always-on sharing when ready.")

    @staticmethod
    def patterns(value: str) -> list[str]:
        return [] if value in {"", "-"} else [part.strip() for part in value.split(",") if part.strip()]

    def access(self) -> None:
        for entry in self.config.computer_permissions:
            print(f"{clean(entry['name'])}: download={clean(entry['download_patterns'])}; "
                  f"upload={clean(entry['upload_patterns'])}")
        choice = ask("Access: g = grant, r = revoke, Enter = back").casefold()
        if choice not in {"g", "r"}:
            return
        name = ask("Computer name")
        entries = self.config.computer_permissions
        existing = next((entry for entry in entries if entry["name"].casefold() == name.casefold()), None)
        if choice == "g":
            if existing:
                raise ValueError("That computer already has a key; revoke it before issuing a new one")
            print("Patterns: ** = everything, Photos/** = folder tree; comma-separated; blank = no access.")
            download = self.patterns(ask("Download patterns"))
            upload = self.patterns(ask("Upload patterns"))
            entry, token = new_computer_permission(name, download, upload)
            self.config.save_computer_permissions([*entries, entry], self.config_path)
            print(f"Computer key (shown once): {token}")
        else:
            if existing is None:
                raise ValueError("No computer with that name")
            self.config.save_computer_permissions([entry for entry in entries if entry is not existing], self.config_path)
            print("Key revoked. Already-authorized requests may finish.")
        if self.background.active():
            self.background.restart()
        print("Access updated. Shared pairing-code access is disabled, even if no keys remain.")

    def activity(self) -> None:
        receipts = ActivityStore(self.config.locker_path).recent()
        if not receipts:
            print("No transfer activity recorded.")
        for receipt in receipts:
            print(clean(f"{receipt['time']} | {receipt['kind']} | {receipt['source']} -> "
                        f"{receipt['destination']} | {receipt['files']} files, {receipt['bytes']} bytes"))

    def ssh(self) -> None:
        from .app import check_environment

        if check_environment():
            return
        direction = Direction(ask("Direction: send / receive", "send"))
        host = ask("SSH host (user@hostname or configured alias)")
        remote_path = ask("Remote folder" if direction is Direction.SEND else "Remote file or folder")
        local = paths() if direction is Direction.SEND else [str(Path(ask("Local destination folder")).expanduser().absolute())]
        dry_run = yes("Preview only (dry run)")
        command = build_rsync_command(TransferRequest(direction, host, remote_path, tuple(local), dry_run=dry_run))
        print("SSH uses your configured keys. Existing destination files may be updated.")
        if not yes("Start transfer"):
            return
        process = subprocess.Popen(command)
        try:
            code = process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            print("SSH transfer cancelled.")
            return
        print("SSH transfer completed." if code == 0 else f"SSH transfer failed (exit {code}).")

    def always_on(self) -> None:
        if not self.background.supported:
            print("Always-on sharing currently requires Linux with systemd.")
            return
        print(self.background.status() or "Always-on sharing has not been set up.")
        print("  1  Enable: keep sharing after closing the menu; start at login")
        print("  2  Also start at boot, before login (requires sudo)")
        print("  3  Disable always-on sharing and stop its server")
        print("  0  Back")
        choice = ask("Choose", "0")
        if choice == "1":
            self.stop()
            self.config.save(self.config_path)
            self.background.enable(advertise=self.advertise)
            print("Always-on sharing enabled. It keeps running after you close this terminal.")
            print(self.background.status())
        elif choice == "2":
            if not self.background.path.is_file():
                raise ValueError("Enable always-on sharing first")
            self.background.enable_at_boot()
            print("Startup before login enabled for your account. Sharing also survives logout.")
        elif choice == "3":
            self.background.disable()
            print("Always-on sharing disabled and stopped.")

    def run(self) -> int:
        print("\nFastFiles · Terminal\nShare files and folders with another computer.")
        print("Locker uses unencrypted HTTP: use a trusted LAN or encrypted VPN.")
        actions = {"1": self.start, "2": self.add, "3": lambda: browse(self.locker().list),
                   "4": self.connect, "5": self.send, "6": self.receive, "7": self.details,
                   "8": self.settings, "9": self.access, "10": self.activity, "11": self.ssh, "12": self.stop, "13": self.always_on}
        try:
            while True:
                state = f"SHARING · port {self.service.port}" if self.service else "Sharing stopped"
                if self.background.active():
                    state = "ALWAYS ON · continues after exit"
                peer = clean(self.client.peer.name) if self.client else "none"
                print(f"\n{'─' * 56}\n  FASTFILES  |  {state}\n  Computer: {peer}\n{'─' * 56}")
                print("  1  Start sharing           7  Connection details / code\n"
                      "  2  Add files / folders     8  Sharing settings\n"
                      "  3  Browse my Locker        9  Computer access keys\n"
                      "  4  Connect to a computer  10  Transfer history\n"
                      "  5  Send files / folders   11  Direct SSH transfer\n"
                      "  6  Browse / receive       12  Stop sharing\n"
                      " 13  Always-on sharing / startup\n"
                      "  0  Quit")
                try:
                    choice = ask("Choose", "0").casefold()
                    if choice in {"0", "q", "quit"}:
                        break
                    action = actions.get(choice)
                    if action:
                        action()
                    else:
                        print("Choose a number from 0 to 13.")
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    print(f"\nCould not complete that action: {clean(error)}")
                except KeyboardInterrupt:
                    print("\nCancelled. Back to the menu; choose 0 to quit.")
        except EOFError:
            print("\nInput closed.")
        finally:
            self.stop()
            if self.discovery:
                self.discovery.close()
        print("Goodbye.")
        return 0


def run_menu(config: LockerConfig, config_path: Path, *, advertise: bool = True) -> int:
    if sys.stdin is not None and sys.stdin.isatty():
        try:
            import readline  # noqa: F401 — enable normal line editing where available
        except ImportError:
            pass
    return TerminalMenu(config, config_path, advertise=advertise).run()
