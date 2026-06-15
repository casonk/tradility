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
