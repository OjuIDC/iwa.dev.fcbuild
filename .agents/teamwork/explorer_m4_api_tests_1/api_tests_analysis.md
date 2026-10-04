# Technical Exploration & Architecture Analysis: Milestone 4 Graph API & Test Suite

**Document Version**: 1.0.0  
**Author**: `explorer_m4_api_tests_1` (`teamwork_preview_explorer`)  
**Target Milestone**: Milestone 4 (Activation Gates & Atomic Approval-to-Dispatch Creation)  
**Date**: 2026-10-03  
**Status**: Ready for Orchestrator Review & Worker Dispatch  

---

## Executive Summary

Milestone 4 introduces the activation gate subsystem and the atomic approval-to-dispatch transition. In Phase 1, `DISPATCH_ENABLED=false` remains enforced by default. However, Phase 1 requires the complete cryptographic, transaction-safe, and gate-governed machinery that enables dispatch *when* configured and *when* all activation gates pass.

This analysis provides the complete design for:
1. **`src/graph_api.py` additions**:
   - Runtime configuration: `FC27_CODE_REVISION` and `FC27_DEPLOYMENT_ID`.
   - Gate re-evaluation lifecycle: `reevaluate_gates(app)` at startup and periodically (every 60s).
   - `GET /v1/config/dispatch`: Exposes configured state, effective state, gate evaluation details, and deployment identity.
   - `POST /v1/activation-gates/{gate_id}/challenge`: Commissioner-only route issuing cryptographic HMAC challenges bound to gate identity and deployment identity with <= 300s TTL.
   - `POST /v1/activation-gates/{gate_id}`: Commissioner-only route verifying HMAC signatures over `GATE_SIGNED_FIELDS`, consuming nonces, logging to `activation_gate_log`, and emitting `activation.gate_recorded` audit events. Refuses automatic gates (422) and Phase 3 gates (`G-OUTBOX` -> 409).
   - Approval decision route wiring: passing `dispatch_enabled=app.state.dispatch_effective` and `dispatch_creator=create_dispatch_for_approval`.
2. **`tests/helpers.py` additions**:
   - `fake_packages_dispatchable()`: Generates lead packages containing valid `Objective` objects and numeric player IDs.
   - `seed_verified_players()`: Seeds active, verified players in `public.players`.
3. **`tests/test_dispatch_creation.py` and `conftest.py`**:
   - `enabled_client` fixture: Sets up an app with `dispatch_enabled=True`, synthetic gate logs for `G-ID`, `G-ATOMIC`, and `G-OUTBOX`, fresh worker heartbeat, and verified players.
   - 24 comprehensive test cases covering gate evaluation, cryptographic challenge/record verification, replay protection, tampering rejection, atomic dispatch creation, rollback guarantees, and concurrency serialization.

---

## 1. Graph API Architecture & Route Specifications

### 1.1 Settings & Deployment Identity

In `src/graph_api.py`, `ApiSettings` must be extended to track deployment identity:

```python
@dataclass
class ApiSettings:
    postgres_uri: str
    authenticator: Authenticator
    signing_secret: str | None = None
    approval_ttl_seconds: int = 120
    redis_url: str | None = None
    run_migrations: bool = False
    checkpoint_setup: bool = False
    enforce_db_posture: bool = True
    dispatch_enabled: bool = False
    cli_hmac_enabled: bool = True
    approval_request_ttl_hours: int = 72
    expiry_interval_seconds: int = 60
    # Milestone 4 additions
    code_revision: str | None = None
    deployment_id: str | None = None

    @classmethod
    def from_env(cls) -> "ApiSettings":
        ...
        return cls(
            ...,
            code_revision=read_secret("FC27_CODE_REVISION", required=False),
            deployment_id=read_secret("FC27_DEPLOYMENT_ID", required=False),
        )
```

**Security Invariants**:
- `code_revision` and `deployment_id` represent the running build artifacts.
- If either is empty or `None`, manual gate challenges and recordings are rejected with `409 deployment_identity_missing`.
- Manual gate records from other code revisions or deployments are ignored by `evaluate_gates()`.

---

### 1.2 Gate Re-evaluation Lifecycle

The application state maintains `app.state.gate_report` and `app.state.dispatch_effective`.

```python
def reevaluate_gates(app: FastAPI) -> None:
    """Evaluates all activation gates and updates app.state.dispatch_effective.
    
    Can be called directly by tests without restarting the server.
    """
    from src.dispatch.gates import DeploymentIdentity, dispatch_effective, evaluate_gates

    settings: ApiSettings = getattr(app.state, "settings", None)
    if settings is None:
        return

    identity = DeploymentIdentity(
        code_revision=settings.code_revision or "",
        deployment_id=settings.deployment_id or "",
        fc_version=FC_VERSION,
        platform=DEFAULT_PLATFORM,
    )
    with psycopg.connect(settings.postgres_uri) as conn:
        report = evaluate_gates(conn, identity)

    app.state.gate_report = report
    app.state.dispatch_effective = dispatch_effective(settings.dispatch_enabled, report)

    if settings.dispatch_enabled and not app.state.dispatch_effective:
        failing = [g.gate_id for g in report if not g.passed]
        log.critical("dispatch configured but blocked; failing gates: %s", failing)
```

**Lifespan & Background Loop**:
1. At startup inside `lifespan(app)`:
   - Call `reevaluate_gates(app)` after database posture checks.
2. In `_background_loop()`:
   - In addition to `store.expire_due()`, execute `await asyncio.to_thread(reevaluate_gates, app)` on every interval (default 60s).
3. On gate record submission:
   - As specified in Task 8 (C-1), the record route returns `effective_after: "next_evaluation"`. It does **not** force synchronous re-evaluation; the change applies on the next `reevaluate_gates(app)` cycle (or an explicit call in test harnesses).

---

### 1.3 Route Specifications

#### A. `GET /v1/config/dispatch`
- **Auth**: Service or Commissioner principal (`Depends(principal)`).
- **Response Model**: `DispatchConfigResponse` (in `src/schemas.py`).
- **Response Structure**:
  ```json
  {
    "configured": false,
    "effective": false,
    "gates": [
      {"gate_id": "G-MIGRATION", "passed": true, "detail": "applied 3/3 migrations"},
      {"gate_id": "G-GRANTS", "passed": true, "detail": "grants policy v5 verified"},
      {"gate_id": "G-WORKER", "passed": false, "detail": "no heartbeat within 60s"},
      {"gate_id": "G-ID", "passed": false, "detail": "no passed record for current deployment"},
      {"gate_id": "G-ATOMIC", "passed": false, "detail": "no passed record for current deployment"},
      {"gate_id": "G-OUTBOX", "passed": false, "detail": "no passed record for current deployment"}
    ],
    "code_revision": "git-sha",
    "deployment_id": "deploy-id",
    "fc_version": "27",
    "platform": "common-gen5"
  }
  ```

#### B. `POST /v1/activation-gates/{gate_id}/challenge`
- **Auth**: Commissioner only (`Depends(commissioner)`).
- **HMAC Check**: `Depends(require_hmac)` -> 403 `proof_type_disabled` if CLI HMAC disabled.
- **Path Parameter**: `gate_id: str = Path(pattern=r"^G-[A-Z]+$")`.
- **Request Body**:
  ```python
  class GateChallengeRequest(BaseModel):
      model_config = ConfigDict(extra="forbid")
      status: Literal["passed", "failed"] = "passed"
      evidence_ref: str = Field(default="manual-verification", min_length=1, max_length=500)
  ```
- **Validation Rules**:
  1. If `gate_id not in ("G-ID", "G-ATOMIC", "G-OUTBOX")`: return 422 `automatic gate`.
  2. If `gate_id == "G-OUTBOX"` (outside `RECORDABLE_GATES` in Phase 1): return 409 `not_recordable_before_phase_3` (C-3).
  3. If `not settings.code_revision or not settings.deployment_id`: return 409 `deployment_identity_missing` (C-4).
- **Execution & Storage**:
  1. Retrieve DB timestamp: `SELECT extract(epoch from now())::bigint AS now_ts`.
  2. `issued_at = int(now_ts)`, `expires_at = issued_at + min(settings.approval_ttl_seconds, 300)`.
  3. Generate `nonce = secrets.token_urlsafe(32)`.
  4. Build payload:
     ```python
     payload = {
         "gate_id": gate_id,
         "status": body.status,
         "evidence_ref": body.evidence_ref,
         "code_revision": settings.code_revision,
         "deployment_id": settings.deployment_id,
         "fc_version": FC_VERSION,
         "platform": DEFAULT_PLATFORM,
         "issued_at": issued_at,
         "expires_at": expires_at,
         "nonce": nonce,
     }
     ```
  5. Compute `digest = gate_payload_sha256(payload)`.
  6. Insert into `public.approval_nonces`:
     ```sql
     INSERT INTO public.approval_nonces (
         nonce, gate_id, payload_sha256, issued_at, expires_at, created_by, subject_type
     ) VALUES (
         %s, %s, %s, to_timestamp(%s), to_timestamp(%s), %s, 'activation_gate'
     )
     ```
  7. Return `payload` (status 200).

#### C. `POST /v1/activation-gates/{gate_id}`
- **Auth**: Commissioner only (`Depends(commissioner)`).
- **HMAC Check**: `Depends(require_hmac)`.
- **Path Parameter**: `gate_id: str`.
- **Request Body**:
  ```python
  class GateRecordRequest(BaseModel):
      model_config = ConfigDict(extra="forbid")
      gate_id: str
      status: Literal["passed", "failed"]
      evidence_ref: str = Field(min_length=1, max_length=500)
      code_revision: str
      deployment_id: str
      fc_version: Literal["27"]
      platform: Literal["common-gen5"]
      issued_at: int
      expires_at: int
      nonce: str
      signature: str
  ```
- **Validation Rules**:
  1. If `body.gate_id != gate_id`: return 409 `gate_id path and body mismatch`.
  2. If `gate_id not in ("G-ID", "G-ATOMIC", "G-OUTBOX")`: return 422 `automatic gate`.
  3. If `gate_id not in RECORDABLE_GATES`: return 409 `not_recordable_before_phase_3`.
  4. If `not settings.code_revision or not settings.deployment_id`: return 409 `deployment_identity_missing`.
  5. Cryptographic signature check:
     - Verify HMAC-SHA256 signature using `settings.signing_secret` over canonical JSON of `GATE_SIGNED_FIELDS`.
     - Comparison via `hmac.compare_digest`.
     - Failure / mismatch -> 403 `invalid gate signature`.
- **Transactional DB Verification (Single Transaction)**:
  1. Query `approval_nonces` `FOR UPDATE`:
     ```sql
     SELECT nonce, gate_id, payload_sha256, consumed_at, revoked_at,
            extract(epoch FROM expires_at)::bigint AS expires_at,
            now() > expires_at AS expired,
            to_char(now() + interval '60 seconds' AT TIME ZONE 'utc', 'YYYY-MM-DD"T"HH24:MI:SS"Z"') AS next_eval
       FROM public.approval_nonces
      WHERE nonce = %s
        FOR UPDATE
     ```
  2. If row is `None`: return 409 `unknown gate nonce`.
  3. If `nonce["gate_id"] != gate_id`: return 409 `nonce is bound to a different gate`.
  4. If `nonce["consumed_at"] is not None`: return 409 `nonce already consumed`.
  5. If `nonce["revoked_at"] is not None`: return 409 `nonce revoked`.
  6. If `nonce["expired"]`: return 409 `nonce expired`.
  7. If `nonce["payload_sha256"] != gate_payload_sha256(payload)`: return 409 `nonce payload mismatch`.
  8. Consume nonce:
     ```sql
     UPDATE public.approval_nonces SET consumed_at = now() WHERE nonce = %s
     ```
  9. Insert into `public.activation_gate_log`:
     ```sql
     INSERT INTO public.activation_gate_log (
         gate_id, status, evidence_ref, code_revision, deployment_id,
         fc_version, platform, nonce, signature_sha256, recorded_by
     ) VALUES (
         %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
     ) RETURNING id
     ```
  10. Emit audit event via `record_event`:
      - `event_type`: `"activation.gate_recorded"`
      - `entity_type`: `"activation_gate"`
      - `entity_id`: `gate_id`
      - `actor`: `f"commissioner:{p.name}"`
      - `correlation_id`: `getattr(request.state, "correlation_id", uuid4())`
      - `visibility`: `"audit_only"`
      - `source_table`: `"activation_gate_log"`
      - `source_id`: `str(log_id)`
  11. Commit transaction and return:
      ```json
      {
        "gate": "G-ID",
        "effective_after": "next_evaluation",
        "next_evaluation_at": "2026-10-03T20:25:00Z"
      }
      ```

---

### 1.4 Decision Route Wiring (`POST /v1/approval-requests/{id}/decisions` and `POST /v1/runs/{id}/decision`)

In `apply_decision_internal()` in `src/graph_api.py`:
```python
def apply_decision_internal(request: Request, thread_id: str, body: SignedDecision, p: Principal) -> dict:
    if not settings.cli_hmac_enabled:
        raise HTTPException(403, "proof_type_disabled")

    cid = getattr(request.state, "correlation_id", None)
    dispatch_effective = getattr(app.state, "dispatch_effective", False)

    from src.dispatch.creation import create_dispatch_for_approval

    try:
        state = store.apply_decision(
            thread_id,
            body,
            p,
            settings.signing_secret,
            correlation_id=cid,
            dispatch_enabled=dispatch_effective,
            dispatch_creator=create_dispatch_for_approval,
        )
    except StoreError as exc:
        raise _store_error(exc) from exc

    publish("fc27:approval:decided", {"thread_id": thread_id, "status": state["status"]})
    res = resume_graph(thread_id)

    dispatch_id = state.get("dispatch_id")
    if dispatch_effective and dispatch_id:
        dispatch_block = DispatchBlock(
            enabled=True,
            created=True,
            message="Dispatch created.",
            dispatch_id=str(dispatch_id),
        )
    else:
        dispatch_block = DispatchBlock(
            enabled=False,
            created=False,
            message="Dispatch is disabled. This approval records the selection only; nothing was delivered to any player.",
            dispatch_id=None,
        )

    return {
        **state,
        "thread_id": thread_id,
        "status": state["status"],
        "package_version": state.get("package_version"),
        "approval_audit_id": state.get("approval_audit_id"),
        "approval_request_id": str(state["approval_request_id"]) if state.get("approval_request_id") else None,
        "dispatch": dispatch_block.model_dump(mode="json"),
        "graph_resume": res,
    }
```

---

## 2. Test Helpers Architecture (`tests/helpers.py`)

Two critical helpers must be added to `tests/helpers.py`:

### 2.1 `fake_packages_dispatchable()`

Generates a candidate lead package set where candidates contain valid `Objective` objects and numeric player IDs.

```python
def fake_packages_dispatchable(player_ids: tuple[str, ...] = ("1001", "1002", "1003")) -> dict[str, Any]:
    """Generates lead packages with valid Objectives and numeric player IDs."""
    sels = ["best", "best_runner_up", "middle", "middle_runner_up", "lowest", "ignore"]
    out = {}
    valid_objective = {
        "metric": "pass_accuracy",
        "direction": "increase",
        "target_delta": 5.0,
        "min_matches": 3,
        "max_matches": 5,
        "bounds_policy": "fc27-objective-bounds-v1",
    }
    for role in ("scout", "analyst", "coach", "tactics", "director"):
        pkg = {"owner": role, "approvable": True}
        for i, sel in enumerate(sels):
            if i < len(player_ids):
                pid = player_ids[i]
                pkg[sel] = {
                    "candidate_id": f"{role}-{sel}-{pid}",
                    "player_id": str(pid),
                    "owner": role,
                    "tier": "best" if "best" in sel else ("middle" if "middle" in sel else "lowest"),
                    "score": 80.0,
                    "rationale": f"Valid recommendation for {pid}",
                    "action": "drill",
                    "evidence_refs": ["match:1"],
                    "objective": valid_objective,
                }
        out[role] = pkg
    return out
```

### 2.2 `seed_verified_players()`

Populates `public.players` with active, verified players to satisfy dispatchability requirements.

```python
def seed_verified_players(admin_db_uri: str, ids: Iterable[str], club_id: str = "330323") -> None:
    """Seeds active, verified players in public.players via admin connection."""
    import psycopg
    with psycopg.connect(admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            for pid in ids:
                cur.execute(
                    """INSERT INTO public.players (
                        player_id, club_id, display_name, active, status, platform, identity_verified_at
                    ) VALUES (%s, %s, %s, true, 'active', 'common-gen5', now())
                    ON CONFLICT (player_id) DO UPDATE
                    SET identity_verified_at = EXCLUDED.identity_verified_at,
                        active = true,
                        status = 'active',
                        club_id = EXCLUDED.club_id""",
                    (str(pid), club_id, f"Player {pid}"),
                )
```

---

## 3. Test Fixtures Architecture (`tests/conftest.py`)

### 3.1 `enabled_client` Fixture Design

The `enabled_client` fixture builds the application with `dispatch_enabled=True` and satisfies all activation gates synthetically so tests can verify atomic dispatch creation.

```python
TEST_CODE_REVISION = "test-rev-12345"
TEST_DEPLOYMENT_ID = "test-deploy-67890"

@pytest.fixture()
def enabled_client(pg_db_migrated, pg_admin_db_uri):
    """TestClient wired with dispatch_enabled=True and synthetic passed gates for testing."""
    import psycopg
    from fastapi.testclient import TestClient
    from src.graph_api import create_app, reevaluate_gates
    from tests.helpers import (
        StubPipeline,
        fake_packages_dispatchable,
        seed_verified_players,
        settings,
    )

    pg_uri = pg_db_migrated.app

    # 1. Seed verified players
    seed_verified_players(pg_admin_db_uri, ["1001", "1002", "1003"], club_id="330323")

    # 2. Insert synthetic gate records via admin connection
    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        with conn.cursor() as cur:
            # G-ID manual gate passed
            cur.execute(
                """INSERT INTO public.activation_gate_log (
                    gate_id, status, evidence_ref, code_revision, deployment_id,
                    fc_version, platform, nonce, signature_sha256, recorded_by
                ) VALUES (
                    'G-ID', 'passed', 'test-evidence-g-id', %s, %s,
                    '27', 'common-gen5', 'test-nonce-gid', 'sig-sha-gid', 'commissioner:batmanL8'
                )""",
                (TEST_CODE_REVISION, TEST_DEPLOYMENT_ID),
            )
            # G-ATOMIC manual gate passed
            cur.execute(
                """INSERT INTO public.activation_gate_log (
                    gate_id, status, evidence_ref, code_revision, deployment_id,
                    fc_version, platform, nonce, signature_sha256, recorded_by
                ) VALUES (
                    'G-ATOMIC', 'passed', 'test-evidence-g-atomic', %s, %s,
                    '27', 'common-gen5', 'test-nonce-gatomic', 'sig-sha-gatomic', 'commissioner:batmanL8'
                )""",
                (TEST_CODE_REVISION, TEST_DEPLOYMENT_ID),
            )
            # Synthetic G-OUTBOX: inserted directly as synthetic test setup;
            # does not represent Phase 3 production evidence (C-3).
            cur.execute(
                """INSERT INTO public.activation_gate_log (
                    gate_id, status, evidence_ref, code_revision, deployment_id,
                    fc_version, platform, nonce, signature_sha256, recorded_by
                ) VALUES (
                    'G-OUTBOX', 'passed', 'test-synthetic-g-outbox', %s, %s,
                    '27', 'common-gen5', 'test-nonce-goutbox', 'sig-sha-goutbox', 'commissioner:batmanL8'
                )""",
                (TEST_CODE_REVISION, TEST_DEPLOYMENT_ID),
            )
            # Synthetic fresh worker heartbeat (C-2): no production worker in Phase 1
            cur.execute(
                """INSERT INTO public.service_heartbeats (service_name, last_heartbeat, details)
                   VALUES ('dispatch-worker', now(), '{"test":"synthetic"}'::jsonb)
                   ON CONFLICT (service_name) DO UPDATE SET last_heartbeat = now()"""
            )

    # 3. Create app settings with dispatch_enabled=True and deployment identity
    app_settings = settings(
        pg_uri,
        dispatch_enabled=True,
        code_revision=TEST_CODE_REVISION,
        deployment_id=TEST_DEPLOYMENT_ID,
    )
    events = []
    pipeline = StubPipeline(packages=fake_packages_dispatchable())
    app = create_app(
        app_settings,
        pipeline=pipeline,
        publisher=lambda ch, p: events.append((ch, p)),
    )

    # 4. Directly reevaluate gates on app without restarting graph-api (C-1)
    reevaluate_gates(app)

    with TestClient(app) as client:
        client.events = events
        client.app = app
        yield client
```

---

## 4. Test Suite Architecture (`tests/test_dispatch_creation.py`)

The test suite consists of 24 required test cases implementing Task 8 Step 1 (lines 980–1018).

### Matrix of Required Test Cases

| # | Test Function Name | Focus / Requirement | Expected Assertion / Behavior |
|---|--------------------|---------------------|-------------------------------|
| 1 | `test_gates_block_by_default` | Default gate posture | `G-WORKER`, `G-ID`, `G-ATOMIC`, `G-OUTBOX` fail; `dispatch_effective` is `False`. |
| 2 | `test_configured_but_blocked_behaves_disabled` | AC-45: Configured but blocked | `dispatch_enabled=True` + no gate records -> no dispatch created; `/health/ready` reports blocked. |
| 3 | `test_gate_recording_requires_commissioner_and_signature` | C-4, §31.2: Security & Roles | Svc token -> 403; automatic gate -> 422; bad sig -> 403; valid -> 200 with `effective_after: "next_evaluation"`; emits `activation.gate_recorded`. |
| 4 | `test_gate_nonce_replay_rejected` | C-4: Nonce replay prevention | Second POST of same payload -> 409; exactly 1 log row in DB. |
| 5 | `test_gate_nonce_expired_rejected` | C-4: Expiration enforcement | Admin moves `approval_nonces.expires_at` into past -> 409; 0 log rows. |
| 6 | `test_gate_tampered_field_rejected` | C-4: Tamper resistance | Modifying `evidence_ref`, `code_revision`, or `nonce` -> 403. |
| 7 | `test_gate_nonce_bound_to_gate` | C-4: Nonce subject binding | Challenge for `G-ID`, submit to `G-ATOMIC` -> 409; 0 log rows. |
| 8 | `test_gate_requires_deployment_identity` | C-4: Identity prerequisite | Empty `code_revision` or `deployment_id` -> 409 `deployment_identity_missing`. |
| 9 | `test_gate_record_from_other_deployment_ignored` | C-4: Multi-deployment isolation | Log row with mismatched `code_revision` or `deployment_id` does not satisfy gate. |
| 10 | `test_gate_signature_excludes_correlation_id` | C-4, C-5: Header exclusion | Two different `X-Correlation-Id` headers verify same signed payload; correlation ID not in `GATE_SIGNED_FIELDS`. |
| 11 | `test_g_outbox_not_recordable_in_phase1` | C-3: Phase boundary protection | Challenge or record for `G-OUTBOX` -> 409 `not_recordable_before_phase_3`. |
| 12 | `test_gate_change_applies_on_next_evaluation` | C-1: Dynamic evaluation | Recording gate does not change `/v1/config/dispatch` until `reevaluate_gates(app)` runs; no process restart needed. |
| 13 | `test_correlation_id_shared_by_dispatch_and_events` | C-5: Correlation propagation | `X-Correlation-Id` on approval request is stored on `dispatches.created_correlation_id` and all generated events. |
| 14 | `test_approve_creates_dispatch_atomically` | AC-1, AC-2: Atomic creation | Valid approval with `dispatch_effective=True` creates 1 pending dispatch with exact provenance, emits `approval.decided` and `dispatch.created`, 2 outbox rows. |
| 15 | `test_dispatch_failure_rolls_back_decision` | Rollback guarantee | Injected DB trigger failure on `dispatches` rolls back decision: thread still awaiting, 0 audit rows, nonce unconsumed. |
| 16 | `test_not_dispatchable_without_objective` | AC-5: Objective required | Candidate missing `objective` -> 409 `not_dispatchable`; decision rolled back. |
| 17 | `test_not_dispatchable_gamertag_player` | AC-46: Player ID format | Candidate `player_id='GamerTag99'` -> 409 `not_dispatchable`. |
| 18 | `test_not_dispatchable_unverified_player` | Verification check | Player with `identity_verified_at IS NULL` -> 409 `not_dispatchable`. |
| 19 | `test_player_id_is_compared_exactly` | Review Focus 3: Exact string match | `players.player_id='7'` vs candidate `player_id='007'` -> 409 `not_dispatchable`. |
| 20 | `test_not_dispatchable_unknown_player` | Existence check | Candidate `player_id='999999'` absent from `players` table -> 409 `not_dispatchable`. |
| 21 | `test_concurrent_supersede_is_serialized` | Advisory lock serialization | Two parallel approvals for same player/owner/metric -> serialized via advisory lock; exactly 1 active dispatch, 1 superseded, no 500. |
| 22 | `test_reject_creates_no_dispatch` | Decision outcome safety | `decision=REJECT` -> thread rejected, 0 rows in `dispatches`. |
| 23 | `test_supersede_is_atomic` | AC-9: Supersede atomicity | Second approval for same player/owner/metric transitions old dispatch to `superseded` (`superseded_by=new_id`) and creates new `pending` dispatch. |
| 24 | `test_parallel_duplicate_approvals_single_dispatch` | AC-18: Idempotency under concurrency | 10 parallel submissions of identical decision -> 1 winner, exactly 1 audit row, exactly 1 dispatch row. |

---

## 5. Detailed Test Implementation Sketches

### Tests 1 & 2: Default Block & Health Probe
```python
def test_gates_block_by_default(pg_uri):
    """All gates evaluated; G-WORKER, G-ID, G-ATOMIC, G-OUTBOX fail; effective False."""
    from src.dispatch.gates import DeploymentIdentity, dispatch_effective, evaluate_gates

    identity = DeploymentIdentity("test-rev", "test-dep")
    with psycopg.connect(pg_uri) as conn:
        report = evaluate_gates(conn, identity)

    assert any(g.gate_id == "G-MIGRATION" and g.passed for g in report)
    assert any(g.gate_id == "G-GRANTS" and g.passed for g in report)
    assert any(g.gate_id == "G-WORKER" and not g.passed for g in report)
    assert any(g.gate_id == "G-ID" and not g.passed for g in report)
    assert any(g.gate_id == "G-ATOMIC" and not g.passed for g in report)
    assert any(g.gate_id == "G-OUTBOX" and not g.passed for g in report)
    assert dispatch_effective(True, report) is False
    assert dispatch_effective(False, report) is False


def test_configured_but_blocked_behaves_disabled(pg_uri):
    """AC-45: dispatch_enabled=True settings, no gate records -> no dispatch, health blocked."""
    from tests.helpers import COM, SVC, start, decide, make_app, settings

    app_settings = settings(pg_uri, dispatch_enabled=True, code_revision="rev1", deployment_id="dep1")
    app, events = make_app(pg_uri, settings=app_settings)
    with TestClient(app) as client:
        # Check health probe reports blocked / degraded
        r_ready = client.get("/health/ready")
        assert r_ready.status_code == 503
        data = r_ready.json()
        assert "blocked" in data.get("reason", "") or "G-WORKER" in data.get("reason", "")

        # Run pipeline and approve -> behaves as disabled
        tid = start(client)
        r_dec = decide(client, tid)
        assert r_dec.status_code == 200
        assert r_dec.json()["dispatch"]["enabled"] is False
        assert r_dec.json()["dispatch"]["created"] is False
```

### Tests 3 to 7: Cryptographic Gate Recording & Security Bounds
```python
def test_gate_recording_requires_commissioner_and_signature(client):
    """Service token -> 403; G-WORKER -> 422; bad sig -> 403; valid -> 200 effective_after next_eval."""
    from tests.helpers import COM, SVC, SIGNING_SECRET
    from src.dispatch.gates import sign_gate

    # Service token rejected
    r = client.post("/v1/activation-gates/G-ID/challenge", headers=SVC, json={})
    assert r.status_code == 403

    # Automatic gate rejected
    r = client.post("/v1/activation-gates/G-WORKER/challenge", headers=COM, json={})
    assert r.status_code == 422

    # Request valid challenge
    r_ch = client.post("/v1/activation-gates/G-ID/challenge", headers=COM, json={"status": "passed", "evidence_ref": "audit-ref-1"})
    assert r_ch.status_code == 200, r_ch.text
    ch_payload = r_ch.json()

    # Bad signature rejected
    bad_sub = {**ch_payload, "signature": "0" * 64}
    assert client.post("/v1/activation-gates/G-ID", headers=COM, json=bad_sub).status_code == 403

    # Valid signature accepted
    valid_sig = sign_gate(ch_payload, SIGNING_SECRET)
    good_sub = {**ch_payload, "signature": valid_sig}
    r_sub = client.post("/v1/activation-gates/G-ID", headers=COM, json=good_sub)
    assert r_sub.status_code == 200, r_sub.text
    res = r_sub.json()
    assert res["gate"] == "G-ID"
    assert res["effective_after"] == "next_evaluation"
    assert "next_evaluation_at" in res
```

### Tests 14 & 15: Atomic Dispatch Creation & Rollback Guarantee
```python
def test_approve_creates_dispatch_atomically(enabled_client, pg_uri):
    """AC-1/AC-2: one pending dispatch; exact provenance; events and outbox rows."""
    from tests.helpers import COM, start, decide, APPROVE_COACH_BEST
    import psycopg

    tid = start(enabled_client)
    r_dec = decide(enabled_client, tid, intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"})
    assert r_dec.status_code == 200, r_dec.text
    dec_data = r_dec.json()
    assert dec_data["dispatch"]["enabled"] is True
    assert dec_data["dispatch"]["created"] is True
    dispatch_id = dec_data["dispatch"]["dispatch_id"]
    assert dispatch_id is not None

    with psycopg.connect(pg_uri) as conn:
        d_row = conn.execute(
            """SELECT dispatch_id, dispatch_status, player_id, candidate_id, owner,
                      approval_id, approval_request_id, package_version
                 FROM public.dispatches WHERE dispatch_id = %s""",
            (dispatch_id,),
        ).fetchone()
        assert d_row is not None
        assert d_row["dispatch_status"] == "pending"
        assert d_row["player_id"] == "1001"
        assert d_row["candidate_id"] == "coach-best-1001"
        assert d_row["approval_id"] == dec_data["approval_audit_id"]

        # Check outbox count: approval.created + approval.decided + dispatch.created = 3
        outbox_count = conn.execute("SELECT count(*) FROM public.outbox WHERE source_id IN (%s, %s, %s)",
                                    (dec_data["approval_request_id"], str(dec_data["approval_audit_id"]), dispatch_id)).fetchone()[0]
        assert outbox_count >= 2


def test_dispatch_failure_rolls_back_decision(enabled_client, pg_admin_db_uri):
    """Injected trigger on dispatches insert -> 500/StoreError; thread awaiting; 0 audit, 0 dispatch."""
    from tests.helpers import COM, start, decide, APPROVE_COACH_BEST
    import psycopg

    with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
        conn.execute("""
            CREATE OR REPLACE FUNCTION trigger_fail_dispatches() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'simulated dispatches failure';
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER trg_fail_dispatch BEFORE INSERT ON public.dispatches
            FOR EACH ROW EXECUTE FUNCTION trigger_fail_dispatches();
        """)

    try:
        tid = start(enabled_client)
        r_dec = decide(enabled_client, tid, intent={**APPROVE_COACH_BEST, "player_id": "1001", "candidate_id": "coach-best-1001"})
        assert r_dec.status_code in (409, 500)

        with psycopg.connect(pg_admin_db_uri) as conn:
            # Thread still awaiting
            t_row = conn.execute("SELECT status FROM public.graph_threads WHERE thread_id = %s", (tid,)).fetchone()
            assert t_row["status"] == "awaiting_approval"
            # 0 audits
            audits = conn.execute("SELECT count(*) FROM public.approval_audit WHERE thread_id = %s", (tid,)).fetchone()[0]
            assert audits == 0
            # 0 dispatches
            dispatches = conn.execute("SELECT count(*) FROM public.dispatches WHERE source_thread_id = %s", (tid,)).fetchone()[0]
            assert dispatches == 0
            # Nonce remains unconsumed
            nonces = conn.execute("SELECT count(*) FROM public.approval_nonces WHERE thread_id = %s AND consumed_at IS NOT NULL", (tid,)).fetchone()[0]
            assert nonces == 0
    finally:
        with psycopg.connect(pg_admin_db_uri, autocommit=True) as conn:
            conn.execute("DROP TRIGGER IF EXISTS trg_fail_dispatch ON public.dispatches;")
            conn.execute("DROP FUNCTION IF EXISTS trigger_fail_dispatches;")
```

### Tests 16 to 20: Strict Dispatchability Checks
```python
def test_not_dispatchable_without_objective(enabled_client):
    """Candidate missing objective -> 409 not_dispatchable; decision rolled back."""
    # Custom pipeline providing candidate with objective=None
    ...

def test_not_dispatchable_gamertag_player(enabled_client):
    """Candidate player_id 'GamerTag99' -> 409 not_dispatchable."""
    ...

def test_not_dispatchable_unverified_player(enabled_client, pg_admin_db_uri):
    """Player with identity_verified_at NULL -> 409 not_dispatchable."""
    ...

def test_player_id_is_compared_exactly(enabled_client, pg_admin_db_uri):
    """players '7', candidate '007' -> 409 not_dispatchable."""
    ...

def test_not_dispatchable_unknown_player(enabled_client):
    """Numeric player_id absent from players table -> 409 not_dispatchable."""
    ...
```

### Tests 21 to 24: Concurrency, Atomicity & Supersession
```python
def test_concurrent_supersede_is_serialized(enabled_client, pg_uri):
    """Two threads, same player/owner/metric, approved in parallel -> advisory lock serializes; 1 active, 1 superseded."""
    ...

def test_reject_creates_no_dispatch(enabled_client):
    """REJECT decision updates thread to rejected, creates no dispatch row."""
    ...

def test_supersede_is_atomic(enabled_client, pg_uri):
    """Second approval transitions prior dispatch to superseded (superseded_by=new_id) and creates new pending."""
    ...

def test_parallel_duplicate_approvals_single_dispatch(enabled_client, pg_uri):
    """10 parallel identical approval requests -> exactly 1 winner commits, 1 audit row, 1 dispatch row."""
    ...
```

---

## 6. Migration & Grants Alignment

Milestone 4 requires DDL objects from Section E of `migrations/0003_dispatch_foundation.sql` and `migrations/post/grants.sql`:
- `public.activation_gate_log`:
  - `fc27_app` role must have `SELECT, INSERT` privileges.
  - Immutability trigger (`fc27_reject_mutation()`) prevents `UPDATE` and `DELETE`.
- `public.activation_gates` view:
  - `fc27_app` role must have `SELECT` privilege.
- `public.service_heartbeats`:
  - `fc27_app` role has `SELECT`.
  - `fc27_dispatch` role has `SELECT, INSERT, UPDATE`.
- `public.approval_nonces`:
  - `fc27_app` has `SELECT, INSERT, UPDATE` (already granted in Task 2).
- `public.fc27_check_event_source()`:
  - Must recognize `activation_gate_log` as a valid source table for `activation.gate_recorded` domain events.

---

## 7. Implementation Checklist for Workers

When Milestone 4 enters implementation:
1. **Worker 1 (Schema & DB)**:
   - Append Section E to `migrations/0003_dispatch_foundation.sql`.
   - Update `migrations/post/grants.sql` and bump `GRANTS_POLICY` version to `5`.
2. **Worker 2 (Gates & Creation Python Logic)**:
   - Create `src/dispatch/gates.py` (`GateResult`, `DeploymentIdentity`, `RECORDABLE_GATES`, `GATE_SIGNED_FIELDS`, `evaluate_gates`, `dispatch_effective`, cryptographic signing helpers).
   - Create `src/dispatch/creation.py` (`NotDispatchable`, `create_dispatch_for_approval`, advisory lock, supersede transition, insertion).
3. **Worker 3 (Graph API Routes & Store Wiring)**:
   - Add `code_revision` and `deployment_id` to `ApiSettings` in `src/graph_api.py`.
   - Mount `/v1/activation-gates/{gate_id}/challenge` and `/v1/activation-gates/{gate_id}` routes.
   - Update `/v1/config/dispatch` to return full gate reports and deployment identity.
   - Wire `dispatch_effective` and `create_dispatch_for_approval` into `apply_decision_internal()`.
4. **Worker 4 (Test Helpers & Suite)**:
   - Add `fake_packages_dispatchable()` and `seed_verified_players()` to `tests/helpers.py`.
   - Add `enabled_client` fixture to `tests/conftest.py`.
   - Implement the 24 test cases in `tests/test_dispatch_creation.py`.
   - Verify 100% pass across focused and regression suites.
