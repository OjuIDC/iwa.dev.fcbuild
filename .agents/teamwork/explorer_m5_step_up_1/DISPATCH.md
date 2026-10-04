# DISPATCH — explorer_m5_step_up_1

**Timestamp**: 2026-10-04T09:35:00Z  
**Role**: Decision Step-Up & Cryptographic Binding Designer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_step_up_1`  
**Authoritative Request**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md`  
**Project Plan**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`  

---

## Mission
Investigate and design the Decision Step-Up Engine and API Router for Phase 2 Milestone 5:
1. Requirements & Scope:
   - Features 18, 19, 20: Decision Step-Up Challenge (Commissioner), Verification, and Replay & Scope Lockout.
   - Challenge generation: POST `/v1/auth/step-up/challenge` bound to exact `package_id`, `version`, `package_hash`, and intended `decision` (`approve`, `reject`, `request_changes`).
   - Challenge verification: POST `/v1/auth/step-up/verify` verifying WebAuthn passkey assertion against challenge and stored credential.
   - Generates single-use `authentication_evidence_id` recorded in `public.fc27_audit_domain_events` or decision evidence table.
   - Replay & Scope Lockout: evidence ID is single-use and bound exclusively to that exact package ID, version, and hash. Cannot be reused or transferred.
2. Inspect existing codebase:
   - Check `src/identity/webauthn.py` (`generate_step_up_options`, `verify_step_up_response`).
   - Check `src/identity/store.py` for challenges and decision tables.
   - Check migrations for step-up or decision evidence schema.
3. Design `src/identity/routers/step_up.py` and store methods:
   - Schemas, routes, dependency injection, error mappings (HTTP 400, 401, 403, 409).
4. Provide comprehensive blueprint, data contracts, and test plans in `handoff.md`. Do NOT edit source files directly.
Deliver handoff report to: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m5_step_up_1\handoff.md`
When finished, notify orchestrator with report path via send_message.
