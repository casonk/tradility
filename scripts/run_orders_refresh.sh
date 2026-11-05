#!/usr/bin/env bash
# Refresh the read-only broker orders export quickly: Schwab's last N days are merged into the
# existing file by order id and the Binance.US history is re-read, so a fill shows up in minutes
# instead of waiting for the nightly analysis. Safe to run often; a lock stops overlapping runs.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
holdings_file="${TRADILITY_HOLDINGS_FILE:-}"
orders_file="${TRADILITY_ORDERS_FILE:-$repo_root/exports/tradility-orders.json}"
python_bin="${TRADILITY_PYTHON:-$repo_root/.venv/bin/python}"
recent_days="${TRADILITY_ORDERS_RECENT_DAYS:-60}"

if [[ -z "$holdings_file" || ! -r "$holdings_file" ]]; then
  echo "TRADILITY_HOLDINGS_FILE is required and must be readable" >&2
  exit 2
fi
if [[ ! -x "$python_bin" ]]; then
  echo "Tradility Python is not executable: $python_bin" >&2
  exit 2
fi

mkdir -p "$(dirname "$orders_file")"
exec flock --nonblock "$orders_file.lock" \
  "$python_bin" -m tradility.orders \
  --holdings "$holdings_file" \
  --output "$orders_file" \
  --recent-days "$recent_days"
