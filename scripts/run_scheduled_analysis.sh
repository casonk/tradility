#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
holdings_file="${TRADILITY_HOLDINGS_FILE:-}"
output_file="${TRADILITY_OUTPUT_FILE:-$repo_root/exports/tradility-analysis.json}"
history_period="${TRADILITY_HISTORY_PERIOD:-5y}"
market_source="${TRADILITY_MARKET_SOURCE:-auto}"
python_bin="${TRADILITY_PYTHON:-$repo_root/.venv/bin/python}"
profile_cache="${TRADILITY_PROFILE_CACHE:-$repo_root/exports/tradility-profile-cache.json}"
profile_max_age_days="${TRADILITY_PROFILE_MAX_AGE_DAYS:-30}"
profile_workers="${TRADILITY_PROFILE_WORKERS:-4}"

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
flock --nonblock "$output_file.lock" \
  "$python_bin" -m tradility \
  --holdings "$holdings_file" \
  --output "$output_file" \
  --period "$history_period" \
  --source "$market_source" \
  --profile-cache "$profile_cache" \
  --profile-max-age-days "$profile_max_age_days" \
  --profile-workers "$profile_workers"

status=$?
if [[ $status -ne 0 ]]; then
  exit "$status"
fi

# Read-only order history and open orders from the brokers (Schwab, Binance.US). Each source
# needs its own credentials in the environment (see README); a missing or failing one is
# reported on stderr and never fails the analysis above.
orders_file="${TRADILITY_ORDERS_FILE:-$repo_root/exports/tradility-orders.json}"
if [[ "${TRADILITY_FETCH_ORDERS:-1}" != "0" ]]; then
  flock --nonblock "$orders_file.lock" \
    "$python_bin" -m tradility.orders \
    --holdings "$holdings_file" \
    --output "$orders_file" ||
    echo "tradility: order fetch reported a problem (analysis is unaffected)" >&2
fi
