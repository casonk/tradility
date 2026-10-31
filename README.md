# tradility

Technical analytics on investment ticker holdings and watchlists.

Fetches multi-year OHLCV price data from the **Schwab Trader API**, with an
explicit **yfinance** fallback, and computes short- and long-horizon indicator
signals for every ticker in your portfolio or a custom list. Outputs a
structured JSON file suitable for dashboards and further analysis.

## Prerequisites

- Python 3.10+
- `pip install -e .` (installs `yfinance` and `pandas`)

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

# From a holdings-aggregate.json (exported by a finance pipeline)
tradility --holdings /path/to/exports/invest/holdings-aggregate.json \
          --output exports/tradility-analysis.json

# Or from an explicit ticker list
tradility --tickers AAPL MSFT BTC-USD ETH-USD \
          --output exports/tradility-analysis.json \
          --period 10y
```

The default history window is five years. Legacy day counts remain accepted
(`--period 365` is normalized to `365d`); `5y`, `10y`, and `max` are clearer for
long-term analysis.

## Market-data sources

`--source auto` is the default. It prefers Schwab for non-crypto tickers when a
token is available and falls back ticker-by-ticker to Yahoo. Crypto continues
through Yahoo until the native Binance adapter is implemented.

Provide a short-lived Schwab access token with either:

```bash
export TRADILITY_SCHWAB_ACCESS_TOKEN='...'

# Preferred for scheduled use: delegate token ownership and rotation to an
# existing credential pipeline. The command may print the token directly or
# JSON containing an access_token field.
export TRADILITY_SCHWAB_TOKEN_COMMAND='/path/to/token-helper --json'
```

Tradility never writes the token into its output or logs. OAuth refresh and
interactive reauthorization remain the credential pipeline's responsibility;
this avoids duplicating secrets or refresh-token state in a public analytics
package. Use `--source schwab` to fail closed when Schwab is unavailable, or
`--source yfinance` for an explicit Yahoo-only run.

## Output Format

`tradility-analysis.json` contains a top-level metadata block and a `tickers` array:

```json
{
  "generated_at": "2026-06-15T21:00:00Z",
  "history_period": "5y",
  "period_days": null,
  "rsi_period": 14,
  "vwap_period": 20,
  "requested_source": "auto",
  "data_source": "mixed",
  "data_sources": {"schwab": 300, "yfinance": 49},
  "ticker_count": 349,
  "fetch_ok": 342,
  "fetch_errors": 7,
  "tickers": [
    {
      "ticker": "AAPL",
      "name": "APPLE INC",
      "asset_class": "Stocks",
      "last_price": 313.62,
      "period_end": "2026-06-15",
      "rsi_14": 62.3,
      "rsi_14_signal": "neutral",
      "vwap_20": 298.45,
      "vwap_20_signal": "above",
      "sma_50": 304.12,
      "sma_200": 275.89,
      "long_term_trend": "bullish",
      "fetch_status": "ok",
      "data_source": "schwab"
    }
  ]
}
```

### RSI signals

| Value | Signal |
|---|---|
| ≤ 30 | `oversold` |
| ≥ 70 | `overbought` |
| 30–70 | `neutral` |

### VWAP signals

| Condition | Signal |
|---|---|
| Price within ±0.5% of VWAP | `at` |
| Price above VWAP | `above` |
| Price below VWAP | `below` |

VWAP here is a rolling 20-day proxy (true intraday VWAP resets each session; daily bars require a windowed approximation).

### Long-term trend

Tradility calculates 50- and 200-trading-day simple moving averages. The
combined signal is `bullish` when the 50-day average is above the 200-day
average and price is above the 200-day average, `bearish` for the inverse,
`mixed` otherwise, and `unknown` when history is insufficient.

## Scheduled refresh

`scripts/run_scheduled_analysis.sh` is the non-interactive workload entry point.
It requires `TRADILITY_HOLDINGS_FILE` and accepts:

- `TRADILITY_OUTPUT_FILE` (default: `exports/tradility-analysis.json`)
- `TRADILITY_HISTORY_PERIOD` (default: `5y`)
- `TRADILITY_MARKET_SOURCE` (default: `auto`)
- `TRADILITY_PYTHON` (default: `.venv/bin/python`)
- `TRADILITY_SCHWAB_TOKEN_COMMAND` (optional credential-pipeline helper)

Clockwork owns the corresponding user-level systemd schedule. The wrapper uses
an output lock so a manual refresh cannot overlap the scheduled run.

## Crypto Tickers

Crypto from Binance uses plain symbols (`BTC`, `ETH`). When loaded from a holdings file these are automatically converted to Yahoo Finance format (`BTC-USD`, `ETH-USD`) for fetching, then restored to the original symbol in the output.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
