#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
FASTFILES_BIN="${SCRIPT_DIR}/.venv/bin/fastfiles"

if [[ ! -x "${FASTFILES_BIN}" ]]; then
    printf 'FastFiles is not installed yet. Run:\n  %s/install.sh\n' "${SCRIPT_DIR}" >&2
    exit 1
fi

cd "${SCRIPT_DIR}"
exec "${FASTFILES_BIN}" "$@"

