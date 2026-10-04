# Task Assignment: worker_m6_3

## 2026-10-03T23:49:42Z

## Objective
Apply the exact drop-in replacements for the 4 test citations in `SECURITY_CHANGES.md` (lines 16, 66, and 86) verified by Explorers 1, 2, and 3.

## Mandatory Inputs to Read
1. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (authoritative user request + Section 1A patch)
2. `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase1\PROJECT.md`
3. Explorer 1 Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_it3_sec_1\remediation_analysis.md` & `handoff.md`
4. Explorer 2 Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_it3_sec_2\remediation_analysis.md` & `handoff.md`
5. Explorer 3 Report: `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m6_it3_sec_3\sweep_analysis.md` & `handoff.md`
6. `c:\Users\iwadev\dev\iwa.dev.fcbuild\SECURITY_CHANGES.md`

## Mandatory Integrity Warning
DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A teamwork_preview_auditor will independently verify your work. Integrity violations WILL be detected and your work WILL be rejected.

## Write Ownership (Exclusive)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\SECURITY_CHANGES.md`
You MUST NOT modify any source code in `src/`, `migrations/`, or test files in `tests/`.

## Task
Apply the exact drop-in replacements in `SECURITY_CHANGES.md`:
1. Line 16 (Row 1.3):
   Replace: `tests/test_graph_api_v1.py::test_health_ready_checks_db_posture`
   With: `tests/test_graph_api_v1.py::test_adversarial_readiness_fails_closed_on_db_role_posture_violation`
2. Line 66 (Row 6.1):
   Replace: `tests/test_dispatch_schema.py::test_append_only_triggers`
   With: `tests/test_dispatch_schema.py::test_tracking_tables_append_only`
3. Line 86 (Row 8.3):
   Replace: `tests/test_graph_persistence.py::test_restart_recovers_interrupted_graph`
   With: `tests/test_graph_persistence.py::test_checkpoint_recovery_across_server_restart`
   AND
   Replace: `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_recovery`
   With: `tests/e2e/test_tier3_combinations.py::TestCrossFeatureCombinations::test_tier3_post_commit_resume_failure_preserves_authoritative_decision`
4. Verify all 4 changes are in place and that the verbatim completion statement remains intact:
   `Secure data foundation complete. Dispatch remains disabled. No player delivery has occurred.`
5. Output `handoff.md` in your working directory and notify the orchestrator via `send_message`.
