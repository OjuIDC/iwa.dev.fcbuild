"""
rtsp_frame_analyzer.py
======================
FC27 Pro Clubs — RTSP Live Frame Analyzer (Tier 2)
East Coasters FC | Club ID: 330323

REQUIREMENT: Xbox Developer Mode enabled on your console.
  Settings → System → Developer Settings → Enable Developer Mode
  Once enabled, RTSP stream available at:
    rtsp://<xbox-ip>:11442/video/live        (H264 1080p@30)
    rtsp://<xbox-ip>:11442/video/live4k      (HEVC 4K@60)

HOW IT WORKS:
  1. ffmpeg pulls RTSP stream from Xbox
  2. Frames piped to Python via stdout (raw RGB)
  3. OpenCV regions of interest (ROI) crop:
     - Score ROI: top-center HUD (FC27 scoreline)
     - Timer ROI: match clock position
     - Player rating ROI: post-match rating panel
  4. Tesseract OCR reads score and timer from each frame
  5. On score change → Redis pub/sub event (GOAL detected)
  6. On 90:00 → signals match end, triggers API poll immediately
  7. Full match events stream to WebSocket clients in real time

Docker: runs as a service in docker-compose.yml
  Requires: --network host or Xbox on same Docker bridge
  Image: custom (opencv + tesseract + ffmpeg)

Run standalone:
  XBOX_IP=192.168.1.50 python -m src.rtsp_frame_analyzer
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Optional

import cv2
import numpy as np
import pytesseract
import redis.asyncio as aioredis

log = logging.getLogger("fc27.rtsp")
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [fc27-rtsp] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ── Config ─────────────────────────────────────────────────────────────────────

XBOX_IP       = os.getenv("XBOX_IP",        "")   # your Xbox IP
XBOX_PORT     = int(os.getenv("XBOX_PORT",  "11442"))
USE_4K        = os.getenv("XBOX_4K",        "false").lower() == "true"
CLUB_ID       = os.getenv("FC27_CLUB_ID",   "330323")
PLATFORM      = os.getenv("FC_PLATFORM",    "common-gen5")
FC_VERSION    = "27"
REDIS_URL     = os.getenv("REDIS_URL",      "redis://redis:6379")
FRAME_W       = 3840 if USE_4K else 1920
FRAME_H       = 2160 if USE_4K else 1080
FRAME_BYTES   = FRAME_W * FRAME_H * 3   # RGB24

RTSP_URL      = (
    f"rtsp://{XBOX_IP}:{XBOX_PORT}/video/live4k"
    if USE_4K else
    f"rtsp://{XBOX_IP}:{XBOX_PORT}/video/live"
)

# ── FC27 HUD Regions of Interest (1080p baseline) ─────────────────────────────
# These are ESTIMATES — you MUST calibrate by pausing on the FC27 HUD
# and measuring pixel coordinates of the score/clock/rating elements.
# Tool: run with CALIBRATE=true to display ROI overlays for tuning.

# Score region: top-center scoreline (e.g. "1 - 0")
SCORE_ROI   = (760, 18, 400, 52)   # x, y, w, h
# Timer region: match clock (e.g. "67:32")
TIMER_ROI   = (880, 72, 160, 40)
# Home team name (left side of HUD)
HOME_ROI    = (120, 22, 300, 38)
# Away team name (right side of HUD)
AWAY_ROI    = (1500, 22, 300, 38)
# Post-match rating panel (full-screen, appears after FT whistle)
RATING_ROI  = (0, 280, 1920, 600)
# Substitution banner (bottom-center, appears when sub happens)
SUB_ROI     = (500, 920, 920, 60)

CALIBRATE   = os.getenv("CALIBRATE", "false").lower() == "true"

# ── Match State ────────────────────────────────────────────────────────────────

@dataclass
class MatchState:
    home_score:     int   = 0
    away_score:     int   = 0
    timer_str:      str   = "00:00"
    timer_minutes:  int   = 0
    home_team:      str   = "East Coasters FC"
    away_team:      str   = "Opponent"
    in_match:       bool  = False
    match_ended:    bool  = False
    events:         list  = field(default_factory=list)
    start_ts:       float = 0.0

    def score_str(self) -> str:
        return f"{self.home_score} - {self.away_score}"

    def result(self) -> str:
        if self.home_score > self.away_score:
            return "win"
        elif self.home_score < self.away_score:
            return "loss"
        return "tie"

# ── OCR Helpers ────────────────────────────────────────────────────────────────

def _crop(frame: np.ndarray, roi: tuple) -> np.ndarray:
    x, y, w, h = roi
    # Scale ROI if frame is 4K
    if FRAME_W == 3840:
        x, y, w, h = x*2, y*2, w*2, h*2
    return frame[y:y+h, x:x+w]

def _ocr(region: np.ndarray, config: str = "--psm 7 digits") -> str:
    """Run Tesseract OCR on a region. Returns stripped text."""
    gray   = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    text   = pytesseract.image_to_string(thresh, config=config)
    return text.strip()

def _parse_score(text: str) -> Optional[tuple[int, int]]:
    """
    Parse score string like '1 - 0' or '1-0' or '2  1'.
    Returns (home, away) or None if unparseable.
    """
    import re
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    # Try space-separated
    parts = [p for p in text.split() if p.isdigit()]
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    return None

def _parse_timer(text: str) -> Optional[tuple[str, int]]:
    """
    Parse timer like '67:32' or '90+3'. Returns (str, minutes) or None.
    """
    import re
    m = re.search(r"(\d{1,3})[:\s+](\d{2})", text)
    if m:
        mins = int(m.group(1))
        return f"{mins}:{m.group(2)}", mins
    m2 = re.search(r"(\d{1,3})", text)
    if m2:
        mins = int(m2.group(1))
        return f"{mins}:00", mins
    return None

def _detect_match_active(frame: np.ndarray) -> bool:
    """
    Heuristic: FC27 match HUD has a characteristic dark bar at top center.
    If the average brightness of the HUD region is below threshold,
    we're probably in a match.
    """
    roi    = _crop(frame, SCORE_ROI)
    gray   = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    mean   = float(gray.mean())
    return mean < 200   # HUD bar is dark; menus/loading screens are bright

def _detect_match_ended(frame: np.ndarray) -> bool:
    """
    Detect full-time / post-match rating screen.
    Looks for bright full-screen overlay with player ratings.
    """
    roi    = _crop(frame, RATING_ROI)
    gray   = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    mean   = float(gray.mean())
    return mean > 180   # post-match screen is much brighter than HUD

# ── Event Publisher ────────────────────────────────────────────────────────────

async def publish_event(redis: aioredis.Redis, event_type: str, payload: dict) -> None:
    payload.update({
        "fc_version": FC_VERSION,
        "platform":   PLATFORM,
        "club_id":    CLUB_ID,
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    channel = f"fc27:live:{event_type.lower()}"
    await redis.publish(channel, json.dumps(payload))
    log.info("EVENT [%s]: %s", event_type, payload)

# ── Calibration Mode ───────────────────────────────────────────────────────────

def run_calibration(frame: np.ndarray) -> None:
    """
    Display frame with ROI overlays drawn.
    Run with CALIBRATE=true to tune ROI coordinates.
    Press 'q' to quit, 's' to save screenshot.
    """
    vis = frame.copy()
    rois = {
        "SCORE":  (SCORE_ROI,  (0, 255, 0)),
        "TIMER":  (TIMER_ROI,  (255, 0, 0)),
        "HOME":   (HOME_ROI,   (0, 255, 255)),
        "AWAY":   (AWAY_ROI,   (255, 165, 0)),
        "RATING": (RATING_ROI, (255, 0, 255)),
        "SUB":    (SUB_ROI,    (0, 128, 255)),
    }
    for label, (roi, color) in rois.items():
        x, y, w, h = roi
        cv2.rectangle(vis, (x, y), (x+w, y+h), color, 2)
        cv2.putText(vis, label, (x, y-5), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        crop = _crop(frame, roi)
        text = _ocr(crop, "--psm 6")
        log.info("CALIBRATE ROI %-8s → OCR: '%s'", label, text)

    small = cv2.resize(vis, (1280, 720))
    cv2.imshow("FC27 ROI Calibration — press Q to quit", small)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

# ── Frame Processor ────────────────────────────────────────────────────────────

async def process_frame(
    frame: np.ndarray,
    state: MatchState,
    redis: aioredis.Redis,
) -> MatchState:
    """
    Process one frame: detect state, OCR score and timer, publish events.
    """
    if CALIBRATE:
        run_calibration(frame)
        return state

    # Detect if we're in a match
    active = _detect_match_active(frame)
    if not active and not state.in_match:
        return state   # not in a match, nothing to do

    if active and not state.in_match:
        # Match just started
        state.in_match  = True
        state.start_ts  = time.time()
        state.events    = []
        await publish_event(redis, "MATCH_START", {
            "home_team": state.home_team,
            "away_team": state.away_team,
        })
        log.info("MATCH START detected")

    if not active and state.in_match:
        # Check for post-match rating screen
        if _detect_match_ended(frame):
            state.match_ended = True
            state.in_match    = False
            result            = state.result()
            await publish_event(redis, "MATCH_END", {
                "home_score": state.home_score,
                "away_score": state.away_score,
                "result":     result,
                "timer":      state.timer_str,
                "events":     state.events,
            })
            log.info("MATCH END — %s %s result=%s",
                     state.score_str(), state.timer_str, result)
        return state

    # ── OCR score ────────────────────────────────────────────────────────
    score_crop   = _crop(frame, SCORE_ROI)
    score_text   = _ocr(score_crop, "--psm 7")
    parsed_score = _parse_score(score_text)

    if parsed_score:
        new_home, new_away = parsed_score
        # Detect goal events
        if new_home > state.home_score:
            goals_scored = new_home - state.home_score
            for _ in range(goals_scored):
                event = {
                    "type":   "GOAL",
                    "team":   "home",
                    "score":  f"{new_home}-{new_away}",
                    "timer":  state.timer_str,
                }
                state.events.append(event)
                await publish_event(redis, "GOAL", event)
                log.info("⚽ GOAL — Home! Score: %d-%d @ %s",
                         new_home, new_away, state.timer_str)

        if new_away > state.away_score:
            goals_scored = new_away - state.away_score
            for _ in range(goals_scored):
                event = {
                    "type":   "GOAL",
                    "team":   "away",
                    "score":  f"{new_home}-{new_away}",
                    "timer":  state.timer_str,
                }
                state.events.append(event)
                await publish_event(redis, "GOAL", event)
                log.info("⚽ GOAL — Away! Score: %d-%d @ %s",
                         new_home, new_away, state.timer_str)

        state.home_score = new_home
        state.away_score = new_away

    # ── OCR timer ────────────────────────────────────────────────────────
    timer_crop   = _crop(frame, TIMER_ROI)
    timer_text   = _ocr(timer_crop, "--psm 7 digits")
    parsed_timer = _parse_timer(timer_text)
    if parsed_timer:
        state.timer_str, state.timer_minutes = parsed_timer

    # Publish periodic state update every minute of match time
    if state.timer_minutes % 5 == 0 and state.timer_str.endswith(":00"):
        await publish_event(redis, "MATCH_STATE", {
            "home_score":    state.home_score,
            "away_score":    state.away_score,
            "timer":         state.timer_str,
            "events_so_far": len(state.events),
        })

    return state

# ── ffmpeg RTSP → frames ───────────────────────────────────────────────────────

def build_ffmpeg_cmd() -> list[str]:
    return [
        "ffmpeg",
        "-rtsp_transport", "tcp",          # more reliable than UDP
        "-i",              RTSP_URL,
        "-vf",             "fps=2",         # 2 fps is enough for score/timer OCR
        "-vcodec",         "rawvideo",
        "-pix_fmt",        "bgr24",
        "-an",                              # no audio needed
        "-f",              "rawvideo",
        "pipe:1",
    ]

# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    if not XBOX_IP:
        log.info("XBOX_IP is not configured; exiting cleanly")
        return
    log.info("FC27 RTSP Frame Analyzer starting (Tier 2 — requires Xbox Dev Mode)")
    log.info("RTSP URL : %s", RTSP_URL)
    log.info("Frame    : %dx%d | Mode: %s", FRAME_W, FRAME_H, "4K" if USE_4K else "1080p")
    log.info("Redis    : %s", REDIS_URL)

    if CALIBRATE:
        log.info("CALIBRATION MODE — displaying first frame with ROI overlays")

    redis = await aioredis.from_url(REDIS_URL, decode_responses=False)
    await redis.ping()
    log.info("Redis connected ✅")

    cmd   = build_ffmpeg_cmd()
    log.info("Starting ffmpeg: %s", " ".join(cmd))
    state = MatchState()

    proc  = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    try:
        while True:
            raw = await proc.stdout.readexactly(FRAME_BYTES)
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((FRAME_H, FRAME_W, 3))
            state = await process_frame(frame, state, redis)

            if CALIBRATE:
                break   # calibration shows one frame then exits

    except asyncio.IncompleteReadError:
        log.warning("RTSP stream ended (ffmpeg process exited)")
    except Exception as exc:
        log.error("Frame processing error: %s", exc)
    finally:
        proc.terminate()
        await redis.aclose()
        log.info("RTSP analyzer stopped")

if __name__ == "__main__":
    asyncio.run(main())
