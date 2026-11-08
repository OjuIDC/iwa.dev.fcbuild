# Role-model selection

The supplied repository uses one cloud-oriented model configuration for every agent, while the role definitions require different behaviors such as extraction, coaching, tactical reasoning, and executive synthesis. The safest immediate baseline remains `llama3.2:3b` for high-volume player observers and `qwen2.5:7b` for all lead/domain roles.

I could not verify the current October 2026 Ollama catalog or current model licenses from the available environment, so the alternatives below are candidates to benchmark, not confirmed current defaults.

## Candidate role families to benchmark
- Player observer: compact instruction model, low latency, reliable JSON. Candidates include newer 3B to 4B Qwen, Phi, Gemma, or Llama instruction variants if available and license-compatible.
- ScoutAlpha: model strong at retrieval-grounded comparison and structured ranking. Try a 7B to 14B Qwen instruction model before changing families.
- AnalystUnit: reasoning and numerical consistency matter most. Benchmark a reasoning-distilled 7B to 14B model, but require schema validation and deterministic calculations outside the LLM.
- CoachPrime: instruction following and clear plan generation matter more than deep reasoning. Gemma or Qwen instruction variants may be suitable.
- TacticsCore: use the strongest reasoning model that fits GPU memory, ideally a 14B class quantized model if the host has sufficient VRAM.
- DirectorPrime: use the strongest synthesis and decision-support model, but never allow it to approve. A 14B class instruction/reasoning model is preferable when hardware permits.
- Reviewer: prefer a different model family from the producing model to reduce correlated errors, subject to benchmark results.

## Evaluation gates
Do not select on subjective chat quality. Benchmark each candidate on JSON validity, evidence citation, hallucination rate, ranking stability, latency, peak VRAM, tokens per second, refusal correctness, prompt-injection resistance, and Commissioner-gate compliance.

Keep `qwen2.5:7b` as the control model until a candidate wins the role-specific evaluation without weakening security or structured-output compliance.

## Runtime behaviour as implemented

**LM Studio fallback** (`LM_STUDIO_FALLBACK_ENABLED`):
- `false`, `0`, `no` or unset: LM Studio entries are removed from every candidate list, so no LM Studio request is ever made.
- `true`, `1` or `yes`: LM Studio routes join the fallback chain after the healthy Ollama routes.
- Any other value: `ConfigError` at startup.
- `LM_STUDIO_BASE_URL` overrides the catalog URL.

**Role backend** (`ROLE_BACKEND`):
- `native`: calls `invoke_role` directly.
- `swarms`: the default in Compose. Uses a real `swarms==15.0.3` `Agent` per role, whose LLM adapter routes through the same `invoke_role`. Swarms OpenTelemetry export is forced off, and the backend refuses to run if it is on.

**Output contract.** Every reply must parse as `RoleOutput` (see `src/roles.py`): a summary, bounded per-player adjustments of ±10 and risks, with no extra keys. Invalid output makes that role's package non-approvable.

**Ranking policy `fc27-rank-v1`.** For each role, final score = base evidence score + that role's adjustment, clamped to 0–100. Players are never duplicated. With fewer players, fewer selections exist.

**Timeouts.** `ROLE_TIMEOUT_SEC` (default 120) applies to each model HTTP call.
