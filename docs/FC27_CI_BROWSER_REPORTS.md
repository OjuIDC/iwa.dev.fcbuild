# FC27 Browser-Previewable Test Reports and Sanitization Policy

## 1. Overview and Authority

Every FC27 GitHub Actions test execution (`test-fast`, `ci`, and `test-nightly`) generates browser-previewable test reports and sanitized report artifacts:

- **Report Builder**: `scripts/build_test_report.py`
- **Shard Report Aggregator & Pages Tree Builder**: `scripts/aggregate_test_reports.py`
- **Fail-Closed Privacy & Self-Contained HTML Scanner**: `scripts/sanitize_test_report.py`
- **Policy Test Suite**: `tests/test_ci_report_sanitization.py`

---

## 2. Browser Report Layers

### 2.1 Layer 1 — GitHub Actions Job Summary (`$GITHUB_STEP_SUMMARY`)
Every test job and every workflow's `aggregate-report` job writes a bounded Markdown report directly to `$GITHUB_STEP_SUMMARY` (`ACTIONS_JOB_SUMMARY_CREATED=YES`, `ACTIONS_AGGREGATE_SUMMARY_CREATED=YES`). Each summary is viewable directly in the GitHub Actions run UI without downloading any ZIP archive and includes:
- Workflow name, test group name, commit SHA, and pull-request number (when applicable);
- Python version and PostgreSQL version (when applicable);
- Reconciled `collected`, `passed`, `failed`, and `skipped` counts;
- Total execution duration and top 10 slowest tests;
- Sanitized failure summary (when any test fails);
- JUnit XML and self-contained HTML report generation status;
- Artifact download instructions and browser-preview mode status.

### 2.2 Layer 2 — Self-Contained HTML Test Report (`test-report/index.html`)
`scripts/build_test_report.py` and `scripts/aggregate_test_reports.py` generate a 100% self-contained HTML report (`HTML_REPORT_SELF_CONTAINED=YES`, `HTML_EXTERNAL_RESOURCES=0`):
- All CSS and interactive metadata are inline inside `index.html`.
- No external `<script src="http...">`, external stylesheets, remote fonts, remote images, or tracking pixels are permitted.
- Raw `pytest-html` `<h2>Environment</h2>` / `<table id="environment">` blocks and `data-jsonblob` environment dumps are stripped prior to rendering.

### 2.3 Layer 3 — Unified Sanitized JUnit XML (`test-results/junit.xml`) and Durations (`test-results/durations.json`)
Every job and aggregate report produces a sanitized JUnit XML file (`test-results/junit.xml`) and a JSON durations report (`test-results/durations.json`).

---

## 3. GitHub Pages Security Evaluation and Job Summary Fallback

### 3.1 Repository Visibility and Pages Security Decision
Repository `iwa-ojuidc/iwa.dev.fcbuild` is a private repository (`visibility: private`) on a plan where private GitHub Pages access control is unavailable (`404 Not Found` on `/repos/iwa-ojuidc/iwa.dev.fcbuild/pages`). Because enabling public GitHub Pages on a private repository would expose internal module names, test names, and commit metadata publicly, the authoritative security posture is:

- `PAGES_SECURITY_DECISION=NOT_APPROVED_USE_JOB_SUMMARY_ONLY`
- `BROWSER_PREVIEW=JOB_SUMMARY_ONLY`
- `HTML_REPORT_PUBLICATION=SKIPPED_PAGES_UNAVAILABLE_OR_UNAPPROVED`

### 3.2 Trusted-Context Gating and Fork Pull-Request Isolation
`scripts/classify_ci_changes.py` (`evaluate_trusted_context`) and the `publish-pages` job enforce:
- `PAGES_PUBLICATION_TRUSTED_CONTEXT_ONLY=YES`
- `UNTRUSTED_FORK_PAGES_PUBLICATION=NO`
- Untrusted fork pull requests (`github.event.pull_request.head.repo.fork == true` or mismatched head repository) and `pull_request_target` events always evaluate to `trusted_context=false` and `can_publish_pages=false`.
- `publish-pages` only executes when **both** `trusted_context == true` **and** the repository variable `vars.FC27_ENABLE_PAGES_TEST_REPORTS == 'true'` are set by an operator after confirming acceptable Pages visibility.
- When `can_publish_pages` is `false`, workflows automatically use the Job Summary fallback (`BROWSER_PREVIEW=JOB_SUMMARY_ONLY`) and attach the sanitized self-contained HTML report as a 7-day workflow artifact without failing the workflow.

---

## 4. Fail-Closed Privacy and Security Sanitization (`scripts/sanitize_test_report.py`)

Before any HTML report, JUnit XML file, Markdown Job Summary, or JSON metadata file is written to `$GITHUB_STEP_SUMMARY` or uploaded as a GitHub Actions artifact, `scripts/sanitize_test_report.py` scans all generated files and fails closed (`exit code 1`) if any forbidden pattern is present:

| Forbidden Category | Description |
| :--- | :--- |
| `POSTGRESQL_URI` | `postgres://` or `postgresql://` URIs and `host=... user=... password=...` conninfo strings |
| `REDIS_URL` | `redis://` and `rediss://` connection URLs |
| `DISCORD_TOKEN` | Discord bot tokens and `DISCORD_BOT_TOKEN=` assignments |
| `JWT_TOKEN` | Raw JWT or signed session tokens (`eyJ...`) |
| `API_KEY` | Langfuse (`sk-lf-`, `pk-lf-`), OpenAI (`sk-`), GitHub (`ghp_`, `github_pat_`), or AWS (`AKIA...`) keys |
| `AUTHORIZATION_HEADER` | `Authorization: Bearer/Basic/Token/Bot ...` headers |
| `COOKIE_HEADER` | `Cookie:` and `Set-Cookie:` headers |
| `WINDOWS_USER_PATH` | Host paths matching `C:\Users\...` or `/home/runner/work/...` |
| `UNC_PATH` | Network share paths (`\\server\share...`) |
| `PRIVATE_IP_ADDRESS` | RFC 1918 private IPv4 addresses (`10.x`, `192.168.x`, `172.16-31.x`) |
| `DISCORD_SNOWFLAKE` | Raw 17-to-20-digit Discord snowflake identifiers |
| `PLAYER_REFERENCE` | Player pseudonyms or synthetic IDs (`plr_ecf_...`, `plr_synth_...`) |
| `RAW_PROVIDER_FIELD` | Raw provider payload keys (`personaName`, `proName`, `blazeId`, `nucleusId`, `eaAccountId`, `gamerTag`) |
| `ENVIRONMENT_DUMP` | `pytest-html` `#environment` tables and CI runtime token environment variables |
| `DATABASE_QUERY_PARAMETER` | PostgreSQL `DETAIL: Key (...)` and `PARAMETERS: $1 = ...` dumps |
| `UNSANITIZED_STACK_TRACE` | Absolute host filesystem paths inside stack trace `File "..."` frames |
| `PASSWORD_OR_SECRET_ASSIGNMENT` | Non-placeholder assignments to `password`, `secret`, `api_key`, `access_token`, `service_token`, etc. |
| `HTML_EXTERNAL_*` | External `<script>`, `<link rel="stylesheet">`, remote `<img>`, or tracking pixels in HTML reports |

The scanner **never** prints matched secret or sensitive values; it reports only the file path, forbidden category name, and line number (`FORBIDDEN_VALUES_PUBLISHED=0`).

---

## 5. Report Count Reconciliation and Retention Policy

1. **Count Reconciliation (`REPORT_COUNT_RECONCILIATION=PASS`)**:
   - `scripts/build_test_report.py` verifies `junit_counts == html_counts == summary_counts` and `passed + failed + skipped == collected`.
   - `scripts/aggregate_test_reports.py` verifies that the aggregate report counts equal the exact sum of all shard report counts, and in the full `ci` workflow verifies that the shard sum equals the complete `pytest --collect-only` count. Any discrepancy fails closed.
2. **Retention and Pruning**:
   - **GitHub Actions Artifacts**: `retention-days: 7` on all fast, full, and nightly test report artifacts.
   - **Static Pages Report Tree**: `prune_pages_report_tree()` caps `reports/<workflow>/<run-number>/<run-attempt>/` at `PAGES_REPORT_RETENTION_COUNT=30` runs and prunes the oldest run directories automatically.

---

## 6. Local Report Generation and Verification Commands

```powershell
# Run the 38-case report sanitization and reconciliation policy suite
.\.venv\Scripts\python.exe -m pytest tests/test_ci_report_sanitization.py -q

# Scan any report directory fail-closed
.\.venv\Scripts\python.exe scripts/sanitize_test_report.py .local-artifacts/ci/report-preview
```
