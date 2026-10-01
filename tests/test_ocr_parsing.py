"""Shared OCR parsing / debounce / plausibility logic (src/ocr_parsing.py)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from src import ocr_parsing as op
from src.ocr_parsing import Clock, LiveMatchTracker, TrackerConfig

ROOT = Path(__file__).resolve().parents[1]


def tracker(**kw) -> LiveMatchTracker:
    cfg = TrackerConfig(debounce_frames=kw.pop("debounce", 2), min_confidence=kw.pop("min_conf", 60.0),
                        resync_frames=kw.pop("resync", 20))
    t = LiveMatchTracker(config=cfg, orientation=kw.pop("orientation", "unknown"))
    t.start_match()
    return t


def feed(t, score=None, clock=None, n=1, score_conf=95.0, clock_conf=95.0):
    out = []
    for _ in range(n):
        out += t.observe(score, score_conf, clock, clock_conf)
    return out


def kinds(events, kind):
    return [e for e in events if e["event"] == kind]


# ── clock parsing ────────────────────────────────────────────────────────────

def test_ocr_parses_67_32():
    assert op.parse_clock("67:32") == Clock(67, 32)
    assert op.parse_clock(" 67 : 32 ") == Clock(67, 32)
    assert str(op.parse_clock("5:07")) == "05:07"
    # What the old 'digits' config produced — must never be read as 673 minutes.
    assert op.parse_clock("6732") is None
    assert op.parse_clock("673") is None
    assert ":" in op.TIMER_OCR_CONFIG and "tessedit_char_whitelist=0123456789:" in op.TIMER_OCR_CONFIG
    assert "digits" not in op.TIMER_OCR_CONFIG.split()


def test_ocr_rejects_invalid_seconds():
    for bad in ["67:60", "67:99", "67:7", "67:3a", "131:00", "999:10", ":32", "", None, "67:32:10"]:
        assert op.parse_clock(bad) is None, bad
    assert op.parse_clock("130:59") == Clock(130, 59)
    assert op.parse_clock("0:00") == Clock(0, 0)


def test_ocr_score_parsing_bounds():
    assert op.parse_score("1 - 0") == (1, 0)
    assert op.parse_score("2-1") == (2, 1)
    assert op.parse_score("2 1") == (2, 1)
    assert op.parse_score("71 - 0") is None
    assert op.parse_score("1 - 0 - 3") is None
    assert op.parse_score("") is None


def test_ocr_text_and_confidence_uses_min_word_conf():
    data = {"text": ["", "67:32", ""], "conf": ["-1", "88.5", -1]}
    assert op.text_and_confidence(data) == ("67:32", 88.5)
    assert op.text_and_confidence({"text": ["1", "-", "0"], "conf": [90, 40, 95]}) == ("1 - 0", 40.0)
    assert op.text_and_confidence({"text": [""], "conf": [-1]}) == ("", None)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract binary not installed")
def test_ocr_real_tesseract_keeps_colon_with_whitelist():
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    pytesseract = pytest.importorskip("pytesseract")
    img = np.full((80, 260), 255, dtype=np.uint8)
    cv2.putText(img, "67:32", (20, 60), cv2.FONT_HERSHEY_SIMPLEX, 2.0, 0, 4)
    data = pytesseract.image_to_data(img, config=op.TIMER_OCR_CONFIG, output_type=pytesseract.Output.DICT)
    text, conf = op.text_and_confidence(data)
    assert op.parse_clock(text) == Clock(67, 32), (text, conf)


# ── debounce / duplicate suppression ─────────────────────────────────────────

def test_ocr_debounces_duplicate_events():
    t = tracker(debounce=3)
    ev = feed(t, (1, 0), n=2)                     # not yet stable
    ev += feed(t, None)                           # unreadable frame breaks the streak
    ev += feed(t, (1, 0), n=2)
    assert kinds(ev, "GOAL") == []
    ev += feed(t, (1, 0), n=5)                    # stable for 3+ frames → exactly one goal
    goals = kinds(ev, "GOAL")
    assert len(goals) == 1 and goals[0]["team"] == "home" and (t.home_score, t.away_score) == (1, 0)
    # Flicker back to 0-0 and again 1-0 must not produce a second GOAL.
    ev2 = feed(t, (0, 0), n=4) + feed(t, (1, 0), n=4) + feed(t, (1, 0), n=10)
    assert kinds(ev2, "GOAL") == []
    assert (t.home_score, t.away_score) == (1, 0)


def test_ocr_debouncer_unit():
    d = op.Debouncer(2)
    assert [d.update(v) for v in ["a", "a", "a", None, "a", "a", "b", "b"]] == [None, "a", None, None, None, None, None, "b"]


def test_ocr_low_confidence_readings_rejected():
    t = tracker(min_conf=70)
    ev = feed(t, (1, 0), n=10, score_conf=40.0)
    assert kinds(ev, "GOAL") == [] and t.home_score == 0
    ev = feed(t, None, Clock(55, 0), n=5, clock_conf=10.0)
    assert t.clock is None
    ev = feed(t, (1, 0), n=2, score_conf=71.0)
    assert len(kinds(ev, "GOAL")) == 1


# ── plausibility ─────────────────────────────────────────────────────────────

def test_ocr_implausible_score_jump_rejected():
    t = tracker(resync=8)
    ev = feed(t, (7, 0), n=6)
    assert kinds(ev, "GOAL") == [] and (t.home_score, t.away_score) == (0, 0)
    assert t.rejected_readings == 1
    # Both sides changing at once or a score decreasing are rejected too.
    t2 = tracker()
    feed(t2, (1, 0), n=2)
    ev = feed(t2, (2, 1), n=3) + feed(t2, (0, 0), n=3)
    assert kinds(ev, "GOAL") == [] and (t2.home_score, t2.away_score) == (1, 0)
    # A persistently stable implausible score is adopted silently (no GOAL events).
    ev = feed(t, (7, 0), n=10)
    assert kinds(ev, "GOAL") == []
    resync = [e for e in ev if e.get("reason") == "score_resync"]
    assert len(resync) == 1 and (t.home_score, t.away_score) == (7, 0)


def test_ocr_one_goal_per_accepted_change():
    t = tracker()
    ev = feed(t, (1, 0), n=2) + feed(t, (1, 1), n=2) + feed(t, (2, 1), n=2)
    assert [g["team"] for g in kinds(ev, "GOAL")] == ["home", "away", "home"]
    assert [g["score"] for g in kinds(ev, "GOAL")] == ["1-0", "1-1", "2-1"]


# ── kickoff reset ────────────────────────────────────────────────────────────

def test_ocr_kickoff_resets_score():
    t = tracker()
    feed(t, (1, 0), Clock(30, 0), n=2)
    feed(t, (2, 0), Clock(70, 0), n=2)
    assert (t.home_score, t.away_score) == (2, 0)
    # New kickoff: clock returns to 0:xx (HUD never disappeared).
    ev = feed(t, (0, 0), Clock(0, 5), n=3)
    assert [e["event"] for e in ev][:2] == ["MATCH_END", "MATCH_START"]
    assert ev[0]["home_score"] == 2
    assert (t.home_score, t.away_score) == (0, 0) and t.events == []
    # The next real goal of the new match is accepted (not suppressed as a duplicate).
    ev = feed(t, (1, 0), Clock(3, 0), n=2)
    assert len(kinds(ev, "GOAL")) == 1
    # start_match itself always resets to 0-0.
    t.home_score, t.away_score = 4, 4
    start = t.start_match()
    assert start[0]["home_score"] == 0 and (t.home_score, t.away_score) == (0, 0)


def test_ocr_second_half_clock_is_not_a_kickoff():
    t = tracker()
    feed(t, (1, 0), Clock(44, 50), n=2)
    ev = feed(t, (1, 0), Clock(45, 0), n=3)
    assert kinds(ev, "MATCH_START") == [] and t.home_score == 1


# ── MATCH_STATE ──────────────────────────────────────────────────────────────

def test_ocr_initial_state_does_not_publish_every_frame():
    t = tracker()
    ev = feed(t, (0, 0), None, n=30)                  # initial state, no clock read yet
    ev += feed(t, (0, 0), Clock(0, 0), n=30)          # 00:00 must not spam MATCH_STATE
    assert kinds(ev, "MATCH_STATE") == []
    ev = feed(t, (0, 0), Clock(5, 0), n=10) + feed(t, (0, 0), Clock(5, 1), n=10)
    assert len(kinds(ev, "MATCH_STATE")) == 1
    ev = feed(t, (0, 0), Clock(6, 0), n=5) + feed(t, (0, 0), Clock(10, 0), n=5)
    assert len(kinds(ev, "MATCH_STATE")) == 1


# ── orientation ──────────────────────────────────────────────────────────────

def test_ocr_unknown_orientation_does_not_attribute_goals():
    t = tracker(orientation="unknown")
    ev = feed(t, (0, 1), n=2)
    goal = kinds(ev, "GOAL")[0]
    assert goal["team"] == "away" and goal["for_club"] is None and goal["orientation"] == "unknown"
    end = t.end_match()[0]
    assert end["result"] == "unknown" and end["home_away_result"] == "away_win"
    assert "goals_for" not in end and "goals_against" not in end


def test_ocr_known_away_orientation_attributes_correctly():
    t = tracker(orientation="away")
    goal = kinds(feed(t, (0, 1), n=2), "GOAL")[0]
    assert goal["for_club"] is True
    end = t.end_match()[0]
    assert end["result"] == "win" and end["goals_for"] == 1 and end["goals_against"] == 0


def test_ocr_resolve_orientation():
    assert op.resolve_orientation("home") == "home"
    assert op.resolve_orientation("AWAY") == "away"
    assert op.resolve_orientation("auto", "", "Anything", "Else") == "unknown"
    assert op.resolve_orientation("auto", "East Coasters FC", "EAST COASTERS", "Rivals United") == "home"
    assert op.resolve_orientation("auto", "East Coasters FC", "Rivals United", "East Coasters F.C.") == "away"
    assert op.resolve_orientation("auto", "East Coasters FC", "", "") == "unknown"
    assert op.resolve_orientation("auto", "East Coasters FC", "East Coasters", "East Coasters") == "unknown"


# ── wiring: RTSP analyzer and Windows capture agent use the shared logic ────

class _PubRedis:
    def __init__(self):
        self.published = []

    async def publish(self, channel, message):
        self.published.append((channel, json.loads(message)))


def _drive(module, monkeypatch, readings):
    """Feed (score_text, timer_text) OCR readings through module.process_frame."""
    import numpy as np
    seq = iter([r for pair in readings for r in pair])
    monkeypatch.setattr(module, "_ocr_conf", lambda region, config: (next(seq), 95.0))
    monkeypatch.setattr(module, "_ocr", lambda region, config="": "")
    for name in ("_detect_match_active", "_hud_active"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda frame: True)
    redis = _PubRedis()
    state = module.new_state()
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    for _ in readings:
        state = asyncio.run(module.process_frame(frame, state, redis))
    return state, redis.published


def _load_capture_agent():
    spec = importlib.util.spec_from_file_location("capture_agent_under_test", ROOT / "host_agent" / "capture_agent.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("which", ["rtsp", "capture"])
def test_ocr_frame_processors_use_shared_tracker(which, monkeypatch):
    pytest.importorskip("cv2")
    if which == "rtsp":
        import src.rtsp_frame_analyzer as module
    else:
        module = _load_capture_agent()
        assert module.parse_clock is op.parse_clock or module.parse_clock.__module__.endswith("ocr_parsing")
    monkeypatch.setattr(module, "CALIBRATE", False, raising=False)
    readings = [("0 - 0", "67:30")] * 3 + [("7 - 0", "67:31")] * 3 + [("1 - 0", "67:32")] * 3
    state, published = _drive(module, monkeypatch, readings)
    goals = [p for c, p in published if c == "fc27:live:goal"]
    assert len(goals) == 1 and goals[0]["home_score"] == 1 and goals[0]["for_club"] is None
    assert state.clock == Clock(67, 32)
    starts = [p for c, p in published if c == "fc27:live:match_start"]
    assert len(starts) == 1 and all(p["fc_version"] == "27" and p["platform"] == "common-gen5" for _, p in published)


def test_ocr_modules_do_not_log_redis_url():
    import re
    for path in ["src/rtsp_frame_analyzer.py", "host_agent/capture_agent.py"]:
        src = (ROOT / path).read_text()
        assert not re.search(r"log\.\w+\([^)]*REDIS_URL", src, re.S), path
        assert '"--psm 7 digits"' not in src, path
