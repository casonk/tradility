"""CLI entry point for tradility."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tradility.analyze import analyze_tickers


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run technical analysis (RSI, VWAP) on investment tickers."
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--holdings",
        metavar="PATH",
        help="Path to holdings-aggregate.json (exported by a finance pipeline).",
    )
    source.add_argument(
        "--tickers",
        nargs="+",
        metavar="TICKER",
        help="Explicit ticker symbols (crypto needs -USD suffix, e.g. BTC-USD).",
    )
    parser.add_argument(
        "--output",
        default="exports/tradility-analysis.json",
        metavar="PATH",
        help="Output JSON file path (default: exports/tradility-analysis.json).",
    )
    parser.add_argument(
        "--period",
        default="5y",
        metavar="LOOKBACK",
        help="History lookback: DAYS, Nd, Ny, or max (default: 5y).",
    )
    parser.add_argument(
        "--source",
        choices=("auto", "schwab", "yfinance"),
        default="auto",
        help="Market-data source. auto prefers Schwab and falls back to yfinance.",
    )
    parser.add_argument(
        "--schwab-token-command",
        default="",
        metavar="COMMAND",
        help="Command that prints a Schwab access token or JSON containing access_token.",
    )
    args = parser.parse_args()

    holdings_path = Path(args.holdings).resolve() if args.holdings else None
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Fetching data for {'holdings file' if holdings_path else len(args.tickers)} ticker(s)…")
    payload = analyze_tickers(
        tickers=args.tickers,
        holdings_path=holdings_path,
        history_period=args.period,
        source=args.source,
        schwab_token_command=args.schwab_token_command,
    )

    output_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(
        f"Wrote: {output_path} "
        f"({payload['ticker_count']} tickers, "
        f"{payload['fetch_ok']} ok, "
        f"{payload['fetch_errors']} errors)"
    )
    if payload["fetch_errors"]:
        errs = [r["ticker"] for r in payload["tickers"] if r.get("fetch_status") == "error"]
        print(f"Errors: {', '.join(errs)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
