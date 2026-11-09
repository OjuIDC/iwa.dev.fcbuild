# Consented East Coasters User-Export Pilot Preparation Package

> **Architecture Role Note**: The **primary** FC 27 acquisition architecture is the community-provider and fixed-contract boundary (`src/fc27_community_provider.py`, `src/fc27_polling_worker.py`, `src/fc27_clubs_api_contracts.py`; see [`FC27_PROVIDER_INTEGRATION.md`](FC27_PROVIDER_INTEGRATION.md)). This document and `scripts/fc27_user_export_import.py` / `scripts/fc27_capture_preflight.py` define the **secondary/offline operator-captured export validation fallback** (`USER_SUPPLIED_EXPORT` / `AUTHORIZED_CAPTURE`).

- **Status:** Approved Offline Pilot Preparation Package
- **Target Club:** East Coasters (`330323` / `common-gen5` / `FC27`)
- **Primary Integration Path:** `USER_SUPPLIED_EXPORT`
- **Live Network / EA Transport:** Disabled (`0` requests, `0` DNS lookups)
- **Persistence (`--apply`):** Deferred / Prohibited
- **Coaching Recommendations:** Disabled / Prohibited

---

## 1. East Coasters Pilot-Input Checklist

Before executing the offline pilot intake, the human operator and authorized club representative must verify every item below:

- [ ] **Authorized Supplier Role Confirmed:** The export file is supplied directly by an authorized East Coasters `CLUB_MANAGER`, `CLUB_ADMIN`, or `DATA_STEWARD`. A claimed role is never verified automatically (`MANAGER_AUTHORITY_STATUS=UNVERIFIED`, `review_status=PENDING_HUMAN_REVIEW`).
- [ ] **Non-Personal Supplier Reference Recorded:** A club-controlled, non-personal identifier (e.g., `ecf-role-ref-club-manager-01`) is assigned. No personal email addresses, EA account IDs, PSN/Xbox IDs, or passwords are requested or included.
- [ ] **Club Identity Metadata Checked:**
  - `CANONICAL_CLUB_NAME=East Coasters` (known alias: `EastCoasters FC`, abbreviation: `ECF`)
  - `CLUB_ID=330323` (`CORROBORATED` via offline secondary observations; **not** authoritative or provider-`VERIFIED`)
  - `PLATFORM=common-gen5` (`CORROBORATED`)
  - `GAME_VERSION=FC27` (`CORROBORATED`)
  - `MATCHMAKING_REGION_CLAIM=East Coast US 1` (`USER_ASSERTED`)
  - `USER_MEMBERSHIP_STATUS=USER_ASSERTED`
  - `MANAGER_AUTHORITY_STATUS=UNVERIFIED` (requires separate human review decision)
- [ ] **Consent Scope & Acknowledgements Verified:** Required base categories (`CLUB_IDENTITY`, `MATCH_RESULTS`) and any conditional categories (`LINEUPS`, `PLAYER_PERFORMANCE`, `BUILD_REFERENCES`) match the contents of the file, with `RETENTION_ACKNOWLEDGED=YES`, `ANALYTICS_USE_ACKNOWLEDGED=YES`, and `TEAM_REVIEW_ACKNOWLEDGED=YES`.
- [ ] **Raw File Retention Agreement Confirmed:** Retention duration and post-review deletion/return handling for the operator-controlled file are agreed **before** validation begins.
- [ ] **Local File Placement Verified:** The file resides at `<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>` outside the repository root (not on a UNC share, HTTP/HTTPS URL, or inside the repository).
- [ ] **File Size & Format Within Bounds:** Regular `.json` or `.csv` file, `<= 5,242,880` bytes (5 MiB), and `<= 100` rows for v1 CSV pilot discipline (`<= 250` matches hard importer cap).
- [ ] **Zero Prohibited Fields:** No `email`, `password`, `token`, `cookie`, `session`, `authorization`, `account_id`, `nucleus_id`, `persona_id`, `gamer_tag`, `psn_id`, `xbox_id`, `ea_id`, `ip_address`, `device_id`, or `raw_provider_payload` fields exist anywhere in the file.

---

## 2. Non-Personal Supplier-Attestation Templates

Supplier attestations must use non-personal, club-controlled references only.

### 2.1 Supplier Attestation Governance Rules
- **Required Supplier Roles:** `CLUB_MANAGER`, `CLUB_ADMIN`, `DATA_STEWARD`.
- **Required Base Authority Scopes:** `CLUB_IDENTITY`, `MATCH_RESULTS`.
- **Conditional Authority Scopes:** `LINEUPS` (required whenever lineup entries appear), `PLAYER_PERFORMANCE` (required whenever player performance records appear), `BUILD_REFERENCES` (required whenever build references appear).
- **No Automatic Verification:** A claimed role is **not** verified automatically. Every template starts with `review_status: "PENDING_HUMAN_REVIEW"` and `manager_authority_status: "UNVERIFIED"`.
- **No Capability Escalation:** Attestation alone never enables `--apply`, persistence, coaching recommendations, or dispatch.

### 2.2 Required Supplier-Attestation Metadata Schema (Full Governance Record)
```json
{
  "supplier_role": "CLUB_MANAGER",
  "supplier_reference": "ecf-role-ref-club-manager-01",
  "club_identity_reference": {
    "club_id": "330323",
    "canonical_club_name": "East Coasters",
    "platform": "common-gen5",
    "game_version": "FC27",
    "club_id_status": "CORROBORATED"
  },
  "authority_scope": [
    "CLUB_IDENTITY",
    "MATCH_RESULTS",
    "LINEUPS",
    "PLAYER_PERFORMANCE",
    "BUILD_REFERENCES"
  ],
  "attested_at": "2026-10-27T12:00:00Z",
  "expires_at": "2026-11-27T12:00:00Z",
  "evidence_references": [
    "obs-proclubstracker-330323",
    "obs-myproclubs-330323"
  ],
  "review_status": "PENDING_HUMAN_REVIEW",
  "reviewer_reference": "ecf-reviewer-ref-placeholder-01",
  "review_reason": "Awaiting independent human review of supplier role and scope",
  "manager_authority_status": "UNVERIFIED",
  "statement_version": "1.0.0"
}
```

### 2.3 Role-Specific Envelope Blocks (`CLUB_MANAGER`, `CLUB_ADMIN`, `DATA_STEWARD`)

#### `CLUB_MANAGER` Envelope Block
```json
{
  "supplier_role": "CLUB_MANAGER",
  "supplier_reference": "ecf-role-ref-club-manager-01",
  "attested_at": "2026-10-27T12:00:00Z",
  "authority_scope": [
    "CLUB_IDENTITY",
    "MATCH_RESULTS",
    "LINEUPS",
    "PLAYER_PERFORMANCE",
    "BUILD_REFERENCES"
  ],
  "statement_version": "1.0.0"
}
```

#### `CLUB_ADMIN` Envelope Block
```json
{
  "supplier_role": "CLUB_ADMIN",
  "supplier_reference": "ecf-role-ref-club-admin-01",
  "attested_at": "2026-10-27T12:00:00Z",
  "authority_scope": [
    "CLUB_IDENTITY",
    "MATCH_RESULTS",
    "LINEUPS",
    "PLAYER_PERFORMANCE"
  ],
  "statement_version": "1.0.0"
}
```

#### `DATA_STEWARD` Envelope Block
```json
{
  "supplier_role": "DATA_STEWARD",
  "supplier_reference": "ecf-role-ref-data-steward-01",
  "attested_at": "2026-10-27T12:00:00Z",
  "authority_scope": [
    "CLUB_IDENTITY",
    "MATCH_RESULTS"
  ],
  "statement_version": "1.0.0"
}
```

---

## 3. Consent-Attestation Template & Scope Matrix

### 3.1 Required Base & Conditional Data Categories

| Category | Requirement Tier | When Required |
|---|---|---|
| `CLUB_IDENTITY` | **Required (Base)** | Required on every East Coasters export pilot submission. |
| `MATCH_RESULTS` | **Required (Base)** | Required on every East Coasters export pilot submission. |
| `LINEUPS` | **Conditional** | Required whenever any match record populates a non-empty `lineup` list. |
| `PLAYER_PERFORMANCE` | **Conditional** | Required whenever any match record populates `player_performances`. |
| `BUILD_REFERENCES` | **Conditional** | Required whenever any player performance record populates `build_reference`. |

### 3.2 Mandatory Operator Acknowledgements & Consent Rules
All three acknowledgements must be explicitly set to `YES` (`true` in JSON):
- `RETENTION_ACKNOWLEDGED=YES`
- `ANALYTICS_USE_ACKNOWLEDGED=YES`
- `TEAM_REVIEW_ACKNOWLEDGED=YES`

**Non-Negotiable Consent Rules:**
- Every included data category in the payload requires matching consent in `data_categories` and matching authority in `authority_scope`.
- Revoked consent (`revoked_at != null`) fails closed immediately (`CONSENT_REVOKED`).
- Consent does **not** imply EA authorization or authorize live provider access.
- Consent does **not** verify club ownership or manager authority (`MANAGER_AUTHORITY_STATUS=UNVERIFIED`).
- Consent does **not** allow raw uploads to be stored or committed (`RAW_UPLOAD_RETAINED: NO`).
- Consent does **not** enable persistence (`--apply`) or coaching recommendations automatically.

### 3.3 Consent-Attestation JSON Template (With Governance Review Status)
```json
{
  "consent_version": "1.0.0",
  "consented_at": "2026-10-27T12:00:00Z",
  "data_categories": [
    "CLUB_IDENTITY",
    "MATCH_RESULTS",
    "LINEUPS",
    "PLAYER_PERFORMANCE",
    "BUILD_REFERENCES"
  ],
  "retention_acknowledged": true,
  "analytics_use_acknowledged": true,
  "team_review_acknowledged": true,
  "revoked_at": null,
  "review_status": "PENDING_HUMAN_REVIEW"
}
```

> **Note:** Inside the strict `FC27UserExportEnvelope` JSON file (`extra="forbid"`), include the 7 core `FC27ConsentAttestation` keys (`consent_version`, `consented_at`, `data_categories`, `retention_acknowledged`, `analytics_use_acknowledged`, `team_review_acknowledged`, `revoked_at`) and track `review_status: "PENDING_HUMAN_REVIEW"` in the companion operator review record.

---

## 4. Blank / Synthetic JSON Export Template (`contract_version=1.0.0`, `schema_version=1.0.0`)

### 4.1 JSON Template Constraints & `payload_sha256` Calculation
- **Fixed Constraints:**
  - `contract_version`: `"1.0.0"`
  - `schema_version`: `"1.0.0"`
  - `game_version`: `"FC27"`
  - `platform`: `"common-gen5"`
  - `source_classification`: `"USER_SUPPLIED_EXPORT"`
- **External Storage Only:** The raw JSON file must remain outside the Git repository at `<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>`.
- **Pseudonymous Player References Only:** Any `local_player_reference` (e.g., `"p-placeholder-01"`) and `display_alias` (e.g., `"PlaceholderForward01"`) must be pseudonymous club-internal placeholders—never real gamer tags, PSN IDs, Xbox IDs, or EA account IDs.
- **Prohibited Identifiers & Session Data:** Remove all emails, passwords, cookies, tokens, session IDs, IP addresses, device IDs, and raw provider payloads before saving.
- **How to Calculate `payload_sha256`:**
  The `payload_sha256` field in the template below (`"<CALCULATE_PAYLOAD_SHA256_BEFORE_VALIDATION>"`) is a **placeholder requiring recalculation**. Compute the SHA-256 hex digest over the canonical UTF-8 JSON serialization of the envelope dictionary with `payload_sha256` excluded (`json.dumps(canonical_dict_without_payload_sha256, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")`), or via `src.fc27_user_export.compute_payload_sha256(envelope_dict)`.

### 4.2 Blank / Synthetic JSON Template
```json
{
  "export_id": "ecf-pilot-export-placeholder-001",
  "contract_version": "1.0.0",
  "schema_version": "1.0.0",
  "generated_at": "2026-10-27T11:55:00Z",
  "supplied_at": "2026-10-27T12:00:00Z",
  "provider": "EA_FC_PRO_CLUBS",
  "game_version": "FC27",
  "platform": "common-gen5",
  "club": {
    "club_id": "330323",
    "club_name": "East Coasters",
    "platform": "common-gen5",
    "game_version": "FC27",
    "supplied_identity_state": "PROVISIONAL",
    "source_reference": "ecf-operator-export-placeholder-v1"
  },
  "supplier_attestation": {
    "supplier_role": "CLUB_MANAGER",
    "supplier_reference": "ecf-role-ref-club-manager-01",
    "attested_at": "2026-10-27T12:00:00Z",
    "authority_scope": [
      "CLUB_IDENTITY",
      "MATCH_RESULTS",
      "LINEUPS",
      "PLAYER_PERFORMANCE",
      "BUILD_REFERENCES"
    ],
    "statement_version": "1.0.0"
  },
  "consent_attestation": {
    "consent_version": "1.0.0",
    "consented_at": "2026-10-27T12:00:00Z",
    "data_categories": [
      "CLUB_IDENTITY",
      "MATCH_RESULTS",
      "LINEUPS",
      "PLAYER_PERFORMANCE",
      "BUILD_REFERENCES"
    ],
    "retention_acknowledged": true,
    "analytics_use_acknowledged": true,
    "team_review_acknowledged": true,
    "revoked_at": null
  },
  "matches": [
    {
      "external_match_id": "PLACEHOLDER-MATCH-001",
      "played_at": "2026-10-27T10:00:00Z",
      "result": "WIN",
      "goals_for": 2,
      "goals_against": 1,
      "opponent_name": "Placeholder Opponent Club",
      "formation": "4-3-3",
      "lineup": [
        "p-placeholder-01"
      ],
      "player_performances": [
        {
          "local_player_reference": "p-placeholder-01",
          "display_alias": "PlaceholderForward01",
          "position": "ST",
          "role": "Advanced Forward",
          "minutes_played": 90,
          "goals": 2,
          "assists": 0,
          "passes_attempted": 18,
          "passes_completed": 15,
          "tackles_attempted": 2,
          "tackles_won": 1,
          "interceptions": 1,
          "shots": 4,
          "shots_on_target": 3,
          "saves": 0,
          "possession_losses": 3,
          "provider_rating": 8.4,
          "build_reference": "build-placeholder-st-01"
        }
      ],
      "source_record_hash": null,
      "match_status": "COMPLETED"
    }
  ],
  "source_classification": "USER_SUPPLIED_EXPORT",
  "payload_sha256": "<CALCULATE_PAYLOAD_SHA256_BEFORE_VALIDATION>"
}
```

---

## 5. Blank / Synthetic CSV Export Template (v1 Match-Summary Scope)

### 5.1 CSV v1 Scope & Safeguards
- **Match-Summary Only:** The v1 CSV contract is strictly match-summary only (`CLUB_IDENTITY` and `MATCH_RESULTS`). No player-level columns or player aliases are permitted in CSV files.
- **Companion Supplier & Consent Process:** When using CSV format, the operator must record the completed Supplier Attestation (Section 2) and Consent Attestation (Section 3) in the companion operator handoff manifest before running `--validate`.
- **Required Safeguards:**
  - Exact 11-column header row (`club_id,club_name,platform,game_version,external_match_id,played_at,result,goals_for,goals_against,opponent_name,formation`)
  - UTF-8 encoding (UTF-8 BOM is stripped automatically; non-UTF-8 bytes fail closed)
  - No unknown columns and no duplicate columns
  - Maximum `100` match rows for the v1 pilot package
  - Bounded cell values (`<= 256` characters per cell)
  - No multiline cells (`\r` or `\n` inside cells are rejected)
  - No embedded credentials or account identifiers
  - **Formula-Injection Prevention:** Any cell beginning effectively with `=`, `+`, `-`, or `@` fails closed (`FC27FormulaInjectionError`), including when preceded by spaces, tabs (`\t`), carriage returns (`\r`), newlines (`\n`), non-breaking spaces (`\u00a0`), Unicode whitespace, or leading apostrophes (`'`).

### 5.2 Blank / Synthetic CSV Template
```csv
club_id,club_name,platform,game_version,external_match_id,played_at,result,goals_for,goals_against,opponent_name,formation
330323,East Coasters,common-gen5,FC27,PLACEHOLDER-CSV-MATCH-001,2026-10-27T10:00:00Z,WIN,2,1,Placeholder Opponent Club,4-3-3
```

---

## 6. Local File Placement Instructions (Outside the Git Repository)

1. **Never place pilot files inside `C:\Users\iwadev\dev\iwa.dev.fcbuild`:** Do not place the file in the repository root, `docs/`, `tests/`, `scripts/`, or any subdirectory of the repository.
2. **Use `<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>`:** Keep the operator-supplied file in an operator-controlled local directory outside the repository worktree, referenced only as `<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>` in documentation, logs, and issue trackers (never publish personal usernames or personal folder paths).
3. **Prohibited Path Types (Rejected Fail-Closed by CLI):**
   - HTTP/HTTPS/FTP/file URLs (`https://...`, `file://...`)
   - Windows UNC network shares (`\\server\share\...` or `//server/share/...`)
   - Windows device namespace paths (`\\.\...`, `\\?\...`) and reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`..`COM9`, `LPT1`..`LPT9`)
   - NTFS Alternate Data Streams (`file.json:stream`)
   - Symlinks or non-regular files

---

## 7. Required 6-Step Operational Command Sequence

All pilot operations use `scripts/fc27_user_export_import.py` and must follow this exact 6-step sequence:

1. **Step 1 — Run `--validate`**
2. **Step 2 — Complete Human Metadata Review Checklist (Section 8)**
3. **Step 3 — Record Explicit Human Approval for Dry-Run (`HUMAN_DRY_RUN_APPROVAL=YES`)**
4. **Step 4 — Run `--dry-run`**
5. **Step 5 — Complete Human Readiness Review (Section 10)**
6. **Step 6 — Stop (No `--apply`, No Persistence, No Recommendations, No Dispatch)**

### 7.1 Step 1 Command: Offline Validation (`--validate`)
```powershell
# Validate JSON export (offline, zero writes):
python scripts/fc27_user_export_import.py --input "<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>" --format json --validate

# Validate CSV export (offline, zero writes):
python scripts/fc27_user_export_import.py --input "<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>" --format csv --validate
```

### 7.2 Step 4 Command: Offline Dry-Run (`--dry-run`) — Only After Human Metadata Review & Explicit Approval
```powershell
# Dry-run JSON export (after human metadata review and explicit approval):
python scripts/fc27_user_export_import.py --input "<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>" --format json --dry-run

# Dry-run CSV export (after human metadata review and explicit approval):
python scripts/fc27_user_export_import.py --input "<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>" --format csv --dry-run
```

### 7.3 Prohibited Commands & Actions
Never execute any of the following during the pilot:
- `--apply` or `--confirm-apply`
- Recommendation generation or activation
- Database persistence or transactional writes
- Dispatch queue or worker execution
- Live provider lookup or public-page fetching
- Copying or moving the pilot file into `C:\Users\iwadev\dev\iwa.dev.fcbuild`

---

## 8. Human Metadata-Review Checklist (Mandatory Gate Between `--validate` and `--dry-run`)

After `--validate` completes and **before** granting `HUMAN_DRY_RUN_APPROVAL=YES` to run `--dry-run`, a human reviewer must verify and record all 28 metadata items below:

- [ ] 1. **Redacted Filename:** Filename recorded without personal username or folder segments (e.g., `<REDACTED_DIR>/east_coasters_pilot.json`).
- [ ] 2. **Local External Location:** Confirmed strictly outside `C:\Users\iwadev\dev\iwa.dev.fcbuild` (`<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>`).
- [ ] 3. **File Size:** Byte count verified (`> 0` and `<= 5,242,880` bytes).
- [ ] 4. **SHA-256 (`PAYLOAD_SHA256`):** Matches the expected canonical SHA-256 digest.
- [ ] 5. **Format:** `json` or `csv`.
- [ ] 6. **Export ID (`EXPORT_ID`):** Non-personal export identifier confirmed.
- [ ] 7. **Contract Version:** `1.0.0`.
- [ ] 8. **Schema Version:** `1.0.0`.
- [ ] 9. **Source Classification (`SOURCE_CLASSIFICATION`):** `USER_SUPPLIED_EXPORT`.
- [ ] 10. **Supplier Role:** `CLUB_MANAGER`, `CLUB_ADMIN`, or `DATA_STEWARD` (`MANAGER_AUTHORITY_STATUS=UNVERIFIED`).
- [ ] 11. **Authority Scopes:** Covers all data categories present in the export (`CLUB_IDENTITY`, `MATCH_RESULTS`, plus any conditional scopes).
- [ ] 12. **Consent Categories:** Covers all data categories present (`CLUB_IDENTITY`, `MATCH_RESULTS`, plus any conditional categories) with all three acknowledgements `YES`.
- [ ] 13. **Club ID (`SUPPLIED_CLUB_ID`):** `330323` (`CORROBORATED`, not provider-`VERIFIED`).
- [ ] 14. **Club Name (`SUPPLIED_CLUB_NAME`):** `East Coasters` (or corroborated alias `EastCoasters FC`).
- [ ] 15. **Platform (`SUPPLIED_PLATFORM`):** `common-gen5`.
- [ ] 16. **Game Version:** `FC27`.
- [ ] 17. **Total Record Count (`TOTAL_RECORDS`):** Count of supplied match records verified.
- [ ] 18. **Lineup Record Count:** Count of matches containing lineup entries verified against `LINEUPS` scope.
- [ ] 19. **Player-Performance Record Count:** Count of player performance entries verified against `PLAYER_PERFORMANCE` scope.
- [ ] 20. **Partial Record Count:** Count of `PARTIAL` matches verified (excluded from analytics eligibility).
- [ ] 21. **Abandoned Record Count:** Count of `ABANDONED` matches verified (excluded from analytics eligibility).
- [ ] 22. **Duplicate Count (`DUPLICATE_RECORDS`):** Count of duplicate match records verified.
- [ ] 23. **Conflict Count (`CONFLICTING_RECORDS`):** Must be `0` to proceed to `--dry-run`.
- [ ] 24. **Rejected Record Count (`REJECTED_RECORDS`):** Must be `0` to proceed to `--dry-run`.
- [ ] 25. **Sensitive-Field Count:** Must be `0` (`SENSITIVE_FIELDS_DETECTED` absent).
- [ ] 26. **Validation Warnings / Diagnostics (`DIAGNOSTICS`):** Reviewed and signed off by human reviewer.
- [ ] 27. **Identity Decision (`CLUB_IDENTITY_DECISION`):** `MATCH_PROVISIONAL_CLUB` confirmed.
- [ ] 28. **Raw-Upload Retention State (`RAW_UPLOAD_RETAINED`):** Confirmed `NO`.

**Prohibited in Human Review Output & Logs:**
The review record must **never** display or log:
- Raw file contents
- Raw JSON objects or raw CSV rows
- Player aliases by default (`--include-safe-aliases` is off by default)
- Account identifiers, persona IDs, or gamer tags
- Any prohibited or sensitive field values
- Full personal filesystem paths containing operator usernames

---

## 9. Readiness-Preview Interpretation Guide

During Step 5 (Human Readiness Review after `--dry-run`), interpret `SAMPLE_SIZE_CONFIDENCE_TIER` using the exact table below:

| Completed Analytics-Eligible Matches (`ANALYTICS_ELIGIBLE_RECORDS`) | `SAMPLE_SIZE_CONFIDENCE_TIER` Label | `COACHING_READINESS` | Operational Interpretation |
|---|---|---|---|
| `0` to `2` | `INSUFFICIENT_DATA` | `NO` | Sample size is insufficient for trend or baseline analysis. |
| `3` to `9` | `EXPLORATORY` | `NO` | Exploratory sample only; high match-to-match variance. |
| `10` to `19` | `PROVISIONAL_TRENDS` | `YES` | Provisional trend visibility for human tactical inspection. |
| `20` or more (`20+`) | `HIGH_SAMPLE_READINESS` | `YES` | High-sample readiness for baseline multi-match review. |

**Mandatory Readiness Invariants:**
1. **Completed Analytics-Eligible Matches Only:** Readiness is computed strictly from `COMPLETED` matches that pass all validation checks (`ANALYTICS_ELIGIBLE_RECORDS`).
2. **Excluded Records Never Increase Readiness:** `PARTIAL`, `ABANDONED`, conflicting, duplicate, or rejected match records **never** increase sample readiness.
3. **Not Team-Quality Scoring:** Readiness measures sample-size adequacy only; it is **not** a score of team quality or skill.
4. **Not Coaching Confidence:** Sample readiness is **not** algorithmic coaching confidence.
5. **Does Not Enable Recommendations:** Reaching `PROVISIONAL_TRENDS` or `HIGH_SAMPLE_READINESS` (`20+` matches) does **not** enable or independently authorize coaching recommendations (`FC27_RECOMMENDATIONS_ENABLED=false`).

---

## 10. Sensitive-Data Incident Procedure

If `--validate` rejects an input with `REJECTION_REASON: SENSITIVE_FIELDS_DETECTED` or `REJECTION_REASON: FORMULA_INJECTION_DETECTED`, follow this exact 10-step incident procedure:

1. **Stop processing immediately:** Halt the pilot workflow at once.
2. **Do not print the value:** Never inspect, echo, or print the offending field value or cell content.
3. **Do not run `--dry-run`:** Do not proceed to `--dry-run` or any downstream step.
4. **Do not copy the source into the repository:** Never copy, stage, or move the rejected file into `C:\Users\iwadev\dev\iwa.dev.fcbuild`.
5. **Report field paths and reason codes only:** Record only the sanitized `SENSITIVE_PATHS` key paths and `REJECTION_REASON` code emitted by the CLI.
6. **Mark raw upload unsafe to retain:** Record `SAFE_TO_RETAIN_RAW_UPLOAD: NO`.
7. **Ask the supplier to sanitize and resubmit:** Instruct the authorized club supplier (`CLUB_MANAGER`, `CLUB_ADMIN`, or `DATA_STEWARD`) to strip all prohibited fields and generate a clean export.
8. **Delete temporary importer-created artifacts:** Remove any temporary CLI stdout/stderr capture files if created outside the repo (the importer itself writes zero files).
9. **Preserve no sensitive diagnostics:** Ensure no terminal scrollback or diagnostic snippet containing raw values is saved.
10. **Record no secret in an issue, PR, log, or chat:** Never paste raw values, personal paths, or gamer tags into GitHub issues, pull requests, logs, or chat transcripts.

> **Operator Source-File Custody Rule:** The operator-owned source file at `<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>` remains under operator control and **must not be deleted by the agent or tooling without explicit operator instruction**.

---

## 11. Raw-File Retention and Deletion Guidance

1. **Never Committed:** Raw user-supplied exports are never staged, committed, or pushed to Git (`RAW_EXPORTS_COMMITTED=0`).
2. **Never Copied into Tests:** Raw exports are never copied into `tests/` or any repository directory.
3. **Never Converted into a Fixture:** Real club or match exports must never be converted into test fixtures.
4. **Importer Retains No Copy:** `scripts/fc27_user_export_import.py` reads the file in memory only and retains no copy (`RAW_UPLOAD_RETAINED: NO`).
5. **Source File Remains Under Operator Control:** The local file at `<ABSOLUTE_LOCAL_PATH_OUTSIDE_REPOSITORY>` stays under the human operator's custody.
6. **Retention Duration Agreed Before Validation:** The operator and club supplier must agree on the local file retention window (`AGREED_RAW_FILE_RETENTION`) before running `--validate`.
7. **Deletion or Return Occurs After Review:** Once the human metadata and readiness reviews conclude, the operator deletes or returns the source file according to `AGREED_RAW_FILE_RETENTION`.
8. **Only Metadata-Only Results Retained:** Only sanitized, metadata-only CLI summary lines may be retained for audit records.
9. **Exclude Aliases and Personal Identifiers by Default:** Retained metadata summaries must exclude player aliases (`--include-safe-aliases` omitted) and must exclude all personal identifiers.

---

## 12. Explicit Operational Prohibitions

1. **Explicit Prohibition on `--apply`:** Running `scripts/fc27_user_export_import.py --apply` or `--confirm-apply` is strictly prohibited. Persistence and transactional apply remain deferred (`PERSISTENCE_IMPLEMENTED=DEFERRED`, `TRANSACTIONAL_APPLY_IMPLEMENTED=DEFERRED`, `READY_FOR_PRODUCTION_APPLY=NO`).
2. **Explicit Prohibition on Recommendations:** Enabling, generating, ranking, or dispatching player or tactical recommendations is strictly prohibited (`FC27_RECOMMENDATIONS_ENABLED=false`, `DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`).
3. **Explicit Prohibition on Committing Pilot Data:** Committing any real East Coasters export, match history, player alias, or personal identifier into the repository is strictly prohibited.

---

## 13. Operator-Captured Displayed Data (`OPERATOR_CAPTURED_DISPLAYED_DATA`) & `.local-data/` Privacy Rules

1. **Private Git-Ignored Directory (`.local-data/`):**
   - `.local-data/` is ignored in `.gitignore` so private operator working files (`.local-data/east-coasters-fc27-normalized.json` and `.local-data/east-coasters-fc27-input-checklist.md`) cannot be staged or committed without explicit override, which is prohibited.
   - The application never deletes the operator's source file automatically; "raw input discarded" means `normalize_operator_displayed_capture` overwrites its in-memory mutable `bytearray` buffer with zeroes immediately after parsing and retains zero raw copies in logs, events, quarantine records, or database tables.
2. **Pseudonymous Player References & Private Alias Map Policy:**
   - All player references in `OPERATOR_CAPTURED_DISPLAYED_DATA` must use the non-reversible pseudonymous format `plr_ecf_<bounded deterministic reference>` (e.g., `plr_ecf_slot_01`), with `display_alias: null` (`STORE_DISPLAY_ALIASES=NO`).
   - If the operator maintains a private alias-to-pseudonym lookup table, it must remain in a separate ignored file outside PostgreSQL and analytics pipelines, must never be committed or printed, and must be deleted or rotated immediately upon member consent revocation or expiration of the agreed team review retention window.
