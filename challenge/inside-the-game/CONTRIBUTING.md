# Contributing to MatchPulse Live

Thanks for your interest in improving MatchPulse Live.

## Scope

This repository contains the standalone **challenge module** for MatchPulse
Live. It is a self-contained FastAPI service that turns synthetic match events
into multilingual, personalized, evidence-backed insights.

## Ground rules

1. **Synthetic data only.** Never add real league, club, player, fixture, or
   match data. `MatchEvent.source` must stay `synthetic`, and
   `CHALLENGE_SYNTHETIC_ONLY` must remain enabled.
2. **No real copyrighted content.** No broadcast footage, commentary, music,
   badges, crests, or protected marks.
3. **No secrets.** API keys, connection strings, and publish profiles must
   never be committed. Use environment variables and placeholders.
4. **The deterministic pipeline stays authoritative.** Microsoft Foundry is an
   optional copy editor. It must never create match state, alter evidence, or
   introduce unsupported numbers. Every failure mode must fall back cleanly.
5. **No weakening of tests.** Do not skip, mark, or weaken a test to make a
   change pass.

## Local setup

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

Python 3.12 is the reference interpreter. Python 3.11+ is supported.

## Required checks before opening a pull request

```bash
python -m compileall src
python -m ruff check src tests scripts
python -m pytest -q
python -m pytest --cov=matchpulse --cov-report=term-missing --cov-fail-under=80
python scripts/preflight.py
```

The same checks run in `.github/workflows/challenge-ci.yml`.

## Visual Studio

Open `MatchPulse.Live.sln` and select the `.venv` interpreter. See
`README-VISUAL-STUDIO.md`. The command-line path above remains the
authoritative validation path.

## Commit messages

Use imperative, scoped subjects, for example:

```
feat(challenge): add guarded foundry provider abstraction
test(infra): assert bicep template has no hardcoded identifiers
```

## Licensing

By contributing you agree that your contributions are licensed under the
Apache License, Version 2.0.
