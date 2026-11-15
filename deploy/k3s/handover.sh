#!/usr/bin/env bash
# handover.sh pod-on|pod-off — start or suspend the CronJob (and its token feeder). No sudo.
# The host timer and the pod must never both run: deploy.sh disables the timer before pod-on and suspends the
# pod before it enables the timer.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
KUBECTL="${KUBECTL:-kubectl}"
case "${1:-}" in
  pod-on)
    "$HERE/feed-token.sh" install
    "$KUBECTL" -n tradility patch cronjob tradility-refresh -p '{"spec":{"suspend":false}}'
    ;;
  pod-off)
    "$KUBECTL" -n tradility patch cronjob tradility-refresh -p '{"spec":{"suspend":true}}'
    "$HERE/feed-token.sh" remove
    ;;
  *) echo "usage: $0 pod-on|pod-off"; exit 2 ;;
esac
