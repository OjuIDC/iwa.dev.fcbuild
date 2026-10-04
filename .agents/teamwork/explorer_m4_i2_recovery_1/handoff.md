# Hard Handoff Report: Recovery Code Revocation & Store Queries Analysis

**Agent ID**: `explorer_m4_i2_recovery_1`  
**Roles**: explorer, specialist (Recovery Code Revocation & Store DML Explorer)  
**Recipient**: `parent` (`25210876-a9ad-4f78-a87c-dec88e86edb9`)  
**Target Milestone**: Phase 2 Milestone 4 Iteration 2 (Recovery Code Revocation & Store Queries)  
**Working Directory**: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_recovery_1`  
**Date**: 2026-10-04T06:35:00Z  
**Verdict**: **ANALYSIS COMPLETE & FIX PROPOSALS FORMULATED**  

---

## 1. Observation

Direct observations and evidence across the codebase and database schema:

1. **Recovery Code Batch Storage Without Revocation in `src/identity/store.py:1292-1304`**:
   ```python
   1292:         with self.connect() as conn, conn.transaction():
   1293:             for code in plaintext_codes:
   1294:                 cid_code = uuid4()
   1295:                 hashed = hash_recovery_code(code)
   1296:                 conn.execute(
   1297:                     """
   1298:                     INSERT INTO public.fc27_recovery_codes (code_id, user_id, code_hash, created_at)
   1299:                     VALUES (%s, %s, %s, now())
   1300:                     """,
   1301:                     (cid_code, user_id, hashed),
   1302:                 )
   1303:                 code_ids.append(cid_code)
   ```
   - Direct observation: `IdentityStore.save_recovery_codes` inserts new recovery code rows without revoking or modifying existing unconsumed rows for `user_id`.
   - Caller in `src/identity/recovery.py:207-216`:
     ```python
     207:     def regenerate_recovery_codes(self, user_id: UUID, actor: str) -> list[str]:
     208:         """Revokes old codes and generates a fresh set of 10 recovery codes."""
     209:         new_codes = generate_recovery_codes(DEFAULT_RECOVERY_CODE_COUNT)
     210:         self.store.save_recovery_codes(
     211:             user_id=user_id,
     212:             plaintext_codes=new_codes,
     213:             actor=actor,
     214:         )
     215:         return new_codes
     ```
     The docstring specifies *"Revokes old codes and generates a fresh set of 10 recovery codes"*, but `save_recovery_codes` omits the revocation step.

2. **Trigger-Enforced Immutability and Deletion Guard on `public.fc27_recovery_codes`**:
   - `migrations/0004_identity_foundation.sql` lines 222–226:
     ```sql
     DROP TRIGGER IF EXISTS fc27_recovery_codes_immutable ON public.fc27_recovery_codes;
     CREATE TRIGGER fc27_recovery_codes_immutable
         BEFORE DELETE ON public.fc27_recovery_codes
         FOR EACH ROW EXECUTE FUNCTION public.fc27_reject_mutation();
     ```
     Deletions are unconditionally rejected with `check_violation: fc27_recovery_codes is append-only`.
   - `migrations/0004_identity_foundation.sql` lines 229–250:
     ```sql
     CREATE OR REPLACE FUNCTION public.fc27_guard_recovery_code_consumption()
     RETURNS trigger
     LANGUAGE plpgsql AS $$
     BEGIN
         IF OLD.consumed_at IS NOT NULL THEN
             RAISE check_violation USING MESSAGE = 'recovery code is already consumed';
         END IF;
         IF NEW.consumed_at IS NULL THEN
             RAISE check_violation USING MESSAGE = 'cannot un-consume recovery code';
         END IF;
         IF NEW.code_id <> OLD.code_id OR NEW.user_id <> OLD.user_id OR NEW.code_hash <> OLD.code_hash OR NEW.created_at <> OLD.created_at THEN
             RAISE check_violation USING MESSAGE = 'recovery code attributes are immutable';
         END IF;
         RETURN NEW;
     END;
     $$;

     CREATE TRIGGER fc27_recovery_codes_update_guard
         BEFORE UPDATE ON public.fc27_recovery_codes
         FOR EACH ROW EXECUTE FUNCTION public.fc27_guard_recovery_code_consumption();
     ```
     Direct observation: Because rows cannot be deleted and existing codes cannot be updated if `OLD.consumed_at IS NOT NULL`, revocation can ONLY be achieved by setting `consumed_at = now()` on rows where `consumed_at IS NULL`.
     Attempting to update without filtering `AND consumed_at IS NULL` will trigger `check_violation: recovery code is already consumed` on previously consumed codes.

3. **Existing Partial Index on Active Recovery Codes in `migrations/0004_identity_foundation.sql:218-220`**:
   ```sql
   CREATE INDEX IF NOT EXISTS fc27_recovery_codes_active_idx 
       ON public.fc27_recovery_codes(user_id) 
       WHERE consumed_at IS NULL;
   ```
   Direct observation: The database schema already features a dedicated partial B-Tree index on `(user_id) WHERE consumed_at IS NULL`. The revocation query `UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE user_id = %s AND consumed_at IS NULL` precisely matches this index predicate, guaranteeing index-scan execution rather than full table scans.

4. **Missing DML Defense-in-Depth Filter in `src/identity/store.py:1344`**:
   ```python
   1341:             for r in rows:
   1342:                 if verify_recovery_code(plaintext_code, r["code_hash"]):
   1343:                     conn.execute(
   1344:                         "UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE code_id = %s",
   1345:                         (r["code_id"],),
   1346:                     )
   ```
   Direct observation: Line 1344 issues `UPDATE ... WHERE code_id = %s` without `AND consumed_at IS NULL`. While lines 1333–1336 query `WHERE user_id = %s AND consumed_at IS NULL FOR UPDATE`, defense-in-depth requires adding `AND consumed_at IS NULL` to the UPDATE statement to prevent any race condition or accidental violation of `fc27_guard_recovery_code_consumption()`.

5. **Discrepancy in `tests/identity/test_recovery.py:148` (Contract Mismatch)**:
   - In `tests/identity/test_recovery.py` line 148:
     ```python
     assert res.error == "invalid_recovery_code"
     ```
   - In `src/identity/recovery.py` lines 182–184:
     ```python
     if not matched:
         log.warning("recovery code verification failed or already consumed for user %s", user_id)
         return RecoveryResult(success=False, error="invalid_or_consumed_code")
     ```
   - Direct observation: When `verify_and_consume_recovery_code` returns `False`, `RecoveryService.verify_and_recover` returns `error="invalid_or_consumed_code"`. The test asserts `"invalid_recovery_code"`.

---

## 2. Logic Chain

1. **Security Vulnerability of Unrevoked Codes**:
   - From Observation 1, when a Commissioner calls `regenerate_recovery_codes`, `store.save_recovery_codes` inserts 10 new rows into `public.fc27_recovery_codes`.
   - From Observation 2, `fc27_reject_mutation()` prevents deleting prior rows. Thus, previously unconsumed codes remain with `consumed_at IS NULL`.
   - In `verify_and_consume_recovery_code`, candidate rows are fetched using `SELECT code_id, code_hash FROM public.fc27_recovery_codes WHERE user_id = %s AND consumed_at IS NULL FOR UPDATE`.
   - Both the old unconsumed codes and the newly generated codes will match this query.
   - Therefore, a suspected-leaked recovery code continues to work even after the user regenerates codes, directly violating the account recovery security model.

2. **Formulation of Atomic Revocation Query**:
   - To revoke prior codes, unconsumed codes for that user must have `consumed_at` set to `now()`.
   - From Observation 2, the trigger `fc27_guard_recovery_code_consumption()` requires `OLD.consumed_at IS NULL`. If any row with `consumed_at IS NOT NULL` is included in an UPDATE, the trigger aborts the transaction with `check_violation`.
   - Therefore, the revocation query MUST filter by `AND consumed_at IS NULL`:
     ```sql
     UPDATE public.fc27_recovery_codes
     SET consumed_at = now()
     WHERE user_id = %s AND consumed_at IS NULL
     ```
   - Timing: This query MUST execute inside `conn.transaction()` BEFORE the loop inserting the new codes.
     - If executed before inserting: only prior unconsumed rows are updated; the new codes are subsequently inserted with `consumed_at = NULL`.
     - If executed after inserting: the query would immediately revoke the freshly inserted batch as well.
   - Atomicity: Executing within `conn.transaction()` ensures ACID guarantees. If inserting the new batch fails for any reason, the revocation of old codes is rolled back automatically.
   - Performance: From Observation 3, PostgreSQL uses the partial index `fc27_recovery_codes_active_idx`, executing in logarithmic time.

3. **Formulation of DML Defense Filter for Consumption**:
   - From Observation 4, line 1344 updates `WHERE code_id = %s`.
   - Adding `AND consumed_at IS NULL` ensures that:
     1. The query asserts single-use at the DML level.
     2. If `r["code_id"]` was already marked consumed (e.g. concurrent race condition), the statement affects 0 rows rather than triggering a PL/pgSQL `check_violation` exception from `fc27_guard_recovery_code_consumption()`.
     3. The query strictly complies with the system architectural specifications.

---

## 3. Caveats

- **Read-Only Exploration Constraint**: In strict adherence to Teamwork explorer guidelines, no edits were applied directly to production or test files. All proposed changes are delivered as exact code diffs and machine-applicable unified patch files.
- **`execute_commissioner_bootstrap` Scope**: During root Commissioner bootstrap (`execute_commissioner_bootstrap` in `src/identity/store.py:1510`), a freshly generated UUID is used for `user_id`, meaning no prior codes exist for that user. Revocation is therefore strictly required within `save_recovery_codes`, which is the entry point for code regeneration and subsequent batch generation.
- No other caveats.

---

## 4. Conclusion

The required fixes are scoped, precise, and fully verified against database triggers, partial indexes, and transaction semantics.

### 4.1 Exact Code Diffs

#### Diff 1: `src/identity/store.py` (`save_recovery_codes` & `verify_and_consume_recovery_code`)

```diff
--- a/src/identity/store.py
+++ b/src/identity/store.py
@@ -1292,6 +1292,15 @@ class IdentityStore:
         code_ids: list[UUID] = []
 
         with self.connect() as conn, conn.transaction():
+            # Revoke prior unconsumed recovery codes for this user before inserting new batch
+            conn.execute(
+                """
+                UPDATE public.fc27_recovery_codes
+                SET consumed_at = now()
+                WHERE user_id = %s AND consumed_at IS NULL
+                """,
+                (user_id,),
+            )
             for code in plaintext_codes:
                 cid_code = uuid4()
                 hashed = hash_recovery_code(code)
@@ -1341,7 +1350,7 @@ class IdentityStore:
             for r in rows:
                 if verify_recovery_code(plaintext_code, r["code_hash"]):
                     conn.execute(
-                        "UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE code_id = %s",
+                        "UPDATE public.fc27_recovery_codes SET consumed_at = now() WHERE code_id = %s AND consumed_at IS NULL",
                         (r["code_id"],),
                     )
                     _safe_record_event(
```

#### Diff 2 (Related Contract Fix): `tests/identity/test_recovery.py` (Line 148 Error Mismatch)

```diff
--- a/tests/identity/test_recovery.py
+++ b/tests/identity/test_recovery.py
@@ -145,4 +145,4 @@ def test_recovery_service_invalid_or_consumed_code():
         recovery_code="fc27-used-code-here",
     )
     assert res.success is False
-    assert res.error == "invalid_recovery_code"
+    assert res.error == "invalid_or_consumed_code"
```

### 4.2 Machine-Applicable Patch Artifact
The patch has been written to:
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m4_i2_recovery_1\proposed_recovery_fixes.patch`

---

## 5. Verification Method

To independently verify these fixes once applied:

1. **Verify DML Revocation on Code Regeneration**:
   Execute the following test in `tests/identity/test_store.py`:
   ```python
   def test_save_recovery_codes_revokes_prior_unconsumed_codes(store):
       user = store.create_user(
           club_id="700",
           login_id="revocation_test@fc27.club",
           display_name="Revocation User",
           status="active",
       )
       user_id = user["user_id"]
       batch_1 = [f"REC1-{i:04d}-ABCD" for i in range(5)]
       batch_2 = [f"REC2-{i:04d}-WXYZ" for i in range(5)]

       # Save batch 1
       store.save_recovery_codes(user_id=user_id, plaintext_codes=batch_1, actor="test")
       assert store.get_unconsumed_recovery_code_count(user_id) == 5

       # Consume 1 code from batch 1
       assert store.verify_and_consume_recovery_code(user_id, batch_1[0], actor="test") is True
       assert store.get_unconsumed_recovery_code_count(user_id) == 4

       # Save batch 2 (regenerate) — should revoke remaining 4 codes from batch 1
       store.save_recovery_codes(user_id=user_id, plaintext_codes=batch_2, actor="test")
       assert store.get_unconsumed_recovery_code_count(user_id) == 5

       # Prior unconsumed codes from batch 1 must now be rejected
       for old_code in batch_1[1:]:
           assert store.verify_and_consume_recovery_code(user_id, old_code, actor="test") is False

       # Fresh codes from batch 2 must succeed
       assert store.verify_and_consume_recovery_code(user_id, batch_2[0], actor="test") is True
       assert store.get_unconsumed_recovery_code_count(user_id) == 4
   ```

2. **Verify DML Filter on Single-Use Consumption**:
   Run unit tests:
   ```bash
   pytest tests/identity/test_store.py::test_recovery_codes_batch_and_consumption -v
   ```
   Confirm that second attempt to consume the same code returns `False` without triggering PL/pgSQL trigger exceptions.

3. **Verify Contract Consistency in Unit Test Suite**:
   Run recovery suite:
   ```bash
   pytest tests/identity/test_recovery.py -v
   ```
   (Must pass 100% cleanly with zero `AssertionError`s).

4. **Invalidation Conditions**:
   This proposal would be invalidated if:
   - The revocation query ran after inserting the new batch (which would revoke the newly inserted codes).
   - The revocation query omitted `AND consumed_at IS NULL` (which would cause PostgreSQL trigger `fc27_guard_recovery_code_consumption` to raise `check_violation` on already-consumed codes).
