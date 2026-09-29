from __future__ import annotations

import argparse
import logging
import shutil
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path

from .logging_config import configure_logging


def check_environment() -> int:
    required = ("ssh", "rsync", "cygpath") if sys.platform == "win32" else ("ssh", "rsync")
    missing = [tool for tool in required if shutil.which(tool) is None]
    if missing:
        print("Missing required tools: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(f"ssh: {shutil.which('ssh')}")
    print(f"rsync: {shutil.which('rsync')}")
    print("Direct SSH command-line tools are ready (GUI dependencies are not checked).")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Share a Locker or transfer files over SSH")
    parser.add_argument("--check", action="store_true", help="check required command-line tools")
    parser.add_argument("--serve", action="store_true", help="run the LAN locker service without the GUI")
    parser.add_argument("--port", type=int, help="override the configured locker port (default: 47832)")
    parser.add_argument("--bind", help="override the configured listening IP (e.g. a VPN address)")
    parser.add_argument("--config", type=Path, help="path to the sharing configuration JSON")
    parser.add_argument("--activity", action="store_true", help="show recent local transfer activity without the GUI")
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="create a sharing config if missing, without starting a server",
    )
    parser.add_argument(
        "--check-config", action="store_true", help="validate an existing sharing config without modifying it"
    )
    parser.add_argument(
        "--no-discovery", action="store_true", help="disable LAN advertisement in headless mode"
    )
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--grant-computer", metavar="NAME", help="create a private access key for a computer")
    access.add_argument("--revoke-computer", metavar="NAME", help="revoke a computer's access key")
    access.add_argument("--list-computers", action="store_true", help="list computer folder permissions")
    parser.add_argument("--download", action="append", default=[], metavar="PATTERN",
                        help="download path pattern for --grant-computer; repeat for multiple folders")
    parser.add_argument("--upload", action="append", default=[], metavar="PATTERN",
                        help="upload path pattern for --grant-computer; repeat for multiple folders")
    args = parser.parse_args(argv)
    managing_access = args.grant_computer is not None or args.revoke_computer is not None or args.list_computers
    if managing_access and (args.serve or args.check or args.init_config or args.check_config):
        parser.error("Manage computer access separately from startup and configuration checks")
    if args.activity and (managing_access or args.serve or args.check or args.init_config or args.check_config):
        parser.error("View activity separately from startup and configuration changes")
    if (args.download or args.upload) and args.grant_computer is None:
        parser.error("--download and --upload require --grant-computer")
    if args.grant_computer is not None and not (args.download or args.upload):
        parser.error("Grant at least one --download or --upload pattern, for example --upload 'Inbox/**'")
    if args.check:
        return check_environment()
    from .locker import LockerConfig, LockerService
    from .storage import config_dir

    config_path = args.config or config_dir() / "config.json"
    try:
        if (args.activity or args.check_config or args.list_computers or args.revoke_computer is not None) and not config_path.is_file():
            raise ValueError(f"Configuration does not exist: {config_path}. Run --init-config first.")
        config = LockerConfig.load(config_path)
        if args.activity:
            from .activity import ActivityStore

            receipts = ActivityStore(config.locker_path).recent()
            if not receipts:
                print("No transfer activity recorded.")
            for receipt in receipts:
                print(f"{receipt['time']} | {receipt['kind']} | {receipt['source']} -> {receipt['destination']}"
                      f" | {receipt['files']} files, {receipt['bytes']} bytes")
            return 0
        if managing_access:
            from .access import new_computer_permission

            if args.list_computers:
                mode = "computer keys" if config.uses_computer_keys else "shared pairing code"
                print(f"Access mode: {mode}")
                for entry in config.computer_permissions:
                    print(f"{entry['name']}: download={entry['download_patterns']}; upload={entry['upload_patterns']}")
                if not config.computer_permissions:
                    print("No computer access keys configured.")
                return 0
            name = (args.grant_computer if args.grant_computer is not None else args.revoke_computer).strip()
            existing = next((entry for entry in config.computer_permissions if entry["name"].casefold() == name.casefold()), None)
            if args.grant_computer is not None:
                if existing:
                    raise ValueError(f"Computer {name!r} already exists; revoke its old key before granting a new one")
                entry, token = new_computer_permission(name, args.download, args.upload)
                config.save_computer_permissions([*config.computer_permissions, entry], config_path)
                print(f"Access granted to {name}. Computer key (shown once): {token}")
                print("Use this key in the other computer's Access key / pairing code field.")
                print("Shared pairing-code access is disabled. Global sharing rules still apply.")
            else:
                if existing is None:
                    raise ValueError(f"No computer named {name!r}")
                config.save_computer_permissions(
                    [entry for entry in config.computer_permissions if entry is not existing], config_path
                )
                print(f"Access revoked for {name}. Shared pairing-code access remains disabled.")
            print("Restart the Locker service or desktop app to apply access changes.")
            return 0
        if args.port is not None:
            config = replace(config, port=args.port)
        if args.bind:
            config = replace(config, bind_address=args.bind)
        if args.init_config or args.check_config:
            print(f"Sharing configuration valid: {config_path}")
            print(f"Locker: {config.locker_path}; read-only: {config.read_only}")
            return 0
        current_log = configure_logging()
    except (OSError, ValueError) as error:
        print(f"FastFiles could not start: {error}", file=sys.stderr)
        return 2
    if args.serve:
        try:
            service = LockerService(config)
            service.start(advertise=not args.no_discovery)
        except OSError as error:
            print(f"Could not start Locker: {error}", file=sys.stderr)
            return 2
        print(f"FastFiles locker: {config.locker_path}")
        print(f"Device: {config.device_name}")
        if config.uses_computer_keys:
            print(f"Access: computer keys ({len(config.computer_permissions)} configured); shared pairing code disabled")
        else:
            print(f"Pairing code: {config.access_code}")
        print(f"Receiving: {'disabled (read-only)' if config.read_only else str(service.server.locker.root)}")
        inbox = service.server.locker.root / "Inbox"
        if not config.read_only and inbox.is_dir() and service.server.locker.is_allowed("Inbox"):
            print(f"Inbox: {inbox} (computer folder permissions apply)")
        print("Keep this service running to receive files. View transfers with --activity or in the desktop app.")
        print(f"Listening on port {service.port}; press Ctrl+C to stop")
        print(f"Log: {current_log}", flush=True)
        stopped = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        try:
            while not stopped.wait(0.5):
                pass
        finally:
            service.stop()
        return 0

    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
        from qt_material import apply_stylesheet
    except ImportError:
        print(
            "Desktop dependencies are missing. Run python -m pip install -e '.[desktop]'.",
            file=sys.stderr,
        )
        return 2

    from .ui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("FastFiles")
    app.setDesktopFileName("fastfiles")
    app.setWindowIcon(QIcon.fromTheme("folder-remote"))
    app.setOrganizationName("FastFiles")
    apply_stylesheet(
        app,
        theme="light_blue.xml",
        invert_secondary=True,
        extra={
            "density_scale": "0",
            "font_family": "Noto Sans",
        },
    )
    try:
        window = MainWindow(config, config_path=config_path)
    except (OSError, ValueError) as error:
        logging.getLogger("fastfiles").exception("Could not create the desktop window")
        print(f"FastFiles could not start: {error}", file=sys.stderr)
        return 2
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
