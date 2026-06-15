# Contributor Architecture Blueprint — tradility

## Purpose

`tradility` computes technical indicators (RSI, VWAP) on investment ticker
holdings and watchlists, outputting a structured JSON for downstream consumers
(clockwork web dashboard, manual review).

## Execution Flow

```
holdings-aggregate.json  ──┐
                           ├──▶  analyze.py  ──▶  fetch.py (yfinance)
explicit --tickers list  ──┘          │
                                      ▼
                               indicators.py
                              (rsi, vwap, signals)
                                      │
                                      ▼
                          exports/tradility-analysis.json
```

## Module Responsibilities

| Module | Responsibility |
|---|---|
| `cli.py` | Argument parsing; orchestrates analyze_tickers call |
| `analyze.py` | Pipeline: load tickers → fetch → compute → serialize JSON |
| `fetch.py` | Network isolation; yfinance download; OHLCVResult dataclass |
| `indicators.py` | Pure math: RSI, VWAP, signal classifiers — no I/O, fully testable |

## Key Design Constraints

- `fetch.py` is the **only** module that makes network calls. Tests mock at this boundary.
- `indicators.py` is stateless; all functions take pandas Series and return Series.
- `exports/` is gitignored. Generated JSON is never committed.
- Crypto tickers map to Yahoo Finance format (`BTC` → `BTC-USD`) in `analyze.py` only.

## Planned Extensions (see BACKLOG.md)

- Additional data source adapters under `src/tradility/sources/`
- Additional indicators in `indicators.py`
- Clockwork web integration via `GET /api/tradility-analysis`
