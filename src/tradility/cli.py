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
        help="Path to holdings-aggregate.json (personal-finance export).",
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
        type=int,
        default=90,
        metavar="DAYS",
        help="Lookback window in calendar days (default: 90).",
    )
    args = parser.parse_args()

    holdings_path = Path(args.holdings).resolve() if args.holdings else None
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Fetching data for {'holdings file' if holdings_path else len(args.tickers)} ticker(s)…")
    payload = analyze_tickers(
        tickers=args.tickers,
        holdings_path=holdings_path,
        period_days=args.period,
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
