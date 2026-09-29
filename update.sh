#!/usr/bin/env bash
set -Eeuo pipefail

update_fastfiles() {
    local script_dir
    script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
    if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--headless" ) ]]; then
        printf 'Usage: %s [--headless]\n' "$0" >&2
        return 2
    fi
    command -v git >/dev/null 2>&1 || { echo 'Install Git before updating FastFiles.' >&2; return 1; }
    cd "$script_dir"
    [[ -e .git ]] || { echo 'This folder is not a Git checkout. Clone FastFiles to use the updater.' >&2; return 1; }
    local changes
    changes="$(git status --porcelain)" || return 1
    if [[ -n "$changes" ]]; then
        echo 'Save your local changes in Git or move them aside before updating.' >&2
        return 1
    fi
    git symbolic-ref --quiet HEAD >/dev/null || { echo 'Check out a branch before updating.' >&2; return 1; }
    git rev-parse --verify '@{upstream}' >/dev/null 2>&1 || {
        echo 'This branch has no upstream. Configure its Git remote tracking branch first.' >&2
        return 1
    }
    echo 'Downloading FastFiles updates...'
    git -c merge.autostash=false -c rebase.autostash=false pull --ff-only --no-rebase || {
        echo 'Git update failed. Check your network and branch history; installation was not run.' >&2
        return 1
    }
    bash "$script_dir/install.sh" "$@" || {
        echo 'Files were updated, but installation failed. Fix the error and rerun install.sh.' >&2
        return 1
    }
    echo 'FastFiles updated. Restart the app to use the new version.'
}

update_fastfiles "$@"
