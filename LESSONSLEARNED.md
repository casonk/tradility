# LESSONSLEARNED.md

Tracked durable lessons for `tradility`.
Unlike `CHATHISTORY.md`, this file should keep only reusable lessons that should change how future sessions work in this repo.

## How To Use

- Read this file after `AGENTS.md` and before `CHATHISTORY.md` when resuming work.
- Add lessons that generalize beyond a single session.
- Keep entries concise and action-oriented.
- Do not use this file for transient status updates or full session logs.
- Before final reporting for meaningful work, either add any durable lesson
  discovered during the request or explicitly report why no durable lesson was
  added.

## Lessons

- Document the repository around its real execution, curation, or integration flow instead of only the top-level folder list.
- Keep local-only, private, reference-only, or generated boundaries explicit so published or runtime behavior is not confused with offline material or non-committable inputs.
- Keep tracked examples, fixtures, and `.example` templates scrubbed of real paths, usernames, hostnames, account identifiers, or other instance-specific values; real operator data belongs only in gitignored local config.
- If the repo exposes a dashboard or admin surface, keep loopback-safe defaults in the app itself and treat wider network exposure as an explicit trust-boundary decision rather than a documentation assumption.
- Re-run repo-appropriate validation after changing generated artifacts, diagrams, workflows, or other CI-facing files so formatting and compatibility issues are caught before push.
- True intraday VWAP resets each session; on daily bar data use a rolling-window proxy and document the distinction clearly in both code and output JSON so consumers are not misled.
- Crypto tickers from Binance use bare symbols (BTC, ETH). yfinance requires the -USD suffix (BTC-USD). Map at fetch time and restore original symbol in output to keep JSON consistent with holdings-aggregate.json.
