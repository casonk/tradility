#!/usr/bin/env bash
# feed-token.sh feed|install|remove — keep the refresh pod's Schwab access token fresh. No sudo.
#   feed     resolve a token on the host and store it as the tradility-schwab Secret (never printed)
#   install  a user timer that feeds a token on weekdays shortly before the 16:30 run
#   remove   remove that timer
# The refresh token never leaves the vault; the pod gets only a token that lives about half an hour.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
ENV_FILE="${TRADILITY_ENV_FILE:-$HOME/.config/tradility/scheduled.env}"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT=tradility-token-feed

case "${1:-feed}" in
  feed)
    PY="${TRADILITY_PYTHON:-$(python3 -I "$HERE/env_value.py" "$ENV_FILE" TRADILITY_PYTHON | sed "s/^[^=]*=//")}"
    PY="${PY:-python3}"
    DIR="$(mktemp -d "${XDG_RUNTIME_DIR:-/tmp}/tradility-token.XXXXXX")"; chmod 700 "$DIR"
    trap 'rm -rf "$DIR"' EXIT
    "$PY" -I "$HERE/make_token_secret.py" "$ENV_FILE" "$DIR/access_token"
    kubectl -n tradility create secret generic tradility-schwab --from-file=access_token="$DIR/access_token" \
      --dry-run=client -o yaml | kubectl apply -f - >/dev/null
    echo "tradility-schwab updated"
    ;;
  install)
    mkdir -p "$UNIT_DIR"
    for f in "$UNIT.service" "$UNIT.timer"; do
      sed "s#__REPO__#$REPO#g" "$HERE/systemd/$f" >"$UNIT_DIR/$f"
    done
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT.timer"
    ;;
  remove)
    systemctl --user disable --now "$UNIT.timer" 2>/dev/null || true
    rm -f "$UNIT_DIR/$UNIT.service" "$UNIT_DIR/$UNIT.timer"
    systemctl --user daemon-reload
    ;;
  *) echo "usage: $0 feed|install|remove"; exit 2 ;;
esac
