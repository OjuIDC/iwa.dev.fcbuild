# DISPATCH — explorer_m4_i2_recovery_1

**Timestamp**: 2026-10-04T06:24:00Z  
**Role**: Recovery Code Revocation & Store DML Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_recovery_1`  
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`  
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`  
**Full Forensic Audit Report**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m4_1\handoff.md`  
**Reviewer Reports**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_1\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m4_2\handoff.md`  
**Challenger Reports**:
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_1\handoff.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m4_2\handoff.md`  

---

## Mission
Analyze and formulate exact fix proposals for recovery code revocation and store queries:
1. In `src/identity/recovery.py` (`regenerate_recovery_codes`) and `src/identity/store.py` (`save_recovery_codes`):
   - Currently, generating or regenerating recovery codes inserts new rows without revoking unconsumed existing codes. Because rows cannot be deleted due to `fc27_reject_mutation()`, unconsumed codes remain usable.
   - Formulate the atomic revocation query in `save_recovery_codes(user_id, plaintext_codes, actor)`: before inserting the new batch, execute `UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL` within the transaction, revoking all existing unconsumed codes.
2. In `src/identity/store.py:1344` (`verify_and_consume_recovery_code`):
   - Add the DML filter `AND consumed_at IS NULL`: `UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE code_id = %s AND consumed_at IS NULL`.
3. Provide exact code diffs and recommendations in `handoff.md`. Do NOT edit source files directly.
