#!/usr/bin/env bash
# apply.sh — create the tradility refresh in K3s. No sudo. Safe to repeat.
# The namespace, volumes, network policy and the first Schwab token exist before the CronJob, which is created
# suspended. First time on this machine: prepare-host.sh and build-image.sh come first.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/k3s.yaml}"
ENV_FILE="${TRADILITY_ENV_FILE:-$HOME/.config/tradility/scheduled.env}"
EXPORTS_DIR="${TRADILITY_EXPORTS_DIR:-$REPO/exports}"
HOLDINGS_FILE="${TRADILITY_HOLDINGS_FILE:-$(python3 -I "$HERE/env_value.py" "$ENV_FILE" TRADILITY_HOLDINGS_FILE | sed "s/^[^=]*=//")}"

kubectl wait --for=condition=Ready node --all --timeout=60s
kubectl get crd ciliumnetworkpolicies.cilium.io >/dev/null
[ -n "$HOLDINGS_FILE" ] || { echo "TRADILITY_HOLDINGS_FILE is not set (export it, or put it in $ENV_FILE)"; exit 1; }
[ "$(basename "$HOLDINGS_FILE")" = "holdings-aggregate.json" ] ||
  { echo "the pod reads /holdings/holdings-aggregate.json; the holdings file is named $(basename "$HOLDINGS_FILE")"; exit 1; }
[ -r "$HOLDINGS_FILE" ] || { echo "holdings input is not readable"; exit 1; }
HOLDINGS_DIR="$(dirname "$HOLDINGS_FILE")"
for d in "$EXPORTS_DIR" "$HOLDINGS_DIR"; do
  ctx="$(stat -c %C "$d")"
  case "$ctx" in *container_file_t*) ;; *) echo "$d is labelled $ctx; run deploy/k3s/prepare-host.sh first"; exit 1 ;; esac
done
NODE="$(kubectl get node -o jsonpath='{.items[0].metadata.name}')"

kubectl apply -f "$HERE/manifests/00-namespace.yaml"
sed -e "s#__EXPORTS_DIR__#$EXPORTS_DIR#g" -e "s#__HOLDINGS_DIR__#$HOLDINGS_DIR#g" -e "s#__NODE__#$NODE#g" \
  "$HERE/manifests/10-volumes.yaml.tmpl" | kubectl apply -f -
kubectl apply -f "$HERE/manifests/30-network-policy.yaml"
kubectl apply -f "$HERE/manifests/20-cronjob.yaml"
# A first token so a manual run works at once; feed-token.sh install keeps it fresh on trading days.
"$HERE/feed-token.sh" feed || echo "note: no Schwab token yet; a run falls back to Yahoo until feed-token.sh succeeds"
echo "Deployed (the CronJob is suspended). Run it now with: kubectl -n tradility create job --from=cronjob/tradility-refresh refresh-now"
echo "Prove the policy with deploy/k3s/verify.sh, then hand over with deploy/deploy.sh --mode k3s up"
