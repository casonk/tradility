# Contributor Architecture Blueprint — tradility

## Purpose

`tradility` computes technical indicators (RSI, VWAP, 50/200-day trend),
historical performance, and transparent technical-rating components on
investment ticker holdings and watchlists. It outputs structured JSON and owns
a standalone web application; portfolio portals such as Clockwork are optional
consumers rather than UI or runtime dependencies.

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
                                      │
                                      ▼
                            web.py + templates/
                         (standalone loopback UI/API)
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
| `web.py` | Standalone Flask application: bounded analysis/detail APIs, CSRF, refresh lifecycle, and trader-service proxy |
| `web_state.py` | Atomic owner-only manual orders, broker-export merge, and position summary |
| `templates/`, `static/` | Packaged responsive dashboard and standalone theme assets |
| `scripts/run_scheduled_analysis.sh` | Locked, non-interactive scheduler entry point |

## Key Design Constraints

- Market/provider I/O stays in the domain adapters. The web application only
  reads local artifacts and optionally proxies to Tradility's loopback trader
  service, which remains the credential boundary.
- `indicators.py` is stateless; all functions take pandas Series and return Series.
- `exports/` is gitignored. Generated JSON is never committed.
- Slow-changing profiles have their own gitignored cache and provider/freshness
  metadata. A failed refresh may use the last cached profile; no synthetic
  mission or vision is presented as company-authored text.
- Crypto tickers map to Yahoo Finance format (`BTC` → `BTC-USD`) in `analyze.py` only.
- OAuth ownership stays outside Tradility. A token command lets the scheduler
  reuse the governed credential pipeline without persisting secrets here.
- The standalone web server binds to loopback by default. Browser mutations
  require a per-session CSRF token; broker credentials remain in the separate
  trader service.

## Planned Extensions (see BACKLOG.md)

- Native Binance and fundamentals/research adapters under `src/tradility/sources/`
- Credential-pipeline token helper for unattended Schwab refresh
- Additional volatility, momentum, and volume indicators in `indicators.py`
