# Task Assignment — explorer_m1_i2_venv_1

## Context & Milestone
Phase 2 Milestone 1: Identity Data Foundation & Cryptography (Iteration 2).
Gate failure in Iteration 1:
`challenger_m1_2` discovered that while `pyproject.toml` and `requirements.txt` are synchronized, the declared packages (`webauthn`, `pyotp`, `cryptography`, `argon2-cffi`) have not been installed in `.venv\Lib\site-packages`, causing `ModuleNotFoundError: No module named 'argon2'`.

## Authoritative Inputs
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md` (Read first!)
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md`
- `c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2\handoff.md`
- `requirements.txt` and `pyproject.toml`

## Your Task
1. Inspect the virtual environment configuration, python interpreter path, and installed packages in `.venv`.
2. Inspect `requirements.txt` and verify what exact wheels/packages need to be installed.
3. Determine the exact installation procedure (e.g. `pip install -r requirements.txt` targeting `.venv`) and verify that it respects project constraints and doesn't pollute global python or modify locked packages like `langgraph==1.2.12`.
4. Formulate the exact execution plan for the worker. Do NOT install packages yourself.
5. Deliver handoff to:
`c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_venv_1\handoff.md`


## 2026-10-04T03:20:12Z
You are explorer_m1_i2_venv_1, a Read-only exploration agent.
Working directory: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_venv_1
Authoritative request: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md
Your dispatch instructions are at: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_venv_1\DISPATCH.md
Project plan: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\orchestrator_phase2\PROJECT.md
Previous iteration failure report: c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\challenger_m1_2\handoff.md

MANDATORY: You MUST read c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\ORIGINAL_REQUEST.md before starting work.

Your task:
1. Inspect the virtual environment configuration in .venv, python version, pip location, and installed packages in .venv\Lib\site-packages.
2. Inspect requirements.txt (lines 16-22) and pyproject.toml (lines 27-33) to verify all 4 newly declared packages: webauthn>=2.5,<3, pyotp>=2.9,<3, cryptography>=44,<45, argon2-cffi>=23,<25.
3. Determine the exact, safe command to install these packages into the active .venv without altering pinned dependencies (such as langgraph==1.2.12 or langgraph-checkpoint-postgres==3.1.2) or touching global python.
4. Formulate the exact commands and verification steps for the upcoming Worker. Do NOT execute package installation yourself.
5. Deliver your complete handoff report to:
c:\Users\iwadev\dev\iwa.dev.fcbuild\.agents\teamwork\explorer_m1_i2_venv_1\handoff.md
When finished, notify me with a summary and the report path via send_message.
