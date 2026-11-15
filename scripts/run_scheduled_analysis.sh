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

# Dividend and interest history (Schwab, read-only). Best effort like the orders: a failure keeps the
# previous export and never fails the analysis.
income_file="${TRADILITY_INCOME_FILE:-$repo_root/exports/tradility-income.json}"
if [[ "${TRADILITY_FETCH_INCOME:-1}" != "0" ]]; then
  flock --nonblock "$income_file.lock" \
    "$python_bin" -m tradility.income \
    --output "$income_file" ||
    echo "tradility: income fetch reported a problem (analysis is unaffected)" >&2
fi

# Actual (trade-based) performance, rebuilt from the order history just fetched (plus the dividends
# received, when the income export is there) and added to the analysis export. It takes the analysis
# lock to rewrite it, and a failure is reported without failing the run: the page then simply
# offers no "actual" basis.
# TRADILITY_FETCH_ACTUALS follows TRADILITY_FETCH_ORDERS unless set: a run that must not fetch orders itself (the
# K3s pod has no broker credentials; the host's orders timer keeps the file) can still rebuild the actuals.
if [[ "${TRADILITY_FETCH_ACTUALS:-${TRADILITY_FETCH_ORDERS:-1}}" != "0" && -r "$orders_file" ]]; then
  income_args=()
  [[ -r "$income_file" ]] && income_args=(--income "$income_file")
  flock --nonblock "$output_file.lock" \
    "$python_bin" -m tradility.actuals \
    --analysis "$output_file" \
    --orders "$orders_file" \
    --holdings "$holdings_file" \
    "${income_args[@]}" ||
    echo "tradility: actual-performance step reported a problem (analysis is unaffected)" >&2
fi
