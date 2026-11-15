#!/usr/bin/env bash
# handover.sh pod-on|pod-off — make the host unit run the refresh in K3s, or on the host again (and run or remove
# the Schwab token feeder that goes with it). No sudo.
# pod-on installs a systemd drop-in that replaces only ExecStart (run-job.sh); the timer, the job guard,
# the resource arbiter and any post-step are untouched. pod-off removes the drop-in. The CronJob stays suspended.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/tradility-refresh.service.d"
DROPIN="$UNIT_DIR/zz-k3s-run.conf"
case "${1:-}" in
  pod-on)
    # Pods cannot reach the vault: a short-lived Schwab access token is fed to a Secret before each weekday run.
    "$HERE/feed-token.sh" install
    kubectl_bin="$(command -v "${KUBECTL:-kubectl}")"
    mkdir -p "$UNIT_DIR"
    sed -e "s#__REPO__#$REPO#g" -e "s#__KUBECTL__#$kubectl_bin#g" "$HERE/systemd/zz-k3s-run.conf" >"$DROPIN"
    systemctl --user daemon-reload
    # systemd applies drop-ins in name order and the last ExecStart= wins; another drop-in (a local.conf, say) can
    # still win over this one. Check the unit really runs the pod runner, and undo the drop-in if it does not.
    if ! systemctl --user show tradility-refresh.service -p ExecStart --value 2>/dev/null | grep -q "run-job.sh"; then
      rm -f "$DROPIN"
      systemctl --user daemon-reload
      echo "another drop-in sets ExecStart after $DROPIN; the unit would not run the pod. Removed it; see docs/deployment.md" >&2
      exit 1
    fi
    ;;
  pod-off)
    "$HERE/feed-token.sh" remove
    rm -f "$DROPIN"
    systemctl --user daemon-reload
    ;;
  *) echo "usage: $0 pod-on|pod-off"; exit 2 ;;
esac
