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
- [ ] **Schwab API adapter** — price-history adapter and token-command boundary added in v0.2; remaining: provide the finance pipeline's non-interactive access-token helper, validate it live, and add batched real-time quotes
- [ ] **Binance API adapter** — `src/tradility/sources/binance.py`; native OHLCV for crypto tickers; eliminates Yahoo Finance proxy for `*-USD` pairs; reuse the finance pipeline's `api.binance.us` integration

## Indicators

- [ ] **Trend — SMA / EMA** — 50 / 200-day simple moving averages and the combined long-term trend landed in v0.2; remaining: 20-day SMA plus 20 / 50 / 200-day EMAs
- [ ] **Momentum — MACD** — 12/26/9 default; signal line crossover; histogram
- [ ] **Volatility — Bollinger Bands** — 20-day SMA ± 2σ; add `bb_upper`, `bb_lower`, `bb_signal` to output
- [ ] **Volatility — ATR** — 14-day Average True Range; useful for position sizing
- [ ] **Volume — OBV** — On-Balance Volume; trend confirmation

## Integration

- [x] **Clockwork to-tradility webpage** — `/to-tradility` loads `tradility-analysis.json` and renders RSI/VWAP signals
- [x] **Clockwork API endpoint** — `GET /api/tradility-analysis` serves the JSON to the webpage
- [ ] **Clockwork daily refresh** — tracked wrapper added in v0.2; add/install a Clockwork-owned weekday systemd timer and expose stale/run status in the dashboard

## Done
