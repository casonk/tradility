#!/usr/bin/env bash
# handover.sh pod-on|pod-off — make the host unit run the refresh in K3s, or on the host again (and run or remove
# the Schwab token feeder that goes with it). No sudo.
# pod-on installs a systemd drop-in that replaces only ExecStart (run-job.sh); the timer, the job guard,
# the resource arbiter and any post-step are untouched. pod-off removes the drop-in. The CronJob stays suspended.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/tradility-refresh.service.d"
DROPIN="$UNIT_DIR/99-k3s-run.conf"
case "${1:-}" in
  pod-on)
    # Pods cannot reach the vault: a short-lived Schwab access token is fed to a Secret before each weekday run.
    "$HERE/feed-token.sh" install
    kubectl_bin="$(command -v "${KUBECTL:-kubectl}")"
    mkdir -p "$UNIT_DIR"
    sed -e "s#__REPO__#$REPO#g" -e "s#__KUBECTL__#$kubectl_bin#g" "$HERE/systemd/99-k3s-run.conf" >"$DROPIN"
    systemctl --user daemon-reload
    ;;
  pod-off)
    "$HERE/feed-token.sh" remove
    rm -f "$DROPIN"
    systemctl --user daemon-reload
    ;;
  *) echo "usage: $0 pod-on|pod-off"; exit 2 ;;
esac
