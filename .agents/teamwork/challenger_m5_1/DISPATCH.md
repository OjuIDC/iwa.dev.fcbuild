# Dispatch: Milestone 5 Challenger 1 (Compose Isolation & Secrets Hardening)

**Agent**: `challenger_m5_1`
**Role**: Milestone 5 Challenger 1 (Compose Isolation & Secrets Hardening)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m5_1`
**Timestamp**: 2026-10-03T21:28:00Z

---

## 1. Objective
Adversarially challenge Docker Compose topology, secrets isolation, environment variable leakage, service network boundaries, and `tests/test_compose_isolation.py`.

## 2. Mandatory Reading
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Worker Handoff: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m5_1\handoff.md`
4. `docker-compose.yml`
5. `config.example.env`
6. `.gitignore`
7. `tests/test_compose_isolation.py`

## 3. Tasks & Attacks
1. Challenge Secrets & Mount Boundaries:
   - Verify `fc27_migrator_uri` is ONLY mounted into `db-migrate`.
   - Verify `fc27_app_uri` is ONLY mounted into `graph-api`.
   - Verify the 4 bootstrap secrets are ONLY mounted into `db-bootstrap`.
   - Verify NO database secrets are mounted into any other service (redis, postgres, model-health, etc.).
2. Challenge Environment Leakage:
   - Verify NO service in `docker-compose.yml` passes `POSTGRES_URI`, `POSTGRES_ADMIN_URI`, or `FC27_*_PASSWORD` as literal environment variables.
   - Verify `secrets/` is strictly ignored by git (`.gitignore`).
3. Challenge Fail-Closed Behavior:
   - Verify `settings.read_secret` raises `ConfigError` when a `*_FILE` variable points to a non-existent file.
4. Verify `tests/test_compose_isolation.py`:
   - Inspect all assertions and test logic for holes or bypasses.
5. Formulate explicit verdict: **APPROVE** or **REQUEST_CHANGES**.
6. Write `challenge_report.md` and `handoff.md` in your working directory.
7. Notify orchestrator via `send_message` when done.
Do NOT modify production source code in `src/` or `migrations/`.
