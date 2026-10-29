> Archived document
>
> This file is retained for historical context and is not an authoritative
> operational guide. See `docs/ARCHITECTURE.md and docs/SECURITY_BOUNDARIES.md` for the current implementation.
> Archived on `2026-10-29` from `ORIGINAL_REQUEST.md`.
>
> Warning: Do not execute commands in this archived document.

# Original User Request

## 2026-10-03T13:58:40Z

**Task**: /teamwork-preview /boost # Teamwork Project Prompt: Phase 1 Secure Data Foundation

Status: Approved execution brief
Goal: Complete and verify Phase 1 only, then stop at the phase boundary
Requested team: Full implementation/review team
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild
Integrity mode: development

## 0. Mission

Implement Phase 1: Secure Data Foundation for the player-dispatch programme.

Phase 1 establishes the authoritative database, event/audit foundation,
versioned recommendation/approval model, LangGraph persistence, and persistent
Graph API needed by later phases.

Phase 1 is NOT the completed player-dispatch programme.

Do not implement Phase 2 through Phase 6 functionality except for schema,
interfaces, placeholders, or compatibility seams explicitly required by the
approved Phase 1 design.

At the Phase 1 boundary, stop implementation and produce:
1. the Phase 1 implementation report;
2. the Phase 1 acceptance trace;
3. the detailed Phase 2 implementation plan for review;
4. the preserved Phase 3 through Phase 6 roadmap and traceability matrix.

Do not start Phase 2 implementation in this execution.

## 1. Non-negotiable Phase 1 invariants

These are programme-level requirements and override convenience shortcuts.

1. DISPATCH_ENABLED=false at all times.
2. No Phase 1 path may deliver anything to a player.
3. No approval endpoint may call a player delivery adapter.
4. No Graph node may transition to dispatched, delivered, acknowledged,
   activated, or tracking.
5. Approved and rejected are terminal Phase 1 outcomes.
6. PostgreSQL is authoritative for approvable packages, approval requests,
   approval decisions, and authoritative audit/domain events.
7. LangGraph checkpoints are workflow state, not authorization state.
8. Redis/pub-sub, WebSocket messages, LLM output, request bodies, Graph state,
   and Command(resume=...) are not authoritative approval evidence.
9. A resume request only wakes/re-enters the Graph. It cannot approve or reject.
10. Only a valid committed PostgreSQL decision bound to the exact current
    package ID/version/hash may produce an approved or rejected Graph terminal
    state.
11. A package must not be modified in place after becoming approvable.
    Material modification requires a new package version/hash and a new
    approval lifecycle.
12. Test-only synthetic satisfaction of gates is testing, not production
    authorization.
13. No production dispatch may be enabled as part of Phase 1 acceptance.
14. Phase 1 completion must never be described as programme completion.

Required Phase 1 completion statement:

"Secure data foundation complete. Dispatch remains disabled.
No player delivery has occurred."

## 2. Repository inspection before changes

Before implementation:

- inspect the current migrations and migration runner;
- inspect database roles and grants;
- inspect graph.py, graph_api.py, approval.py, approval_store.py,
  decision-related modules, event/outbox modules, configuration,
  Docker Compose, tests, and Phase 1 design/plan documents;
- preserve already completed and passing Phase 1 work;
- do not recreate existing functionality under duplicate tables/modules;
- reconcile this brief with the current branch state before editing.

Use test-first implementation where practical.

Do not change unrelated live-monitoring, OCR, analytics, or Swarms functionality
unless a change is strictly required to keep Phase 1 tests or configuration
consistent.

## 3. Agent A: Authoritative data model and migrations

Complete the Phase 1 authoritative schema.

The final schema must provide or reconcile the approved equivalents of:

### 3.1 graph_runs

Required semantics:

- unique thread/run identity;
- correlation to club and trigger/source;
- durable status;
- current workflow position where required;
- creation/update/terminal timestamps;
- safe failure information.

Allowed Phase 1 lifecycle states should remain within the secure foundation,
for example:

created
running
awaiting_approval
approved
rejected
failed

Do NOT add dispatch/delivery states as active Phase 1 transitions.

### 3.2 recommendation_packages

Requirements:

- immutable package identity/version relationship;
- thread/run correlation;
- club correlation;
- package type;
- canonical structured payload or approved equivalent;
- deterministic payload hash;
- source/evidence linkage where required by the design;
- validation state;
- creation metadata;
- explicit supersession/version relationship.

An existing approvable package must not be silently overwritten.

### 3.3 approval_requests

Bind each request to:

- exact recommendation package ID;
- exact package version;
- exact package hash;
- Graph thread/run;
- required approving authority;
- lifecycle status;
- creation time;
- expiry/invalidation fields where required by the approved Phase 1 plan.

### 3.4 approval_decisions

Store authoritative terminal decisions with:

- decision ID;
- approval request ID;
- package ID/version/hash binding;
- decision limited to approved or rejected;
- notes/reason where supported;
- actor reference under the approved Phase 1 bootstrap authorization model;
- decision timestamp;
- idempotency/replay protection;
- placeholder/evidence fields needed for future Phase 2 authentication
  integration, without implementing Phase 2 identity.

Required behavior:

- nonexistent approval request: reject;
- wrong package: reject;
- wrong version: reject;
- wrong hash: reject;
- expired/invalid request: reject;
- duplicate identical idempotent request: safe;
- conflicting terminal decision: reject.

### 3.5 Audit/domain event infrastructure

Use and extend the existing approved event/audit foundation rather than
creating an unnecessary parallel audit architecture.

Authoritative events must cover at minimum:

- package created/versioned;
- approval request created;
- approval decision committed;
- Graph resume/reconciliation requested;
- terminal approved/rejected state;
- failures relevant to the authoritative lifecycle.

Preserve append-only behavior and source validation.

### 3.6 Outbox boundary

Preserve the transactional outbox infrastructure required by later phases.

Important:

Phase 1 may create outbox records where explicitly required by the approved
data-foundation design, but MUST NOT run a production dispatch relay or deliver
a player package.

The presence of an outbox row does not mean dispatch occurred.

## 4. Agent B: Graph core

Refactor the Graph into a reusable application module suitable for both tests
and the persistent Graph API.

### 4.1 Remove double interruption

Remove:

interrupt_before=["approval"]

Retain exactly one intentional LangGraph approval interruption point.

Acceptance requires proof that exactly one approval interrupt occurs per
approval lifecycle.

### 4.2 Recommendation package creation

Before approval interruption:

1. generate the recommendation output;
2. canonicalize the approvable structured package using the existing design;
3. determine the package version;
4. compute the deterministic package hash;
5. persist the immutable package;
6. create the approval request bound to that package;
7. record corresponding authoritative audit/domain events;
8. enter awaiting_approval;
9. interrupt.

Do not allow the LLM narrative itself to become authorization evidence.

### 4.3 Approval interrupt payload

The interrupt payload must contain only identifiers and display-safe data
needed by a future human interface.

Include the approved subset of:

- thread_id;
- approval_request_id;
- package_id;
- package_version;
- package_hash;
- safe summary;
- required approving authority.

Do not place credentials, secrets, unrestricted checkpoint data, or hidden
authorization state into the interrupt payload.

### 4.4 Resume semantics

CRITICAL SECURITY REQUIREMENT:

Command(resume=...), POST /resume, or any request body containing values such
as:

approved=true
decision=approved
commissioner_input=APPROVE

MUST NOT constitute authorization.

After resume, the Graph must reread the authoritative PostgreSQL decision.

Validate:

- decision exists;
- approval request matches;
- package ID matches;
- package version matches;
- package hash matches;
- request remains valid;
- terminal decision value is approved or rejected;
- Phase 1 actor/authorization evidence required by the current approved design
  is valid.

If no authoritative decision exists, do not approve.

If the decision is stale, mismatched, invalid, expired, or otherwise
unacceptable, fail closed and preserve auditable state.

### 4.5 Terminal Graph states

Approved:

APPROVED, NOT DISPATCHED

Rejected:

REJECTED, NO ACTION TAKEN

Remove wording such as:

APPROVED and ACTIVE

No Phase 1 edge may lead from approval to dispatch.

### 4.6 Failure ordering

Authoritative decision persistence occurs before Graph resume.

Required order:

authorize current Phase 1 actor
→ validate approval request
→ validate exact package/version/hash
→ write authoritative decision
→ write audit/domain event
→ COMMIT
→ request Graph resume
→ Graph rereads committed decision
→ terminal approved/rejected state

If Graph resume fails after the database commit:

- do not roll back or erase the Commissioner decision;
- preserve the committed decision as authoritative;
- record a recoverable/reconciliation-required condition;
- allow safe idempotent reconciliation/retry.

## 5. Agent C: Persistent Graph API

Implement or complete the persistent Graph API as the HTTP boundary around the
reusable Graph runner.

Use versioned routes.

### 5.1 POST /v1/runs

Purpose:
create/start a Graph run.

Requirements:

- schema validation;
- numeric/approved club-ID validation consistent with the existing system;
- idempotency or duplicate-request protection;
- persistent run record created before or atomically with workflow start as
  appropriate to the approved design;
- must not accept approval as part of run creation;
- must return dispatch_enabled=false;
- external caller-supplied thread identifiers must not permit unsafe collision
  or takeover.

### 5.2 GET /v1/runs/{thread_id}

Return an authorized/safe projection including as applicable:

- thread_id;
- status;
- current node/state;
- package ID/version;
- approval request ID;
- errors safe for the caller;
- dispatch_enabled=false.

Do not expose secrets or unrestricted checkpoint internals.

### 5.3 GET /v1/runs/{thread_id}/approval-request

Return:

- approval request identity/state;
- package identity/version/hash;
- display-safe package summary;
- expiry/invalidation status if applicable.

### 5.4 POST /v1/approval-requests/{approval_request_id}/decisions

This is the authoritative Phase 1 decision-write path.

The endpoint must:

1. authorize under the approved Phase 1 bootstrap policy;
2. validate the decision schema;
3. lock/read the approval request transactionally;
4. validate current request state;
5. load and validate exact package ID/version/hash;
6. enforce idempotency;
7. reject conflicting decisions;
8. persist the decision;
9. persist audit/domain event evidence;
10. commit;
11. only then request Graph resume/reconciliation.

A Graph failure after step 10 cannot invalidate the committed decision.

### 5.5 POST /v1/runs/{thread_id}/resume

If this route remains exposed, it must be internal/restricted.

It MUST NOT accept or honor an approval/rejection value.

Its purpose is only to request that the Graph resume and reread authoritative
database state.

### 5.6 GET /health/live

Process liveness only.

### 5.7 GET /health/ready

Readiness should verify the Phase 1 dependencies required by the implementation,
including:

- database/connectivity;
- Graph/checkpointer readiness;
- required configuration;
- database posture where required by the approved role model;
- DISPATCH_ENABLED=false.

Readiness must fail if Phase 1 safety configuration is invalid.

### 5.8 Graph API access policy

Do not inherit permissive browser-facing behavior from unrelated services.

Implement the restrictive Phase 1 service-access mechanism already approved by
the repository plan/design.

Do NOT silently invent the Phase 2 user/session/WebAuthn system here.

Phase 2 owns production identity and authorization.

## 6. Agent D: Compose, database roles, and configuration

Add or complete the persistent graph-api service in Compose.

Requirements:

- persistent service, not the existing one-shot graph runner;
- port 8001 internally;
- expose to host only if required by the approved Phase 1 development model;
- depend on required healthy services;
- run under the intended least-privileged application database role;
- migrations continue to run under the migration role;
- do not let graph-api run as database owner/superuser;
- do not let runtime call checkpointer schema setup if that requires owner DDL;
- use the migration path for schema creation;
- enforce DISPATCH_ENABLED=false.

Update the live watcher Graph target to the actual graph-api service and
versioned run endpoint if watcher integration remains in Phase 1 acceptance.

Standardize configuration names.

Use one canonical interpretation for:

POSTGRES_URI
FC27_MCP_URL
GRAPH_URL
COMMISSIONER / bootstrap authority settings
DISPATCH_ENABLED

Remove conflicting fallback database names/credentials where they undermine the
current role/migration design.

Do not commit secrets.

## 7. Agent E: Security and lifecycle tests

Add negative tests before treating Phase 1 as accepted.

### 7.1 Graph interruption

- exactly one interrupt;
- checkpoint persists at interruption;
- restart retains resumability.

### 7.2 Authorization bypass attempts

Prove all of the following fail to authorize by themselves:

- Command(resume={"approved": true});
- resume payload with "APPROVE";
- POST /resume with approved=true;
- altered checkpoint state claiming approved;
- package approval with wrong version;
- package approval with wrong hash;
- decision attached to another request/thread/package;
- stale/superseded approval;
- conflicting terminal decision.

### 7.3 Valid completion

Test:

- committed valid approval → Graph resumes → APPROVED, NOT DISPATCHED;
- committed valid rejection → Graph resumes → REJECTED, NO ACTION TAKEN.

### 7.4 Idempotency

Test:

- repeated identical run request, where supported;
- repeated identical decision;
- repeated resume after terminal completion;
- reconciliation after simulated Graph resume failure.

No duplicate decision or terminal transition may be produced.

### 7.5 Database safety

Test that runtime database identities cannot:

- run unauthorized DDL;
- alter migration-controlled schema objects;
- acquire or inherit the database owner role;
- operate as superuser;
- bypass append-only protections on authoritative history/event records;
- modify an immutable recommendation package in place after it becomes approvable;
- update or delete an authoritative terminal approval decision;
- substitute another package ID, version, or hash into an existing approval lifecycle;
- grant themselves additional privileges;
- execute migration/bootstrap operations intended only for privileged migration roles.

Verify least privilege through negative tests using the actual runtime
connection identities, not only through static inspection of grants.

The Graph API runtime identity must not require schema-owner privileges for
normal startup, run creation, checkpoint reads/writes, approval processing,
or recovery.

Schema/checkpointer setup requiring DDL must occur through the approved
migration/bootstrap path, not normal Graph API startup.

### 7.6 No-dispatch proof

Phase 1 acceptance must explicitly demonstrate:

- DISPATCH_ENABLED=false;
- no production dispatch worker is started;
- no player delivery adapter is invoked;
- no dashboard delivery is treated as player dispatch;
- no WebSocket publication is treated as authoritative delivery;
- no approval decision causes player delivery;
- no approved Graph state transitions to dispatched, delivered, activated,
  acknowledged, or tracking;
- no test-only gate satisfaction is represented as production authorization;
- recommendation approval terminates as APPROVED, NOT DISPATCHED;
- recommendation rejection terminates as REJECTED, NO ACTION TAKEN.

Redis and WebSocket infrastructure may remain available for existing
non-authoritative monitoring, but neither constitutes authoritative approval
or player delivery.

## 8. Agent F: Integration and adversarial review

After the scoped implementation from Agents A through E is integrated, assign
an independent review agent to validate the entire Phase 1 result.

The review agent must inspect the implementation itself and run the relevant
tests. Do not accept another agent's summary or statement that tests passed as
sufficient evidence.

The review must include:

1. inspect the complete integrated diff;
2. compare the implementation against the approved Phase 1 plan/specification;
3. verify migrations from a fresh database;
4. verify migration replay/idempotency behavior;
5. verify database role/grant posture;
6. verify Graph checkpoint creation and restart persistence;
7. verify the Graph has exactly one approval interruption mechanism;
8. verify recommendation packages are persisted before approval interruption;
9. verify approval requests bind to the exact package ID/version/hash;
10. attempt authorization bypasses;
11. verify decision commit occurs before Graph resume;
12. simulate Graph resume failure after decision commit;
13. verify reconciliation/recovery without changing the committed decision;
14. validate Graph API endpoint behavior and failure responses;
15. inspect Compose and environment-variable wiring;
16. verify DISPATCH_ENABLED=false;
17. verify no production player dispatch path exists;
18. run focused Phase 1 tests;
19. run the complete repository test suite;
20. inspect git status and final diff for unrelated or future-phase changes.

Do not repair a failing security test by removing, weakening, skipping,
xfailing, or rewriting the test so that unsafe behavior becomes acceptable.

If an acceptance criterion cannot legitimately pass, leave it failed and
document the blocker.

## 9. Documentation corrections

Update the authoritative Phase 1 planning and handoff documentation, along with
the repository README documentation relevant to this programme.

Use this wording:

"Phase 1 intentionally implements only the secure data foundation. The
dashboard, identity, player-agent pipeline, personalization, outbox relay,
tracking, analytics, audit UI, and operational deployment remain required
deliverables in Phases 2 through 6."

Use this Phase 1 completion statement:

"Secure data foundation complete. Dispatch remains disabled.
No player delivery has occurred."

Correct existing wording that implies any of the following are Phase 1
outcomes:

- Commissioner approval immediately executes an action;
- approved recommendations become active;
- training programs are issued to players;
- dispatch is operational;
- the final dashboard is complete;
- identity/authentication is complete;
- player acknowledgment is implemented;
- tracking and analytics are complete;
- completion of Phase 1 means completion of the player-dispatch programme.

Documentation must distinguish:

approval != activation
approval != dispatch
Graph resume != authorization
Redis publication != authoritative delivery
Phase 1 completion != programme completion

## 10. Persistent programme status

Maintain the following checklist in the programme documentation:

- [ ] Phase 1: Secure data foundation
- [ ] Phase 2: Identity and authorization
- [ ] Phase 3: Dispatch worker and player-agent pipeline
- [ ] Phase 4: Dashboard UI and real-time delivery
- [ ] Phase 5: Tracking and analytics
- [ ] Phase 6: Operations, audit, and rollout

A checked Phase 1 box must never imply overall programme completion.

When Phase 1 is accepted, only the Phase 1 box may be checked.

## 11. Required Phase 1 acceptance criteria

### 11.1 Database and migration acceptance

- [ ] Fresh database/bootstrap succeeds.
- [ ] Required database roles are created/configured through the approved
      bootstrap mechanism.
- [ ] All Phase 1 migrations apply in version order.
- [ ] Re-running migrations produces the designed safe/idempotent result.
- [ ] Modification of an already-applied migration is detected.
- [ ] Runtime application role cannot run unauthorized DDL.
- [ ] Runtime application role is not owner/superuser.
- [ ] Recommendation package versions cannot be silently overwritten.
- [ ] Canonical package hashing is deterministic.
- [ ] An approval decision cannot reference a nonexistent approval request.
- [ ] A decision for the wrong package is rejected.
- [ ] A decision for the wrong package version is rejected.
- [ ] A decision for the wrong package hash is rejected.
- [ ] Expired/invalidated requests fail closed where applicable.
- [ ] Repeated identical idempotent decision submission is safe.
- [ ] A conflicting second terminal decision is rejected.
- [ ] Required authoritative audit/domain records are append-only.
- [ ] Required domain-event/outbox writes have the designed transaction
      atomicity.
- [ ] Application runtime cannot mutate authoritative immutable history.

### 11.2 Graph acceptance

- [ ] The Graph uses exactly one approval interrupt.
- [ ] No interrupt_before=["approval"] remains in addition to an explicit
      interrupt().
- [ ] Recommendation package is persisted before the approval interrupt.
- [ ] Approval request exists before the approval interrupt.
- [ ] Approval request binds exact package ID/version/hash.
- [ ] Graph checkpoint persists while awaiting approval.
- [ ] Restart preserves the interrupted Graph.
- [ ] Command(resume={"approved": true}) cannot authorize.
- [ ] Resume text containing "APPROVE" cannot authorize.
- [ ] A forged commissioner_input cannot authorize.
- [ ] Resume without a committed authoritative decision cannot approve.
- [ ] Stale package decision fails closed.
- [ ] Wrong version fails closed.
- [ ] Wrong hash fails closed.
- [ ] Valid committed approval resumes to APPROVED, NOT DISPATCHED.
- [ ] Valid committed rejection resumes to REJECTED, NO ACTION TAKEN.
- [ ] Repeated resume after terminal completion is safe/idempotent.
- [ ] A Graph-resume failure after decision commit preserves the authoritative
      decision.
- [ ] Reconciliation can safely complete a committed decision after restart.
- [ ] No Graph transition dispatches or delivers a package.

### 11.3 Graph API acceptance

- [ ] POST /v1/runs validates its input.
- [ ] POST /v1/runs cannot directly set approval state.
- [ ] Run creation has the designed duplicate/idempotency protection.
- [ ] GET /v1/runs/{thread_id} returns a safe run projection.
- [ ] GET /v1/runs/{thread_id}/approval-request returns the correct request
      and exact package binding.
- [ ] Decision endpoint validates exact package/request/version/hash.
- [ ] Decision transaction commits before Graph resume.
- [ ] Identical decision retries are safe.
- [ ] Conflicting terminal decisions fail.
- [ ] POST /v1/runs/{thread_id}/resume cannot carry authorization.
- [ ] Graph resume rereads PostgreSQL rather than trusting request input.
- [ ] GET /health/live reports process liveness only.
- [ ] GET /health/ready validates required Phase 1 dependencies/configuration.
- [ ] Readiness fails when required safety posture is invalid.
- [ ] API errors do not expose database passwords, API keys, tokens, or other
      secrets.
- [ ] Graph API uses the approved Phase 1 restrictive service-access model.
- [ ] Phase 2 identity/WebAuthn/session functionality is not silently
      implemented inside the Graph API.

### 11.4 Deployment and configuration acceptance

- [ ] graph-api exists as a persistent service.
- [ ] graph-api uses the intended least-privileged database connection.
- [ ] graph-api does not perform privileged schema setup during normal startup.
- [ ] Migration/bootstrap identity remains separate from runtime identity.
- [ ] POSTGRES_URI naming/database targeting is consistent.
- [ ] FC27_MCP_URL naming is consistent.
- [ ] GRAPH_URL targets the actual graph-api service.
- [ ] Existing Graph callers use the supported versioned endpoint.
- [ ] DISPATCH_ENABLED=false is present and enforced.
- [ ] Unsafe production configuration causes startup/readiness failure where
      required by the approved Phase 1 design.
- [ ] No production dispatch worker/relay is active.
- [ ] No secrets are committed into repository configuration.

### 11.5 Integration acceptance

- [ ] Focused Phase 1 database tests pass.
- [ ] Focused approval-security tests pass.
- [ ] Focused LangGraph-flow tests pass.
- [ ] Focused Graph API tests pass.
- [ ] Fresh-database integration tests pass.
- [ ] Restart/recovery tests pass.
- [ ] Entire repository test suite passes from the final integrated state.
- [ ] Compose configuration validation passes.
- [ ] Final working-tree status is reviewed.
- [ ] Final diff contains no accidental Phase 2-6 implementation.
- [ ] No security acceptance test was weakened to obtain a pass.

## 12. Required Phase 1 acceptance trace

Before marking Phase 1 accepted, produce a traceable implementation report
containing:

1. final branch and commit identifiers;
2. complete file-change inventory;
3. migrations introduced or modified;
4. migration execution results;
5. database-role and privilege posture;
6. redacted configuration inventory;
7. Graph node/state inventory;
8. Graph API endpoint inventory;
9. authoritative package lifecycle trace;
10. approval-request lifecycle trace;
11. valid approval trace;
12. valid rejection trace;
13. resume-payload authorization-bypass tests;
14. stale-version negative test;
15. wrong-hash negative test;
16. conflicting-decision negative test;
17. database privilege negative tests;
18. checkpoint restart/recovery evidence;
19. simulated post-commit Graph-resume failure evidence;
20. reconciliation evidence;
21. focused test results;
22. full test-suite result;
23. Compose/configuration validation result;
24. explicit evidence that DISPATCH_ENABLED=false;
25. explicit evidence that no player delivery occurred;
26. known limitations and intentionally deferred requirements;
27. Phase 1 completion statement;
28. detailed Phase 2 planning handoff;
29. preserved Phase 3-6 roadmap and traceability.

Never mark an unchecked criterion as passed.

A partially completed acceptance report means Phase 1 remains incomplete.

## 13. Phase boundary and Phase 2 handoff

Once every applicable Phase 1 acceptance criterion legitimately passes:

STOP IMPLEMENTATION.

Do not continue directly into Phase 2 code.

Produce the detailed Phase 2: Identity and Authorization implementation plan
for review.

The Phase 2 plan must preserve the already-approved programme requirements:

1. local user accounts and roles;
2. invitations;
3. server-side sessions;
4. Commissioner WebAuthn registration and sign-in;
5. Commissioner decision step-up;
6. player passkeys for acknowledgment;
7. action tokens;
8. TOTP protection for applicable staff roles;
9. encrypted identity secrets;
10. BFF service role;
11. Commissioner bootstrap;
12. Commissioner recovery.

Do not implement those capabilities as part of this Phase 1 execution except
for explicitly approved schema compatibility fields or interfaces required to
avoid future destructive migrations.

## 14. Preserve Phases 3 through 6

The Phase 2 planning handoff must retain the remaining programme roadmap.

### Phase 3: Dispatch worker and player-agent pipeline

Preserve requirements for:

- transactional outbox relay;
- worker heartbeat and reconciliation;
- logical per-player agents;
- typed personalized plans;
- deterministic policy validation;
- distinct schema-invalid, policy-invalid, unavailable, valid, and
  material-change outcomes;
- reapproval for valid material changes;
- dashboard-storage delivery adapter;
- WebSocket notification adapter;
- dispatch activation gates;
- stable EA player-ID evidence.

Phase 3 is the earliest phase in which authoritative player dispatch machinery
may be implemented.

### Phase 4: Dashboard UI and real-time delivery

Preserve requirements for:

- separate FastAPI BFF;
- Jinja2/HTMX interface;
- no dashboard database access;
- Commissioner approval inbox;
- dispatch center;
- player workspace;
- passkey-based acknowledgment;
- operational overview;
- authorized event projection;
- authenticated WebSocket updates;
- opaque event cursors and dropped-connection recovery;
- responsive/accessibility requirements;
- browser-security protections;
- restricted Redis access or Graph API event fallback.

### Phase 5: Tracking and analytics

Preserve requirements for:

- baseline initialization after acknowledgment;
- observation ingestion;
- deterministic metrics;
- observation windows;
- evidence-backed assessments;
- tracking states/outcomes;
- player/team analytics;
- trend display only when supported by sufficient data;
- coach/analyst feedback integration;
- deterministic measurements remaining authoritative over model output.

### Phase 6: Operations, audit, and rollout

Preserve requirements for:

- player-agent operational view;
- LangGraph thread/checkpoint inspector;
- authoritative-source-labelled audit explorer;
- authorized/audited exports;
- health and redacted-configuration views;
- Langfuse dispatch spans;
- TLS edge for applicable network access;
- Compose isolation validation;
- end-to-end acceptance;
- rollout documentation.

## 15. Final programme guard

Do not call the player-dispatch programme complete when Phase 1 passes.

At the Phase 1 boundary, report exactly:

"Secure data foundation complete. Dispatch remains disabled.
No player delivery has occurred."

The overall programme is complete only when the applicable requirements and
acceptance criteria for Phases 2 through 6 have also been implemented,
reviewed, tested, integrated, and accepted.

## 16. Execution discipline

Throughout execution:

- preserve completed passing work unless change is required;
- make scoped commits aligned with the approved work breakdown;
- run targeted tests after each logical unit;
- run the complete suite before Phase 1 acceptance;
- do not hide failures;
- do not weaken safeguards to make tests pass;
- do not fabricate external data or acceptance evidence;
- do not place secrets in logs, reports, commits, fixtures, or screenshots;
- redact credentials from errors and acceptance artifacts;
- do not assume a Graph checkpoint is authoritative approval;
- do not assume Redis delivery equals durable state;
- do not enable dispatch;
- do not begin the next phase automatically.

Where implementation choices are not dictated by the approved plan, prefer the
smallest design that satisfies the Phase 1 invariants and preserves clean
interfaces for subsequent phases.

## 17. Completion output required from the team

The final team response must clearly separate:

### Implemented
List only work actually present in the integrated branch.

### Tests executed
List the actual commands/suites executed and their results.

### Acceptance criteria
Report each applicable criterion as PASS, FAIL, or NOT APPLICABLE.

### Security invariants
Explicitly report whether:
- authoritative approval is PostgreSQL-backed;
- Graph resume cannot authorize;
- one interrupt remains;
- package version/hash binding is enforced;
- runtime DB role remains least-privileged;
- DISPATCH_ENABLED=false;
- no player delivery occurred.

### Known issues
List unresolved defects without minimizing them.

### Phase 1 status
Use either:

"Phase 1 accepted. Secure data foundation complete. Dispatch remains disabled.
No player delivery has occurred."

or:

"Phase 1 not yet accepted."

### Next deliverable
If and only if Phase 1 is accepted:

"Detailed Phase 2 Identity and Authorization implementation plan for review."

Do not begin Phase 2 implementation.


## 2026-10-03T14:11:05Z

**Task**: /teamwork-preview /boost a { text-decoration: none; color: #464feb; } tr th, tr td { border: 1px solid #e6e6e6; } tr th { background-color: #f5f5f5; } Yes. The two concepts should be merged into one coherent policy: player consent for player-scoped actions, plus scope-limited fallback authority when the primary approver is unavailable. This resolves the current single-approver dependency without turning player agents into autonomous approvers or allowing players to authorize club-wide actions.
The existing materials currently assign sole activation authority to the Commissioner, describe the Commissioner as the only source of authorized instructions, and hard-code the Graph around Commissioner input. [README_v27.txt](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/README_v27.txt?EntityRepresentationId=28275b9c-ff06-4491-ba8c-88519690bc14) and [swarms_agents.txt](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/swarms_agents.txt?EntityRepresentationId=be8e9134-eda1-429c-bc58-77ae95100a4d) also describe all actions as requiring Commissioner approval. [\[agency_org.txt | Txt\]](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/agency_org.txt?EntityRepresentationId=014b26ed-f8e4-4e6b-b90d-d5a79d237f21), [\[graph.txt | Txt\]](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/graph.txt?EntityRepresentationId=dd258a90-1d12-439c-b7b4-132a3e252fde), [\[README_v27.txt | Txt\]](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/README_v27.txt?EntityRepresentationId=28275b9c-ff06-4491-ba8c-88519690bc14), [\[swarms_agents.txt | Txt\]](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/swarms_agents.txt?EntityRepresentationId=be8e9134-eda1-429c-bc58-77ae95100a4d)
Patch for the previous Phase 1 prompt
Insert this section after “1. Non-negotiable Phase 1 invariants” and before the agent assignments.
## 1A. Human authority, player consent, and approval continuity
The programme must support both:
1. player consent for player-scoped assignments; and
2. operational continuity when the primary club approver is unavailable.
These capabilities must not collapse club authorization, delegated authority,
player consent, and player fallback into one ambiguous approval state.
### 1A.1 Core authority model
Use the following authority model:
1. Club authorization
The Commissioner remains the primary authority for club-level decisions.
2. Delegated club authorization
An explicitly designated human deputy may authorize only the club-level
actions covered by an active, unexpired, and unrevoked delegation.
3. Player consent
An affected player may accept, reject, pause, acknowledge, or request
changes to a player-specific assignment addressed to that player.
4. Affected-player fallback
When the configured escalation policy has been satisfied and no eligible
club approver has acted, the affected player may become eligible to
authorize only their own eligible player-specific assignment.
5. No authorized fallback
If neither a primary approver, authorized delegate, nor eligible affected
player may decide the item, the request remains held.
Fallback authority does not mean automatic approval.
No absence, inactivity period, timeout, unread notification, offline status,
delivery failure, or lack of response may itself create an approval.
Inactivity may only change which authenticated human actor becomes eligible to
receive and decide the request.
### 1A.2 Human decision requirement
No agent may approve or reject for a human.
This applies to:
- club agents;
- coach agents;
- Commissioner-facing agents;
- player agents;
- Graph nodes;
- language models;
- background workers;
- Redis subscribers;
- WebSocket clients.
A player agent may:
- present an authorized assignment;
- explain the assignment;
- summarize supporting evidence;
- compare package versions;
- identify material changes;
- explain available decision options;
- collect player-authored feedback;
- transmit an authenticated player decision through the authorized
server-side path.
A player agent must not:
- choose a decision for the player;
- infer consent from prior behavior;
- auto-submit acceptance;
- describe a choice as already selected;
- use silence as approval;
- alter the assignment before decision submission;
- submit a decision without authenticated player action.
### 1A.3 Decision terminology
Use distinct terms throughout the implementation and documentation:
- Commissioner authorization:
club-level authority exercised by the Commissioner;
- delegated authorization:
club-level authority exercised by an explicitly authorized human delegate;
- player acceptance:
affirmative agreement to an exact player-specific assignment;
- affected-player fallback acceptance:
player acceptance that also satisfies an explicitly configured fallback
requirement for that player's eligible assignment;
- player rejection:
refusal of the player-specific assignment;
- player change request:
request to revise the assignment without activating it;
- player pause:
temporary prevention of activation;
- player acknowledgment:
confirmation of receipt or review of informational content.
Do not treat acknowledgment as acceptance.
Do not represent every outcome with a single approved boolean.
### 1A.4 Decision rights
The Commissioner retains primary authority over:
- club-wide strategy;
- team tactics and formations;
- programme release;
- club dispatch activation;
- policy exceptions;
- roster decisions;
- recruitment and transfer actions;
- fair-play and disciplinary decisions;
- packages affecting multiple players as one club action;
- identity, access-control, and delegation-policy changes.
An explicitly authorized human delegate may decide only the action types and
scopes included in that delegation.
A player may decide only an assignment that:
- is addressed to that player;
- has that player as the sole affected subject;
- is within the configured player-consent policy;
- is player-specific rather than club-wide;
- has passed deterministic schema and policy validation;
- matches the current assignment and package version/hash;
- does not include a prohibited club-level action.
### 1A.5 Actions eligible for affected-player fallback
The future authority policy may permit affected-player fallback for:
- personal training assignments;
- personal build or AP recommendations;
- personal drill schedules;
- personal Mastery or Amp recommendations;
- other explicitly classified player-only developmental assignments.
Eligibility must be explicit in policy.
Do not infer player-fallback eligibility merely because a player is named in
the package.
### 1A.6 Actions prohibited from affected-player fallback
A player must not use fallback authority to approve:
- another player's assignment;
- a club-wide package;
- team tactics or formations;
- transfers or recruitment;
- roster changes;
- disciplinary or fair-play determinations;
- policy exceptions;
- identity or access-control changes;
- delegation-policy changes;
- global dispatch activation;
- a package affecting multiple players;
- a mixed player-and-club package;
- a schema-invalid or policy-invalid package;
- a superseded package;
- a materially changed package that has not been reauthorized.
Those actions require an eligible club-level human authority or remain:
held_no_authorized_approver
### 1A.7 No mixed-scope fallback
A package containing player-specific actions and club-level actions must not be
authorized through affected-player fallback.
Split mixed-scope content into independently versioned packages or assignments
with distinct:
- package IDs;
- assignment IDs;
- versions;
- hashes;
- approval requests;
- authority policies;
- decision lifecycles.
### 1A.8 Decision authority sources
An authoritative decision must identify its authority basis.
Supported conceptual authority bases are:
- primary;
- explicit_delegate;
- affected_player_consent;
- affected_player_fallback.
The authority basis must be validated against:
- actor identity;
- actor type;
- authority role;
- package type;
- decision scope;
- delegation, if applicable;
- subject player;
- assignment ownership;
- escalation state;
- package ID;
- assignment ID;
- version;
- hash;
- request validity;
- authentication evidence.
Graph state, request bodies, resume payloads, model output, agent output,
Redis messages, WebSocket events, online status, and notification status are
not authority evidence.
### 1A.9 Authoritative player decision
A player decision is authoritative only when it:
- comes from the authenticated player identity;
- is submitted through an authorized server-side path;
- applies to an assignment owned by that player;
- is bound to the exact package ID;
- is bound to the exact assignment ID;
- is bound to the exact version and hash;
- is bound to an applicable action token or decision challenge;
- includes required passkey or other approved authentication evidence;
- is committed to PostgreSQL;
- is recorded in authoritative audit/domain-event history.
Phase 1 prepares the foundation for this model but does not activate player
identity, player authentication, player-decision endpoints, or player
fallback.
### 1A.10 Escalation semantics
The future escalation lifecycle must follow this conceptual sequence:
approval request created
→ primary authority notified
→ reminders or policy-defined escalation
→ fallback eligibility evaluated
→ eligible human actor authenticates
→ exact actor authority and decision scope validated
→ decision committed to PostgreSQL
→ Graph resumes and rereads the committed decision
The escalation lifecycle must not transition directly from timeout to
approved, accepted, active, dispatched, or delivered.
For Phase 1, escalation state may be persisted and tested, but no production
timer-driven fallback or player decision path may be activated.
### 1A.11 Decision outcomes
The data model must be capable of distinguishing future outcomes such as:
- approved_by_primary;
- approved_by_delegate;
- accepted_by_player;
- accepted_by_affected_player_fallback;
- rejected_by_primary;
- rejected_by_delegate;
- rejected_by_player;
- changes_requested_by_player;
- paused_by_player;
- acknowledged_by_player;
- held_no_authorized_approver;
- expired;
- invalidated;
- superseded;
- conflict_requires_resolution.
These states do not all need to become active Phase 1 Graph states.
Phase 1 Graph terminal states remain:
- APPROVED, NOT DISPATCHED;
- REJECTED, NO ACTION TAKEN;
- failed or held, where no eligible authoritative decision exists.
### 1A.12 Late and conflicting decisions
A late decision must not silently overwrite a committed decision.
The data foundation must support:
- immutable prior decision history;
- separate recording of the late decision attempt or event;
- decision precedence evaluation;
- conflict state;
- explicit reconciliation;
- invalidation without deletion;
- replacement through an explicit transition or new package version.
An unresolved authoritative conflict must prevent future dispatch or
activation.
### 1A.13 Material changes
If an accepted or authorized package changes materially:
1. create a new immutable package or assignment version;
2. compute a new canonical hash;
3. supersede the prior version without deleting it;
4. invalidate the prior authorization for future activation where required;
5. obtain required club reauthorization;
6. present the revised assignment to the affected player;
7. require a new player decision where player consent is required.
Prior decisions remain immutable audit evidence.
`
Patch for Agent A: Data model and migrations
Add this subsection under Agent A, after the existing approval-decision requirements.
### Human authority and player-decision compatibility
Phase 1 must prepare the authoritative data model for:
- Commissioner authorization;
- delegated human authorization;
- ordinary player consent;
- affected-player fallback;
- escalation;
- authority conflicts;
- later player-agent interaction.
Do not assume every future decision actor is the Commissioner.
Do not store club authorization and player consent as one ambiguous approval
record where that would hide their distinct meanings.
Recommended conceptual separation:
- approval_requests:
club-level authorization requests;
- approval_decisions:
Commissioner or human-delegate decisions;
- authority_policies:
allowed primary, delegated, and fallback authority by action scope;
- authority_delegations:
explicit, time-bounded, revocable human delegation records;
- player_assignments:
one versioned package assignment per affected player;
- player_decision_requests:
exact player decision challenge for one assignment;
- player_decisions:
accepted, rejected, changes_requested, paused, or acknowledged decisions;
- approval_escalations:
auditable request escalation and fallback-eligibility history;
- authority_conflicts:
unresolved late or conflicting authoritative decisions.
Phase 1 may implement these as separate tables or as an equally clear
normalized design approved during repository inspection.
The result must preserve distinct authority and lifecycle semantics.
### Required compatibility fields
The authoritative model must be capable of representing:
- actor_type;
- actor_id;
- authority_role;
- authority_basis;
- delegation_id, nullable;
- decision_scope;
- subject_player_id, nullable;
- package_id;
- package_version;
- package_hash;
- assignment_id, nullable;
- assignment_version, nullable;
- assignment_hash, nullable;
- decision;
- authentication_evidence_id, nullable for Phase 1 bootstrap;
- action_token_reference, nullable;
- escalation_state;
- fallback_eligibility_state;
- idempotency_key;
- decided_at;
- invalidated_at;
- superseded_by;
- conflict_state.
Recommended authority_basis values:
- primary;
- explicit_delegate;
- affected_player_consent;
- affected_player_fallback.
Recommended request or escalation states include:
- awaiting_primary;
- primary_reminder_due;
- evaluating_fallback;
- awaiting_delegate;
- awaiting_affected_player;
- held_no_authorized_approver;
- terminal;
- expired;
- invalidated;
- superseded;
- conflict_requires_resolution.
Phase 1 must not activate time-based escalation, player fallback endpoints,
delegate sign-in, player passkeys, player sessions, or player dispatch.
DISPATCH_ENABLED remains false.
### Phase 1 authority constraints
During Phase 1:
- only the approved Phase 1 bootstrap actor may submit an active decision;
- no player identity is treated as production-authenticated;
- no player decision is treated as production consent;
- no affected-player fallback decision may activate a package;
- no delegate decision may be accepted unless delegation support is explicitly
included and approved within Phase 1 scope;
- synthetic player or delegate records may be used only for schema and
negative-test coverage;
- synthetic authority evidence must never activate dispatch;
- authority-policy records must fail closed when no matching policy exists.
### Referential and integrity requirements
Enforce or test that:
- a player decision cannot reference a nonexistent assignment;
- an assignment must identify exactly one subject player for player fallback;
- a player decision actor must match the assignment owner;
- a club authorization cannot be stored as player acceptance;
- player acceptance cannot be stored as Commissioner authorization;
- an affected-player fallback decision cannot reference a multi-player package;
- a decision cannot change its authority basis after commit;
- an expired or revoked delegation cannot authorize a new decision;
- a delegation cannot authorize an action outside its declared scope;
- escalation state cannot itself create a decision;
- a timeout cannot write a terminal approval;
- a superseded version cannot receive a valid new decision;
- conflicting terminal decisions cannot silently overwrite one another;
- prior decisions remain immutable.
Patch for Agent B: Graph core
Add this after the existing Resume semantics section.
### Actor-neutral authoritative decision validation
Do not hard-code the resumed Graph to require a field named:
commissioner_input
The interruption may describe the applicable primary authority, but the
resumed Graph must seek an authoritative eligible decision rather than trusting
a particular input field or actor claim.
Conceptual flow:
persist package
→ create scoped approval request
→ persist applicable authority-policy reference
→ interrupt awaiting authoritative decision
→ resume as a wake-up signal
→ reread PostgreSQL decision
→ validate actor identity
→ validate actor type
→ validate authority basis
→ validate decision scope
→ validate delegation if applicable
→ validate subject-player ownership if applicable
→ validate package/assignment version and hash
→ enter approved, rejected, or held terminal Phase 1 state
The Phase 1 Graph does not need to execute delegate or player-fallback
authorization.
It must, however:
- avoid schema assumptions that only a Commissioner can ever be an actor;
- preserve the authority basis on all authoritative decisions;
- reject unsupported authority bases in Phase 1;
- distinguish unsupported future authority from malformed authority;
- fail closed when no eligible authoritative decision exists;
- never treat request inactivity as authorization.
### Phase 1 authority-policy validation
For a Phase 1 decision, validate:
- decision actor uses the approved Phase 1 bootstrap authority;
- actor type is permitted by the active Phase 1 policy;
- authority basis is permitted by the active Phase 1 policy;
- decision scope matches the package scope;
- package ID matches;
- package version matches;
- package hash matches;
- request is current and valid;
- decision is not invalidated;
- no unresolved conflict exists.
If any check fails:
- do not approve;
- do not dispatch;
- record the failure;
- remain held or transition to a safe failure state.
### No timeout authorization
Neither the Graph nor a scheduler may implement logic equivalent to:
if approval_request_timed_out:
approved = True
A timeout or escalation event may only update the request or escalation state.
It must not create an approval decision.
Patch for Agent C: Graph API
Add this after the decision endpoint requirements.
### Future actor-aware decision API compatibility
The Phase 1 decision API must not assume that all future decisions will be
submitted by the Commissioner.
Its internal service contracts and persistence boundaries must support future:
- primary club decisions;
- delegated human decisions;
- player decisions;
- affected-player fallback decisions.
Phase 1 must accept only the actor and authority basis permitted by the Phase 1
bootstrap policy.
Do not activate player or delegate decision endpoints in Phase 1.
The decision transaction must validate:
- authenticated Phase 1 actor;
- actor type;
- authority basis;
- decision scope;
- current approval request;
- exact package ID/version/hash;
- absence of an unresolved authority conflict;
- idempotency.
The endpoint must reject:
- actor_type=player in Phase 1 production mode;
- authority_basis=affected_player_fallback in Phase 1 production mode;
- unsupported delegated authority;
- timeout-generated approval;
- scope inferred from untrusted request input;
- a decision for a superseded package;
- conflicting terminal decisions.
### Future endpoints reserved for later phases
Reserve these conceptual interfaces in the roadmap but do not implement them
as active Phase 1 endpoints:
- player assignment retrieval;
- player decision challenge creation;
- player decision submission;
- delegated authority administration;
- approval escalation administration;
- authority conflict resolution.
Phase 1 documentation may describe these future interfaces without exposing
working production routes.
Patch for Agent E: Tests
Add the following subsections.
### Authority-separation tests
- [ ] Schema distinguishes club authorization from player consent.
- [ ] Decision records identify actor type and actor identity.
- [ ] Decision records identify authority basis.
- [ ] Decision records identify decision scope.
- [ ] Commissioner authorization cannot be recorded as player acceptance.
- [ ] Player acceptance cannot be recorded as Commissioner authorization.
- [ ] A player decision can bind to an exact assignment/version/hash.
- [ ] An assignment intended for player fallback has exactly one subject player.
- [ ] A multi-player package cannot be marked player-fallback eligible.
- [ ] A mixed-scope package cannot be marked player-fallback eligible.
- [ ] A timeout cannot insert a terminal approval decision.
- [ ] Escalation state cannot substitute for an authoritative decision.
- [ ] Unsupported future authority bases fail closed in Phase 1.
- [ ] No player decision endpoint is active in Phase 1.
- [ ] No synthetic player decision activates dispatch.
### Delegation-foundation tests
- [ ] Delegation records can be scoped by action/package type.
- [ ] Delegation records can be activated, expired, and revoked.
- [ ] An expired delegation is not considered eligible.
- [ ] A revoked delegation is not considered eligible.
- [ ] A delegation cannot authorize outside its declared scope.
- [ ] Delegation identity does not broaden through inference.
- [ ] Missing authority policy results in a safe held or failed state.
- [ ] Delegation records do not enable Phase 1 dispatch.
### Player-fallback foundation tests
- [ ] Future affected-player fallback binds to one subject-player assignment.
- [ ] One player cannot be represented as approving another player's assignment.
- [ ] A player agent is not recorded as the human decision actor.
- [ ] Affected-player fallback cannot authorize club-wide action types.
- [ ] Affected-player fallback cannot authorize transfers, roster actions,
discipline, fair-play determinations, policy exceptions, or access-control
changes.
- [ ] Player acknowledgment is distinct from player acceptance.
- [ ] Player rejection does not activate an assignment.
- [ ] Player pause does not activate an assignment.
- [ ] Player change request does not activate an assignment.
- [ ] Superseding an assignment prevents acceptance of the old version.
- [ ] Material change requires a new version and future new decision.
- [ ] Phase 1 does not perform real player authentication or fallback.
### Late-decision and conflict tests
- [ ] A late decision cannot overwrite an existing terminal decision.
- [ ] A conflicting decision attempt is retained as auditable evidence.
- [ ] An unresolved authority conflict prevents approval completion.
- [ ] Prior decision history remains immutable.
- [ ] Conflict reconciliation does not rewrite prior audit history.
- [ ] Conflict handling produces no dispatch.
Patch for the Phase 1 acceptance criteria
Add this as a new acceptance subsection:
### Human authority and consent foundation acceptance
- [ ] The schema distinguishes club authorization, delegated authorization,
player consent, and affected-player fallback.
- [ ] Authority basis is stored on authoritative decisions.
- [ ] Actor type and actor identity are stored separately.
- [ ] Decision scope is explicit rather than inferred from actor role.
- [ ] Player-specific assignments identify their subject player.
- [ ] Future player decisions bind to exact assignment/package version/hash.
- [ ] Commissioner authorization cannot masquerade as player consent.
- [ ] Player consent cannot masquerade as Commissioner authorization.
- [ ] Player agents cannot be recorded as human decision actors.
- [ ] No inactivity condition automatically approves a request.
- [ ] No timeout automatically approves a request.
- [ ] No escalation-state change creates a terminal approval.
- [ ] Missing authority policy fails closed.
- [ ] Unsupported actor types fail closed in Phase 1.
- [ ] Unsupported authority bases fail closed in Phase 1.
- [ ] Multi-player packages are
Patch for the acceptance trace
Add these items to the required acceptance trace:
30. authority-policy schema inventory;
31. authority-basis field and constraint evidence;
32. club-authorization versus player-consent separation evidence;
33. player-assignment ownership constraint evidence;
34. timeout-cannot-approve negative-test evidence;
35. escalation-state-cannot-approve negative-test evidence;
36. unsupported-player-actor Phase 1 negative-test evidence;
37. unsupported-delegate Phase 1 negative-test evidence;
38. multi-player player-fallback rejection evidence;
39. mixed-scope player-fallback rejection evidence;
40. late-decision conflict evidence;
41. authority-conflict prevents completion evidence;
42. confirmation that no player/delegate/fallback endpoint is active;
43. confirmation that future-phase consent and fallback requirements remain in
the roadmap and traceability matrix.
Patch for documentation corrections
Add this to the documentation section:
Replace absolute Commissioner-only wording such as:
"No agent action executes without explicit Commissioner approval."
with:
"Club-level actions require authorization from the Commissioner or an
explicitly authorized human delegate under the applicable authority policy.
Player-specific assignments require the affected player's authenticated
decision when the consent policy requires it. When the configured fallback
policy permits, the affected player may authorize only their own eligible
player-specific assignment after escalation. No agent may approve for a human,
and inactivity never constitutes approval."
Documentation must distinguish:
- club authorization from player consent;
- delegated authorization from player fallback;
- player acceptance from acknowledgment;
- escalation from authorization;
- timeout from approval;
- agent assistance from human decision-making;
- personal assignment activation from club dispatch activation.
This replaces the current Commissioner-only language in [agency_org.txt](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/agency_org.txt?EntityRepresentationId=014b26ed-f8e4-4e6b-b90d-d5a79d237f21), [README_v27.txt](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/README_v27.txt?EntityRepresentationId=28275b9c-ff06-4491-ba8c-88519690bc14), [graph.txt](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/graph.txt?EntityRepresentationId=dd258a90-1d12-439c-b7b4-132a3e252fde), and [swarms_agents.txt](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/swarms_agents.txt?EntityRepresentationId=be8e9134-eda1-429c-bc58-77ae95100a4d). [\[agency_org.txt | Txt\]](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/agency_org.txt?EntityRepresentationId=014b26ed-f8e4-4e6b-b90d-d5a79d237f21), [\[graph.txt | Txt\]](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/graph.txt?EntityRepresentationId=dd258a90-1d12-439c-b7b4-132a3e252fde), [\[README_v27.txt | Txt\]](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/README_v27.txt?EntityRepresentationId=28275b9c-ff06-4491-ba8c-88519690bc14), [\[swarms_agents.txt | Txt\]](https://olutechsys-my.sharepoint.com/personal/iwadev_olutechsys_onmicrosoft_com/Documents/Microsoft%20Copilot%20Chat%20Files/Copilot%20Notebook%20Uploads/swarms_agents.txt?EntityRepresentationId=be8e9134-eda1-429c-bc58-77ae95100a4d)
Patch for the future-phase preservation section
Although the current implementation prompt must stop after Phase 1, add this compact traceability requirement so the later functionality is not lost:
## Future-phase authority and consent requirements to preserve
The Phase 1 handoff and programme traceability matrix must preserve the
following approved future requirements.
### Phase 2
Preserve:
- Commissioner identity and WebAuthn;
- delegated-human identity and authorization scope;
- delegation creation, expiration, and revocation;
- player local accounts;
- stable player identity binding;
- player passkey registration and sign-in;
- player decision step-up;
- object-scoped authorization;
- player-scoped action tokens;
- assignment-bound decision challenges;
- server-side sessions;
- authentication-evidence records;
- identity recovery that does not permit staff or agents to impersonate a
player decision;
- authority-policy administration;
- approval escalation configuration.
### Phase 3
Preserve:
- logical player agents;
- player-specific versioned assignments;
- player-agent consent pipeline;
- player-agent isolation;
- material-change detection;
- reauthorization and renewed player consent;
- distinction among awaiting club authorization, awaiting player decision,
accepted, rejected, changes requested, paused, acknowledged, held,
superseded, and conflict states;
- affected-player fallback eligibility evaluation;
- dispatch remaining blocked until every applicable authority and consent gate
is satisfied.
### Phase 4
Preserve:
- Commissioner approval workspace;
- delegate approval workspace;
- player decision workspace;
- passkey decision step-up;
- Accept, Reject, Request changes, Pause, and Acknowledge actions;
- no preselected decision;
- clear version and material-change display;
- visible distinction between club authorization and player consent;
- escalation and fallback-status display;
- assignment ownership checks in the BFF;
- immutable decision receipts and history.
### Phase 5
Preserve:
- reporting by authority basis;
- reporting by player-decision outcome;
- measurement of held and escalated requests;
- acknowledgment and acceptance as distinct analytics states;
- no use of analytics to infer consent.
### Phase 6
Preserve:
- authority-policy audit view;
- delegation audit view;
- approval-escalation audit view;
- player-consent audit view;
- conflict and reconciliation audit view;
- authoritative actor and authority-basis exports;
- operational monitoring for held requests without automatic approval;
- rollout validation for delegated and affected-player fallback policy.
Persistent design decision
I will carry this combined design forward in this conversation as:
Club-level actions use Commissioner or explicitly delegated human authorization. Player-specific assignments use authenticated player consent. After policy-defined escalation, an affected player may provide fallback authorization only for their own eligible assignment. Inactivity never constitutes approval, and no agent may decide for a human.


## 2026-10-04T02:30:08Z

# Teamwork Project Prompt: Phase 2 Identity and Authorization

Status: Ready for execution after Phase 1 acceptance
Programme: FC27 Player Dispatch
Phase: 2 of 6
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild
Integrity mode: development
Requested team: Full implementation, security, testing, adversarial-review,
and integration team

## 0. Mission

Implement Phase 2: Identity and Authorization.

Phase 1 established the secure data foundation, authoritative approval
lifecycle, audit/event infrastructure, Graph persistence/API, authority-policy
foundation, delegation compatibility, player-assignment compatibility, and
dispatch safety controls.

Phase 2 establishes authenticated HUMAN and SERVICE identities and the
authorization mechanisms required by Phases 3 through 6.

Phase 2 must implement:

1. local user accounts;
2. roles and object-scoped authorization;
3. invitations and account activation;
4. server-side sessions;
5. Commissioner WebAuthn/passkey registration and sign-in;
6. Commissioner WebAuthn decision step-up;
7. player passkey registration and sign-in;
8. authenticated player decision capability;
9. scoped action tokens and decision challenges;
10. applicable staff TOTP;
11. encrypted identity secrets;
12. BFF service identity;
13. Graph/service identities;
14. explicit human delegation authorization;
15. Commissioner bootstrap;
16. Commissioner recovery;
17. authentication and authorization audit evidence.

Phase 2 MUST NOT activate player dispatch.

Phase 2 MUST NOT implement the Phase 3 dispatch worker, transactional outbox
relay, player-agent execution pipeline, personalized-plan generation/delivery,
or production player delivery.

At Phase 2 completion:

DISPATCH_ENABLED=false

and:

DISPATCH_WORKER_ENABLED=false

must remain enforced.

No player delivery may occur.

## 1. Required completion statement

When Phase 2 legitimately passes all applicable acceptance criteria, use:

"Identity and authorization foundation complete. Human and service identities
are authenticated and scope-authorized. Dispatch remains disabled.
No player delivery has occurred."

Do not call the overall player-dispatch programme complete.

## 2. Preserve Phase 1

Before editing:

1. inspect the final Phase 1 branch and acceptance artifacts;
2. identify the exact Phase 1 baseline commit;
3. run the relevant Phase 1 regression suites;
4. inspect existing migrations;
5. inspect approval, authority-policy, delegation, assignment, nonce, event,
   audit, Graph API, database-role, dispatch-schema, activation-gate, and
   security implementations;
6. preserve Phase 1 invariants unless an explicitly documented Phase 2 change
   requires an extension.

Do not replace working Phase 1 security controls with weaker Phase 2 controls.

No Phase 1 authoritative history may be rewritten.

Phase 2 migrations must extend Phase 1 without destructively recreating
existing authoritative data.

## 3. Non-negotiable invariants

### 3.1 Dispatch safety

Throughout Phase 2:

- DISPATCH_ENABLED=false;
- DISPATCH_WORKER_ENABLED=false;
- no production outbox relay;
- no player delivery adapter;
- no Graph transition to delivered;
- no player-agent execution;
- no acknowledgment may be treated as delivery activation;
- no successful authentication event may enable dispatch;
- no WebAuthn ceremony may enable dispatch;
- no player acceptance may directly enable dispatch;
- no delegation may enable global dispatch;
- no synthetic test gate may authorize production delivery.

Phase 2 builds authorization required by later phases. It does not execute
Phase 3 dispatch.

### 3.2 PostgreSQL authority

PostgreSQL remains authoritative for:

- users;
- roles;
- invitations;
- sessions;
- credentials;
- identity bindings;
- delegations;
- authentication evidence;
- action-token state;
- authoritative human decisions;
- audit/domain events.

LangGraph checkpoints remain workflow state, not identity or authorization
state.

Redis, WebSocket events, agents, LLMs, browser state, cookies without valid
server-side session state, and Graph resume payloads are not authoritative
human identity evidence.

### 3.3 No agent-human impersonation

No agent may authenticate or decide for a human.

This includes:

- player agents;
- Coach agents;
- Commissioner-facing agents;
- model agents;
- LangGraph;
- MCP tools;
- background workers;
- BFF service;
- WebSocket service;
- Redis subscribers.

Agents may assist humans. Agents cannot become the human actor.

### 3.4 Identity is not display name

Gamertags and display names are presentation data.

Do not use a mutable display name as the internal authorization identity.

Internally use immutable local user identifiers.

Keep EA player identity evidence separate from local authentication identity.

If stable EA numeric player identity is still unverified, preserve that state
and do not invent, infer, or substitute a gamertag as the missing identifier.

### 3.5 Inactivity never authorizes

No timeout, absence, inactivity period, unread notification, offline status,
failed delivery, session expiry, lack of response, or escalation event may
automatically create:

- approval;
- delegation;
- player acceptance;
- player acknowledgment;
- dispatch;
- activation.

Escalation changes which authenticated human MAY become eligible to act.

It does not create the decision.

## 4. Human authority model

Phase 2 must implement identity and authorization compatible with four distinct
authority bases:

1. primary;
2. explicit_delegate;
3. affected_player_consent;
4. affected_player_fallback.

These must remain distinguishable in storage, authorization logic, API
contracts, and audit evidence.

### 4.1 Commissioner

The Commissioner remains primary club-level authority.

Applicable high-value Commissioner actions must require successful
authentication and an appropriate step-up ceremony.

### 4.2 Human delegate

A human delegate may act only if an explicit delegation is:

- active;
- not expired;
- not revoked;
- associated with that authenticated user;
- applicable to the current club;
- applicable to the action type;
- applicable to the decision scope;
- compatible with the requested authority level.

Role membership alone must not magically expand a delegation.

### 4.3 Player consent

An authenticated player may decide only player-specific objects that the
authorization policy allows that player to access.

Player decisions include:

- accepted;
- rejected;
- changes_requested;
- paused;
- acknowledged, when acknowledgment rather than consent is applicable.

accepted != acknowledged

An agent must not transform acknowledgment into acceptance.

### 4.4 Affected-player fallback

Phase 2 must implement the IDENTITY AND AUTHORIZATION prerequisites for
affected-player fallback.

A future fallback decision is eligible only for the affected player's own
eligible player-specific assignment.

Player fallback may never authorize:

- club-wide tactics;
- formations;
- transfers;
- recruitment;
- roster changes;
- fair-play determinations;
- disciplinary decisions;
- policy exceptions;
- another player's assignment;
- multi-player packages;
- mixed-scope packages;
- global dispatch;
- access-control changes;
- identity administration.

Phase 2 may implement the authenticated decision mechanism necessary for this
policy.

Phase 2 must NOT turn such a decision into player delivery.

## 5. Agent A: Identity database and migrations

Implement a new versioned Phase 2 identity migration or migrations following
the repository's established migration policy.

Inspect existing Phase 1 structures first and extend rather than duplicate them.

### 5.1 Users

Implement an authoritative users model supporting:

- immutable user ID;
- club scope;
- normalized login identifier;
- display name;
- account status;
- account creation timestamp;
- activation timestamp;
- suspension state;
- recovery state;
- audit linkage.

Recommended statuses:

invited
active
suspended
recovery_required
disabled

Do not use gamertag as the primary key.

### 5.2 Roles and grants

Implement role assignment separately from user identity.

Minimum conceptual application roles:

- commissioner;
- commissioner_delegate;
- coach;
- analyst;
- staff;
- player;
- bff_service;
- graph_service.

Do not confuse application roles with PostgreSQL roles.

A user's application role must not grant direct database-owner capability.

### 5.3 Invitations

Implement invitations with:

- opaque random token;
- token stored only in irreversible form;
- invited user or destination binding;
- intended role;
- club scope;
- creator identity;
- issued timestamp;
- expiry timestamp;
- single use;
- revocation;
- consumed timestamp.

Expired, revoked, or consumed invitations fail closed.

### 5.4 Sessions

Implement server-side sessions with:

- opaque random session identifier;
- session identifier stored safely;
- user ID;
- authentication level;
- creation time;
- last-used time;
- idle expiry;
- absolute expiry;
- revocation;
- credential/version binding where applicable;
- correlation/audit linkage.

Client state must not be sufficient to reconstruct or forge authorization.

### 5.5 WebAuthn credentials

Persist WebAuthn credential records with appropriate:

- credential identifier;
- user binding;
- public-key material;
- sign-counter or applicable authenticator state;
- credential creation;
- last use;
- revocation;
- credential label;
- authentication evidence/audit information.

Never store private authenticator key material supplied by the authenticator.

### 5.6 Player identity binding

Represent separately:

local authenticated player account
        |
        v
local player record
        |
        v
EA player identity evidence

Do not falsify this relationship if EA stable numeric identity remains
unverified.

No gamertag fallback.

### 5.7 TOTP credential state

For the staff roles where policy requires TOTP, support:

- encrypted seed;
- enrollment state;
- verification state;
- revocation;
- recovery/re-enrollment metadata;
- audit evidence.

Do not log raw seeds.

### 5.8 Recovery material

Recovery codes, if used, must be:

- high entropy;
- stored irreversibly;
- one-time use;
- individually revocable/consumable;
- auditable without logging the secret.

### 5.9 Identity audit

Authentication and identity administration must generate authoritative,
append-only audit/domain events.

At minimum cover:

- invitation issued;
- invitation revoked;
- invitation consumed;
- account activated;
- role granted;
- role revoked;
- session created;
- session rotated;
- session revoked;
- session expired;
- passkey registered;
- passkey revoked;
- sign-in success;
- sign-in failure;
- step-up success;
- step-up failure;
- delegation granted;
- delegation revoked;
- recovery initiated;
- recovery completed.

Never store credentials or recoverable authentication secrets in audit payloads.

## 6. Agent B: Authentication service

Create a reusable authentication module independent of the Phase 4 dashboard.

### 6.1 Sign-in abstraction

Implement server-side authentication primitives that produce an authenticated
principal containing only authoritative identity data.

At minimum:

user_id
club_id
role/scopes
authentication_method
authentication_level
session_id
authentication_time

Do not trust caller-supplied roles or user IDs.

### 6.2 WebAuthn registration

Implement Commissioner and player passkey registration using server-generated
challenges.

Registration challenges must be:

- high entropy;
- time limited;
- single use;
- bound to the intended user;
- bound to the ceremony type;
- invalid after consumption.

### 6.3 WebAuthn authentication

Implement sign-in verification and server-side session creation.

Authentication failure must not expose sensitive credential internals.

### 6.4 Step-up authentication

High-value human decisions must support decision-specific step-up.

The step-up must bind to the authoritative operation being approved.

For a Commissioner decision, include authoritative identifiers such as:

approval_request_id
package_id
package_version
package_hash
decision
actor_id
authority_basis

For a player decision, include:

player_id
assignment_id
assignment_version
assignment_hash
package_id
package_version
package_hash
decision
actor_id
authority_basis

A successful sign-in alone is not equivalent to decision approval.

### 6.5 Replay prevention

A successful decision challenge cannot be reused for another:

- decision;
- package version;
- assignment;
- actor;
- authority basis;
- action scope.

## 7. Agent C: Authorization service

Implement centralized authorization rather than duplicating role checks in
individual routes.

Conceptual API:

authorize(
    principal,
    action,
    resource,
    authority_basis=None
)

Authorization must evaluate both subject and object.

### 7.1 Required checks

Evaluate as applicable:

- authenticated user ID;
- application role;
- club scope;
- action;
- resource type;
- resource owner;
- subject player;
- authority policy;
- delegation;
- authentication level;
- step-up freshness/evidence;
- assignment ownership;
- package scope;
- package/version validity;
- conflict state.

### 7.2 Player isolation

A player must not:

- read another player's private assignment;
- submit another player's decision;
- obtain another player's decision challenge;
- revoke another player's credential;
- access staff-only authorization data.

Test this as object-level authorization, not merely route-role authorization.

### 7.3 Delegation

Activate the explicit human delegation foundation created in Phase 1.

Delegation validation must check:

principal
→ delegation holder
→ active
→ not expired
→ not revoked
→ club scope
→ action scope
→ resource scope
→ required authentication strength

A delegation cannot be broadened through interpretation.

No matching authorization means deny.

## 8. Agent D: Player consent authentication

Implement player-authenticated decision capability without activating delivery.

### 8.1 Player decision challenge

The server must issue a single-use decision challenge bound to the exact
player-specific assignment.

The player must be authenticated and authorized for that assignment first.

### 8.2 Player decisions

Implement:

accepted
rejected
changes_requested
paused
acknowledged

No default choice.

No preselection.

No automatic conversion of:

acknowledged -> accepted

or:

timeout -> accepted

### 8.3 Player agent boundary

Do not give logical player agents credentials that allow them to impersonate
the player.

Player-agent code may eventually call safe read interfaces, but the player
must personally satisfy the authentication ceremony required to commit a human
decision.

## 9. Agent E: Commissioner bootstrap and recovery

### 9.1 Bootstrap

Implement a one-time bootstrap mechanism for establishing the first
Commissioner identity.

Requirements:

- inaccessible once bootstrap completes;
- cannot silently replace an existing Commissioner;
- audited;
- must establish a strong authentication credential;
- bootstrap secrets must not remain reusable.

### 9.2 Recovery

Build Commissioner recovery so it restores account access without allowing an
attacker, agent, service, coach, or administrator to forge an existing
Commissioner decision.

Recovery must not rewrite prior approval audit records.

Recovery must not recover old WebAuthn private keys.

Recovery should result in issuance/registration of new credential material and
an auditable identity transition.

High-risk decisions after recovery may require renewed step-up according to the
policy implemented by the team.

## 10. Agent F: Staff TOTP

Implement TOTP only for applicable approved staff roles.

TOTP is not a universal substitute for WebAuthn.

Commissioner high-value decision authorization should use the required
Commissioner step-up mechanism.

Tests must cover:

- valid TOTP;
- invalid TOTP;
- expired/out-of-window value;
- replay where applicable;
- revoked enrollment;
- recovery/re-enrollment;
- rate limiting;
- no logging of seeds or current codes.

## 11. Agent G: BFF and internal service identity

Create the authentication/authorization foundation required by the future
Phase 4 BFF.

Do not build the Phase 4 dashboard now.

The BFF service identity must have:

- unique service identity;
- narrow API scope;
- separate credential;
- credential rotation path;
- audit identity;
- no authority to impersonate users;
- no authority to submit human decisions without authoritative human
  authentication evidence.

The BFF must have no direct PostgreSQL access in the final architecture.

Where Phase 2 introduces the identity now, maintain that trust boundary.

Similarly maintain separate identities for:

- Graph API/service;
- BFF;
- future dispatch worker;
- operational tooling.

Do not reuse one omnipotent shared service credential.

## 12. Agent H: Secrets and cryptographic material

Centralize identity-secret handling.

Requirements:

- no secrets committed to Git;
- no secrets emitted through logs;
- no secrets returned by APIs;
- no secrets included in exception text;
- encrypted-at-rest storage for recoverable sensitive values;
- irreversible hashing for tokens/code values that do not need recovery;
- explicit key/secret versioning;
- rotation support;
- fail-closed startup when required key material is unavailable.

Do not print `.env` contents as a test.

## 13. API surface

Implement a versioned authentication API.

Exact route organization may be adapted to the existing repository style, but
the following conceptual capabilities are required:

### Invitations

POST   /v1/auth/invitations
DELETE /v1/auth/invitations/{id}
POST   /v1/auth/invitations/{token}/accept

### Sessions

GET    /v1/auth/session
POST   /v1/auth/logout
DELETE /v1/auth/sessions/{id}

### WebAuthn

POST /v1/auth/passkeys/register/options
POST /v1/auth/passkeys/register/verify
POST /v1/auth/passkeys/login/options
POST /v1/auth/passkeys/login/verify
GET  /v1/auth/passkeys
DELETE /v1/auth/passkeys/{credential_id}

### Decision step-up

POST /v1/auth/step-up/options
POST /v1/auth/step-up/verify

### Player decisions

POST /v1/player-assignments/{assignment_id}/decision-challenge
POST /v1/player-assignments/{assignment_id}/decision

### Delegations

Create/read/revoke only through authorized human administrative paths.

### Recovery

Expose only the minimal endpoints needed by the approved recovery design.

All mutations require server-side authorization.

Do not expose database-driven administration directly to browser clients.

## 14. Existing Graph API integration

Migrate the Phase 1 decision boundary from bootstrap Commissioner proof toward
Phase 2 authenticated-human evidence.

Preserve this invariant:

decision authentication succeeds
→ authorization succeeds
→ authoritative decision commits
→ audit event commits
→ transaction commits
→ Graph resumes
→ Graph rereads PostgreSQL decision

Never:

WebAuthn response
→ resume Graph with "approved=true"

Graph resume remains workflow control only.

It never becomes authorization.

The Phase 1 CLI/HMAC mechanism may be retained temporarily only if the reviewed
migration design explicitly requires it.

If retained:

- isolate it;
- keep its kill switch;
- document its status;
- do not silently treat it as equivalent to the final browser authentication
  path.

## 15. Phase 2 security tests

Build a dedicated adversarial test suite.

### 15.1 User isolation

- [ ] Player A cannot read Player B's assignments.
- [ ] Player A cannot request a challenge for Player B.
- [ ] Player A cannot submit a decision for Player B.
- [ ] Player role cannot access Commissioner endpoints.
- [ ] Staff role cannot impersonate a player.

### 15.2 WebAuthn

- [ ] Registration challenge is single use.
- [ ] Authentication challenge is single use.
- [ ] Expired challenge fails.
- [ ] Foreign-user challenge fails.
- [ ] Wrong origin/RP validation fails according to implementation policy.
- [ ] Credential for another user cannot authenticate current user.
- [ ] Revoked credential fails.
- [ ] Replayed authentication evidence fails where replay protection applies.
- [ ] Decision step-up is bound to the exact target decision.

### 15.3 Invitations

- [ ] Invitation replay fails.
- [ ] Expired invitation fails.
- [ ] Revoked invitation fails.
- [ ] Invitation cannot escalate its intended role.
- [ ] Invitation cannot switch clubs.
- [ ] User input cannot select a stronger role than the invitation grants.

### 15.4 Sessions

- [ ] Unknown session fails.
- [ ] Revoked session fails.
- [ ] Expired session fails.
- [ ] Session rotation invalidates the appropriate prior state.
- [ ] Suspended user session fails authorization.
- [ ] Logout invalidates server-side session.
- [ ] Browser mutation protection is tested.

### 15.5 Delegation

- [ ] Expired delegation fails.
- [ ] Revoked delegation fails.
- [ ] Delegate cannot exceed action scope.
- [ ] Delegate cannot exceed club scope.
- [ ] Delegate cannot expand their own authority.
- [ ] Delegate cannot create unauthorized recursive delegation.
- [ ] Absence of delegation fails closed.

### 15.6 Player decisions

- [ ] Player may decide only own assignment.
- [ ] Player acceptance binds exact assignment/version/hash.
- [ ] Player rejection binds exact assignment/version/hash.
- [ ] Changes requested do not activate assignment.
- [ ] Pause does not activate assignment.
- [ ] Acknowledgment is not acceptance.
- [ ] Superseded version cannot be accepted.
- [ ] Materially changed package requires new future consent.
- [ ] Player agent cannot submit the decision for the player.
- [ ] BFF cannot manufacture player acceptance.
- [ ] Commissioner cannot be recorded as player acceptance.
- [ ] Player cannot be recorded as Commissioner authorization.

### 15.7 Affected-player fallback foundation

- [ ] No timeout creates fallback acceptance.
- [ ] No inactivity creates fallback acceptance.
- [ ] Multi-player package cannot use affected-player fallback.
- [ ] Mixed-scope package cannot use affected-player fallback.
- [ ] Player cannot fallback-authorize club-wide action.
- [ ] Player cannot fallback-authorize another player's assignment.
- [ ] Unsupported fallback policy fails closed.

### 15.8 TOTP

- [ ] Invalid TOTP fails.
- [ ] Revoked TOTP enrollment fails.
- [ ] Rate limiting works.
- [ ] Secret is never returned after enrollment.
- [ ] Logs contain no usable TOTP secret.

### 15.9 Recovery

- [ ] Recovery cannot rewrite old decisions.
- [ ] Recovery cannot reactivate revoked credentials.
- [ ] Used recovery code fails.
- [ ] Invalid recovery attempt fails.
- [ ] Recovery event is audited.
- [ ] Recovery cannot activate dispatch.

### 15.10 Service boundaries

- [ ] BFF service cannot impersonate Commissioner.
- [ ] Graph service cannot impersonate human.
- [ ] Player agent cannot impersonate player.
- [ ] Internal service credentials are scope-limited.
- [ ] No Phase 2 service can enable production dispatch.

## 16. Phase 1 regression gate

The complete accepted Phase 1 regression suite must remain passing.

Specifically prove that Phase 2 does not break:

- PostgreSQL-authoritative approval;
- immutable packages;
- approval request/package version/hash binding;
- single Graph interrupt;
- forged-resume rejection;
- append-only domain/audit history;
- least-privilege database roles;
- activation gates;
- player identity fail-closed behavior;
- no-dispatch terminal states;
- transactional outbox foundation;
- Section 1A timeout-not-approval invariant.

Security failures must be fixed.

Do not delete or weaken Phase 1 tests to make Phase 2 pass.

## 17. Migration and rollback verification

Test:

- fresh Phase 1 -> Phase 2 migration;
- fully fresh database through all migrations;
- migration idempotency as designed;
- migration tamper detection;
- grant policy version update;
- runtime identity cannot perform migration DDL;
- identity tables are owned by the intended database owner;
- runtime APIs retain least privilege;
- failed Phase 2 migration leaves the system recoverable.

## 18. Documentation updates

Update the authoritative programme documentation.

Remove stale Commissioner-only statements that conflict with the accepted
authority model.

Use:

"Club-level actions require authorization from the Commissioner or an
explicitly authorized human delegate under the applicable authority policy.
Player-specific assignments require the affected player's authenticated
decision when the consent policy requires it. After policy-defined escalation,
affected-player fallback may apply only to that player's eligible assignment.
No agent may decide for a human, and inactivity never constitutes approval."

Document clearly that:

Commissioner authorization != player consent
delegated authorization != player fallback
acceptance != acknowledgment
sign-in != decision authorization
step-up != automatic approval
escalation != authorization
timeout != approval
agent assistance != human decision
Phase 2 authentication != dispatch activation

## 19. Programme roadmap

Preserve:

- [x] Phase 1: Secure data foundation
- [ ] Phase 2: Identity and authorization
- [ ] Phase 3: Dispatch worker and player-agent pipeline
- [ ] Phase 4: Dashboard UI and real-time delivery
- [ ] Phase 5: Tracking and analytics
- [ ] Phase 6: Operations, audit, and rollout

Do not collapse later phases into Phase 2.

## 20. Phase 3 preservation requirements

Ensure the Phase 2 handoff preserves Phase 3 requirements for:

- transactional outbox relay;
- dispatch worker;
- worker heartbeat and reconciliation;
- logical per-player agents;
- authenticated player-agent projections;
- typed personalized plans;
- deterministic policy validation;
- schema-invalid outcome;
- policy-invalid outcome;
- unavailable outcome;
- valid outcome;
- material-change outcome;
- reapproval/reconsent on material change;
- dashboard-storage delivery adapter;
- authenticated WebSocket notification adapter;
- stable EA player-ID evidence;
- dispatch activation gates;
- affected-player fallback evaluation;
- no activation until every required authorization and consent gate passes.

Do not implement the production Phase 3 relay or worker in Phase 2.

## 21. Independent review

After implementation, deploy independent reviewers/challengers for:

1. identity schema and cryptographic storage;
2. WebAuthn challenge lifecycle;
3. object-level authorization;
4. player isolation;
5. delegation escalation;
6. session security;
7. decision step-up;
8. account recovery;
9. service impersonation;
10. Phase 1 regression and no-dispatch invariants.

At least one adversarial track must explicitly attempt:

- cross-player access;
- Commissioner impersonation;
- delegate privilege escalation;
- replay;
- session fixation;
- stale challenge reuse;
- BFF-to-human impersonation;
- player-agent-to-player impersonation;
- timeout-to-approval;
- fallback-scope escalation.

Do not accept self-certification by the implementing agent as sufficient.

## 22. Acceptance trace

Produce a Phase 2 acceptance report containing:

1. baseline Phase 1 commit;
2. final Phase 2 commit;
3. file inventory;
4. migration inventory;
5. identity-schema inventory;
6. role/authorization matrix;
7. encrypted/hashed secret inventory by category, with values redacted;
8. Commissioner bootstrap trace;
9. Commissioner WebAuthn registration trace;
10. Commissioner sign-in trace;
11. Commissioner decision-step-up trace;
12. player passkey registration trace;
13. player sign-in trace;
14. own-assignment authorization trace;
15. cross-player denial trace;
16. player-decision trace;
17. delegation success trace;
18. delegation scope denial trace;
19. delegation expiry/revocation evidence;
20. invitation lifecycle evidence;
21. session lifecycle evidence;
22. TOTP evidence;
23. recovery evidence;
24. service-identity boundary evidence;
25. replay-attack evidence;
26. stale challenge evidence;
27. timeout-does-not-approve evidence;
28. Graph-resume-still-cannot-authorize evidence;
29. Phase 1 regression result;
30. focused Phase 2 test results;
31. full repository test result;
32. Compose/configuration validation;
33. no-secret-leak evidence;
34. DISPATCH_ENABLED=false evidence;
35. DISPATCH_WORKER_ENABLED=false evidence;
36. explicit zero-player-delivery evidence;
37. remaining known limitations;
38. Phase 3 handoff;
39. preserved Phases 4-6 roadmap.

Never report a criterion as passing without evidence.

## 23. Phase boundary

When all applicable Phase 2 criteria legitimately pass:

STOP IMPLEMENTATION.

Do not automatically begin Phase 3.

Produce the detailed Phase 3 implementation plan for review.

The Phase 3 plan must cover the dispatch worker, transactional outbox relay,
logical per-player agents, typed personalized plans, deterministic policy
validation, player assignments, material-change reapproval/reconsent,
authenticated delivery adapters, activation gates, reconciliation,
and stable EA player identity evidence.

## 24. Final output required from the team

Report:

### Implemented
Only functionality actually present in the integrated branch.

### Tests executed
Actual commands and actual results.

### Security review
Independent reviewer/challenger findings.

### Identity invariants
Explicit PASS/FAIL for:

- Commissioner WebAuthn;
- decision step-up;
- player passkeys;
- object-level player isolation;
- delegate scoping;
- invitation safety;
- session safety;
- recovery safety;
- player-agent non-impersonation;
- BFF non-impersonation;
- inactivity never approves.

### Phase 1 regression
PASS or FAIL.

### Dispatch safety
Explicitly report:

DISPATCH_ENABLED
DISPATCH_WORKER_ENABLED
production outbox relay active/inactive
player delivery count/evidence

### Phase 2 status

Use only one:

"Phase 2 accepted. Identity and authorization foundation complete.
Human and service identities are authenticated and scope-authorized.
Dispatch remains disabled. No player delivery has occurred."

or:

"Phase 2 not yet accepted."

### Next deliverable

If Phase 2 is accepted:

"Detailed Phase 3 Dispatch Worker and Player-Agent Pipeline plan for review."

Do not begin Phase 3 implementation automatically.
