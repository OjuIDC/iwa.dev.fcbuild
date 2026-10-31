> Archived document
> **Archived: historical reference only.**
> This document is non-authoritative and must not be used as current operational
> guidance.
> Replacement: `docs/FC27_COMPONENT_STATUS.md`.
> Archive reason: Historical v2.0.0 watcher/OCR/rundown phase note; non-primary legacy subsystem outside the current FC27 community-provider path.
> Operational status: DO NOT USE
> Original path: docs/notes/watcher_ocr_rundown.md
> Archived on: 2026-10-31
> Superseded by: docs/FC27_COMPONENT_STATUS.md
> Reason: Historical v2.0.0 watcher/OCR/rundown phase note; non-primary legacy subsystem outside the current FC27 community-provider path.
> Warning: Do not execute commands in this archived document.

# Watcher, OCR and post-match rundown: reliability notes

## Live match watcher (`src/live_match_watcher.py`)

- Every match returned by `get_matches` is checked, not only `matches[0]`. Unseen matches are processed oldest-first (by `timestamp`, or in reverse API order when there is no timestamp).
- A match counts as processed only after every step has succeeded, including a successful Graph API `/run` that returns a `thread_id`. The Graph API allocates thread ids; the watcher never creates one.
- The watcher has no PostgreSQL access. All processing state lives in Redis:

| Key | Type | Purpose |
|---|---|---|
| `fc27:watcher:<club>:match:<matchId>` | hash | `state` (`processing`/`done`/`dead`), `attempts`, `last_error`, `thread_id`, `match_type`, step markers `result_recorded`, `new_published`, `analysis_published`, `baseline`. TTL `WATCHER_RECORD_TTL_SEC`. |
| `fc27:watcher:<club>:match:<matchId>:lease` | string | Processing lease, taken with `SET NX EX WATCHER_LEASE_TTL_SEC`. The value is an owner token, and only the owner releases the lease. |
| `fc27:watcher:<club>:<matchType>:baseline` | string | Marks the first boot. Matches already listed by the API at that point are marked `done` (`baseline=1`) and are not triggered. |
| `fc27:watcher:<club>:results` | list | Last 10 results, used for tilt detection (unchanged). |

- **Crash after detection:** the hash has no `done` state, so after the lease expires the next poll retries the match.
- **Duplicates:** a match whose state is `done` or `dead` is skipped. A concurrent worker sees `busy` because it cannot take the lease. Each side effect (tilt-history push, `fc27:match:new`, the fair-play and tilt check, `fc27:match:end`) is guarded by `HSETNX` on its step marker, so a retry does not repeat it. A stored `thread_id` means the graph is not triggered a second time.
- **Failures:** `attempts` is incremented on every attempt. When it reaches `WATCHER_MAX_ATTEMPTS`, the state becomes `dead` and `last_error` is kept (an exception class name, never secrets).
- **Known small windows:**
  - The step marker is set before its side effect runs. A crash in that gap drops the side effect rather than repeating it (at-most-once).
  - Lease release is a GET followed by a DEL, not an atomic Lua compare-and-delete.
- **Error handling:** `handle_match` and `poll_match_type` never raise, and `main()` also wraps each poll.
- `fc27:match:end` (`ANALYSIS_READY`) now carries `match_id`, `match_type` and `match` for the rundown agent.
- **Detection latency:** at worst, EA publication delay + MCP `clubs/matches` cache TTL (300 s) + `POLL_INTERVAL_SEC`. The old "~30s" was wrong.
  - The MCP `get_matches` tool does not accept a cache argument; unknown kwargs fail.
  - So `WATCHER_MATCH_CACHE_TTL_SEC` is forwarded as `cache_ttl_sec` only if `tools/list` advertises that property. Otherwise it has no effect, and this is logged once.
- **MCP auth:** calls send `Authorization: Bearer $MCP_API_TOKEN` (never logged). The Graph API call keeps `X-API-Key: $GRAPH_SERVICE_TOKEN`.
- `REDIS_URL` is no longer logged.

## OCR (`src/ocr_parsing.py`, shared by `src/rtsp_frame_analyzer.py` and `host_agent/capture_agent.py`)

- `ocr_parsing.py` is a pure module that uses only the standard library.
- The capture agent puts the repository root on `sys.path` and imports `src.ocr_parsing`, so it must be run from a full checkout, as `install.ps1` already does.
  - If you copy the agent elsewhere, place `src/__init__.py` and `src/ocr_parsing.py` in a `src/` folder one level above `capture_agent.py`.
- **Clock:**
  - Tesseract config is `--psm 7 -c tessedit_char_whitelist=0123456789:`. The `digits` preset stripped the `:`.
  - `parse_clock` requires `M:SS`, with seconds 00–59 and minutes 0–130.
  - Readings without a colon (`6732`, `673`) are rejected. 67:32 never becomes 673.
- **Score:** whitelist `0123456789-`. Scores above 30 are rejected.
- **Confidence:** the minimum word confidence from `image_to_data` must be at least `OCR_MIN_CONFIDENCE`. If confidences are unavailable, the reading is accepted.
- **Debounce:**
  - A score must read the same for `OCR_DEBOUNCE_FRAMES` consecutive frames. An unreadable or low-confidence frame breaks the streak.
  - The clock is debounced on its minute value.
- **Plausibility:**
  - An accepted change may only add one goal to one side.
  - A jump such as 0-0 → 7-0, a decrease, or both sides changing at once is rejected.
  - If an implausible score stays stable for `OCR_SCORE_RESYNC_FRAMES`, it is adopted silently as a `MATCH_STATE` with `reason=score_resync`. It never produces GOAL events.
- **Duplicates:** each event key (for example `GOAL:1-0` or `STATE:10`) is emitted at most once per match.
- **Kickoff:** `start_match()` always resets the score to 0-0. If the clock falls back to 0:xx from 2:00 or later, the tracker emits MATCH_END for the old match, then MATCH_START (`reason=kickoff_clock_reset`), and resets. The second-half restart at 45:00 is not treated as a kickoff.
- **MATCH_STATE:** published once for each multiple-of-5 minute (configurable with `OCR_STATE_EVERY_MINUTES`), and never before a clock is read or at 0:xx. Previously the initial "00:00" state published on every frame.
- **Orientation:**
  - `FC27_HOME_AWAY=home|away` forces the side.
  - `auto` (the default) compares the OCR'd HUD team names with `FC27_CLUB_NAME`, retrying every `OCR_ORIENTATION_RETRY_FRAMES` frames while the side is unknown.
  - With orientation `unknown`, GOAL events carry `team` home or away and `for_club: null`. MATCH_END carries `result: "unknown"` plus the neutral `home_away_result`, with no goals_for or goals_against.
- Events still go to the existing `fc27:live:*` channels. A bad frame no longer stops the loop.

## Post-match rundown (`src/post_match_rundown.py`)

- **Match resolution order:**
  1. The event's `match` payload.
  2. The event's `match_type`, if it names one.
  3. Otherwise `RUNDOWN_MATCH_TYPES` in order (`leagueMatch,playoffMatch,cup,clubFriendly`).
- When the event names a `match_id`, only that match is accepted. Types the MCP rejects (`playoffMatch` today) are skipped.
- **Tackles:**
  - `tackles` means attempts (`tackleattempts`/`tackleAttempts`). `tackles_won` means successful tackles (`tacklesmade`/`tacklesMade`).
  - The club-level fields were swapped and are fixed.
  - When club-level tackle fields are missing, the values fall back to the match `aggregate`, then to the sum over players.
- **Prompt:**
  - The instruction text is fixed and says the data block is data, not instructions.
  - EA strings are passed only inside `<match_data>` as `json.dumps` output, with control characters stripped, strings capped at `RUNDOWN_MAX_STR_LEN`, and `<`/`>` escaped to `<`/`>`.
  - `ai_prompt` is now a dict with `instruction`, `data` and `messages`.
- The hardcoded club id, club name and gamertag were removed.
- The commissioner identity comes from `COMMISSIONER_ID` and is display-only.

## Environment variables introduced

| Name | Default | Purpose |
|---|---|---|
| `WATCHER_MATCH_CACHE_TTL_SEC` | unset | Cache hint for MCP `get_matches`, forwarded only if the tool supports `cache_ttl_sec`. |
| `WATCHER_MAX_ATTEMPTS` | 5 | Processing attempts allowed per match before it is marked `dead`. |
| `WATCHER_LEASE_TTL_SEC` | `GRAPH_RUN_TIMEOUT_SEC`+120 | Processing lease TTL. |
| `WATCHER_RECORD_TTL_SEC` | 2592000 | TTL of each per-match record. |
| `WATCHER_BASELINE_ON_FIRST_BOOT` | true | On first boot, skip matches already listed by the API. |
| `OCR_MIN_CONFIDENCE` | 60 | Minimum Tesseract word confidence. |
| `OCR_DEBOUNCE_FRAMES` | 2 | Consecutive identical frames required before a reading is accepted. |
| `OCR_SCORE_RESYNC_FRAMES` | 20 | Frames a stable implausible score must persist before it is adopted silently. |
| `OCR_STATE_EVERY_MINUTES` | 5 | MATCH_STATE cadence in match minutes. |
| `OCR_ORIENTATION_RETRY_FRAMES` | 10 | How often, in frames, to retry orientation while it is unknown. |
| `FC27_HOME_AWAY` | auto | Our club's side: `home`, `away` or `auto`. |
| `FC27_CLUB_NAME` | "" | Club name for HUD orientation and display. |
| `RUNDOWN_MATCH_TYPES` | `leagueMatch,playoffMatch,cup,clubFriendly` | Fallback match types for the rundown, tried in order. |
| `RUNDOWN_MAX_STR_LEN` | 64 | Length cap for untrusted strings in the prompt data. |
| `COMMISSIONER_ID` | "" | Commissioner display identity (replaces `COMMISSIONER`). |
| `MCP_API_TOKEN` | (required by the proxy) | Bearer token sent to the MCP proxy. |
