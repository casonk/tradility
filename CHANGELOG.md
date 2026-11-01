# Changelog

## [Unreleased]

- Add order placement, off by default: `tradility.trading` and the local `tradility.trader_service` (127.0.0.1:8768). A policy file (`~/.config/tradility/trading.toml`, owner-only, fails closed) sets the enable switch, ticker allowlist, per-order and daily dollar caps, a price band and the one Schwab account. Orders are previewed (broker validation without placing), then confirmed by typing the exact phrase against a single-use two-minute token; every step goes to an owner-only audit log, and an unknown outcome counts against the daily cap and is never retried. Limit and market orders and cancel, for Schwab and Binance.US. The order reader stays GET-only.
- Order fetch: a held coin Binance.US does not list (HTTP 400 Invalid symbol) is skipped and counted as `unlisted_symbols` instead of failing the whole Binance source; other errors still fail it. The symbol cap rose to 120.
- Add read-only live orders: `python -m tradility.orders` fetches order history and open orders from Schwab (the same token command as price history) and Binance.US (signed GETs; `TRADILITY_BINANCE_API_KEY`/`TRADILITY_BINANCE_API_SECRET` or `TRADILITY_BINANCE_CREDENTIAL_COMMAND` printing `{api_key, api_secret}` JSON) and writes one owner-only `exports/tradility-orders.json` that Clockwork reads. The module only issues GET requests, never logs or stores a credential, and reports a failing source by class name only. The scheduled run fetches orders after the analysis and never fails on them (`TRADILITY_FETCH_ORDERS=0` skips it).
### Added

- Cached, sourced qualitative company/fund profiles with canonical names,
  descriptions, sector/industry or fund metadata, location, website, exchange,
  employee count, entity type, and retrieval timestamps.
- Chart-ready historical closes, calendar-period returns, 52-week range, and a
  transparent component-based technical rating for stock detail consumers.
- Schwab Trader API daily price-history adapter with an external token-command
  boundary and explicit per-ticker Yahoo fallback.
- Five-year default history, 50/200-day simple moving averages, and a combined
  long-term trend signal.
- Locked scheduled-analysis wrapper for Clockwork-owned systemd execution.

### Changed

- Scheduled and ordinary CLI analysis now enriches eligible tickers from a
  30-day Yahoo profile cache by default; `--skip-profiles` keeps price-only runs
  available.
- `--period` now accepts long horizons such as `5y`, `10y`, and `max`; legacy
  integer day counts remain supported.

## [0.1.0] — 2026-06-15

### Added

- Initial release: RSI (14-period) and rolling VWAP (20-day) indicators via yfinance
- `--holdings` flag to load tickers from `holdings-aggregate.json` (exported by a finance pipeline)
- `--tickers` flag for explicit ticker lists
- Crypto ticker mapping: bare symbols (BTC) → Yahoo Finance format (BTC-USD) at fetch time
- RSI signals: oversold / neutral / overbought
- VWAP signals: above / at / below (±0.5% tolerance band)
- JSON output: `exports/tradility-analysis.json`
- CLI entry point: `tradility`
