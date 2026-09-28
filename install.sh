#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"
INSTALL_TARGET="${SCRIPT_DIR}[desktop]"
if [[ "${1:-}" == "--headless" ]]; then
    INSTALL_TARGET="${SCRIPT_DIR}"
elif [[ "$#" -gt 0 ]]; then
    printf 'Usage: %s [--headless]\n' "$0" >&2
    exit 2
fi

info() {
    printf '\n\033[1;36mFastFiles:\033[0m %s\n' "$1"
}

fail() {
    printf '\n\033[1;31mFastFiles setup failed:\033[0m %s\n' "$1" >&2
    exit 1
}

command -v python3 >/dev/null 2>&1 || fail "Python 3 is required but was not found."

python3 - <<'PY' || fail "Python 3.10 or newer is required."
import sys
if sys.version_info < (3, 10):
    raise SystemExit(1)
print(f"Using Python {sys.version.split()[0]}")
PY

if [[ ! -d "${VENV_DIR}" ]]; then
    info "Creating the virtual environment"
    python3 -m venv "${VENV_DIR}" || fail \
        "Could not create a virtual environment. Install your distribution's python3-venv package and retry."
else
    info "Using the existing virtual environment"
fi

if [[ "${INSTALL_TARGET}" == *'[desktop]' && "$(uname -s)" == "Linux" ]]; then
    info "Checking Linux desktop system libraries"
    missing_libraries="$(python3 - <<'PYLIBS'
import ctypes

for library in ("libxcb-cursor.so.0", "libEGL.so.1", "libOpenGL.so.0"):
    try:
        ctypes.CDLL(library)
    except OSError:
        print(library)
PYLIBS
)"
    if [[ -n "${missing_libraries}" ]]; then
        printf 'Missing libraries:\n%s\n' "${missing_libraries}"
        if command -v apt-get >/dev/null 2>&1; then
            package_manager=apt-get
            packages=(libxcb-cursor0 libegl1 libopengl0)
        elif command -v dnf >/dev/null 2>&1; then
            package_manager=dnf
            packages=(xcb-util-cursor libglvnd-egl libglvnd-opengl)
        elif command -v pacman >/dev/null 2>&1; then
            package_manager=pacman
            packages=(xcb-util-cursor libglvnd)
        else
            fail "Install the listed libraries with your distribution's package manager, then rerun ./install.sh."
        fi
        privilege=()
        if (( EUID != 0 )); then
            command -v sudo >/dev/null 2>&1 || fail \
                "Administrator access is needed to install system libraries. Ask your administrator to install: ${packages[*]}"
            privilege=(sudo)
        fi
        info "Installing desktop system packages: ${packages[*]} (may require your sudo password)"
        case "${package_manager}" in
            apt-get)
                "${privilege[@]}" apt-get update || fail "Could not refresh system package lists."
                "${privilege[@]}" apt-get install -y "${packages[@]}" || fail "Could not install desktop system libraries."
                ;;
            dnf)
                "${privilege[@]}" dnf install -y "${packages[@]}" || fail "Could not install desktop system libraries."
                ;;
            pacman)
                "${privilege[@]}" pacman -S --needed "${packages[@]}" || fail "Could not install desktop system libraries."
                ;;
        esac
        python3 - <<'PYVERIFY' || fail "Desktop system libraries are still unavailable after package installation."
import ctypes
for library in ("libxcb-cursor.so.0", "libEGL.so.1", "libOpenGL.so.0"):
    ctypes.CDLL(library)
PYVERIFY
    fi
fi

info "Installing FastFiles and its dependencies"
"${VENV_DIR}/bin/python" -m pip install --upgrade pip
"${VENV_DIR}/bin/python" -m pip install --editable "${INSTALL_TARGET}"

info "Checking the installation"
"${VENV_DIR}/bin/python" -m pip check
if [[ "${INSTALL_TARGET}" == *'[desktop]' ]]; then
    QT_QPA_PLATFORM=offscreen "${VENV_DIR}/bin/python" - <<'PY' || fail \
        "The desktop runtime could not load. Check the Qt/OpenGL libraries installed by your Linux distribution."
from PySide6.QtWidgets import QApplication
from qt_material import apply_stylesheet
app = QApplication([])
apply_stylesheet(app, theme="light_blue.xml", invert_secondary=True)
print("Offscreen desktop runtime is ready.")
PY
    info "Checking the desktop display plugin"
    "${VENV_DIR}/bin/python" - <<'PYDESKTOP' || fail \
        "The desktop display check failed. Install the missing system libraries shown above, then rerun ./install.sh."
import os
import resource
import subprocess
import sys

if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
    print("No desktop session detected; the display plugin could not be checked.")
    print("Run the installer from a terminal inside your desktop session to verify GUI startup.")
    sys.exit(0)

# Probe Qt in a child: plugin failures abort instead of raising Python exceptions.
# Avoid leaving a core dump when a system dependency is missing.
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
env = os.environ.copy()
# A test-only backend must not hide a broken desktop installation.
if env.get("QT_QPA_PLATFORM", "").split(":", 1)[0] in {"offscreen", "minimal"}:
    env.pop("QT_QPA_PLATFORM")
try:
    result = subprocess.run(
        [sys.executable, "-c",
         "from PySide6.QtWidgets import QApplication; app = QApplication([]); "
         "print('Desktop display plugin is ready: ' + app.platformName())"],
        env=env, capture_output=True, text=True, timeout=30,
    )
except subprocess.TimeoutExpired:
    print("Qt timed out connecting to the desktop display.", file=sys.stderr)
    sys.exit(1)
if result.returncode:
    print(result.stderr, file=sys.stderr)
    print("Qt could not open the desktop display. Check that the display session is accessible.",
          file=sys.stderr)
    print("For a missing xcb cursor library, install your distribution's package:", file=sys.stderr)
    print("  Ubuntu/Debian: sudo apt install libxcb-cursor0", file=sys.stderr)
    print("  Fedora: sudo dnf install xcb-util-cursor", file=sys.stderr)
    print("  Arch: sudo pacman -S xcb-util-cursor", file=sys.stderr)
    print("For other missing libraries, run QT_DEBUG_PLUGINS=1 ./start.sh for details.",
          file=sys.stderr)
    sys.exit(1)
print(result.stdout, end="")
PYDESKTOP
fi
QT_QPA_PLATFORM=offscreen "${VENV_DIR}/bin/python" -m unittest discover -s "${SCRIPT_DIR}/tests" -v

missing_tools=()
for tool in ssh rsync; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
        missing_tools+=("${tool}")
    fi
done

if (( ${#missing_tools[@]} )); then
    printf '\n\033[1;33mNote:\033[0m %s is not installed. The Locker works, but Direct SSH transfers need it.\n' \
        "$(IFS=', '; printf '%s' "${missing_tools[*]}")"
else
    "${VENV_DIR}/bin/fastfiles" --check
fi

if [[ "${INSTALL_TARGET}" == *'[desktop]' ]]; then
    info "Adding FastFiles to the application launcher"
    "${VENV_DIR}/bin/python" "${SCRIPT_DIR}/scripts/install_launcher.py"
    printf '\nYou can now open FastFiles from your application launcher.\n'
fi

printf '\n\033[1;32mFastFiles is ready.\033[0m Start it with:\n  %s/start.sh\n\n' "${SCRIPT_DIR}"
