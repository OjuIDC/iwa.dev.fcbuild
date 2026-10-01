"""
rtsp_frame_analyzer.py
======================
FC27 Pro Clubs — RTSP Live Frame Analyzer (Tier 2)
Club: FC27_CLUB_ID / FC27_CLUB_NAME (env)

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
  5. Readings are debounced (OCR_DEBOUNCE_FRAMES), confidence-gated
     (OCR_MIN_CONFIDENCE) and plausibility-checked (a score may only rise by
     one goal per accepted change) by src/ocr_parsing.LiveMatchTracker
  6. Accepted changes → Redis pub/sub events (GOAL, MATCH_STATE, ...)
  7. Post-match screen → MATCH_END; clock jumping back to 0:xx → kickoff reset
  8. Orientation (is our club home or away?) from FC27_HOME_AWAY or by
     comparing HUD team names with FC27_CLUB_NAME; "unknown" → neutral events

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
import time
from typing import Optional

import cv2
import numpy as np
import pytesseract
import redis.asyncio as aioredis

from src.ocr_parsing import (
    SCORE_OCR_CONFIG, TIMER_OCR_CONFIG, LiveMatchTracker, TrackerConfig, event_channel,
    parse_clock, parse_score, resolve_orientation, text_and_confidence,
)

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
# All event decisions (debounce, plausibility, kickoff reset, orientation,
# duplicate suppression) live in the pure module src/ocr_parsing.py, shared
# with host_agent/capture_agent.py.

HOME_AWAY_MODE = os.getenv("FC27_HOME_AWAY", "auto")   # home | away | auto
CLUB_NAME      = os.getenv("FC27_CLUB_NAME", "")
ORIENTATION_RETRY_FRAMES = int(os.getenv("OCR_ORIENTATION_RETRY_FRAMES", "10"))

MatchState = LiveMatchTracker   # backwards-compatible name


def new_state() -> LiveMatchTracker:
    # 'home'/'away' are forced; 'auto' starts unknown until HUD names are read.
    return LiveMatchTracker(config=TrackerConfig.from_env(),
                            orientation=resolve_orientation(HOME_AWAY_MODE))

# ── OCR Helpers ────────────────────────────────────────────────────────────────

def _crop(frame: np.ndarray, roi: tuple) -> np.ndarray:
    x, y, w, h = roi
    # Scale ROI if frame is 4K
    if FRAME_W == 3840:
        x, y, w, h = x*2, y*2, w*2, h*2
    return frame[y:y+h, x:x+w]

def _preprocess(region: np.ndarray) -> np.ndarray:
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY)
    return cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

def _ocr(region: np.ndarray, config: str = "--psm 7") -> str:
    """Run Tesseract OCR on a region. Returns stripped text."""
    return pytesseract.image_to_string(_preprocess(region), config=config).strip()

def _ocr_conf(region: np.ndarray, config: str) -> tuple[str, Optional[float]]:
    """OCR with per-word confidences (min over words). Confidence None if unavailable."""
    img = _preprocess(region)
    try:
        data = pytesseract.image_to_data(img, config=config, output_type=pytesseract.Output.DICT)
        return text_and_confidence(data)
    except Exception:   # older tesseract builds: fall back without confidence
        return pytesseract.image_to_string(img, config=config).strip(), None

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

def _read_team_names(frame: np.ndarray) -> tuple[str, str]:
    return _ocr(_crop(frame, HOME_ROI), "--psm 7"), _ocr(_crop(frame, AWAY_ROI), "--psm 7")

def _refresh_orientation(frame: np.ndarray, state: LiveMatchTracker) -> None:
    if HOME_AWAY_MODE in ("home", "away"):
        state.set_orientation(HOME_AWAY_MODE)
        return
    home, away = _read_team_names(frame)
    if home:
        state.home_team = home[:64]
    if away:
        state.away_team = away[:64]
    state.set_orientation(resolve_orientation(HOME_AWAY_MODE, CLUB_NAME, home, away))

# ── Event Publisher ────────────────────────────────────────────────────────────

async def publish_event(redis: aioredis.Redis, event_type: str, payload: dict) -> None:
    payload = {k: v for k, v in payload.items() if k != "event"}
    payload.update({
        "type":       event_type,
        "fc_version": FC_VERSION,
        "platform":   PLATFORM,
        "club_id":    CLUB_ID,
        "ts":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    await redis.publish(event_channel(event_type), json.dumps(payload))
    log.info("EVENT [%s]: score=%s-%s timer=%s", event_type,
             payload.get("home_score", "?"), payload.get("away_score", "?"), payload.get("timer", ""))

async def publish_all(redis: aioredis.Redis, events: list[dict]) -> None:
    for ev in events:
        await publish_event(redis, ev["event"], ev)

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
    state: LiveMatchTracker,
    redis: aioredis.Redis,
) -> LiveMatchTracker:
    """
    Process one frame: detect HUD state, OCR score and clock, let the shared
    tracker decide which (debounced, plausible, de-duplicated) events to publish.
    """
    if CALIBRATE:
        run_calibration(frame)
        return state

    active = _detect_match_active(frame)
    if not active and not state.in_match:
        return state   # not in a match, nothing to do

    if active and not state.in_match:
        _refresh_orientation(frame, state)
        await publish_all(redis, state.start_match())
        state.frames_since_orientation = 0
        log.info("MATCH START detected (orientation=%s)", state.orientation)

    if not active and state.in_match:
        if _detect_match_ended(frame):
            events = state.end_match()
            await publish_all(redis, events)
            log.info("MATCH END — %s %s result=%s",
                     state.score_str(), state.timer_str, state.result()["result"])
        return state

    # Keep trying to establish orientation while it is unknown.
    if state.orientation == "unknown" and CLUB_NAME:
        n = getattr(state, "frames_since_orientation", 0) + 1
        state.frames_since_orientation = n
        if n >= ORIENTATION_RETRY_FRAMES:
            state.frames_since_orientation = 0
            _refresh_orientation(frame, state)

    score_text, score_conf = _ocr_conf(_crop(frame, SCORE_ROI), SCORE_OCR_CONFIG)
    timer_text, timer_conf = _ocr_conf(_crop(frame, TIMER_ROI), TIMER_OCR_CONFIG)
    events = state.observe(parse_score(score_text), score_conf, parse_clock(timer_text), timer_conf)
    await publish_all(redis, events)
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
    log.info("Redis    : configured (URL not logged)")
    log.info("Orientation mode: %s | club name set: %s", HOME_AWAY_MODE, bool(CLUB_NAME))

    if CALIBRATE:
        log.info("CALIBRATION MODE — displaying first frame with ROI overlays")

    redis = await aioredis.from_url(REDIS_URL, decode_responses=False)
    await redis.ping()
    log.info("Redis connected ✅")

    cmd   = build_ffmpeg_cmd()
    log.info("Starting ffmpeg: %s", " ".join(cmd))
    state = new_state()

    proc  = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )

    try:
        while True:
            raw = await proc.stdout.readexactly(FRAME_BYTES)
            frame = np.frombuffer(raw, dtype=np.uint8).reshape((FRAME_H, FRAME_W, 3))
            try:
                state = await process_frame(frame, state, redis)
            except Exception as exc:   # one bad frame must not stop the analyzer
                log.error("Frame processing error: %s", type(exc).__name__)

            if CALIBRATE:
                break   # calibration shows one frame then exits

    except asyncio.IncompleteReadError:
        log.warning("RTSP stream ended (ffmpeg process exited)")
    except Exception as exc:
        log.error("RTSP analyzer error: %s", type(exc).__name__)
    finally:
        proc.terminate()
        await redis.aclose()
        log.info("RTSP analyzer stopped")

if __name__ == "__main__":
    asyncio.run(main())
