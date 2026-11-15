"""Resolve a short-lived Schwab access token on the host and write it to one file.

The pod cannot reach the vault or the refresh token, so the host resolves a fresh access token with
tradility's own resolver (the configured TRADILITY_SCHWAB_TOKEN_COMMAND) shortly before the scheduled run.
Only the access token (about 30 minutes) ever reaches the cluster, as a Secret.

    make_token_secret.py ENV_FILE OUT_FILE

writes OUT_FILE (mode 0600) and prints only whether it succeeded, never the token.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def env_value(text: str, key: str) -> str:
    found = ""
    for line in text.splitlines():
        match = re.match(rf"\s*{re.escape(key)}\s*=\s*(.*?)\s*$", line)
        if match:
            value = match.group(1)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            found = value
    return found


def main(argv: list[str]) -> int:
    env_file, out_file = argv
    try:
        text = Path(env_file).read_text(encoding="utf-8")
    except OSError:
        print("cannot read the environment file", file=sys.stderr)
        return 1
    command = env_value(text, "TRADILITY_SCHWAB_TOKEN_COMMAND")
    if not command:
        print("TRADILITY_SCHWAB_TOKEN_COMMAND is not set", file=sys.stderr)
        return 1
    sys.path.insert(0, str(REPO / "src"))
    from tradility.sources.schwab import resolve_access_token

    resolution = resolve_access_token(token_command=command)
    if not resolution.token:
        print(f"no token: {resolution.error}", file=sys.stderr)
        return 1
    fd = os.open(out_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(resolution.token)
    print("token written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
