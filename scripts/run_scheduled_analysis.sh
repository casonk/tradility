#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
holdings_file="${TRADILITY_HOLDINGS_FILE:-}"
output_file="${TRADILITY_OUTPUT_FILE:-$repo_root/exports/tradility-analysis.json}"
history_period="${TRADILITY_HISTORY_PERIOD:-5y}"
market_source="${TRADILITY_MARKET_SOURCE:-auto}"
python_bin="${TRADILITY_PYTHON:-$repo_root/.venv/bin/python}"

if [[ -z "$holdings_file" ]]; then
  echo "TRADILITY_HOLDINGS_FILE is required" >&2
  exit 2
fi
if [[ ! -r "$holdings_file" ]]; then
  echo "Tradility holdings input is not readable" >&2
  exit 2
fi
if [[ ! -x "$python_bin" ]]; then
  echo "Tradility Python is not executable: $python_bin" >&2
  exit 2
fi

mkdir -p "$(dirname "$output_file")"
exec flock --nonblock "$output_file.lock" \
  "$python_bin" -m tradility \
  --holdings "$holdings_file" \
  --output "$output_file" \
  --period "$history_period" \
  --source "$market_source"
