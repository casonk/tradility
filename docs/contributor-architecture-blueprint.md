# Contributor Architecture Blueprint — tradility

## Purpose

`tradility` computes technical indicators (RSI, VWAP, 50/200-day trend),
historical performance, and transparent technical-rating components on
investment ticker holdings and watchlists. It outputs structured JSON for
downstream consumers (Clockwork summary and stock-detail views, manual review).

## Execution Flow

```
holdings-aggregate.json  ──┐                              ┌─▶ Schwab Trader API
                           ├──▶  analyze.py  ──▶  fetch.py ┤
explicit --tickers list  ──┘          │                   └─▶ yfinance fallback
                                      ▼
                               indicators.py
                              (rsi, vwap, SMA, signals)
                                      │
                                      ▼
                          exports/tradility-analysis.json
```

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing; orchestrates analyze_tickers call |
| `analyze.py` | Pipeline: load tickers → fetch → compute indicators, returns, rating, and chart series → serialize JSON |
| `fetch.py` | Source selection, yfinance download/fallback, OHLCVResult dataclass |
| `sources/schwab.py` | Schwab access-token command boundary and price-history requests |
| `indicators.py` | Pure math: RSI, VWAP, SMA, signal classifiers — no I/O, fully testable |
| `scripts/run_scheduled_analysis.sh` | Locked, non-interactive entry point for Clockwork |

## Key Design Constraints

- Network I/O is limited to `fetch.py` and adapters under `sources/`; tests mock at those boundaries.
- `indicators.py` is stateless; all functions take pandas Series and return Series.
- `exports/` is gitignored. Generated JSON is never committed.
- Crypto tickers map to Yahoo Finance format (`BTC` → `BTC-USD`) in `analyze.py` only.
- OAuth ownership stays outside Tradility. A token command lets the scheduler
  reuse the governed credential pipeline without persisting secrets here.

## Planned Extensions (see BACKLOG.md)

- Native Binance and fundamentals/research adapters under `src/tradility/sources/`
- Credential-pipeline token helper for unattended Schwab refresh
- Additional volatility, momentum, and volume indicators in `indicators.py`
