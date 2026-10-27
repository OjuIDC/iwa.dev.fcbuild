# Consented East Coasters User-Export Pilot Preparation Package

**Status:** Approved Offline Pilot Preparation Package  
**Target Club:** East Coasters (`330323` / `common-gen5` / `FC27`)  
**Primary Integration Path:** `USER_SUPPLIED_EXPORT`  
**Live Network / EA Transport:** Disabled (`0` requests, `0` DNS lookups)  
**Persistence (`--apply`):** Deferred / Prohibited  
**Coaching Recommendations:** Disabled / Prohibited

---

## 1. East Coasters Pilot-Input Checklist

Before executing the offline pilot intake, the human operator and authorized club representative must verify every item below:

- [ ] **Authorized Supplier Role Confirmed:** The export file is supplied directly by an authorized East Coasters `CLUB_MANAGER`, `CLUB_ADMIN`, or `DATA_STEWARD`.
- [ ] **Non-Personal Supplier Reference Recorded:** A club-controlled, non-personal identifier (e.g., `ecf-role-ref-club-manager-01`) is assigned. No personal email addresses, EA account IDs, PSN/Xbox IDs, or passwords are included.
- [ ] **Club Identity Metadata Checked:**
  - `CANONICAL_CLUB_NAME=East Coasters` (known alias: `EastCoasters FC`, abbreviation: `ECF`)
  - `CLUB_ID=330323` (`CORROBORATED` via offline secondary observations; not `VERIFIED`)
  - `PLATFORM=common-gen5` (`CORROBORATED`)
  - `GAME_VERSION=FC27` (`CORROBORATED`)
  - `MATCHMAKING_REGION_CLAIM=East Coast US 1` (`USER_ASSERTED`)
  - `USER_MEMBERSHIP_STATUS=USER_ASSERTED`
  - `MANAGER_AUTHORITY_STATUS=UNVERIFIED` (requires separate human review decision)
- [ ] **Consent Scope & Acknowledgements Verified:** Base categories (`CLUB_IDENTITY`, `MATCH_RESULTS`) and any conditional categories (`LINEUPS`, `PLAYER_PERFORMANCE`, `BUILD_REFERENCES`) match the contents of the file, with `RETENTION_ACKNOWLEDGED=YES`, `ANALYTICS_USE_ACKNOWLEDGED=YES`, and `TEAM_REVIEW_ACKNOWLEDGED=YES`.
- [ ] **Local File Placement Verified:** The file resides at an absolute local path **outside** `C:\Users\iwadev\dev\iwa.dev.fcbuild` (not on a UNC share, HTTP/HTTPS URL, or cloud sync folder inside the repository).
- [ ] **File Size & Format Within Bounds:** Regular `.json` or `.csv` file, `<= 5,242,880` bytes (5 MiB), and `<= 250` matches.
- [ ] **Zero Prohibited Fields:** No `email`, `password`, `token`, `cookie`, `session`, `authorization`, `account_id`, `nucleus_id`, `persona_id`, `gamer_tag`, `psn_id`, `xbox_id`, `ea_id`, `ip_address`, `device_id`, or `raw_provider_payload` fields exist anywhere in the file.

---

## 2. Non-Personal Supplier-Attestation Templates

Supplier attestations must use non-personal, club-controlled references only. Supplying an attestation template does **not** automatically verify manager authority (`MANAGER_AUTHORITY_STATUS` remains `UNVERIFIED` until an independent human review decision is recorded).

### 2.1 `CLUB_MANAGER` Supplier Attestation Template
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

### 2.2 `CLUB_ADMIN` Supplier Attestation Template
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

### 2.3 `DATA_STEWARD` Supplier Attestation Template
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
| `LINEUPS` | **Conditional** | Required if any match record populates a non-empty `lineup` list. |
| `PLAYER_PERFORMANCE` | **Conditional** | Required if any match record populates `player_performances`. |
| `BUILD_REFERENCES` | **Conditional** | Required if any player performance record populates `build_reference`. |

### 3.2 Mandatory Operator Acknowledgements
All three acknowledgements must be explicitly set to `YES` (`true` in JSON):
- `RETENTION_ACKNOWLEDGED=YES`
- `ANALYTICS_USE_ACKNOWLEDGED=YES`
- `TEAM_REVIEW_ACKNOWLEDGED=YES`

### 3.3 Consent-Attestation JSON Block
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
  "revoked_at": null
}
```

---

## 4. Blank / Synthetic JSON Export Template

This template contains synthetic placeholders only. Replace timestamps, match entries, and `payload_sha256` locally outside the repository before running `--validate`.

```json
{
  "export_id": "ecf-pilot-export-template-001",
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
    "source_reference": "ecf-operator-export-v1"
  },
  "supplier_attestation": {
    "supplier_role": "CLUB_MANAGER",
    "supplier_reference": "ecf-role-ref-club-manager-01",
    "attested_at": "2026-10-27T12:00:00Z",
    "authority_scope": [
      "CLUB_IDENTITY",
      "MATCH_RESULTS"
    ],
    "statement_version": "1.0.0"
  },
  "consent_attestation": {
    "consent_version": "1.0.0",
    "consented_at": "2026-10-27T12:00:00Z",
    "data_categories": [
      "CLUB_IDENTITY",
      "MATCH_RESULTS"
    ],
    "retention_acknowledged": true,
    "analytics_use_acknowledged": true,
    "team_review_acknowledged": true,
    "revoked_at": null
  },
  "matches": [
    {
      "external_match_id": "SYNTHETIC-TEMPLATE-MATCH-001",
      "played_at": "2026-10-27T10:00:00Z",
      "result": "WIN",
      "goals_for": 2,
      "goals_against": 1,
      "opponent_name": "Synthetic Template Opponent",
      "formation": "4-3-3",
      "lineup": [],
      "player_performances": [],
      "source_record_hash": null,
      "match_status": "COMPLETED"
    }
  ],
  "source_classification": "USER_SUPPLIED_EXPORT",
  "payload_sha256": "0000000000000000000000000000000000000000000000000000000000000000"
}
```

---

## 5. Blank / Synthetic CSV Export Template

CSV exports must use the exact 11 approved header columns (`APPROVED_CSV_COLUMNS`). Every cell must be `<= 256` characters and must not begin with spreadsheet formula characters (`=`, `+`, `-`, `@`, `\t`, `\r`).

```csv
club_id,club_name,platform,game_version,external_match_id,played_at,result,goals_for,goals_against,opponent_name,formation
330323,East Coasters,common-gen5,FC27,SYNTHETIC-TEMPLATE-CSV-001,2026-10-27T10:00:00Z,WIN,2,1,Synthetic Template Opponent,4-3-3
```

---

## 6. Local File Placement Instructions (Outside the Git Repository)

1. **Never place pilot files inside `C:\Users\iwadev\dev\iwa.dev.fcbuild`:** Do not place the file in the repository root, `docs/`, `tests/`, `scripts/`, `.tmp_pytest/`, or any subdirectory of the repository.
2. **Use a Dedicated Local Staging Path Outside the Repo:** Place the operator-supplied file in a restricted local directory outside the repository worktree (for example, `C:\Users\iwadev\fc27_pilot_intake\east_coasters_pilot.json`).
3. **Prohibited Path Types:** The CLI automatically rejects:
   - HTTP/HTTPS/FTP/file URLs (`https://...`, `file://...`)
   - Windows UNC network shares (`\\server\share\...` or `//server/share/...`)
   - Windows device namespace paths (`\\.\...`, `\\?\...`) and reserved device names (`CON`, `PRN`, `AUX`, `NUL`, `COM1`..`COM9`, `LPT1`..`LPT9`)
   - NTFS Alternate Data Streams (`file.json:stream`)
   - Symlinks or non-regular files

---

## 7. Step 1 Execution: Exact `--validate` Command

Run the offline validation command first to verify schema compliance, SHA-256 payload integrity, sensitive-field absence, and consent/supplier attestation rules without planning or writing:

```powershell
# For JSON pilot file:
python scripts/fc27_user_export_import.py --input "C:\path\outside\repo\east_coasters_pilot.json" --format json --validate

# For CSV pilot file:
python scripts/fc27_user_export_import.py --input "C:\path\outside\repo\east_coasters_pilot.csv" --format csv --validate
```

Expected metadata-only output on success:
- `IMPORT_STATUS: VALIDATED`
- `MODE: VALIDATE`
- `SUPPLIED_CLUB_ID: 330323`
- `SUPPLIED_CLUB_NAME: East Coasters`
- `SUPPLIED_PLATFORM: common-gen5`
- `HUMAN_REVIEW_REQUIRED: YES`
- `RAW_UPLOAD_RETAINED: NO`

---

## 8. Human Metadata-Review Checklist (Between `--validate` and `--dry-run`)

Before proceeding from `--validate` to `--dry-run`, a human reviewer must verify the CLI metadata output:

- [ ] `IMPORT_STATUS` is `VALIDATED` (exit code `0`).
- [ ] `PAYLOAD_SHA256` matches the expected SHA-256 digest recorded by the supplier.
- [ ] `SUPPLIED_CLUB_ID` is `330323`, `SUPPLIED_CLUB_NAME` is `East Coasters`, and `SUPPLIED_PLATFORM` is `common-gen5`.
- [ ] `CLUB_IDENTITY_DECISION` is `MATCH_PROVISIONAL_CLUB` (confirming alignment with the provisional/corroborated East Coasters profile without auto-promoting to `VERIFIED`).
- [ ] `REJECTED_RECORDS` is `0` and `CONFLICTING_RECORDS` is `0`.
- [ ] `RAW_UPLOAD_RETAINED` is `NO`.

---

## 9. Step 2 Execution: Exact `--dry-run` Command

After human metadata review of `--validate` succeeds, run the offline idempotent import planner and baseline readiness preview in `--dry-run` mode:

```powershell
# For JSON pilot file:
python scripts/fc27_user_export_import.py --input "C:\path\outside\repo\east_coasters_pilot.json" --format json --dry-run

# For CSV pilot file:
python scripts/fc27_user_export_import.py --input "C:\path\outside\repo\east_coasters_pilot.csv" --format csv --dry-run
```

Expected metadata-only output on success:
- `IMPORT_STATUS: DRY_RUN`
- `MODE: DRY-RUN`
- `CAN_APPLY: YES`
- `SAMPLE_SIZE_CONFIDENCE_TIER: <INSUFFICIENT_DATA|EXPLORATORY|PROVISIONAL_TRENDS|HIGH_SAMPLE_READINESS>`
- `COACHING_READINESS: <YES|NO>`
- `HUMAN_REVIEW_REQUIRED: YES`
- `RAW_UPLOAD_RETAINED: NO`
- `PERSISTENCE_DETAIL: Dry-run completed. No records persisted.`

---

## 10. Readiness-Preview Interpretation Guide

The `SAMPLE_SIZE_CONFIDENCE_TIER` emitted during `--validate` and `--dry-run` is determined strictly by the count of analytics-eligible completed matches (`ANALYTICS_ELIGIBLE_RECORDS`):

| Analytics-Eligible Match Count | `SAMPLE_SIZE_CONFIDENCE_TIER` Label | `COACHING_READINESS` | Interpretation & Operational Meaning |
|---|---|---|---|
| `0-2` | `INSUFFICIENT_DATA` | `NO` | Sample size is too small for trend or baseline analysis. Collect additional completed matches before evaluating club trends. |
| `3-9` | `EXPLORATORY` | `NO` | Early directional metrics only. High variance across opponents and lineups; not eligible for coaching baseline readiness. |
| `10-19` | `PROVISIONAL_TRENDS` | `YES` | Sufficient sample for provisional formation, goal-differential, and role trend inspection under human review. |
| `20+` | `HIGH_SAMPLE_READINESS` | `YES` | High-sample baseline readiness achieved for multi-match tactical and build performance comparisons. |

> **Important:** Even when `SAMPLE_SIZE_CONFIDENCE_TIER` reaches `PROVISIONAL_TRENDS` or `HIGH_SAMPLE_READINESS`, coaching recommendations remain disabled (`FC27_RECOMMENDATIONS_ENABLED=false`) and persistence remains deferred (`PERSISTENCE_IMPLEMENTED=DEFERRED`).

---

## 11. Sensitive-Data Incident Procedure

If `--validate` or `--dry-run` outputs:
```text
IMPORT_STATUS: REJECTED
REJECTION_REASON: SENSITIVE_FIELDS_DETECTED
SENSITIVE_PATHS: [...]
SAFE_TO_RETAIN_RAW_UPLOAD: NO
```
or `REJECTION_REASON: FORMULA_INJECTION_DETECTED`:

1. **Halt Immediately:** Do not re-run with modified flags or attempt to bypass the scanner.
2. **Never Log or Copy Raw Values:** Record only the sanitized `SENSITIVE_PATHS` field names emitted by the CLI; never copy, paste, or log the offending values.
3. **Delete the Local Staging File Immediately:** Permanently delete the rejected file from the local staging directory outside the repository.
4. **Verify Repository Cleanliness:** Run `git status --short` and `git diff` inside `C:\Users\iwadev\dev\iwa.dev.fcbuild` to confirm zero untracked or modified files were created in the repository.
5. **Request a Newly Sanitized Export:** Instruct the authorized club supplier (`CLUB_MANAGER`, `CLUB_ADMIN`, or `DATA_STEWARD`) to strip all personal/account identifiers and regenerate the export file from scratch.

---

## 12. Raw-File Retention and Deletion Instructions

- **Zero In-Repo Retention:** `iwa.dev.fcbuild` never copies, caches, or persists raw user-supplied export files (`RAW_UPLOAD_RETAINED: NO`).
- **Post-Dry-Run Local Cleanup:** Once the operator has recorded the metadata-only `--validate` and `--dry-run` summary lines, delete the local staging file from the host filesystem unless the club supplier has explicitly authorized temporary offline local custody outside the repository.
- **Revocation Handling:** If `consent_attestation.revoked_at` is set or the supplier revokes consent, delete all local staging files immediately; the importer automatically blocks any revoked export (`CONSENT_REVOKED`).

---

## 13. Explicit Operational Prohibitions

1. **Prohibition of `--apply`:** Running `scripts/fc27_user_export_import.py --apply --confirm-apply` is strictly prohibited during pilot intake. Persistence and transactional apply are intentionally deferred (`PERSISTENCE_IMPLEMENTED=DEFERRED`, `TRANSACTIONAL_APPLY_IMPLEMENTED=DEFERRED`, `READY_FOR_PRODUCTION_APPLY=NO`).
2. **Prohibition of Recommendations:** Generating, enabling, or dispatching coaching or build recommendations from pilot data is strictly prohibited (`FC27_RECOMMENDATIONS_ENABLED=false`, `DISPATCH_ENABLED=false`, `DISPATCH_WORKER_ENABLED=false`).
3. **Prohibition of Committing Pilot Data:** Staging, committing, or pushing any real club export, match log, player alias list, or operator attestation payload into Git is strictly prohibited. Only synthetic test fixtures and blank documentation templates may exist in the repository.
