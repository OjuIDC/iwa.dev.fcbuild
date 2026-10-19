# Challenge alignment trace

This trace maps the official Inside the Game challenge to concrete evidence in this package. Status values are **Implemented**, **Deployment action**, or **Submission action**; they avoid implying that an Azure deployment, public repository, registration, or video already exists.

## Challenge requirements

| Requirement | Status | Evidence / action |
|---|---|---|
| New solution | Implemented | New MatchPulse challenge module, version 0.1.0; keep its commit history after the registration-period start |
| Synthetic football-realistic data | Implemented | `matchpulse.synthetic`; schema literal `source="synthetic"`; safety gate |
| Ingest as events happen | Implemented | Single/batch event APIs and live seeded producer |
| Meaningful statistics and patterns | Implemented | Pass difficulty, shot speed, pressing surge, possession swing, high regain |
| Explain why moments matter | Implemented | Evidence IDs, inputs, formula, confidence; explainability agent blocks missing evidence |
| Render alongside live match | Implemented | Timed WebSocket JSON and `/demo` overlay UI |
| Personalize for different audiences | Implemented | Analyst/fan/broadcaster profiles, favorites, interests, detail levels |
| Real-time narratives and recap | Implemented | Live narrative outputs and profile-specific recap endpoint |
| Multi-language storytelling | Implemented | English, Spanish, French deterministic catalogs |
| Microsoft platform use | Deployment action | Configure Microsoft Foundry adapter; deploy supplied Azure Container Apps template |
| Public source repository | Submission action | Publish sanitized repository; verify license and history |
| Demo under two minutes | Submission action | Record `submission/DEMO_SCRIPT_120S.md`; keep final cut <= 1:55 |
| Public video URL | Submission action | Upload to YouTube/Vimeo/Facebook Video/Youku |
| Testable project link/build | Deployment action | Deploy to Azure or provide a no-login test build through the project page |
| English submission materials | Implemented | All submission documents are in English |

## Judging criteria (equally weighted)

| Criterion | Evidence judges can see | Final proof to capture |
|---|---|---|
| Technological implementation | Strict models, deterministic state, guarded Foundry adapter, Docker, CI, tests, Bicep | Green CI; public commit; Azure resource screenshot; test output |
| Agentic design and innovation | Supervisor + interpretation + explainability + personalization agents; explicit handoffs and fallbacks | One diagram and a live same-event/multi-profile comparison |
| Real-world impact and applicability | Broadcast overlay, analyst detail, fan mode, multilingual global reach; safe model fallback | Brief use-case slide and running public demo |
| User experience and presentation | Responsive demo UI, live overlay, profiles, machine-readable WebSocket | 120-second video with visible profile/language switch |
| Category adherence | Synthetic-only guard, five-stage pipeline, evidence-backed narrative, recap | Link this trace and show source event IDs in demo |

## Recommended prize positioning

1. **Grand Prize:** complete end-to-end audience experience with strong controls.
2. **Best Multi-Agent System:** clearest current fit; distinct agents, shared state, handoffs, and recovery are implemented.
3. **Best Azure Cloud Native Integration:** credible after deploying the Bicep and showing Event Hubs/Container Apps/Application Insights.
4. **Best Use of Microsoft Foundry:** credible only after a real Foundry endpoint is configured and demonstrated; do not claim this category from the adapter alone.
5. **Best Enterprise Solution:** support with security boundaries, evidence lineage, idempotency, CI, health checks, and documented production gaps.

## Disqualifier prevention

- Do not publish secrets or private club/player information.
- Do not use real match footage or copyrighted music in the video.
- Do not use Premier League or club trademarks as if endorsed.
- Do not demonstrate the EA-backed path; use only the provided synthetic generator.
- Confirm the entrant/team is eligible and that every contributor is authorized.
- Keep the public repository accessible through the end of judging.
- Make the demo match the actual behavior and final commit.
