# Changelog

## [Unreleased]

## [0.1.0] — 2026-06-15

### Added

- Initial release: RSI (14-period) and rolling VWAP (20-day) indicators via yfinance
- `--holdings` flag to load tickers from `holdings-aggregate.json` (personal-finance export)
- `--tickers` flag for explicit ticker lists
- Crypto ticker mapping: bare symbols (BTC) → Yahoo Finance format (BTC-USD) at fetch time
- RSI signals: oversold / neutral / overbought
- VWAP signals: above / at / below (±0.5% tolerance band)
- JSON output: `exports/tradility-analysis.json`
- CLI entry point: `tradility`
