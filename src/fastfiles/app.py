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
    parser = argparse.ArgumentParser(description="Friendly rsync-over-SSH transfers")
    parser.add_argument("--check", action="store_true", help="check required command-line tools")
    parser.add_argument("--serve", action="store_true", help="run the LAN locker service without the GUI")
    parser.add_argument("--port", type=int, help="override the configured locker port (default: 47832)")
    parser.add_argument("--bind", help="override the configured listening IP (e.g. a VPN address)")
    parser.add_argument("--config", type=Path, help="path to the sharing configuration JSON")
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
    args = parser.parse_args(argv)
    if args.check:
        return check_environment()
    from .locker import LockerConfig, LockerService
    from .storage import config_dir

    config_path = args.config or config_dir() / "config.json"
    try:
        if args.check_config and not config_path.is_file():
            raise ValueError(f"Configuration does not exist: {config_path}. Run --init-config first.")
        config = LockerConfig.load(config_path)
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
        print(f"Pairing code: {config.access_code}")
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
        window = MainWindow(config)
    except (OSError, ValueError) as error:
        logging.getLogger("fastfiles").exception("Could not create the desktop window")
        print(f"FastFiles could not start: {error}", file=sys.stderr)
        return 2
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
