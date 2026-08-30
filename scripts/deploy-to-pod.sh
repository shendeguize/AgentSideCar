#!/usr/bin/env bash
#
# Deploy the checked-out Sidecar and its DSH plugin to the configured debug pod.
# This is an operator-controlled development deploy, not a release installer.
#
# The target is deliberately explicit: no credentials are copied, and no agent
# configuration is modified. The remote daemon uses the current user's runtime
# directory and can be restarted with --restart-daemon. If the pod has a
# private Copilot BYOK env file, the generated sidecar wrapper sources it only
# inside the child process used for injection; its contents never cross SSH.

set -euo pipefail
IFS=$'\n\t'

readonly SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
readonly REPO_ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
readonly DEFAULT_REMOTE_DIR="/home/caros/workspace/dsh_debug"
readonly DEFAULT_FLEET="$HOME/Workspace/Helpers/skills/pod-init-sync/scripts/fleet.py"
readonly DEFAULT_SCAN="$HOME/Workspace/Helpers/skills/pod-init-sync/scripts/scan.sh"

HOST=""
REMOTE_DIR="$DEFAULT_REMOTE_DIR"
FLEET="${POD_INIT_SYNC_FLEET_SCRIPT:-$DEFAULT_FLEET}"
SCAN="${POD_INIT_SYNC_SCAN_SCRIPT:-$DEFAULT_SCAN}"
DRY_RUN=0
INSTALL_PLUGIN=1
RESTART_DAEMON=1

die() {
    printf 'deploy-to-pod: ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf 'deploy-to-pod: %s\n' "$*"
}

usage() {
    cat <<'EOF'
Usage: scripts/deploy-to-pod.sh [options] <ssh-alias>

Options:
  --remote-dir PATH       remote workspace (default: /home/caros/workspace/dsh_debug)
  --fleet PATH            fresh SSH fleet scan script
  --scan PATH             fresh SSH host scan script
  --without-plugin        do not install/update the local DSH plugin
  --without-daemon        do not restart the remote Sidecar daemon
  --dry-run               validate and show actions without writing
  -h, --help              show this help
EOF
}

while (($#)); do
    case "$1" in
        --remote-dir)
            (($# >= 2)) || die "--remote-dir requires a path"
            REMOTE_DIR="$2"
            shift 2
            ;;
        --fleet)
            (($# >= 2)) || die "--fleet requires a path"
            FLEET="$2"
            shift 2
            ;;
        --scan)
            (($# >= 2)) || die "--scan requires a path"
            SCAN="$2"
            shift 2
            ;;
        --without-plugin)
            INSTALL_PLUGIN=0
            shift
            ;;
        --without-daemon)
            RESTART_DAEMON=0
            shift
            ;;
        --dry-run)
            DRY_RUN=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        -*)
            die "unknown option: $1"
            ;;
        *)
            [[ -z "$HOST" ]] || die "exactly one SSH alias is required"
            HOST="$1"
            shift
            ;;
    esac
done

[[ -n "$HOST" ]] || die "an SSH alias is required"
[[ "$HOST" =~ ^[A-Za-z0-9._-]+$ ]] || die "unsafe SSH alias: $HOST"
[[ "$REMOTE_DIR" = /* ]] || die "remote directory must be absolute"
[[ -f "$FLEET" && -f "$SCAN" ]] || die "fresh SSH scan helpers not found"
command -v rsync >/dev/null 2>&1 || die "rsync is required"
command -v ssh >/dev/null 2>&1 || die "ssh is required"

note "Rescanning SSH config and pod state"
python3 "$FLEET" hosts --hosts "$HOST" >/dev/null
"$SCAN" "$HOST" >/dev/null

ARTIFACT="${TMPDIR:-/tmp}/agent-sidecar-deploy.$$.pyz"
cleanup() {
    rm -f "$ARTIFACT"
}
trap cleanup EXIT

note "Building a deterministic Sidecar zipapp"
(
    cd "$REPO_ROOT"
    ./agent-sidecar package build --output "$ARTIFACT" >/dev/null
)

if ((INSTALL_PLUGIN)); then
    note "Building the DSH plugin host/client bundles"
    (
        cd "$REPO_ROOT/plugin"
        pnpm build >/dev/null
    )
fi

REMOTE_ARTIFACT="$REMOTE_DIR/.agent-sidecar.pyz"
REMOTE_PLUGIN="$REMOTE_DIR/plugin"
REMOTE_COMMAND="$REMOTE_DIR/agent-sidecar-with-auth"

note "Preparing remote workspace: $REMOTE_DIR"
if ((DRY_RUN)); then
    note "DRY-RUN: would rsync zipapp and plugin to $HOST:$REMOTE_DIR"
    ((INSTALL_PLUGIN)) && note "DRY-RUN: would install/update DSH web plugin"
    ((RESTART_DAEMON)) && note "DRY-RUN: would restart Sidecar daemon"
    exit 0
fi

ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes "$HOST" \
    "mkdir -p '$REMOTE_DIR' '$REMOTE_PLUGIN'"
rsync -a "$ARTIFACT" "$HOST:$REMOTE_ARTIFACT"
rsync -a --exclude node_modules/ "$REPO_ROOT/plugin/" "$HOST:$REMOTE_PLUGIN/"

ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes "$HOST" \
    "install -m 0755 '$REMOTE_ARTIFACT' '$REMOTE_DIR/agent-sidecar.pyz'"
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes "$HOST" \
    "cat > '$REMOTE_COMMAND' <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
export PATH=\"\$HOME/.canon/node/bin:\$HOME/.local/bin:\$PATH\"
if [ -r \"\$HOME/.nexis-agent-settings/copilot-byok.env\" ]; then
    set -a
    . \"\$HOME/.nexis-agent-settings/copilot-byok.env\"
    set +a
fi
exec '$REMOTE_DIR/agent-sidecar.pyz' \"\$@\"
EOF
chmod 0755 '$REMOTE_COMMAND'"
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes "$HOST" \
    "export PATH=\"\$HOME/.canon/node/bin:\$HOME/.local/bin:\$PATH\"; \
     if [ ! -e \"\$HOME/.local/bin/dsh\" ]; then \
         ln -s \"\$HOME/.canon/node/bin/dsh\" \"\$HOME/.local/bin/dsh\"; \
     fi; \
     if [ ! -e /usr/local/bin/dsh ]; then \
         ln -s \"\$HOME/.canon/node/bin/dsh\" /usr/local/bin/dsh; \
     fi; \
     if [ ! -e /usr/local/bin/node ]; then \
         ln -s \"\$HOME/.canon/node/bin/node\" /usr/local/bin/node; \
     fi"
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes "$HOST" \
    "if [ -L /usr/local/bin/agent-sidecar ]; then \
         ln -sfn '$REMOTE_COMMAND' /usr/local/bin/agent-sidecar; \
     elif [ ! -e /usr/local/bin/agent-sidecar ]; then \
         ln -s '$REMOTE_COMMAND' /usr/local/bin/agent-sidecar; \
     fi"

if ((INSTALL_PLUGIN)); then
    note "Installing local Sidecar plugin into remote DSH web profile"
    ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes "$HOST" \
        "export PATH=\"\$HOME/.canon/node/bin:\$HOME/.local/bin:\$PATH\"; \
         cd '$REMOTE_PLUGIN'; \
         pnpm install --prod --frozen-lockfile --ignore-scripts; \
         dsh plugin --profile web add '$REMOTE_PLUGIN'"
fi

if ((RESTART_DAEMON)); then
    note "Restarting remote Sidecar daemon"
    ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes "$HOST" \
        "export PATH=\"\$HOME/.canon/node/bin:\$HOME/.local/bin:\$PATH\"; \
         '$REMOTE_DIR/agent-sidecar.pyz' daemon stop >/dev/null 2>&1 || true; \
         nohup '$REMOTE_DIR/agent-sidecar.pyz' daemon run \
             >/dev/null 2>&1 </dev/null & \
         for i in 1 2 3 4 5 6 7 8 9 10; do \
             '$REMOTE_DIR/agent-sidecar.pyz' daemon status >/dev/null 2>&1 && exit 0; \
             sleep 0.2; \
         done; \
         echo 'remote daemon did not become ready' >&2; exit 1"
fi

note "Remote deployment complete"
ssh -T -o BatchMode=yes -o StrictHostKeyChecking=yes "$HOST" \
    "export PATH=\"\$HOME/.canon/node/bin:\$HOME/.local/bin:\$PATH\"; \
     printf 'sidecar=%s\n' \"\$('$REMOTE_DIR/agent-sidecar.pyz' --version)\"; \
     printf 'python=%s\n' \"\$(python3 --version 2>&1)\"; \
     printf 'dsh=%s\n' \"\$(dsh --version 2>&1 || true)\""
