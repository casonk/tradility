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

## Trading

- [ ] [manual:2026-11-01] **Enable Binance.US trading** — the code is done (signed orders, tick/lot/minimum
  filters, `api_credentials.py --trade` in the finance repo) but there is no trade-enabled key yet. Create a key
  with trading on and withdrawals off in its own KeePass entry, add `institutions.bin.trade_credentials` to the
  finance config, set `TRADILITY_BINANCE_TRADE_CREDENTIAL_COMMAND`, set `binance = true` in
  `~/.config/tradility/trading.toml`, then check a preview and place one small order. Also disable withdrawals
  on the current read-only key, which the Accounts panel shows can withdraw.
- [ ] [manual:2026-11-01] **Enable Binance.US withdrawals (optional, high risk)** — the code is done and off by
  default. Needs a separate withdraw-enabled key restricted to this machine's IP behind
  `TRADILITY_BINANCE_WITHDRAW_CREDENTIAL_COMMAND`, destinations and caps under `[withdrawals]` in the policy and
  Binance.US's own address whitelist; test with a tiny amount.
- [ ] [manual:2026-11-01] **Replace or amend an open Schwab order** — orders can be previewed, sent and cancelled
  but not edited; add a replace (cancel plus new, or Schwab's replace endpoint) with the same preview and typed
  confirmation.

## Data Sources

- [ ] **Alpha Vantage adapter** — `src/tradility/sources/alpha_vantage.py`; free tier with API key; better fundamentals and EPS data than yfinance; rate-limited (5 calls/min free tier)
- [ ] **Schwab API adapter** — five-year price history, the finance pipeline's
  noninteractive rotating-token helper, live validation, and explicit Yahoo
  fallback landed in v0.2; remaining: add batched real-time quotes and surface
  token-health detail without exposing credentials
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

## Done

- [x] **Qualitative company profiles** — attach cached, sourced names,
  descriptions, sector/industry or fund metadata, headquarters, website,
  exchange, employees, entity type, and freshness; leave official vision empty
  rather than generating one.
- [x] **Clickable stock-detail data** — each successful ticker carries a
  chart-ready five-year history, period returns, 52-week range, and transparent
  technical-rating components for the Clockwork detail view.
- [x] **Clockwork daily refresh** — the tracked wrapper and Clockwork-owned
  weekday user-systemd timer are installed; the dashboard exposes stale/run
  status and a safe manual refresh action.
- [x] **Multi-year trend baseline** — the default horizon is five years and
  each successful ticker includes 50/200-day simple moving averages plus a
  combined long-term trend signal alongside RSI and VWAP.
