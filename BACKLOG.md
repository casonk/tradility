# BACKLOG.md

## Active

- [ ] [manual:2026-11-05] **Fill the missing current-day Schwab bar** — Schwab posts a
  day's daily candle after the 16:30 ET refresh, so 238 of 312 Schwab-sourced tickers
  ended a session behind (Yahoo-sourced ones did not) and the chart skips that day
  until the next run. Add a later refresh, or fill the last bar from Yahoo.
- [ ] [manual:2026-11-05] **Refresh the holdings aggregate on a schedule** — the ticker
  list comes from personal-finance's `holdings-aggregate.json`, rebuilt only by its
  own job (last 2026-11-04 16:11 ET), so new positions and fills do not appear until
  then.
- [ ] [manual:2026-11-05] **Move fundamentals with the live price** — valuation ratios
  (P/E, PEG, P/B) stay on the last report while price, RSI, VWAP and trend are live.
- [ ] [manual:2026-11-05] **Offer an intraday VWAP** — VWAP 20 is a rolling daily-bar
  proxy (typical price × volume), so it will not match a charting platform's session
  VWAP; Schwab's API has no indicator endpoints, so this would be computed from
  intraday bars.

- [ ] [manual:2026-10-15] **Ask GitHub Support to purge pre-rewrite commits** — this
  repository's published history was rewritten (most recently on 2026-10-15,
  to remove per-commit AI attribution trailers). GitHub keeps serving the old
  commits to anyone with a hash, and through the read-only `refs/pull/*` refs
  of earlier pull requests, until Support garbage-collects them. The request
  is filed once for the whole portfolio from traction-control's backlog; mark
  this done when an old hash returns 404 here. Never push an old clone's
  history back.

## Trading

- [ ] [manual:2026-11-06] **More elaborate order strategies** — ladders ship first (the Order templates
  tab); next are dollar-sized rungs (turned into whole shares or a coin's lot size), a take-profit sell
  placed above each filled buy rung, geometric rung spacing and sizing, and a way to cancel or replace a
  whole ladder in one step. Each needs its own limit and failure rules before it places anything.
- [ ] [manual:2026-11-06] **Options straddles (a call and a put)** — Tradility's order path handles stocks,
  ETFs and coins only: no option chains, expiries, strikes or Schwab option legs, and the ticker format does not
  accept an option symbol. A real options straddle needs a read-only chain view and a preview first, then its
  own limits and failure rules. (The two-sided ladder, buys below and sells above the price, is done.)

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

- [ ] [manual:2026-11-01] **Fidelity live read** — Fidelity accounts are only known from monthly or quarterly
  statements (the latest is as of 2026-02-28), so their balances, positions and the "everything" totals are stale
  and there are no open orders. Fidelity has no individual API and SnapTrade's connection is read-only; options are a
  read-only authenticated browser session (windshield, with the 2FA handling the statement downloader already has),
  or an aggregator that supports Fidelity positions. Either one feeds `statement_accounts`' replacement.
- [ ] [manual:2026-11-01] **Fidelity order submission (high risk, unofficial)** — decided against for now: only
  Fidelity's website can submit orders, so it would mean browser automation behind the same policy, caps, preview
  and audit log, and it may breach Fidelity's terms. Revisit if Fidelity or an aggregator adds trading.

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

- [ ] [manual:2026-11-04] **Finish portal federation** — configure Clockwork to
  link or reverse-proxy the standalone Tradility service, add optional provider
  contracts for To Invest targets and consolidated finances, then remove its
  duplicated Tradility template and domain API implementations after the
  standalone deployment is verified.
- [x] **Standalone Tradility web application** — `tradility-web` serves the
  packaged indicator/detail UI and Tradility-owned analysis, order, refresh,
  account, transfer, and trader-proxy APIs without Clockwork.
- [x] **Legacy Clockwork to-tradility webpage** — `/to-tradility` loads
  `tradility-analysis.json`; retained temporarily as a migration fallback.

## Done

- [x] [manual:2026-11-06] **Order templates on the Clockwork page** — the same tab on `/to-tradility` (Clockwork
  #176), sharing the saved templates with the standalone application.
- [x] [manual:2026-11-06] **Two-sided ladders (straddles of orders)** — a template can hold buy rungs below and
  sell rungs above the price: one preview, one phrase naming both sides (`BUY 3 SELL 3 AAPL`), both sides in
  the per-order and daily limits, and the sell rungs checked against what the account holds before anything is
  sent.
- [x] [manual:2026-11-05] **Live prices for every ticker** — a top-bar Live control (off,
  5 s to 5 min) fetches one batch of Schwab and Binance.US prices; coin prices work
  with Binance trading off. (#53, #54)
- [x] [manual:2026-11-05] **Live indicators** — RSI, VWAP, moving averages, trend and the
  technical rating move with the live price from stored bars; the detail rating,
  range and indicator cards follow too. (#55, #59)
- [x] [manual:2026-11-05] **Fast orders refresh** — parallel broker requests and a
  recent-days merge (about 12 s instead of 2.5 minutes), with a user timer. (#55)
- [x] [manual:2026-11-05] **Never exchange the Schwab token per request** — one exchange
  per 20 minutes per process, locked across processes, with a failure backoff; the
  per-request exchange had got the refresh token revoked. (#57)
- [x] [manual:2026-11-05] **Serve strict JSON** — bare NaN in the export broke Safari;
  it is filled at the source and read as null by the servers. (#56)

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
