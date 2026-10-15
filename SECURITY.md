# Security Policy

## Reporting a Vulnerability

Please report security issues privately to casonk@umich.edu rather than opening a public issue.

## Scope

- `tradility` makes outbound HTTPS requests to Yahoo Finance (via yfinance). No credentials are sent.
- Planned adapters (Alpha Vantage, Schwab, Binance) will use API keys stored via `auto-pass` (KeePassXC) — never hardcoded or committed.
- Generated `exports/` output may contain portfolio data and is gitignored by default.
- Keep local holdings inputs and generated portfolio reports out of public
  issues, pull requests, and tracked examples. Use synthetic ticker lists
  when reporting a bug.
