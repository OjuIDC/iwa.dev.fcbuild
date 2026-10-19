from __future__ import annotations

from enum import Enum
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class EventType(str, Enum):
    MATCH_START = "match_start"
    PASS = "pass"
    SHOT = "shot"
    GOAL = "goal"
    TACKLE = "tackle"
    PRESSURE = "pressure"
    POSSESSION = "possession"
    CARD = "card"
    SUBSTITUTION = "substitution"
    MATCH_END = "match_end"


class Persona(str, Enum):
    ANALYST = "analyst"
    FAN = "fan"
    BROADCASTER = "broadcaster"


class DetailLevel(str, Enum):
    COMPACT = "compact"
    STANDARD = "standard"
    DEEP = "deep"


class MatchEvent(StrictModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()), min_length=8, max_length=80)
    match_id: str = Field(min_length=3, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    sequence: int = Field(ge=0)
    clock_ms: int = Field(ge=0, le=8_000_000)
    event_type: EventType
    team_id: str | None = Field(default=None, max_length=80)
    player_id: str | None = Field(default=None, max_length=80)
    target_player_id: str | None = Field(default=None, max_length=80)
    outcome: Literal["success", "failure", "on_target", "off_target", "blocked"] | None = None
    x: float | None = Field(default=None, ge=0, le=100)
    y: float | None = Field(default=None, ge=0, le=100)
    end_x: float | None = Field(default=None, ge=0, le=100)
    end_y: float | None = Field(default=None, ge=0, le=100)
    speed_kmh: float | None = Field(default=None, ge=0, le=250)
    pressure: float | None = Field(default=None, ge=0, le=1)
    source: Literal["synthetic"] = "synthetic"
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def event_requirements(self) -> MatchEvent:
        if self.event_type in {EventType.PASS, EventType.SHOT, EventType.GOAL, EventType.TACKLE}:
            if not self.team_id or not self.player_id:
                raise ValueError("team_id and player_id are required for this event type")
        if self.event_type == EventType.PASS and None in (self.x, self.y, self.end_x, self.end_y):
            raise ValueError("pass events require x, y, end_x, and end_y")
        return self


class AudienceProfile(StrictModel):
    profile_id: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9._:-]+$")
    persona: Persona
    locale: Literal["en", "es", "fr"] = "en"
    favorite_club: str | None = Field(default=None, max_length=80)
    favorite_player: str | None = Field(default=None, max_length=80)
    metric_interests: list[Literal["passing", "shooting", "pressing", "possession", "defending"]] = Field(default_factory=list, max_length=5)
    detail: DetailLevel = DetailLevel.STANDARD
    captions: bool = True
    high_contrast: bool = False


class Evidence(StrictModel):
    event_ids: list[str] = Field(min_length=1, max_length=12)
    inputs: dict[str, float | int | str]
    formula: str
    confidence: float = Field(ge=0, le=1)


class Insight(StrictModel):
    insight_id: str = Field(default_factory=lambda: str(uuid4()))
    match_id: str
    sequence: int
    clock_ms: int
    kind: Literal["goal", "shot_speed", "pass_quality", "pressing_surge", "possession_swing", "tackle", "recap"]
    severity: Literal["info", "notable", "major"]
    team_id: str | None = None
    player_id: str | None = None
    stats: dict[str, float | int | str]
    evidence: Evidence
    expires_in_ms: int = Field(default=12_000, ge=2_000, le=60_000)


class PersonalizedInsight(StrictModel):
    insight_id: str
    match_id: str
    profile_id: str
    locale: Literal["en", "es", "fr"]
    persona: Persona
    priority: float = Field(ge=0, le=1)
    title: str = Field(min_length=1, max_length=140)
    body: str = Field(min_length=1, max_length=800)
    overlay_text: str = Field(min_length=1, max_length=180)
    clock_ms: int
    sequence: int
    severity: Literal["info", "notable", "major"]
    evidence: Evidence
    stats: dict[str, float | int | str]
    expires_in_ms: int


class TeamState(StrictModel):
    team_id: str
    passes: int = 0
    completed_passes: int = 0
    shots: int = 0
    shots_on_target: int = 0
    goals: int = 0
    tackles: int = 0
    pressure_events: int = 0
    possession_pct: float = Field(default=50, ge=0, le=100)


class MatchState(StrictModel):
    match_id: str
    last_sequence: int = -1
    clock_ms: int = 0
    status: Literal["pending", "live", "finished"] = "pending"
    teams: dict[str, TeamState] = Field(default_factory=dict)
    event_count: int = 0
    duplicate_count: int = 0


class EventIngestResponse(StrictModel):
    accepted: bool
    duplicate: bool = False
    latency_ms: float = Field(ge=0)
    state: MatchState
    insights: list[PersonalizedInsight]
    warnings: list[str] = Field(default_factory=list)


class MatchRecap(StrictModel):
    match_id: str
    profile_id: str
    locale: Literal["en", "es", "fr"]
    headline: str
    bullets: list[str]
    evidence_event_ids: list[str]
    final_state: MatchState
