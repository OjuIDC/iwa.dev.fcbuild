"""
ocr_parsing.py
==============
Pure parsing, debounce and match-event logic shared by the Tier 2 RTSP
analyzer (src/rtsp_frame_analyzer.py) and the Tier 3 Windows capture agent
(host_agent/capture_agent.py).

This module is deliberately stdlib-only (no cv2 / numpy / pytesseract / redis)
so it can be unit-tested anywhere and imported by the Windows agent, which adds
the repository root to sys.path and does ``from src.ocr_parsing import ...``.

The I/O wrappers (frame capture, Tesseract, Redis publish) stay in the two
callers; everything that decides *whether* an event happened lives here.
"""

from __future__ import annotations

import difflib
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ── Tesseract configs ─────────────────────────────────────────────────────────
# The built-in "digits" config strips ':' — "67:32" would OCR as "6732"/"673".
# A whitelist that keeps ':' is required for the match clock.
TIMER_OCR_CONFIG = "--psm 7 -c tessedit_char_whitelist=0123456789:"
SCORE_OCR_CONFIG = "--psm 7 -c tessedit_char_whitelist=0123456789-"

MAX_CLOCK_MINUTES = 130   # 120 + generous stoppage time in extra time
MAX_SCORE = 30            # anything above this is an OCR artefact
KICKOFF_MAX_MINUTES = 0   # the clock only reads 0:xx right after kickoff
KICKOFF_PREV_MIN_MINUTES = 2  # a previous clock >= 2:00 means the clock went backwards

ORIENTATIONS = ("home", "away", "unknown")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# ── Parsing ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True, order=True)
class Clock:
    minutes: int
    seconds: int

    @property
    def total_seconds(self) -> int:
        return self.minutes * 60 + self.seconds

    def __str__(self) -> str:
        return f"{self.minutes:02d}:{self.seconds:02d}"


_CLOCK_RE = re.compile(r"^(\d{1,3})\s*:\s*(\d{2})$")


def parse_clock(text: Optional[str]) -> Optional[Clock]:
    """Parse a match clock reading such as '67:32'.

    Strict: requires a ':' separator, two-digit seconds 00-59 and minutes
    0-130. Anything else (including a colon-less '6732' or '673') is rejected
    rather than guessed, so 67:32 can never become 673 minutes.
    """
    if not text:
        return None
    cleaned = re.sub(r"\s+", "", text.strip())
    m = _CLOCK_RE.match(cleaned)
    if not m:
        return None
    minutes, seconds = int(m.group(1)), int(m.group(2))
    if not 0 <= seconds <= 59 or not 0 <= minutes <= MAX_CLOCK_MINUTES:
        return None
    return Clock(minutes, seconds)


_SCORE_RE = re.compile(r"^(\d{1,2})\s*[-–]\s*(\d{1,2})$")


def parse_score(text: Optional[str]) -> Optional[tuple[int, int]]:
    """Parse a scoreline such as '1 - 0' / '1-0' / '2 1'. Returns (home, away)."""
    if not text:
        return None
    cleaned = text.strip()
    m = _SCORE_RE.match(cleaned)
    if m:
        home, away = int(m.group(1)), int(m.group(2))
    else:
        parts = cleaned.split()
        if len(parts) != 2 or not all(p.isdigit() and len(p) <= 2 for p in parts):
            return None
        home, away = int(parts[0]), int(parts[1])
    if home > MAX_SCORE or away > MAX_SCORE:
        return None
    return home, away


def text_and_confidence(data: dict[str, Any]) -> tuple[str, Optional[float]]:
    """Reduce a pytesseract ``image_to_data(..., output_type=Output.DICT)`` result.

    Returns the joined text and the *minimum* confidence over recognised words
    (Tesseract reports -1 for non-word boxes; those are ignored). Returns
    ("", None) when nothing was recognised.
    """
    words: list[str] = []
    confs: list[float] = []
    for raw_text, raw_conf in zip(data.get("text", []), data.get("conf", [])):
        word = str(raw_text).strip()
        try:
            conf = float(raw_conf)
        except (TypeError, ValueError):
            conf = -1.0
        if not word or conf < 0:
            continue
        words.append(word)
        confs.append(conf)
    if not words:
        return "", None
    return " ".join(words), min(confs)


def _norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def names_match(ocr_name: str, club_name: str, threshold: float = 0.8) -> bool:
    a, b = _norm_name(ocr_name), _norm_name(club_name)
    if len(a) < 3 or len(b) < 3:
        return False
    if a == b or (len(a) >= 5 and a in b) or (len(b) >= 5 and b in a):
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= threshold


def resolve_orientation(mode: Optional[str], club_name: Optional[str] = None,
                        home_name: Optional[str] = None, away_name: Optional[str] = None) -> str:
    """Decide whether the monitored club is the HUD's home or away side.

    mode: 'home' | 'away' force the side (env FC27_HOME_AWAY); 'auto' (default)
    compares OCR'd HUD team names to FC27_CLUB_NAME. Returns 'unknown' when it
    cannot be established — callers must then emit neutral events only.
    """
    mode = (mode or "auto").strip().lower()
    if mode in ("home", "away"):
        return mode
    if not club_name:
        return "unknown"
    home_hit = bool(home_name) and names_match(home_name or "", club_name)
    away_hit = bool(away_name) and names_match(away_name or "", club_name)
    if home_hit and not away_hit:
        return "home"
    if away_hit and not home_hit:
        return "away"
    return "unknown"


# ── Debounce ──────────────────────────────────────────────────────────────────

class Debouncer:
    """Emit a value only after it was read ``frames`` consecutive times.

    A None (unparseable / low-confidence) reading breaks the streak. The same
    stable value is emitted once; it is emitted again only after a different
    value became stable in between.
    """

    def __init__(self, frames: int):
        self.frames = max(1, frames)
        self._candidate: Any = None
        self._count = 0
        self._last_emitted: Any = None

    def reset(self) -> None:
        self._candidate, self._count, self._last_emitted = None, 0, None

    @property
    def streak(self) -> int:
        return self._count

    @property
    def candidate(self) -> Any:
        return self._candidate

    def update(self, value: Any) -> Any:
        if value is None:
            self._candidate, self._count = None, 0
            return None
        if value == self._candidate:
            self._count += 1
        else:
            self._candidate, self._count = value, 1
        if self._count >= self.frames and value != self._last_emitted:
            self._last_emitted = value
            return value
        return None


# ── Match tracker ─────────────────────────────────────────────────────────────

@dataclass
class TrackerConfig:
    debounce_frames: int = 2
    min_confidence: float = 60.0
    # A stable but implausible scoreline (e.g. after missed frames) is adopted
    # silently — never as GOAL events — once stable for this many frames.
    resync_frames: int = 20
    state_every_minutes: int = 5

    @classmethod
    def from_env(cls) -> "TrackerConfig":
        return cls(
            debounce_frames=max(1, _env_int("OCR_DEBOUNCE_FRAMES", 2)),
            min_confidence=_env_float("OCR_MIN_CONFIDENCE", 60.0),
            resync_frames=max(2, _env_int("OCR_SCORE_RESYNC_FRAMES", 20)),
            state_every_minutes=max(1, _env_int("OCR_STATE_EVERY_MINUTES", 5)),
        )


@dataclass
class LiveMatchTracker:
    """State machine turning per-frame OCR readings into live match events.

    Events are plain dicts ``{"event": <TYPE>, ...}`` where TYPE is one of
    MATCH_START, GOAL, MATCH_STATE, MATCH_END. The caller publishes them.
    """

    config: TrackerConfig = field(default_factory=TrackerConfig)
    orientation: str = "unknown"
    home_team: str = "Home"
    away_team: str = "Away"
    home_score: int = 0
    away_score: int = 0
    clock: Optional[Clock] = None
    in_match: bool = False
    match_seq: int = 0
    events: list = field(default_factory=list)
    rejected_readings: int = 0
    frames_since_orientation: int = 0
    _emitted: set = field(default_factory=set)
    _last_state_minute: Optional[int] = None
    _resync_candidate: Optional[tuple[int, int]] = None
    _resync_count: int = 0

    def __post_init__(self) -> None:
        if self.orientation not in ORIENTATIONS:
            self.orientation = "unknown"
        self._score_db = Debouncer(self.config.debounce_frames)
        self._clock_db = Debouncer(self.config.debounce_frames)

    # -- helpers -------------------------------------------------------------
    @property
    def timer_str(self) -> str:
        return str(self.clock) if self.clock else "--:--"

    def score_str(self) -> str:
        return f"{self.home_score} - {self.away_score}"

    def set_orientation(self, orientation: str) -> None:
        self.orientation = orientation if orientation in ORIENTATIONS else "unknown"

    def _confident(self, conf: Optional[float]) -> bool:
        return conf is None or conf >= self.config.min_confidence

    def _emit(self, key: str, event: dict) -> list[dict]:
        """Duplicate suppression: one event per key per match."""
        full_key = f"{self.match_seq}:{key}"
        if full_key in self._emitted:
            return []
        self._emitted.add(full_key)
        if event.get("event") == "GOAL":
            self.events.append(event)   # per-match goal timeline
        return [event]

    def _reset_match(self) -> None:
        self.home_score = self.away_score = 0
        self.clock = None
        self.events = []
        self._emitted = set()
        self._last_state_minute = None
        self._resync_candidate, self._resync_count = None, 0
        self._score_db.reset()
        self._clock_db.reset()
        self.match_seq += 1

    def result(self) -> dict:
        """Result from the club's perspective when orientation is known, else neutral."""
        if self.home_score > self.away_score:
            neutral = "home_win"
        elif self.home_score < self.away_score:
            neutral = "away_win"
        else:
            neutral = "draw"
        out: dict[str, Any] = {"home_away_result": neutral, "orientation": self.orientation}
        if self.orientation == "unknown":
            out["result"] = "unknown"
            return out
        gf, ga = ((self.home_score, self.away_score) if self.orientation == "home"
                  else (self.away_score, self.home_score))
        out.update({"goals_for": gf, "goals_against": ga,
                    "result": "win" if gf > ga else "loss" if gf < ga else "tie"})
        return out

    # -- lifecycle -----------------------------------------------------------
    def start_match(self, reason: str = "hud_detected") -> list[dict]:
        """Kickoff: scores always reset to 0-0."""
        self._reset_match()
        self.in_match = True
        return self._emit("MATCH_START", {
            "event": "MATCH_START", "reason": reason,
            "home_team": self.home_team, "away_team": self.away_team,
            "orientation": self.orientation, "home_score": 0, "away_score": 0,
        })

    def end_match(self) -> list[dict]:
        if not self.in_match:
            return []
        self.in_match = False
        ev = {
            "event": "MATCH_END",
            "home_score": self.home_score, "away_score": self.away_score,
            "timer": self.timer_str, "events": list(self.events),
            **self.result(),
        }
        return self._emit("MATCH_END", ev)

    # -- per-frame -----------------------------------------------------------
    def observe(self, score: Optional[tuple[int, int]], score_conf: Optional[float] = None,
                clock: Optional[Clock] = None, clock_conf: Optional[float] = None) -> list[dict]:
        if not self.in_match:
            return []
        out: list[dict] = []
        # The clock ticks every second, so debounce on the *minute* value: a
        # reading is accepted once its minute was read N consecutive frames.
        clock_reading = clock if self._confident(clock_conf) else None
        self._clock_db.update(clock_reading.minutes if clock_reading else None)
        if (clock_reading is not None and self._clock_db.candidate == clock_reading.minutes
                and self._clock_db.streak >= self._clock_db.frames):
            out += self._accept_clock(clock_reading)
        score_reading = score if self._confident(score_conf) else None
        stable_score = self._score_db.update(score_reading)
        if stable_score is not None:
            out += self._accept_score(stable_score)
        elif score_reading is not None and score_reading == self._score_db.candidate:
            out += self._maybe_resync(score_reading)
        out += self._maybe_state()
        return out

    def _accept_clock(self, clock: Clock) -> list[dict]:
        out: list[dict] = []
        prev = self.clock
        if (prev is not None and clock.minutes <= KICKOFF_MAX_MINUTES
                and prev.minutes >= KICKOFF_PREV_MIN_MINUTES):
            # Clock went back to 0:xx — a new match kicked off without the HUD
            # disappearing (e.g. rematch). Close the old one, reset to 0-0.
            out += self.end_match()
            out += self.start_match(reason="kickoff_clock_reset")
        self.clock = clock
        return out

    def _goal(self, side: str) -> list[dict]:
        ev: dict[str, Any] = {
            "event": "GOAL", "team": side,
            "score": f"{self.home_score}-{self.away_score}",
            "home_score": self.home_score, "away_score": self.away_score,
            "timer": self.timer_str, "orientation": self.orientation,
        }
        if self.orientation == "unknown":
            ev["for_club"] = None   # neutral: never attributed to our club
        else:
            ev["for_club"] = side == self.orientation
        return self._emit(f"GOAL:{self.home_score}-{self.away_score}", ev)

    def _accept_score(self, score: tuple[int, int]) -> list[dict]:
        h, a = score
        if (h, a) == (self.home_score, self.away_score):
            self._resync_candidate, self._resync_count = None, 0
            return []
        if h == self.home_score + 1 and a == self.away_score:
            self.home_score = h
            self._resync_candidate, self._resync_count = None, 0
            return self._goal("home")
        if a == self.away_score + 1 and h == self.home_score:
            self.away_score = a
            self._resync_candidate, self._resync_count = None, 0
            return self._goal("away")
        # Implausible jump (e.g. 0-0 -> 7-0, a score decreasing, both sides changing).
        self.rejected_readings += 1
        self._resync_candidate, self._resync_count = (h, a), self._score_db.streak
        return []

    def _maybe_resync(self, score: tuple[int, int]) -> list[dict]:
        if self._resync_candidate != score:
            return []
        self._resync_count = self._score_db.streak
        if self._resync_count < self.config.resync_frames:
            return []
        self.home_score, self.away_score = score
        self._resync_candidate, self._resync_count = None, 0
        return self._emit(f"RESYNC:{score[0]}-{score[1]}", {
            "event": "MATCH_STATE", "reason": "score_resync",
            "home_score": self.home_score, "away_score": self.away_score,
            "timer": self.timer_str, "orientation": self.orientation,
        })

    def _maybe_state(self) -> list[dict]:
        # Never publish from the initial (no clock read yet) state.
        if self.clock is None or self.clock.minutes == 0:
            return []
        minute = self.clock.minutes
        if minute % self.config.state_every_minutes or minute == self._last_state_minute:
            return []
        self._last_state_minute = minute
        return self._emit(f"STATE:{minute}", {
            "event": "MATCH_STATE", "reason": "periodic",
            "home_score": self.home_score, "away_score": self.away_score,
            "timer": self.timer_str, "events_so_far": len(self.events),
            "orientation": self.orientation,
        })


def event_channel(event_type: str) -> str:
    """Redis channel for a tracker event (matches live_ws_server subscriptions)."""
    return f"fc27:live:{event_type.lower()}"


__all__: Iterable[str] = (
    "TIMER_OCR_CONFIG", "SCORE_OCR_CONFIG", "Clock", "parse_clock", "parse_score",
    "text_and_confidence", "names_match", "resolve_orientation", "Debouncer",
    "TrackerConfig", "LiveMatchTracker", "event_channel",
)
