# FC27 Multi-Language Decision Matrix

- **Status**: Current
- **Audience**: Developers, Operators, Architecture Reviewers
- **Last verified commit**: `c55f1a18a7281dd9af6779c309d1bc9eabaadbf8`
- **Source of truth**: `src/fc27_performance.py`, `tests/test_fc27_performance.py`, `docs/FC27_PERFORMANCE_BASELINE.md`
- **Supersedes**: None

---

## 1. Purpose and Governance Boundary

This document records the measured, evidence-backed decision matrix for whether any component of the East Coasters (`club_id=330323`, `platform=common-gen5`, `game_version=FC27`) local analytics platform warrants introducing another implementation language or runtime beyond Python 3.11+ and PostgreSQL 16.

All decisions are constrained to the five canonical decision values enforced by `FC27OptimizationDecision` in [`src/fc27_performance.py`](../src/fc27_performance.py):

- `KEEP_PYTHON`
- `USE_NATIVE_BACKED_PYTHON`
- `EXTRACT_WHEN_THRESHOLD_REACHED`
- `PROTOTYPE_SEPARATELY`
- `NOT_JUSTIFIED`

Unless a measured metric exceeds its review threshold in [`docs/FC27_PERFORMANCE_BASELINE.md`](FC27_PERFORMANCE_BASELINE.md) **and** SQL tuning, index review, caching, and Python serialization improvements have been exhausted, the default decision is `KEEP_PYTHON`.

---

## 2. Review Thresholds Before Considering a New Language

| Metric | Review Threshold | First Remedy Before Any Language Extraction |
| :--- | :---: | :--- |
| PostgreSQL operational read query `p95` | `> 250 ms` | SQL tuning, index review |
| Discord command handler `p95` | `> 1,000 ms` | Advisory report cache improvement |
| Dashboard endpoint warm `p95` | `> 500 ms` | Cache improvement, serialization reduction |
| Full advisory pipeline `p95` (`1,000` matches) | `> 2,000 ms` | Canonical hash caching, serialization reduction |
| Polling worker container idle RSS | `> 300 MB` | Process isolation, dependency audit |
| Discord bot container idle RSS | `> 250 MB` | Process isolation |
| Container cold start | `> 5,000 ms` | Image layer and entrypoint review |
| Analytical DataFrame transformation `p95` | `> 500 ms` | Native-backed Python library (pandas / optional Polars) |
| Large-record traced peak memory | `> 500 MB` | Streaming iteration / native-backed Python library |

None of these thresholds are exceeded at operational scale (`10` persisted matches, `60` player-match rows, growing by at most `20` matches/day under `ONCE_EVERY_12_HOURS` polling) or at `1,000`-match (`LARGE`) scale.

---

## 3. Candidate Language Evaluation Matrix

### 3.1 Python 3.11+ (Current Platform)

- **Target Component**: Domain orchestration, 12-hour polling (`src/fc27_polling_worker.py`), validation and pseudonymization (`src/fc27_clubs_api_contracts.py`), transactional PostgreSQL persistence (`src/fc27_repository.py`), deterministic advisory chain (`Search -> Scout -> Analyst -> Coach -> Tactics -> Director`), local dashboard (`src/fc27_demo_api.py`), and read-only Discord bot (`src/fc27_discord_bot.py`).
- **Measured Current Bottleneck**: Operational `SMALL` (`10` matches) full advisory pipeline completes in `< 15 ms` median / `< 25 ms` `p95`; `LARGE` (`1,000` matches) completes well below the `2,000 ms` threshold.
- **Expected Benefit**: Preserves a single schema authority in Pydantic v2, single pseudonymization and privacy-scanning boundary, and `1,950+` offline tests.
- **Integration Cost**: Zero (already implemented and verified).
- **Contract Duplication Risk**: None (single source of truth in `src/`).
- **Privacy Boundary Risk**: Lowest (all `plr_ecf_<12-hex>` pseudonymization, sensitive-field scanning, and read-only SQL checks reside in audited Python modules).
- **Deployment Complexity**: Low (digest-pinned Python slim containers).
- **Testing Burden**: Low (unified `pytest` suite).
- **Observability Burden**: Low (`FC27PerformanceRecorder` in `src/fc27_performance.py`).
- **Rollback Complexity**: Trivial (standard Git revert).
- **Decision**: `KEEP_PYTHON`
- **Trigger for Reconsideration**: Full advisory pipeline `p95 > 2,000 ms` at operational scale after SQL, caching, and serialization optimizations.

### 3.2 Python Plus Native-Backed Libraries (psycopg3, pandas, Optional Polars, ONNX Runtime, CTranslate2)

- **Target Component**: Tabular analytical views (`src/fc27_pandas_view.py`), bulk export generation, and optional future isolated local inference runtimes.
- **Measured Current Bottleneck**: `60`-row operational DataFrame view runs in `< 5 ms`; `60,000`-row stress aggregation runs in `< 50 ms`.
- **Expected Benefit**: Vectorized C/Rust-backed execution inside the existing Python process without splitting trust boundaries or duplicating schemas.
- **Integration Cost**: Low (in-process library call over sanitized pseudonymous dictionaries).
- **Contract Duplication Risk**: Low (consumes validated `FC27MatchEnvelopeSchema` / `FC27SuppliedMatch` objects).
- **Privacy Boundary Risk**: Low (operates strictly post-pseudonymization).
- **Deployment Complexity**: Low (pinned wheel in `requirements.txt` if adopted).
- **Testing Burden**: Low (result-equivalence tests in `tests/test_fc27_performance.py`).
- **Observability Burden**: Low.
- **Rollback Complexity**: Low (adapter swap).
- **Decision**: `USE_NATIVE_BACKED_PYTHON` (note: Polars specifically is `NOT_JUSTIFIED` at current scale; see Section 4).
- **Trigger for Reconsideration**: Persisted player-match rows `> 50,000` AND analytical export `p95 > 500 ms`.

### 3.3 Go (Service or Gateway Candidate)

- **Target Component**: Read-only Discord slash-command gateway, health check proxy, or container sidecar.
- **Measured Current Bottleneck**: Offline Discord command handlers execute in `< 35 ms` `p95` and the `fc27-discord-bot` container uses `~48.5 MB` idle RSS (threshold: `250 MB`).
- **Expected Benefit**: Lower idle memory (`~15–25 MB`) and high fan-out concurrency if serving hundreds of clubs simultaneously.
- **Integration Cost**: High (requires IPC/HTTP boundary to Python advisory engine or re-implementing 6 analytical layers in Go).
- **Contract Duplication Risk**: High (would duplicate 13 slash-command contracts, privacy regex guards, and mandatory epistemic footers).
- **Privacy Boundary Risk**: Medium–High (risk of sanitizer divergence between Python and Go).
- **Deployment Complexity**: Medium (multi-stage Go + Python container builds).
- **Testing Burden**: High (separate Go test harness plus cross-process contract tests).
- **Observability Burden**: Medium.
- **Rollback Complexity**: Medium (Compose profile swap back to `Dockerfile.discord`).
- **Decision**: `EXTRACT_WHEN_THRESHOLD_REACHED`
- **Trigger for Reconsideration**: Discord command handler `p95 > 1,000 ms`, Discord bot container idle RSS `> 250 MB`, or multi-tenant expansion beyond single-club East Coasters scope.

### 3.4 Rust (Compute or Native Extension Candidate)

- **Target Component**: High-throughput match normalization, canonical SHA-256 hashing, and large-scale batch analytics (via PyO3/maturin or standalone service).
- **Measured Current Bottleneck**: 12-hour polling ingests at most `10` matches (`60` player rows) per cycle in `< 20 ms`; `LARGE` (`1,000` matches) runs well under `2,000 ms`.
- **Expected Benefit**: Sub-millisecond execution and lower heap allocations at `100,000+` match scale.
- **Integration Cost**: High (Rust toolchain, PyO3 ABI bindings, cross-platform wheel builds).
- **Contract Duplication Risk**: High (duplicating 16-stage validation rules and canonical JSON hashing).
- **Privacy Boundary Risk**: Medium (must guarantee bit-identical `plr_ecf_<12-hex>` pseudonymization and sensitive-field rejection).
- **Deployment Complexity**: High (Rust compiler in Docker build / CI).
- **Testing Burden**: High (cross-language hash-equivalence and fuzz testing).
- **Observability Burden**: Medium (FFI panic boundary handling).
- **Rollback Complexity**: Medium (revert to pure Python module).
- **Decision**: `EXTRACT_WHEN_THRESHOLD_REACHED`
- **Trigger for Reconsideration**: Full advisory pipeline `p95 > 2,000 ms` at `1,000` matches after Python caching and serialization tuning, or batch ingestion exceeding `10,000` matches per cycle.

### 3.5 C#/.NET (Windows Host / Desktop Candidate)

- **Target Component**: Local Windows Service host, desktop tray controller, or native Windows capture utility.
- **Measured Current Bottleneck**: Docker Compose profiles (`fc27-polling`, `fc27-discord`) and Python CLI scripts already provide deterministic lifecycle management on Windows.
- **Expected Benefit**: Native Windows Service control manager integration or desktop UI if operator workflow ever moves away from Docker Compose / browser dashboard.
- **Integration Cost**: Medium–High (separate `.sln` / `.csproj` and local loopback contract).
- **Contract Duplication Risk**: Medium (status and health DTO duplication).
- **Privacy Boundary Risk**: Medium (host wrapper must never inspect raw provider payloads or tokens).
- **Deployment Complexity**: Medium (Windows-specific runtime packaging).
- **Testing Burden**: Medium (`dotnet test` runner in CI).
- **Observability Burden**: Medium.
- **Rollback Complexity**: Low (optional host wrapper only; domain logic stays in Python).
- **Decision**: `EXTRACT_WHEN_THRESHOLD_REACHED`
- **Trigger for Reconsideration**: Explicit operator requirement for a native Windows Service wrapper or desktop UI outside Docker Desktop.

### 3.6 Custom C or C++ (Native Kernel Candidate)

- **Target Component**: Custom JSON parsing, cryptographic hashing, or numerical kernels.
- **Measured Current Bottleneck**: Zero custom CPU hotspots exist that are not already served by Python's C-backed `hashlib` (OpenSSL), `json` (`_json` C extension), or `psycopg` / `pandas`.
- **Expected Benefit**: Negligible (`< 1 ms` per 12-hour polling cycle).
- **Integration Cost**: Very High (manual memory management, C/C++ ABI stability, compiler toolchain matrix).
- **Contract Duplication Risk**: High (unsafe struct marshaling).
- **Privacy Boundary Risk**: High (memory-safety bugs such as buffer overruns or use-after-free could expose sensitive buffers).
- **Deployment Complexity**: Very High.
- **Testing Burden**: Very High (requires ASan/UBSan/fuzzing).
- **Observability Burden**: High.
- **Rollback Complexity**: Medium.
- **Decision**: `NOT_JUSTIFIED`
- **Trigger for Reconsideration**: Not justified under any foreseeable FC27 club analytics workload; if native speed is ever required, prefer audited native-backed Python wheels or Rust (`PyO3`).

---

## 4. Summary Table

| Candidate Language / Runtime | Target Component | Decision | Action in This Phase |
| :--- | :--- | :---: | :--- |
| **Python 3.11+** | Polling, validation, persistence, Search/Scout/Analyst/Coach/Tactics/Director, dashboard, Discord bot | `KEEP_PYTHON` | Retained as sole domain implementation language |
| **Native-Backed Python** (`psycopg3`, `pandas`, optional `Polars`) | Tabular analytical exports (`src/fc27_pandas_view.py`) | `USE_NATIVE_BACKED_PYTHON` (`Polars`: `NOT_JUSTIFIED`) | Retained `pandas` view; added optional Polars benchmark harness in `src/fc27_performance.py` |
| **Local Inference Sidecar** (`Ollama`, `llama.cpp`, `ONNX Runtime`, `CTranslate2`) | Optional future downstream narrative summary boundary | `PROTOTYPE_SEPARATELY` | Documented strict isolation boundary in [`docs/FC27_LOCAL_INFERENCE_BOUNDARY.md`](FC27_LOCAL_INFERENCE_BOUNDARY.md); `0` models deployed |
| **Go** | High-concurrency Discord gateway / webhook proxy | `EXTRACT_WHEN_THRESHOLD_REACHED` | No rewrite performed (`p95 < 35 ms`, idle RSS `~48.5 MB`) |
| **Rust** | High-throughput normalization / hashing extension | `EXTRACT_WHEN_THRESHOLD_REACHED` | No rewrite performed (`SMALL` pipeline `< 25 ms`) |
| **C#/.NET** | Native Windows Service / desktop tray host | `EXTRACT_WHEN_THRESHOLD_REACHED` | No rewrite performed (Docker Compose profiles active) |
| **Custom C/C++** | Custom native parsing / numerical kernels | `NOT_JUSTIFIED` | Rejected due to memory-safety and maintenance risk with zero measured benefit |
