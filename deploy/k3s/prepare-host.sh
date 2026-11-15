#!/usr/bin/env bash
# prepare-host.sh — label the folders the refresh pod mounts so containers may use them. No sudo.
# SELinux keeps containers out of ordinary home-directory files; container_file_t opens exactly these two
# folders: tradility's exports (the pod writes the analysis there) and the finance pipeline's holdings folder
# (the pod mounts it read-only). Host programs read both as before.
# Undo with:  restorecon -RF <the two folders>   (printed below)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
ENV_FILE="${TRADILITY_ENV_FILE:-$HOME/.config/tradility/scheduled.env}"
EXPORTS_DIR="${TRADILITY_EXPORTS_DIR:-$REPO/exports}"
HOLDINGS_FILE="${TRADILITY_HOLDINGS_FILE:-$(python3 -I "$HERE/env_value.py" "$ENV_FILE" TRADILITY_HOLDINGS_FILE | sed "s/^[^=]*=//")}"
[ -n "$HOLDINGS_FILE" ] || { echo "TRADILITY_HOLDINGS_FILE is not set (export it, or put it in $ENV_FILE)"; exit 1; }
HOLDINGS_DIR="$(dirname "$HOLDINGS_FILE")"
for d in "$EXPORTS_DIR" "$HOLDINGS_DIR"; do
  [ -d "$d" ] || { echo "missing $d"; exit 1; }
  chcon -R -t container_file_t "$d"
  echo "labelled $d: $(ls -Zd "$d" | awk '{print $1}')"
done
echo "Undo with: restorecon -RF '$EXPORTS_DIR' '$HOLDINGS_DIR'"
echo "A system-wide relabel (restorecon or an OS upgrade) would reset these; re-run this script if the pod loses access."
