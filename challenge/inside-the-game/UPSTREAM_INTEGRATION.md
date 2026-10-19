# Integrating with iwa.dev.fcbuild

## Recommended repository layout

```text
iwa.dev.fcbuild/
  challenge/inside-the-game/     # this package, unchanged
  src/                           # existing secure platform
  tests/                         # existing test suites
  docker-compose.yml             # existing stack
```

This keeps challenge code auditable and prevents the seven-day submission sprint from crossing the parent program's dispatch/identity phase boundaries.

## Integration steps

1. Create branch `challenge/inside-the-game-2026` from the current validated main branch.
2. Copy this package to `challenge/inside-the-game/`.
3. Run its tests independently:

   ```powershell
   cd challenge\inside-the-game
   py -3.12 -m venv .venv
   .\.venv\Scripts\python -m pip install -e ".[dev]"
   .\.venv\Scripts\python -m pytest
   .\.venv\Scripts\python scripts\preflight.py
   ```

4. Keep these parent flags unchanged:

   ```text
   DISPATCH_ENABLED=false
   DISPATCH_WORKER_ENABLED=false
   ```

5. Do not route challenge outputs into approval, assignment, invitation, or player-delivery endpoints.
6. Use `compose.challenge.yml` separately for the public demo. Do not publish the parent `.env`, secrets, local logs, database dumps, identity fixtures, or club configuration.
7. Create a clean public repository from the challenge directory if publishing the full private parent repository would expose history or unrelated files.

## Optional compose attachment

If the parent stack is used only on the entrant's machine, add MatchPulse as a separate service by referencing the challenge Dockerfile and exposing `127.0.0.1:8080`. Do not attach it to the PostgreSQL identity network. The challenge module requires no access to approval or identity stores.

## What not to reuse in the public submission

- EA API responses, club IDs, real player identifiers, screenshots, or credentials.
- Identity/remediation logs that may reveal internal topology.
- Legacy FC26 files, unrelated assignments, or password exports.
- Any proprietary footage, music, logo, crest, or copied commentary.

## Public-repository cut

From the private repo root:

```bash
mkdir ../iwa-fcbuild-matchpulse-public
cp -R challenge/inside-the-game/. ../iwa-fcbuild-matchpulse-public/
cd ../iwa-fcbuild-matchpulse-public
rm -f .env
python scripts/preflight.py
git init
git add .
git commit -m "Inside the Game submission: MatchPulse Live"
```

Review `git status`, `git log --stat`, and the GitHub secret-scanning result before adding the public URL to the submission.
