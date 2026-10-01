"""
capture_agent.py
================
FC27 Pro Clubs — Windows Host Capture Agent (Tier 3)
East Coasters FC | Club ID: 330323

RUNS ON: Windows 11 host machine (NOT inside Docker)
REQUIRES: Xbox Remote Play open and full-screen in Xbox app
          Python 3.11+ on Windows 11
          pip install mss dxcam pillow pytesseract redis websockets

HOW IT WORKS:
  1. dxcam (fastest Windows screen capture, GPU-accelerated) grabs frames
     from the Xbox Remote Play window at 2fps
  2. Same ROI + OCR logic as rtsp_frame_analyzer.py
  3. Publishes events to Redis running inside Docker
     (Redis port 6379 exposed to Windows host via docker-compose)
  4. No Xbox Developer Mode required — works on any retail Xbox Series X/S

SETUP:
  1. Start Docker stack: docker compose up -d redis fc27-mcp langfuse postgres
  2. Launch Xbox app on Windows 11 → Remote Play → East Coasters FC session
  3. Put Xbox Remote Play in full-screen (Win + Shift + Enter or F11 in Xbox app)
  4. Run: python host_agent/capture_agent.py
  5. Agent auto-detects FC27 match HUD and starts capturing

CALIBRATION:
  Run with --calibrate to show ROI overlays on your screen for tuning.
  Coordinates are for 1920x1080. Scale if your display is different.

NOTES:
  - dxcam requires Windows + DirectX. Falls back to mss on older systems.
  - Redis must be reachable on localhost:6379 (default docker-compose config)
  - The agent runs in the background — keep Xbox Remote Play open
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
import time
import re
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

# Windows-only imports — guarded for CI/lint
try:
    import dxcam
    DXCAM_AVAILABLE = True
except ImportError:
    DXCAM_AVAILABLE = False

try:
    import mss
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False

import cv2
import numpy as np
import pytesseract
import redis.asyncio as aioredis

log = logging.getLogger("fc27.capture")
logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO")),
    format="%(asctime)s [fc27-capture] %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)

# ── Config ─────────────────────────────────────────────────────────────────────

CLUB_ID      = os.getenv("FC27_CLUB_ID",   "330323")
PLATFORM     = os.getenv("FC_PLATFORM",    "common-gen5")
FC_VERSION   = "27"
# Redis inside Docker — exposed to Windows host on port 6379
REDIS_URL    = os.getenv("REDIS_URL",      "redis://localhost:6379")
CAPTURE_FPS  = int(os.getenv("CAPTURE_FPS", "2"))     # 2fps sufficient for OCR
CALIBRATE    = "--calibrate" in sys.argv

# Display resolution of your Windows 11 monitor running Xbox Remote Play
DISPLAY_W    = int(os.getenv("DISPLAY_W", "1920"))
DISPLAY_H    = int(os.getenv("DISPLAY_H", "1080"))

# ── FC27 HUD ROIs (1080p — calibrate with --calibrate flag) ───────────────────
# IMPORTANT: These assume Xbox Remote Play is full-screen at 1920x1080.
# Run with --calibrate to verify and tune these for your setup.

SCORE_ROI    = (760,  18, 400,  52)   # Score: "1 - 0"
TIMER_ROI    = (880,  72, 160,  40)   # Clock: "67:32"
HOME_ROI     = (120,  22, 300,  38)   # Home team name
AWAY_ROI     = (1500, 22, 300,  38)   # Away team name
RATING_ROI   = (0,   280, 1920, 600)  # Post-match rating panel

# Redis channels (must match live_match_watcher.py)
CH_GOAL      = "fc27:live:goal"
CH_START     = "fc27:live:match_start"
CH_END       = "fc27:live:match_end"
CH_STATE     = "fc27:live:match_state"
CH_FAIR_PLAY = "fc27:fairplay:flag"

# ── Match State ────────────────────────────────────────────────────────────────

@dataclass
class MatchState:
    home_score:    int  = 0
    away_score:    int  = 0
    timer_str:     str  = "00:00"
    timer_minutes: int  = 0
    home_team:     str  = "East Coasters FC"
    away_team:     str  = "Opponent"
    in_match:      bool = False
    match_ended:   bool = False
    events:        list = field(default_factory=list)
    last_state_pub: int = 0

    def score_str(self) -> str:
        return f"{self.home_score} - {self.away_score}"

    def result(self) -> str:
        if self.home_score > self.away_score:   return "win"
        elif self.home_score < self.away_score: return "loss"
        return "tie"

# ── Screen Capture ─────────────────────────────────────────────────────────────

class ScreenCapture:
    """
    Wraps dxcam (preferred, GPU-accelerated) or mss (fallback).
    dxcam: fastest on Windows, zero CPU overhead, handles HDR games.
    mss: pure Python, works everywhere, slight CPU cost.
    """

    def __init__(self):
        self._camera = None
        self._mss    = None
        self._method = None

        if DXCAM_AVAILABLE:
            try:
                self._camera = dxcam.create(output_color="BGR")
                self._camera.start(target_fps=CAPTURE_FPS, video_mode=True)
                self._method = "dxcam"
                log.info("Screen capture: dxcam (GPU-accelerated) ✅")
            except Exception as exc:
                log.warning("dxcam init failed (%s), falling back to mss", exc)

        if self._method is None and MSS_AVAILABLE:
            self._mss    = mss.mss()
            self._method = "mss"
            log.info("Screen capture: mss (CPU) ✅")

        if self._method is None:
            raise RuntimeError(
                "No screen capture backend available. "
                "Install dxcam: pip install dxcam  OR  mss: pip install mss"
            )

    def grab(self) -> Optional[np.ndarray]:
        """Grab current frame as BGR numpy array."""
        if self._method == "dxcam":
            frame = self._camera.get_latest_frame()
            return frame   # already BGR numpy

        if self._method == "mss":
            monitor = {"top": 0, "left": 0, "width": DISPLAY_W, "height": DISPLAY_H}
            img     = self._mss.grab(monitor)
            return cv2.cvtColor(np.array(img), cv2.COLOR_BGRA2BGR)

        return None

    def stop(self):
        if self._camera:
            self._camera.stop()
        if self._mss:
            self._mss.close()

# ── OCR Helpers ────────────────────────────────────────────────────────────────

def _crop(frame: np.ndarray, roi: tuple) -> np.ndarray:
    x, y, w, h = roi
    # Scale if display resolution differs from 1920x1080
    sx = DISPLAY_W / 1920
    sy = DISPLAY_H / 1080
    x, y = int(x*sx), int(y*sy)
    w, h = int(w*sx), int(h*sy)
    return frame[y:y+h, x:x+w]

def _preprocess(region: np.ndarray) -> np.ndarray:
    """Preprocess region for OCR: grayscale + OTSU threshold + upscale."""
    gray   = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    # Upscale 2x — Tesseract works better on larger text
    return cv2.resize(thresh, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)

def _ocr(region: np.ndarray, config: str = "--psm 7") -> str:
    processed = _preprocess(region)
    return pytesseract.image_to_string(processed, config=config).strip()

def _parse_score(text: str) -> Optional[tuple[int, int]]:
    m = re.search(r"(\d+)\s*[-–]\s*(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    parts = [p for p in text.split() if p.isdigit()]
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    return None

def _parse_timer(text: str) -> Optional[tuple[str, int]]:
    m = re.search(r"(\d{1,3})[:\s+](\d{2})", text)
    if m:
        mins = int(m.group(1))
        return f"{mins}:{m.group(2)}", mins
    m2 = re.search(r"(\d{1,3})", text)
    if m2:
        mins = int(m2.group(1))
        return f"{mins}:00", mins
    return None

def _hud_active(frame: np.ndarray) -> bool:
    """FC27 match HUD: dark bar at top-center."""
    roi  = _crop(frame, SCORE_ROI)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < 200

def _postmatch_screen(frame: np.ndarray) -> bool:
    """Post-match rating screen: bright full-screen overlay."""
    roi  = _crop(frame, RATING_ROI)
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) > 180

# ── Calibration Mode ───────────────────────────────────────────────────────────

def run_calibration(frame: np.ndarray) -> None:
    """Draw ROI overlays on captured frame and print OCR results."""
    vis  = frame.copy()
    sx   = DISPLAY_W / 1920
    sy   = DISPLAY_H / 1080
    rois = {
        "SCORE":  (SCORE_ROI,  (0,   255, 0)),
        "TIMER":  (TIMER_ROI,  (255, 0,   0)),
        "HOME":   (HOME_ROI,   (0,   255, 255)),
        "AWAY":   (AWAY_ROI,   (255, 165, 0)),
        "RATING": (RATING_ROI, (255, 0,   255)),
    }
    for label, (roi, color) in rois.items():
        x, y, w, h = roi
        x, y, w, h = int(x*sx), int(y*sy), int(w*sx), int(h*sy)
        cv2.rectangle(vis, (x, y), (x+w, y+h), color, 2)
        cv2.putText(vis, label, (x, max(y-5,0)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, color, 2)
        crop  = frame[y:y+h, x:x+w]
        text  = _ocr(crop, "--psm 6")
        print(f"  ROI {label:<8}: OCR → '{text}'")

    small = cv2.resize(vis, (1280, 720))
    cv2.imshow("FC27 Capture Agent — ROI Calibration (press Q to quit)", small)
    cv2.waitKey(0)
    cv2.destroyAllWindows()

# ── Frame Processor ────────────────────────────────────────────────────────────

async def process_frame(
    frame: np.ndarray,
    state: MatchState,
    redis: aioredis.Redis,
) -> MatchState:

    def _pub(channel, payload):
        payload.update({
            "fc_version": FC_VERSION,
            "platform":   PLATFORM,
            "club_id":    CLUB_ID,
            "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source":     "capture_agent_tier3",
        })
        return redis.publish(channel, json.dumps(payload))

    active = _hud_active(frame)

    if active and not state.in_match:
        state.in_match = True
        state.events   = []
        log.info("⚽ MATCH START detected via screen capture")
        await _pub(CH_START, {"home_team": state.home_team, "away_team": state.away_team})

    if not active and state.in_match:
        if _postmatch_screen(frame):
            state.in_match   = False
            state.match_ended = True
            result            = state.result()
            log.info("🏁 MATCH END — %s | result=%s", state.score_str(), result)
            await _pub(CH_END, {
                "score":  state.score_str(),
                "result": result,
                "events": state.events,
                "timer":  state.timer_str,
            })
        return state

    if not state.in_match:
        return state

    # OCR score
    score_text   = _ocr(_crop(frame, SCORE_ROI), "--psm 7")
    parsed_score = _parse_score(score_text)
    if parsed_score:
        nh, na = parsed_score
        if nh > state.home_score:
            for _ in range(nh - state.home_score):
                ev = {"team": "home", "score": f"{nh}-{na}", "timer": state.timer_str}
                state.events.append(ev)
                await _pub(CH_GOAL, ev)
                log.info("⚽ GOAL — Home! %d-%d @ %s", nh, na, state.timer_str)
        if na > state.away_score:
            for _ in range(na - state.away_score):
                ev = {"team": "away", "score": f"{nh}-{na}", "timer": state.timer_str}
                state.events.append(ev)
                await _pub(CH_GOAL, ev)
                log.info("⚽ GOAL — Away! %d-%d @ %s", nh, na, state.timer_str)
        state.home_score, state.away_score = nh, na

    # OCR timer
    timer_text   = _ocr(_crop(frame, TIMER_ROI), "--psm 7 digits")
    parsed_timer = _parse_timer(timer_text)
    if parsed_timer:
        state.timer_str, state.timer_minutes = parsed_timer

    # Publish state every 5 match-minutes
    now = int(time.time())
    if state.timer_minutes > 0 and state.timer_minutes % 5 == 0 \
            and now - state.last_state_pub > 200:
        await _pub(CH_STATE, {
            "score": state.score_str(), "timer": state.timer_str,
            "events_count": len(state.events),
        })
        state.last_state_pub = now

    return state

# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("FC27 Windows Host Capture Agent (Tier 3)")
    log.info("Display : %dx%d | FPS: %d | Redis: %s",
             DISPLAY_W, DISPLAY_H, CAPTURE_FPS, REDIS_URL)
    log.info("Club ID : %s | Platform: %s", CLUB_ID, PLATFORM)

    if CALIBRATE:
        log.info("CALIBRATION MODE — grab one frame, show ROIs, exit")

    capture = ScreenCapture()
    redis   = await aioredis.from_url(REDIS_URL, decode_responses=False)
    await redis.ping()
    log.info("Redis connected ✅")
    log.info("Watching for FC27 match HUD... (keep Xbox Remote Play full-screen)")

    state    = MatchState()
    interval = 1.0 / CAPTURE_FPS

    try:
        while True:
            t0    = time.monotonic()
            frame = capture.grab()

            if frame is None:
                await asyncio.sleep(interval)
                continue

            if CALIBRATE:
                run_calibration(frame)
                break

            state = await process_frame(frame, state, redis)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, interval - elapsed))

    except KeyboardInterrupt:
        log.info("Capture agent stopped by user")
    finally:
        capture.stop()
        await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())
