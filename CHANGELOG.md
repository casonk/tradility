# Changelog

## [Unreleased]

### Added

- Schwab Trader API daily price-history adapter with an external token-command
  boundary and explicit per-ticker Yahoo fallback.
- Five-year default history, 50/200-day simple moving averages, and a combined
  long-term trend signal.
- Locked scheduled-analysis wrapper for Clockwork-owned systemd execution.

### Changed

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
