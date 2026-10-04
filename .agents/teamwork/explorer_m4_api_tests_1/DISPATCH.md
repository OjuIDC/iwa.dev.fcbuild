# Dispatch Assignment: Milestone 4 API Routes & Test Suite Explorer

**Agent**: `explorer_m4_api_tests_1`  
**Archetype**: `teamwork_preview_explorer`  
**Role**: Milestone 4 API Routes & Test Suite Explorer  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_api_tests_1`  
**Timestamp**: 2026-10-03T20:08:00Z  

---

## 1. Context & Scope

You are exploring the Graph API route additions, test helpers, and test suite requirements for Milestone 4.

### Mandatory Reading
You MUST read:
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. `docs/superpowers/plans/2026-10-02-phase1-dispatch-data-foundation.md` (Task 8: lines 919–1040)
4. `src/graph_api.py` (settings, lifespan, routes, middleware)
5. `tests/helpers.py`
6. `tests/test_dispatch_schema.py` and `tests/test_dispatch_transition.py`

---

## 2. Exploration Objectives

Analyze and design:
1. `src/graph_api.py` additions:
   - Configuration settings: `FC27_CODE_REVISION` and `FC27_DEPLOYMENT_ID`.
   - Gate reevaluation loop: `reevaluate_gates(app)` at startup and periodically / on-demand.
   - Routes:
     - `GET /v1/config/dispatch`: Returns status of `dispatch_enabled`, `dispatch_effective`, gate reports, and deployment identity.
     - `POST /v1/activation-gates/{gate_id}/challenge`: Commissioner-only route generating HMAC challenge payload and storing `activation_gate` nonce in `approval_nonces`.
     - `POST /v1/activation-gates/{gate_id}`: Commissioner-only route verifying HMAC signature, consuming nonce, inserting into `activation_gate_log`, emitting `activation.gate_recorded` audit event.
     - Integration in approval decision route: passing `dispatch_enabled=app.state.dispatch_effective` and `dispatch_creator=create_dispatch_for_approval`.
2. `tests/helpers.py` additions:
   - `fake_packages_dispatchable(player_ids=...)`: Generates lead packages with valid Objectives.
   - `seed_verified_players(admin_db_uri, ids, club_id="330323")`: Seeds active, verified players in `public.players`.
3. `tests/test_dispatch_creation.py`:
   - Comprehensive test suite as outlined in Task 8 Step 1 (lines 980–1018):
     - `test_gates_block_by_default`
     - `test_configured_but_blocked_behaves_disabled`
     - `test_gate_recording_requires_commissioner_and_signature`
     - `test_gate_nonce_replay_rejected`
     - `test_gate_nonce_expired_rejected`
     - `test_gate_tampered_field_rejected`
     - `test_gate_nonce_bound_to_gate`
     - `test_gate_requires_deployment_identity`
     - `test_gate_record_from_other_deployment_ignored`
     - `test_gate_signature_excludes_correlation_id`
     - `test_g_outbox_not_recordable_in_phase1`
     - `test_gate_change_applies_on_next_evaluation`
     - `test_correlation_id_shared_by_dispatch_and_events`
     - `test_approve_creates_dispatch_atomically`
     - `test_dispatch_failure_rolls_back_decision`
     - `test_not_dispatchable_without_objective`
     - `test_not_dispatchable_gamertag_player`
     - `test_not_dispatchable_unverified_player`
     - `test_player_id_is_compared_exactly`
     - `test_not_dispatchable_unknown_player`
     - `test_concurrent_supersede_is_serialized`
     - `test_reject_creates_no_dispatch`
     - `test_supersede_is_atomic`
     - `test_parallel_duplicate_approvals_single_dispatch`
   - `enabled_client` fixture design in `conftest.py`.

---

## 3. Output Requirements

Write `api_tests_analysis.md` and `handoff.md` in your working directory.
Include exact route definitions, request/response models, test fixture specifications, and test cases.
Do NOT modify production source code in `src/` or `migrations/`.
Notify orchestrator via `send_message` when done.
