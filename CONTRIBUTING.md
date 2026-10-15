# Contributing

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pre-commit install
```

## Local CI

```bash
pre-commit run --all-files
pytest -q
```

## Conventions

- Conventional Commits: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`
- Keep `fetch.py` network-only; indicators must be testable offline with synthetic data
- Generated `exports/` output is gitignored — never commit analysis JSON
- Follow portfolio standards in `../../util-repos/traction-control/AGENTS.md`

## AI Contribution Disclosure

Parts of this repository's code, documentation and commit history were drafted
with AI coding assistants, including Claude Code, OpenAI Codex, GitHub Copilot
and Continue. The maintainer directs that work, reviews and tests each change
before it merges, and is responsible for the result.

This section is the repository-wide disclosure. Individual commits, pull
requests and files do not carry per-tool attribution lines.
