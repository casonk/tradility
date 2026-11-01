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
                                      ├──────────────▶ profiles.py
                                      │                (normalized TTL cache)
                                      ▼                         ▲
                               indicators.py                    │
                              (rsi, vwap, SMA, signals)    Yahoo profile API
                                      │
                                      ▼
                          exports/tradility-analysis.json
```

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing; orchestrates analyze_tickers call |
| `analyze.py` | Pipeline: load tickers → fetch → compute indicators, returns, rating, chart series, and attach profiles → serialize JSON |
| `fetch.py` | Source selection, yfinance download/profile calls, fallback, OHLCVResult dataclass |
| `profiles.py` | Stable qualitative-profile contract, safe normalization, atomic TTL cache, and bounded concurrent refresh |
| `sources/schwab.py` | Schwab access-token command boundary and price-history requests |
| `indicators.py` | Pure math: RSI, VWAP, SMA, signal classifiers — no I/O, fully testable |
| `scripts/run_scheduled_analysis.sh` | Locked, non-interactive entry point for Clockwork |

## Key Design Constraints

- Network I/O is limited to `fetch.py` and adapters under `sources/`; tests mock at those boundaries.
- `indicators.py` is stateless; all functions take pandas Series and return Series.
- `exports/` is gitignored. Generated JSON is never committed.
- Slow-changing profiles have their own gitignored cache and provider/freshness
  metadata. A failed refresh may use the last cached profile; no synthetic
  mission or vision is presented as company-authored text.
- Crypto tickers map to Yahoo Finance format (`BTC` → `BTC-USD`) in `analyze.py` only.
- OAuth ownership stays outside Tradility. A token command lets the scheduler
  reuse the governed credential pipeline without persisting secrets here.

## Planned Extensions (see BACKLOG.md)

- Native Binance and fundamentals/research adapters under `src/tradility/sources/`
- Credential-pipeline token helper for unattended Schwab refresh
- Additional volatility, momentum, and volume indicators in `indicators.py`
