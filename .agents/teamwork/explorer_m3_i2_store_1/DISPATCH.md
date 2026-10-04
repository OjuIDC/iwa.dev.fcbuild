# Dispatch Instructions for explorer_m3_i2_store_1

**Agent ID**: `explorer_m3_i2_store_1`
**Role**: Store Signature & Rotation Concurrency Explorer
**Milestone**: Phase 2 Milestone 3 (Iteration 2 Remediation)
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_store_1`

## Full Forensic Audit & Review Evidence (MANDATORY TO READ)
1. Full Audit Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md`
2. Reviewer Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\handoff.md`
3. Challenger Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\handoff.md`
4. Authoritative Request: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`
5. Project Plan: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`

## Problem Statement from Audit & Review
1. **`create_session` Signature Mismatch**:
   - `src/identity/store.py` line 719: `create_session` does not accept `club_id`.
   - In test suites, 11 call sites pass `club_id=TEST_CLUB_ID`.
   - Also note that `reviewer_m3_2` suggested adding `assign_user_role` as an alias to `assign_role(..., actor="system")`.
2. **`rotate_session` Concurrency Race Condition**:
   - In `src/identity/store.py` line 834: `UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s` does not check `AND revoked_at IS NULL` and does not check `rowcount`. Concurrent replay creates duplicate rotated sessions.

## Your Task
1. Inspect `src/identity/store.py` around lines 719–760 (`create_session`), lines 820–870 (`rotate_session`), and role assignment methods.
2. Formulate the exact, robust fix recommendations:
   - Update `create_session` signature to accept `club_id: str | None = None` and `**kwargs: Any` gracefully.
   - Update `rotate_session` to execute:
     ```python
     res = conn.execute(
         "UPDATE public.fc27_sessions SET revoked_at = now() WHERE session_id = %s AND revoked_at IS NULL",
         (old_sess["session_id"],),
     )
     if res.rowcount == 0:
         raise IdentityStoreError(409, "session already rotated or revoked")
     ```
   - Provide an alias `assign_user_role` in `IdentityStore` forwarding to `assign_role(..., actor="system")`.
3. Provide line-by-line patch proposals and rationale. Do NOT modify source code directly.
4. Deliver your complete handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_store_1\handoff.md`.
When finished, notify me with your report path via `send_message`.


## 2026-10-04T05:10:17Z
[Message] timestamp=2026-10-04T05:10:17Z sender=25210876-a9ad-4f78-a87c-dec88e86edb9 priority=MESSAGE_PRIORITY_HIGH content=You are explorer_m3_i2_store_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_store_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_store_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Full Forensic Audit Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\auditor_m3_1\handoff.md
Reviewer Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\reviewer_m3_1\handoff.md
Challenger Report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m3_1\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md and the full audit report before starting work.

Your task:
Analyze and formulate exact fix proposals for the store signature and session rotation race condition:
1. In src/identity/store.py (line 719), update create_session signature to accept club_id: str | None = None and **kwargs: Any.
2. In rotate_session (line 834), add concurrency locking: AND revoked_at IS NULL on the UPDATE and check res.rowcount == 0 to raise IdentityStoreError(409).
3. Add alias assign_user_role forwarding to assign_role(..., actor="system").
4. Provide exact code diffs and rationale. Do NOT edit source files directly.
5. Deliver handoff report to: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m3_i2_store_1\handoff.md
When finished, notify me with your report path via send_message.
