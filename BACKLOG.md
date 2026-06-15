# BACKLOG.md

## Active

## Data Sources

- [ ] **Alpha Vantage adapter** — `src/tradility/sources/alpha_vantage.py`; free tier with API key; better fundamentals and EPS data than yfinance; rate-limited (5 calls/min free tier)
- [ ] **Schwab API adapter** — `src/tradility/sources/schwab.py`; real-time intraday quotes for equity holdings; reuse OAuth credentials from personal-finance `downloader/shared.py`
- [ ] **Binance API adapter** — `src/tradility/sources/binance.py`; native OHLCV for crypto tickers; eliminates Yahoo Finance proxy for `*-USD` pairs; reuse `api.binance.us` integration from personal-finance

## Indicators

- [ ] **Trend — SMA / EMA** — 20 / 50 / 200-day simple and exponential moving averages; add to `indicators.py`
- [ ] **Momentum — MACD** — 12/26/9 default; signal line crossover; histogram
- [ ] **Volatility — Bollinger Bands** — 20-day SMA ± 2σ; add `bb_upper`, `bb_lower`, `bb_signal` to output
- [ ] **Volatility — ATR** — 14-day Average True Range; useful for position sizing
- [ ] **Volume — OBV** — On-Balance Volume; trend confirmation

## Integration

- [ ] **Clockwork to-tradility webpage** — new Flask route `/to-tradility` in clockwork web; loads `tradility-analysis.json`; shows RSI and VWAP signal badges per ticker; filter by signal (overbought / oversold / below VWAP)
- [ ] **Clockwork API endpoint** — `GET /api/tradility-analysis` to serve the JSON to the webpage (similar to `/api/invest-holdings`)
- [ ] **Monthly controller hook** — add tradility run to personal-finance `monthly_controller.py` or create a standalone cron entry in clockwork

## Done
