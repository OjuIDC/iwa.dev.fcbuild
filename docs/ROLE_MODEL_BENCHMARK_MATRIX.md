# Role-by-role model benchmark matrix

| Role | Workload | Control | Required strengths | Hard gates | Suggested candidates to benchmark |
|---|---|---|---|---|---|
| Player observer | High-volume per-player extraction | llama3.2:3b | JSON, low latency, evidence fidelity | No approval language; valid player/evidence fields | Compact 3B to 4B instruction models available under approved licenses |
| ScoutAlpha | Comparative ranking and recruitment fit | qwen2.5:7b | Evidence-grounded ranking, stable top/middle/low tiers | Exactly 3/3/3; no fabricated stats | Qwen instruction 7B to 14B class |
| AnalystUnit | Match calculations and anomaly reasoning | qwen2.5:7b | Numerical accuracy, citations, contradiction detection | Deterministic math must match code | Reasoning-distilled 7B to 14B class |
| CoachPrime | Training-plan generation | qwen2.5:7b | Instruction following, personalization, clarity | Cannot activate program; schema-valid action | Qwen or Gemma instruction class |
| TacticsCore | Constraint-based tactical comparison | qwen2.5:7b | Multi-variable reasoning, trade-off analysis | Cannot deploy tactics | Strongest quantized reasoning model fitting VRAM |
| DirectorPrime | Cross-swarm synthesis | qwen2.5:7b | Conflict resolution, concise executive recommendation | Cannot approve or bypass Commissioner | 14B instruction/reasoning class when hardware permits |
| Reviewer | Independent critique | qwen2.5:7b | Unsupported-claim detection, security review | Must reject missing evidence and gate bypass | Prefer a different family from producer after testing |

## Scoring
Every role uses a weighted 100-point score plus hard gates for schema validity, human-approval compliance, availability, and latency. Automatic selection keeps the current control unless another configured model passes all gates and produces a higher benchmark score.
