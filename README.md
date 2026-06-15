# tradility

Technical analytics on investment ticker holdings and watchlists.

Fetches OHLCV price data via **yfinance** and computes indicator signals (RSI, VWAP) for every ticker in your portfolio or a custom list. Outputs a structured JSON file suitable for dashboards and further analysis.

## Prerequisites

- Python 3.10+
- `pip install -e .` (installs `yfinance` and `pandas`)

## Quick Start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .

# From a holdings-aggregate.json (personal-finance export)
tradility --holdings /path/to/exports/invest/holdings-aggregate.json \
          --output exports/tradility-analysis.json

# Or from an explicit ticker list
tradility --tickers AAPL MSFT BTC-USD ETH-USD \
          --output exports/tradility-analysis.json \
          --period 60
```

## Output Format

`tradility-analysis.json` contains a top-level metadata block and a `tickers` array:

```json
{
  "generated_at": "2026-06-15T21:00:00Z",
  "period_days": 90,
  "rsi_period": 14,
  "vwap_period": 20,
  "data_source": "yfinance",
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
      "fetch_status": "ok",
      "data_source": "yfinance"
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

## Crypto Tickers

Crypto from Binance uses plain symbols (`BTC`, `ETH`). When loaded from a holdings file these are automatically converted to Yahoo Finance format (`BTC-USD`, `ETH-USD`) for fetching, then restored to the original symbol in the output.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT — see [LICENSE](LICENSE).
