"""
capture_agent.py
================
FC27 Pro Clubs — Windows Host Capture Agent (Tier 3)
Club: FC27_CLUB_ID / FC27_CLUB_NAME (env)

RUNS ON: Windows 11 host machine (NOT inside Docker)
REQUIRES: Xbox Remote Play open and full-screen in Xbox app
          Python 3.11+ on Windows 11
          pip install mss dxcam pillow pytesseract redis websockets

HOW IT WORKS:
  1. dxcam (fastest Windows screen capture, GPU-accelerated) grabs frames
     from the Xbox Remote Play window at 2fps
  2. Same ROI + OCR logic as rtsp_frame_analyzer.py; event decisions
     (debounce, confidence, plausibility, kickoff reset, orientation) come
     from the shared pure module src/ocr_parsing.py
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
from typing import Any, Optional, cast
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name(".env"))

# Windows-only imports — guarded for CI/lint
try:
    import dxcam  # type: ignore
    DXCAM_AVAILABLE = True
except ImportError:
    DXCAM_AVAILABLE = False

try:
    import mss  # type: ignore
    MSS_AVAILABLE = True
except ImportError:
    MSS_AVAILABLE = False

import cv2
import numpy as np
import pytesseract
import redis.asyncio as aioredis

# install.ps1 writes TESSERACT_CMD into host_agent/.env (PATH changes are not
# visible to already-open sessions).
if os.getenv("TESSERACT_CMD"):
    pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]

# Shared pure OCR/debounce logic lives in <repo>/src/ocr_parsing.py (stdlib-only,
# also used by src/rtsp_frame_analyzer.py). The agent is run from a full repo
# checkout (install.ps1: `python host_agent\capture_agent.py`), so put the
# repository root on sys.path and import it as src.ocr_parsing. If you copy the
# agent elsewhere, copy src/__init__.py and src/ocr_parsing.py alongside it in a
# src/ folder one level above this file.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
from src.ocr_parsing import (  # noqa: E402
    SCORE_OCR_CONFIG, TIMER_OCR_CONFIG, LiveMatchTracker, TrackerConfig, event_channel,
    parse_clock, parse_score, resolve_orientation, text_and_confidence,
)

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

HOME_AWAY_MODE = os.getenv("FC27_HOME_AWAY", "auto")   # home | away | auto
CLUB_NAME      = os.getenv("FC27_CLUB_NAME", "")
ORIENTATION_RETRY_FRAMES = int(os.getenv("OCR_ORIENTATION_RETRY_FRAMES", "10"))

MatchState = LiveMatchTracker   # backwards-compatible name


def new_state() -> LiveMatchTracker:
    return LiveMatchTracker(config=TrackerConfig.from_env(),
                            orientation=resolve_orientation(HOME_AWAY_MODE))

# ── Screen Capture ─────────────────────────────────────────────────────────────

class ScreenCapture:
    """
    Wraps dxcam (preferred, GPU-accelerated) or mss (fallback).
    dxcam: fastest on Windows, zero CPU overhead, handles HDR games.
    mss: pure Python, works everywhere, slight CPU cost.
    """

    def __init__(self):
        self._camera: Any = None
        self._mss: Any = None
        self._method: Optional[str] = None

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
        if self._method == "dxcam" and self._camera is not None:
            frame = self._camera.get_latest_frame()
            return frame   # already BGR numpy

        if self._method == "mss" and self._mss is not None:
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
    return cast(str, pytesseract.image_to_string(processed, config=config)).strip()

def _ocr_conf(region: np.ndarray, config: str) -> tuple[str, Optional[float]]:
    """OCR with per-word confidences (min over words). Confidence None if unavailable."""
    processed = _preprocess(region)
    try:
        data = pytesseract.image_to_data(processed, config=config, output_type=pytesseract.Output.DICT)
        return text_and_confidence(data)
    except Exception:
        return cast(str, pytesseract.image_to_string(processed, config=config)).strip(), None

def _refresh_orientation(frame: np.ndarray, state: LiveMatchTracker) -> None:
    if HOME_AWAY_MODE in ("home", "away"):
        state.set_orientation(HOME_AWAY_MODE)
        return
    home = _ocr(_crop(frame, HOME_ROI), "--psm 7")
    away = _ocr(_crop(frame, AWAY_ROI), "--psm 7")
    if home:
        state.home_team = home[:64]
    if away:
        state.away_team = away[:64]
    state.set_orientation(resolve_orientation(HOME_AWAY_MODE, CLUB_NAME, home, away))

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
    state: LiveMatchTracker,
    redis: aioredis.Redis,
) -> LiveMatchTracker:

    async def _pub_all(events: list[dict]) -> None:
        for ev in events:
            event_type = ev["event"]
            payload = {k: v for k, v in ev.items() if k != "event"}
            payload.update({
                "type":       event_type,
                "fc_version": FC_VERSION,
                "platform":   PLATFORM,
                "club_id":    CLUB_ID,
                "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "source":     "capture_agent_tier3",
            })
            await redis.publish(event_channel(event_type), json.dumps(payload))
            log.info("EVENT [%s] %s-%s @ %s", event_type, payload.get("home_score", "?"),
                     payload.get("away_score", "?"), payload.get("timer", ""))

    active = _hud_active(frame)

    if active and not state.in_match:
        _refresh_orientation(frame, state)
        log.info("MATCH START detected via screen capture (orientation=%s)", state.orientation)
        await _pub_all(state.start_match())

    if not active and state.in_match:
        if _postmatch_screen(frame):
            await _pub_all(state.end_match())
            log.info("MATCH END — %s | result=%s", state.score_str(), state.result()["result"])
        return state

    if not state.in_match:
        return state

    if state.orientation == "unknown" and CLUB_NAME:
        state.frames_since_orientation += 1
        if state.frames_since_orientation >= ORIENTATION_RETRY_FRAMES:
            state.frames_since_orientation = 0
            _refresh_orientation(frame, state)

    score_text, score_conf = _ocr_conf(_crop(frame, SCORE_ROI), SCORE_OCR_CONFIG)
    timer_text, timer_conf = _ocr_conf(_crop(frame, TIMER_ROI), TIMER_OCR_CONFIG)
    await _pub_all(state.observe(parse_score(score_text), score_conf,
                                 parse_clock(timer_text), timer_conf))
    return state

# ── Main ───────────────────────────────────────────────────────────────────────

async def main() -> None:
    log.info("FC27 Windows Host Capture Agent (Tier 3)")
    log.info("Display : %dx%d | FPS: %d | Redis: configured (URL not logged)",
             DISPLAY_W, DISPLAY_H, CAPTURE_FPS)
    log.info("Orientation mode: %s | club name set: %s", HOME_AWAY_MODE, bool(CLUB_NAME))
    log.info("Club ID : %s | Platform: %s", CLUB_ID, PLATFORM)

    if CALIBRATE:
        log.info("CALIBRATION MODE — grab one frame, show ROIs, exit")

    capture = ScreenCapture()
    redis   = await aioredis.from_url(REDIS_URL, decode_responses=False)
    await redis.ping()
    log.info("Redis connected ✅")
    log.info("Watching for FC27 match HUD... (keep Xbox Remote Play full-screen)")

    state    = new_state()
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

            try:
                state = await process_frame(frame, state, redis)
            except Exception as exc:   # one bad frame must not stop the agent
                log.error("Frame processing error: %s", type(exc).__name__)
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, interval - elapsed))

    except KeyboardInterrupt:
        log.info("Capture agent stopped by user")
    finally:
        capture.stop()
        await redis.aclose()

if __name__ == "__main__":
    asyncio.run(main())
