# AGENTS.md

## Purpose

`tradility` is a Python package that computes technical indicators (RSI, VWAP) on investment
ticker holdings and watchlists. It reads ticker lists from a personal-finance
`holdings-aggregate.json` export or from an explicit CLI argument, fetches OHLCV price data via
**yfinance**, and outputs a structured `tradility-analysis.json`.

## Portfolio Standards

This repository follows the portfolio-wide conventions defined in
`../../util-repos/traction-control`. Read `traction-control/AGENTS.md`,
`traction-control/CHATHISTORY.md`, and `traction-control/LESSONSLEARNED.md` before making
cross-repo changes.

Shared utility repos available for common capabilities:

- `../../util-repos/archility` — architecture bootstrap, Graphviz diagrams, blueprint audits
- `../../util-repos/auto-pass` — KeePassXC-backed secret retrieval
- `../../util-repos/clockwork` — cron / systemd manifest rendering; tradility integrates here
- `../../util-repos/tachometer` — repo profiling and resource measurement
- `../../util-repos/nordility` — VPN switching
- `../../util-repos/shock-relay` — external messaging (Signal, Telegram, SMS, Gmail)
- `../../util-repos/dyno-lab` — unified test bench (fixtures, mocks, schema validation)
- `../../util-repos/crew-chief` — local Ollama LLM inference

## Repository Layout

```
src/tradility/
  __init__.py       — public re-exports
  __main__.py       — python -m tradility entry point
  cli.py            — argparse CLI (tradility command)
  fetch.py          — yfinance OHLCV download wrapper
  indicators.py     — RSI and VWAP calculations
  analyze.py        — orchestration: load → fetch → compute → serialize
tests/
  test_indicators.py
exports/            — gitignored; runtime JSON output lives here
docs/
  contributor-architecture-blueprint.md
  diagrams/repo-architecture.puml
  diagrams/repo-architecture.drawio
```

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Local CI Verification

Run before every push:

```bash
pre-commit run --all-files
pytest -q
```

## Architecture Notes

- `fetch.py` is the only module that makes network calls. Keep it isolated so indicators and
  analyze are fully testable offline.
- Crypto tickers from Binance use bare symbols (`BTC`). `analyze.py` maps them to Yahoo Finance
  format (`BTC-USD`) for fetching and restores the original symbol in JSON output.
- `exports/` is gitignored. Never commit generated analysis JSON.
- The `--holdings` flag reads `holdings-aggregate.json` from the personal-finance pipeline.
  The personal-finance repo path is a local reference — record it in `REFS-LOCAL.md`.

## Backlog Data Sources

Planned data source adapters (see BACKLOG.md):

- **Alpha Vantage** — fundamentals and additional technical endpoints
- **Schwab API** — real-time quotes for equity holdings (reuse personal-finance credentials)
- **Binance API** — native crypto OHLCV (better granularity than Yahoo for crypto tickers)
