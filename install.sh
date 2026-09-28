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
print("Desktop runtime is ready.")
PY
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
