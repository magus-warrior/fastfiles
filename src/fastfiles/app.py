from __future__ import annotations

import argparse
import shutil
import signal
import sys
import threading

from .logging_config import configure_logging


def check_environment() -> int:
    missing = [tool for tool in ("ssh", "rsync") if shutil.which(tool) is None]
    if missing:
        print("Missing required tools: " + ", ".join(missing), file=sys.stderr)
        return 1
    print(f"ssh: {shutil.which('ssh')}")
    print(f"rsync: {shutil.which('rsync')}")
    print("FastFiles environment looks ready.")
    return 0


def main(argv: list[str] | None = None) -> int:
    current_log = configure_logging()
    parser = argparse.ArgumentParser(description="Friendly rsync-over-SSH transfers")
    parser.add_argument("--check", action="store_true", help="check required command-line tools")
    parser.add_argument("--serve", action="store_true", help="run the LAN locker service without the GUI")
    parser.add_argument("--port", type=int, default=47832, help="locker service port (default: 47832)")
    args = parser.parse_args(argv)
    if args.check:
        return check_environment()
    if args.serve:
        from .locker import LockerConfig, LockerService
        config = LockerConfig.load()
        service = LockerService(config, args.port)
        service.start()
        print(f"FastFiles locker: {config.locker_path}")
        print(f"Device: {config.device_name}")
        print(f"Pairing code: {config.access_code}")
        print(f"Listening on port {service.port}; press Ctrl+C to stop")
        print(f"Log: {current_log}")
        stopped = threading.Event()
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        signal.signal(signal.SIGTERM, lambda *_: stopped.set())
        stopped.wait()
        service.stop()
        return 0

    try:
        from PySide6.QtWidgets import QApplication
        from qt_material import apply_stylesheet
    except ImportError:
        print("PySide6 is not installed. Run: pip install -e .", file=sys.stderr)
        return 2

    from .ui import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("FastFiles")
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
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
