# EA Player Identity Verification Runbook (AC-47)

**Status: UNVERIFIED (G-ID not met)**  
**Applies to**: FC27 Pro Clubs Platform (`fc_version: "27"`, `platform: "common-gen5"`)  

---

## 1. Objective and Invariants

Activation Gate `G-ID` requires concrete, documented proof that EA Pro Clubs API match payloads key individual players by a stable, persistent numeric identifier (`player_id`) rather than transient gamertags.

### Invariant Rules (Spec §13.1, §29 [A-2], AC-46, AC-47)
1. **Gamertags are NOT identifiers**: Gamertags (`display_name`, `playername`) are untrusted presentation strings (max 64 characters) subject to player renaming. They MUST NEVER be used for identity, routing, authentication, or authorization.
2. **Strict numeric format**: `player_id` must match `^[0-9]{1,20}$`.
3. **No gamertag fallback**: The system will NEVER fall back to gamertags if an identifier is missing or unverified. Candidates without a verified numeric player ID are rejected with HTTP 409 `not_dispatchable`.
4. **Gate G-ID precondition**: If any item in AC-47 remains unproven or incomplete, G-ID remains unmet and dispatch remains disabled across the platform.

---

## 2. AC-47 Evidence Items and Records

Before Gate `G-ID` can be marked as passed, the Commissioner and lead analyst must execute manual verification against live EA API data and record findings in the tables below.

### 2.1 Captured, Redacted `clubs/matches` and `members/stats` Structures

*Requirement: Document raw structure of EA API responses, demonstrating exactly where numeric player IDs are keyed in relation to gamertags and stats.*

| Match ID / Endpoint | API Path | Player ID Field | Key Type | Sample Redacted Value | Notes |
|---|---|---|---|---|---|
| *[TBD]* | `/clubs/matches` -> `match.players[clubId]` | *[TBD]* | *[TBD]* | *[TBD]* | *Pending live EA capture* |
| *[TBD]* | `/members/stats` -> `members[i]` | *[TBD]* | *[TBD]* | *[TBD]* | *Pending live EA capture* |

### 2.2 Same Identifier Across at Least 3 Separate Matches

*Requirement: Prove that the numeric identifier persists invariant for the same individual across at least 3 distinct match events.*

| Player ID | Match 1 ID | Match 2 ID | Match 3 ID | Observed Gamertag | Stability Verified |
|---|---|---|---|---|---|
| *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *Pending capture across 3 matches* |
| *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *Pending capture across 3 matches* |
| *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *Pending capture across 3 matches* |

### 2.3 Unchanged Identifier Across a Gamertag Change

*Requirement: Document concrete evidence showing that when a player updates or changes their EA gamertag, their underlying numeric identifier remains identical.*

| Player ID | Previous Gamertag | New Gamertag | Verification Date | Verification Source / Match | Verified |
|---|---|---|---|---|---|
| *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *[TBD]* | *Pending player rename test* |

### 2.4 Club and Platform Scoping

*Requirement: Prove that player identifiers are properly scoped within the club (`FC27_CLUB_ID`) and platform (`common-gen5`), demonstrating no cross-club or cross-platform collision.*

| Player ID | Club ID | Platform | Scoping Method | Collision Risk Assessment | Verified |
|---|---|---|---|---|---|
| *[TBD]* | `330323` | `common-gen5` | Database composite constraint | Isolated | *Pending verification* |

### 2.5 Handling of Transferred, Removed, Duplicate, and Unavailable Players

*Requirement: Document edge-case behavior when players leave the club roster, appear duplicated across sessions, or are missing from subsequent match logs.*

| Scenario | Expected Behavior | Tested Condition | System Response | Verification Status |
|---|---|---|---|---|
| Transferred player | Removed from active club roster; historical records immutable | *[TBD]* | Mark inactive; refuse new dispatches | *Pending test* |
| Removed player | Historical dispatches preserved; future proposals blocked | *[TBD]* | `active=false` in `players` | *Pending test* |
| Duplicate entry | Reject duplicate insert; fail closed | *[TBD]* | Unique constraint on `player_id` | *Pending test* |
| Unavailable player | Fail candidate check with 409 `not_dispatchable` | *[TBD]* | `identity_verified_at` is NULL | *Pending test* |

---

## 3. Verification Sign-Off Procedure (Phase 3 Entry)

Prior to Phase 3 dispatch worker enablement:
1. Complete all five evidence tables above with empirical, verified data.
2. Commit this document to version control.
3. Obtain human Commissioner approval.
4. Record Gate `G-ID` via `POST /v1/activation-gates/G-ID` referencing the git commit hash containing the completed tables.
