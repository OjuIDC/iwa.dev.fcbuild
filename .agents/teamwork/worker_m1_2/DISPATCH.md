# Task Assignment — worker_m1_2

## Context & Objective
You are worker_m1_2, a versatile worker agent assigned to implement the remediation for Phase 2 Milestone 1: Identity Data Foundation & Cryptography (Iteration 2).

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read first!)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2\handoff.md` (Defect reports)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_store_1\handoff.md` (Patch specification)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_venv_1\handoff.md` (Virtual environment protocol)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_suite_1\handoff.md` (Verification matrix)

## MANDATORY INTEGRITY WARNING
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## File Ownership
You exclusively own and may edit:
- `tests/identity/test_store.py`

## Remediation Tasks
1. **Fix Missing Import in `tests/identity/test_store.py`**:
   Add `import hashlib` at the top of `tests/identity/test_store.py` (around line 4).
   Verify that lines 381 and 392 (`hashlib.sha256`) execute cleanly with no `NameError`.
2. **Install Declared Dependencies in `.venv`**:
   Execute:
   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```
   Verify that `webauthn`, `pyotp`, `cryptography`, and `argon2-cffi` are installed.
3. **Verify Pinned Dependencies Intact**:
   Ensure `langgraph==1.2.12` and `langgraph-checkpoint-postgres==3.1.2` were NOT altered or upgraded.
   Run:
   ```powershell
   .\.venv\Scripts\python.exe -c "import langgraph, langgraph_checkpoint_postgres; assert langgraph.__version__ == '1.2.12'; assert langgraph_checkpoint_postgres.__version__ == '3.1.2'; print('PINS_INTACT')"
   ```
4. **Execute Full Milestone 1 Verification Matrix**:
   - `.\.venv\Scripts\pytest.exe tests/test_packaging.py tests/test_phase1_scope.py -v` (12 passed)
   - `.\.venv\Scripts\pytest.exe tests/test_m1_phase2_adversarial.py -v` (10 passed, resolving both failures)
   - `.\.venv\Scripts\pytest.exe tests/identity/test_crypto.py -v` (22 passed)
   - `.\.venv\Scripts\pytest.exe tests/identity/test_crypto_stress.py -v` (30 passed)
   - `.\.venv\Scripts\pytest.exe tests/identity/test_store.py -v` (11 passed, or clean skip if no DB)
   - `.\.venv\Scripts\pytest.exe tests/test_db_migrate_roles.py -v` (16 passed)
5. **Document Results & Deliver Handoff**:
   Write your hard handoff report to:
   `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_2\handoff.md`
   When finished, notify me with your report path and test outcomes via send_message.


## 2026-10-04T03:29:21Z
You are worker_m1_2, a versatile worker agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_2
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_2\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md

Explorer handoffs to implement:
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_store_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_venv_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_suite_1\handoff.md
- c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

MANDATORY INTEGRITY WARNING:
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

File ownership:
You exclusively own and may edit:
- tests/identity/test_store.py

Implement Milestone 1 Iteration 2 Remediation:
1. Fix missing import in tests/identity/test_store.py:
   Add 'import hashlib' at the top of tests/identity/test_store.py (line 4). Verify lines 381 and 392 now execute with hashlib bound.
2. Install declared dependencies in .venv:
   Execute: .\.venv\Scripts\python.exe -m pip install -r requirements.txt
3. Verify pinned dependencies intact:
   Assert langgraph==1.2.12 and langgraph-checkpoint-postgres==3.1.2 were not altered.
4. Execute full verification suite:
   - .\.venv\Scripts\pytest.exe tests/test_packaging.py tests/test_phase1_scope.py -v (12 passed)
   - .\.venv\Scripts\pytest.exe tests/test_m1_phase2_adversarial.py -v (10 passed, resolving both previous failures)
   - .\.venv\Scripts\pytest.exe tests/identity/test_crypto.py -v (22 passed)
   - .\.venv\Scripts\pytest.exe tests/identity/test_crypto_stress.py -v (30 passed)
   - .\.venv\Scripts\pytest.exe tests/identity/test_store.py -v (11 passed / clean skip if no DB)
   - .\.venv\Scripts\pytest.exe tests/test_db_migrate_roles.py -v (16 passed)
5. Write your hard handoff report to:
   c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\worker_m1_2\handoff.md
When finished, notify me with your report path and verification results via send_message.
