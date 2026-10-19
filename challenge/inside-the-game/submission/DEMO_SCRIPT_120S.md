# Demo script - target 1:50, hard stop before 2:00

## 0:00-0:12 - Problem and promise

**Screen:** MatchPulse Live landing view.

**Voice:** "A football analyst, broadcaster, and fan should not receive the same story. MatchPulse Live turns synthetic match events into evidence-backed intelligence, then makes that moment feel native to every audience."

## 0:12-0:28 - Architecture in one sentence

**Screen:** Architecture diagram; highlight the agents.

**Voice:** "A supervisor coordinates interpretation, explainability, personalization, and localization agents. Every insight keeps its source event IDs, inputs, and formula. Microsoft Foundry can improve the copy, while deterministic output keeps the live path fast and resilient."

## 0:28-0:48 - Live synthetic ingestion

**Screen:** Terminal starts `matchpulse-synthetic --delay .15`; browser is connected as `fan-en`.

**Voice:** "This seeded producer emits only synthetic passes, shots, tackles, pressure, possession, and goals. The service accepts events in sequence, ignores duplicates, and pushes timed overlays over WebSocket."

## 0:48-1:12 - Same moment, different audiences

**Screen:** Let a shot-speed or pressing insight appear. Switch from `fan-en` to `analyst-en`, then `broadcaster-es` or `fan-fr`. Show the evidence IDs.

**Voice:** "Here is one grounded moment. Fan mode makes it immediate. Analyst mode adds the derivation and inputs. Broadcaster mode compresses it into overlay copy, and Spanish and French use curated language templates. The fact and evidence do not change - only relevance and presentation do."

## 1:12-1:32 - Explainability and failure recovery

**Screen:** Briefly show API JSON or test output, then `/health/ready` with Foundry mode.

**Voice:** "The explainability agent drops any insight without valid source evidence. If Foundry times out, returns invalid JSON, or invents a number, the guard falls back to deterministic text instead of interrupting the broadcast."

## 1:32-1:47 - Recap and Azure path

**Screen:** Open a Spanish recap, then architecture or Azure resource view.

**Voice:** "The same state creates an automated, profile-specific recap. The containerized service deploys to Azure Container Apps, with Event Hubs as the event seam and Application Insights for operations."

## 1:47-1:53 - Close

**Screen:** Product title and public repository URL.

**Voice:** "MatchPulse Live: one explainable match, made for every fan."

## Recording rules

- Final cut <= 1:55; judges are not required to watch after 2:00.
- Use no match footage, copyrighted music, club crests, league marks, or third-party trademarks.
- Show actual running behavior from the final public commit.
- Add English captions and keep terminal text readable at 1080p.
