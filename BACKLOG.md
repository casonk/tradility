# BACKLOG.md

## Active

- [ ] [manual:2026-10-15] **Ask GitHub Support to purge pre-rewrite commits** — this
  repository's published history was rewritten (most recently on 2026-10-15,
  to remove per-commit AI attribution trailers). GitHub keeps serving the old
  commits to anyone with a hash, and through the read-only `refs/pull/*` refs
  of earlier pull requests, until Support garbage-collects them. The request
  is filed once for the whole portfolio from traction-control's backlog; mark
  this done when an old hash returns 404 here. Never push an old clone's
  history back.

## Data Sources

- [ ] **Alpha Vantage adapter** — `src/tradility/sources/alpha_vantage.py`; free tier with API key; better fundamentals and EPS data than yfinance; rate-limited (5 calls/min free tier)
- [ ] **Schwab API adapter** — `src/tradility/sources/schwab.py`; real-time intraday quotes for equity holdings; reuse OAuth credentials from the finance pipeline's downloader
- [ ] **Binance API adapter** — `src/tradility/sources/binance.py`; native OHLCV for crypto tickers; eliminates Yahoo Finance proxy for `*-USD` pairs; reuse the finance pipeline's `api.binance.us` integration

## Indicators

- [ ] **Trend — SMA / EMA** — 20 / 50 / 200-day simple and exponential moving averages; add to `indicators.py`
- [ ] **Momentum — MACD** — 12/26/9 default; signal line crossover; histogram
- [ ] **Volatility — Bollinger Bands** — 20-day SMA ± 2σ; add `bb_upper`, `bb_lower`, `bb_signal` to output
- [ ] **Volatility — ATR** — 14-day Average True Range; useful for position sizing
- [ ] **Volume — OBV** — On-Balance Volume; trend confirmation

## Integration

- [ ] **Clockwork to-tradility webpage** — new Flask route `/to-tradility` in clockwork web; loads `tradility-analysis.json`; shows RSI and VWAP signal badges per ticker; filter by signal (overbought / oversold / below VWAP)
- [ ] **Clockwork API endpoint** — `GET /api/tradility-analysis` to serve the JSON to the webpage (similar to `/api/invest-holdings`)
- [ ] **Monthly controller hook** — add a tradility run to the finance pipeline's monthly controller or create a standalone cron entry in clockwork

## Done
