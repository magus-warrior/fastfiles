"""Register this checkout in the current user's Linux application launcher."""

import os
import shutil
import subprocess
from pathlib import Path


def desktop_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def main() -> None:
    checkout = Path(__file__).resolve().parent.parent
    launcher = checkout / "start.sh"
    # Exec has its own quoting rules, applied before desktop-entry escaping.
    command = str(launcher).replace("%", "%%")
    for character in ("\\", '"', "`", "$"):
        command = command.replace(character, "\\" + character)
    quoted_command = desktop_value(f'"{command}"')
    data_home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    if not data_home.is_absolute():
        data_home = Path.home() / ".local/share"
    applications = data_home / "applications"
    applications.mkdir(parents=True, exist_ok=True)
    destination = applications / "fastfiles.desktop"
    destination.write_text(
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=FastFiles\n"
        "Comment=Send and receive files over your network\n"
        f"Exec={quoted_command}\n"
        "Icon=folder-remote\n"
        "Terminal=false\n"
        "Categories=Network;FileTransfer;\n"
        "Keywords=files;transfer;SSH;rsync;locker;\n",
        encoding="utf-8",
    )
    if refresh := shutil.which("update-desktop-database"):
        subprocess.run([refresh, str(applications)], check=False)
    print(f"Launcher installed: {destination}")


if __name__ == "__main__":
    main()
