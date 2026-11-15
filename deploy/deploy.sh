#!/usr/bin/env bash
# deploy.sh — Scheduled portfolio technical analysis One entry point, one setting.
#
#   deploy/deploy.sh [--mode dev|host|k3s] up|down|status|verify|mode
#
# The mode comes from, first match wins:
#   1. --mode on the command line
#   2. TRADILITY_DEPLOY_MODE in the environment
#   3. TRADILITY_DEPLOY_MODE in config/deploy.local.env (copy config/deploy.example.env; never committed)
#   4. dev
#
# Only k3s mode touches kubectl, podman or a cluster. See docs/deployment.md.
# -E: the ERR trap used by `k3s up` must also fire for failures inside functions.
set -eEuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
K3S_DIR="${TRADILITY_K3S_DIR:-$HERE/k3s}"
CONFIG_FILE="${TRADILITY_DEPLOY_CONFIG:-$REPO/config/deploy.local.env}"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/tradility"
MARKER="$STATE_DIR/deploy-mode"
MODES="dev host k3s"
TIMER="tradility-refresh.timer"
NAMESPACE="tradility"

usage() {
  sed -n '2,12p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-2}"
}

mode="" source_of=""
cmd=""
while [ $# -gt 0 ]; do
  case "$1" in
    --mode) mode="${2:?--mode needs dev, host or k3s}"; source_of="--mode"; shift 2 ;;
    --mode=*) mode="${1#--mode=}"; source_of="--mode"; shift ;;
    -h|--help) usage 0 ;;
    up|down|status|verify|mode) cmd="$1"; shift ;;
    *) echo "unknown argument: $1" >&2; usage 2 ;;
  esac
done
[ -n "$cmd" ] || usage 2

if [ -z "$mode" ] && [ -n "${TRADILITY_DEPLOY_MODE:-}" ]; then mode="$TRADILITY_DEPLOY_MODE"; source_of="environment"; fi
if [ -z "$mode" ] && [ -f "$CONFIG_FILE" ]; then
  # No `|| true`: if reading the config fails, say so rather than quietly choosing dev.
  line="$(python3 -I "$K3S_DIR/env_value.py" "$CONFIG_FILE" TRADILITY_DEPLOY_MODE)"
  if [ -n "$line" ]; then mode="${line#TRADILITY_DEPLOY_MODE=}"; source_of="$CONFIG_FILE"; fi
fi
if [ -z "$mode" ]; then mode="dev"; source_of="default"; fi
case " $MODES " in *" $mode "*) ;; *) echo "unknown deployment mode '$mode' (from $source_of); use one of: $MODES" >&2; exit 2 ;; esac

need() { command -v "$1" >/dev/null 2>&1 || { echo "$2" >&2; exit 1; }; }
marker_is_k3s() { [ -f "$MARKER" ] && [ "$(cat "$MARKER")" = k3s ]; }
set_marker() { mkdir -p "$STATE_DIR"; echo "$1" > "$MARKER"; }
clear_marker() { rm -f "$MARKER"; }
kc() { KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}" "${KUBECTL:-kubectl}" "$@"; }
registry_ready() { curl -fsS --max-time 5 http://10.43.0.50:5000/v2/ >/dev/null 2>&1; }
require_cluster_tools() {
  need "${KUBECTL:-kubectl}" "k3s mode needs kubectl and a running K3s (see docs/deployment.md). dev and host modes need neither."
  need podman "k3s mode needs podman, to build the image"
  export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
  registry_ready || {
    echo "k3s mode needs the in-cluster registry: run install-registry.sh and enable-registry-mirror.sh from crew-chief's deploy/k3s" >&2
    exit 1
  }
}

timer_installed() { systemctl --user cat "$TIMER" >/dev/null 2>&1; }
show_timer() {
  printf '%-34s %-9s %s\n' "$TIMER" "$(systemctl --user is-active "$TIMER" 2>/dev/null || true)" \
    "$(systemctl --user is-enabled "$TIMER" 2>/dev/null || true)"
}

case "$cmd:$mode" in
  mode:*) echo "$mode (from $source_of)" ;;

  # ---- dev: one run in the foreground, no systemd, no cluster ----
  up:dev) bash scripts/run_scheduled_analysis.sh ;;
  down:dev) echo "dev mode runs once in the foreground; there is nothing to stop" ;;
  status:dev|verify:dev) echo "dev mode runs once in the foreground; there is no background job to check" ;;

  # ---- host: the systemd user timer ----
  up:host)
    if marker_is_k3s; then
      echo "the K3s job is active; suspending it before the host timer starts"
      if command -v "${KUBECTL:-kubectl}" >/dev/null 2>&1; then "$K3S_DIR/handover.sh" pod-off
      else echo "kubectl is not installed: suspend the CronJob yourself (docs/deployment.md) or both will run" >&2; exit 1; fi
    fi
    timer_installed || {
      echo "$TIMER is not installed. Install it with Clockwork from its manifest (see docs/deployment.md)" >&2
      exit 1
    }
    systemctl --user enable --now "$TIMER"
    clear_marker
    ;;
  down:host) systemctl --user disable --now "$TIMER"; clear_marker ;;
  status:host) show_timer ;;
  verify:host) systemctl --user is-active --quiet "$TIMER" && echo "$TIMER is active" ;;

  # ---- k3s: a CronJob, created suspended and handed work only after verify passes ----
  up:k3s)
    require_cluster_tools
    "$K3S_DIR/prepare-host.sh"
    "$K3S_DIR/build-image.sh"
    "$K3S_DIR/apply.sh"
    "$K3S_DIR/verify.sh"
    # Hand over only after verify passed: the timer keeps running until the pod is proven.
    systemctl --user disable --now "$TIMER" 2>/dev/null || true
    "$K3S_DIR/handover.sh" pod-on
    set_marker k3s
    ;;
  down:k3s) "$0" --mode host up; echo "rolled back to the host timer; the CronJob is suspended and left in place (kubectl delete ns $NAMESPACE removes it)" ;;
  status:k3s) show_timer; kc -n "$NAMESPACE" get cronjob,pods 2>&1 || true ;;
  verify:k3s) "$K3S_DIR/verify.sh" ;;
esac
