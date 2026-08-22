#!/bin/sh

set -eu

die() {
    printf '%s\n' "agent-sidecar installer: $*" >&2
    exit 1
}

usage() {
    printf '%s\n' "usage: $0 [--uninstall]" >&2
    exit 2
}

canonical_path() {
    candidate=$1
    symlink_depth=0
    seen_paths=

    while :; do
        parent=$(CDPATH= cd -P "$(dirname "$candidate")" 2>/dev/null && pwd -P) ||
            return 1
        candidate=$parent/$(basename "$candidate")

        cycle_found=0
        while IFS= read -r seen_path; do
            if [ -n "$seen_path" ] && [ "$seen_path" = "$candidate" ]; then
                cycle_found=1
            fi
        done <<EOF
$seen_paths
EOF
        [ "$cycle_found" -eq 0 ] || return 1
        seen_paths="${seen_paths}
${candidate}"

        if [ -L "$candidate" ]; then
            symlink_depth=$((symlink_depth + 1))
            [ "$symlink_depth" -le 40 ] || return 1
            target=$(readlink "$candidate") || return 1
            case $target in
                /*) candidate=$target ;;
                *) candidate=$(dirname "$candidate")/$target ;;
            esac
            continue
        fi

        if [ -d "$candidate" ]; then
            CDPATH= cd -P "$candidate" 2>/dev/null && pwd -P
        else
            printf '%s\n' "$candidate"
        fi
        return
    done
}

resolved_link_target() {
    link=$1
    [ -L "$link" ] || return 1
    target=$(readlink "$link") || return 1
    case $target in
        /*) candidate=$target ;;
        *) candidate=$(dirname "$link")/$target ;;
    esac
    canonical_path "$candidate"
}

targets_this_repo() {
    resolved=$(resolved_link_target "$1") || return 1
    case $resolved in
        "$REPO_ROOT"|"$REPO_ROOT"/*) return 0 ;;
        *) return 1 ;;
    esac
}

preflight_destination() {
    destination=$1
    if [ -L "$destination" ]; then
        targets_this_repo "$destination" ||
            die "refusing to replace unrelated symlink: $destination"
    elif [ -e "$destination" ]; then
        die "refusing to overwrite existing file or directory: $destination"
    fi
}

install_link() {
    source=$1
    destination=$2

    if [ -L "$destination" ]; then
        current=$(resolved_link_target "$destination") || return 1
        if [ "$current" = "$source" ]; then
            printf '%s\n' "already installed: $destination"
            return
        fi
        rm "$destination"
    fi

    ln -s "$source" "$destination"
    printf '%s\n' "installed: $destination -> $source"
}

uninstall_link() {
    destination=$1
    if [ -L "$destination" ] && targets_this_repo "$destination"; then
        rm "$destination"
        printf '%s\n' "removed: $destination"
    elif [ -L "$destination" ] || [ -e "$destination" ]; then
        printf '%s\n' "left unrelated path unchanged: $destination"
    fi
}

case $# in
    0) action=install ;;
    1)
        [ "$1" = "--uninstall" ] || usage
        action=uninstall
        ;;
    *) usage ;;
esac

: "${HOME:?HOME must be set}"

SCRIPT_PATH=$(canonical_path "$0") ||
    die "cannot locate installer"
SCRIPT_DIR=$(CDPATH= cd -P "$(dirname "$SCRIPT_PATH")" && pwd -P) ||
    die "cannot locate installer directory"
REPO_ROOT=$(CDPATH= cd -P "$SCRIPT_DIR/.." && pwd -P) ||
    die "cannot locate repository root"

SKILL_SOURCE=$(canonical_path "$REPO_ROOT/skills/agent-sidecar") ||
    die "cannot locate skill bundle"
CLI_SOURCE=$(canonical_path "$REPO_ROOT/agent-sidecar") ||
    die "cannot locate CLI entry point"

[ -d "$SKILL_SOURCE" ] || die "missing skill bundle: $SKILL_SOURCE"
[ -f "$CLI_SOURCE" ] || die "missing CLI entry point: $CLI_SOURCE"

CURSOR_DESTINATION=$HOME/.cursor/skills/agent-sidecar
CLAUDE_DESTINATION=$HOME/.claude/skills/agent-sidecar
CLI_DESTINATION=$HOME/.local/bin/agent-sidecar

if [ "$action" = "uninstall" ]; then
    uninstall_link "$CURSOR_DESTINATION"
    uninstall_link "$CLAUDE_DESTINATION"
    uninstall_link "$CLI_DESTINATION"
    exit 0
fi

preflight_destination "$CURSOR_DESTINATION"
preflight_destination "$CLAUDE_DESTINATION"
preflight_destination "$CLI_DESTINATION"

mkdir -p "$HOME/.cursor/skills" "$HOME/.claude/skills" "$HOME/.local/bin"

install_link "$SKILL_SOURCE" "$CURSOR_DESTINATION"
install_link "$SKILL_SOURCE" "$CLAUDE_DESTINATION"
install_link "$CLI_SOURCE" "$CLI_DESTINATION"
