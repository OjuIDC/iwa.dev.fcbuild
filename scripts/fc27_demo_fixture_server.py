"""Local Loopback Synthetic Fixture HTTP Server for East Coasters (FC 27).

Invariants enforced by this module:
- Binds strictly to IPv4 loopback `127.0.0.1` (wildcard `0.0.0.0`, `::`, and non-loopback hosts are rejected).
- Supports ephemeral port allocation (`port=0`).
- Exposes only fixed allowlisted FC 27 contract paths:
  - `/api/fc/clubs/info` (`CLUB_IDENTITY`)
  - `/api/fc/clubs/overallStats` (`CLUB_STATISTICS`)
  - `/api/fc/clubs/matches` (`RECENT_MATCHES` / `RECENT_LEAGUE_MATCHES` / `MATCH_PLAYER_STATISTICS`)
- Uses deterministic synthetic FC 27 data scoped to East Coasters (`330323`, `common-gen5`, `FC27`).
- Uses synthetic pre-pseudonymization player keys (`fixture_player_001` .. `fixture_player_006`) only;
  contains zero real player aliases, gamer tags, external player IDs, cookies, auth tokens, or browser headers.
- Retains zero request bodies or caller headers.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Final, Literal, Mapping, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOOPBACK_IPV4: Final[str] = "127.0.0.1"
TARGET_CLUB_ID: Final[str] = "330323"
TARGET_CLUB_NAME: Final[str] = "East Coasters"
TARGET_PLATFORM: Final[str] = "common-gen5"
TARGET_GAME_VERSION: Final[str] = "FC27"

FixtureScenario = Literal[
    "valid",
    "empty_history",
    "duplicates",
    "changed_replay_conflict",
    "malformed_json",
    "duplicate_json_keys",
    "schema_drift",
    "sensitive_field",
    "oversized_response",
    "timeout",
    "http_401",
    "http_403",
    "html_challenge",
    "redirect",
    "unknown_match_type",
    "missing_optional_metrics",
    "wrong_club",
    "wrong_platform",
    "wrong_game_version",
]

ALLOWED_FIXTURE_SCENARIOS: Final[Tuple[str, ...]] = (
    "valid",
    "empty_history",
    "duplicates",
    "changed_replay_conflict",
    "malformed_json",
    "duplicate_json_keys",
    "schema_drift",
    "sensitive_field",
    "oversized_response",
    "timeout",
    "http_401",
    "http_403",
    "html_challenge",
    "redirect",
    "unknown_match_type",
    "missing_optional_metrics",
    "wrong_club",
    "wrong_platform",
    "wrong_game_version",
)

UI_SAFE_FIXTURE_SCENARIOS: Final[Tuple[str, ...]] = (
    "valid",
    "empty_history",
    "duplicates",
)

FIXED_FIXTURE_PATHS: Final[frozenset[str]] = frozenset(
    {
        "/api/fc/clubs/info",
        "/api/fc/clubs/overallStats",
        "/api/fc/clubs/matches",
    }
)


def validate_loopback_bind_host(host: str) -> str:
    """Enforces that a server bind host is strictly `127.0.0.1`."""
    cleaned = str(host or "").strip()
    if cleaned != LOOPBACK_IPV4:
        raise ValueError(
            f"Loopback-only policy violation: bind host must be '{LOOPBACK_IPV4}', got '{host}'"
        )
    return LOOPBACK_IPV4


def validate_fixture_scenario(scenario: str) -> str:
    """Validates that the requested scenario is in ALLOWED_FIXTURE_SCENARIOS."""
    cleaned = str(scenario or "").strip()
    if cleaned not in ALLOWED_FIXTURE_SCENARIOS:
        raise ValueError(
            f"Unsupported fixture scenario '{scenario}'; allowed={ALLOWED_FIXTURE_SCENARIOS}"
        )
    return cleaned


def _build_synthetic_players(
    match_index: int,
    *,
    goals_for: int,
    missing_optional: bool = False,
) -> list[dict[str, Any]]:
    """Builds deterministic synthetic player rows using `fixture_player_001`..`004`."""
    base_specs = [
        ("fixture_player_001", "ST", max(0, goals_for - 1), 1 if goals_for >= 2 else 0, 8.1),
        ("fixture_player_002", "CAM", 1 if goals_for >= 1 else 0, max(0, goals_for - 1), 7.8),
        ("fixture_player_003", "CM", 0, 1 if goals_for >= 3 else 0, 7.4),
        ("fixture_player_004", "CB", 0, 0, 7.3),
    ]
    rows: list[dict[str, Any]] = []
    for p_idx, (p_key, pos, g_val, a_val, rating_val) in enumerate(base_specs):
        if missing_optional:
            rows.append(
                {
                    "memberKey": p_key,
                    "position": pos,
                    "minutesPlayed": 90,
                    "goals": g_val,
                    "assists": a_val,
                }
            )
        else:
            shots_val = max(g_val + 1, 2) if pos in ("ST", "CAM") else 1
            sot_val = max(g_val, 1)
            p_att = 24 + match_index + p_idx
            p_cmp = 20 + match_index + p_idx
            t_att = 5 + p_idx
            t_won = 4 + (p_idx % 2)
            rows.append(
                {
                    "memberKey": p_key,
                    "position": pos,
                    "minutesPlayed": 90,
                    "goals": g_val,
                    "assists": a_val,
                    "rating": round(rating_val - (0.1 if match_index % 3 == 0 else 0.0), 2),
                    "shots": shots_val,
                    "shotsOnTarget": min(sot_val, shots_val),
                    "passesCompleted": p_cmp,
                    "passesAttempted": p_att,
                    "tacklesWon": min(t_won, t_att),
                    "tacklesAttempted": t_att,
                    "interceptions": 2 + (p_idx % 2),
                    "saves": 0,
                    "eventBitmap": (1 if g_val > 0 else 0) | (2 if a_val > 0 else 0),
                }
            )
    return rows


def build_synthetic_club_identity_payload(scenario: str = "valid") -> dict[str, Any]:
    """Returns deterministic synthetic payload for `/api/fc/clubs/info`."""
    validate_fixture_scenario(scenario)
    club_id = "999999" if scenario == "wrong_club" else TARGET_CLUB_ID
    club_name = "Other Synthetic Club" if scenario == "wrong_club" else TARGET_CLUB_NAME
    platform = "common-gen4" if scenario == "wrong_platform" else TARGET_PLATFORM
    node: dict[str, Any] = {
        "clubId": club_id,
        "name": club_name,
        "platform": platform,
        "regionId": 41001,
        "teamId": 1024,
        "customKit": {
            "stadName": "East Coast Synthetic Arena",
            "crestAssetId": 5001,
            "customKitId": 2001,
            "selectedKitType": 1,
        },
    }
    if scenario == "wrong_game_version":
        node["gameVersion"] = "FC26"
    if scenario == "schema_drift":
        node["unexpectedUpstreamField"] = "drift_value"
    if scenario == "sensitive_field":
        node["ea_account_id"] = "999888777666"
    return {club_id: node}


def build_synthetic_club_statistics_payload(scenario: str = "valid") -> dict[str, Any]:
    """Returns deterministic synthetic payload for `/api/fc/clubs/overallStats`."""
    validate_fixture_scenario(scenario)
    if scenario == "empty_history":
        node: dict[str, Any] = {
            "clubId": TARGET_CLUB_ID,
            "gamesPlayed": 0,
            "wins": 0,
            "ties": 0,
            "losses": 0,
            "goals": 0,
            "goalsAgainst": 0,
            "cleanSheets": 0,
            "skillRating": 1000,
            "bestDivision": 5,
            "wstreak": 0,
            "unbeatenstreak": 0,
        }
    else:
        node = {
            "clubId": TARGET_CLUB_ID,
            "gamesPlayed": 6,
            "wins": 4,
            "ties": 1,
            "losses": 1,
            "goals": 13,
            "goalsAgainst": 5,
            "cleanSheets": 2,
            "skillRating": 1840,
            "bestDivision": 2,
            "wstreak": 2,
            "unbeatenstreak": 3,
        }
    if scenario == "schema_drift":
        node["unapprovedTelemetryMetric"] = 42
    if scenario == "sensitive_field":
        node["persona_id"] = "123456789"
    return {TARGET_CLUB_ID: node}


def build_synthetic_matches_payload(scenario: str = "valid") -> dict[str, Any]:
    """Returns deterministic synthetic payload for `/api/fc/clubs/matches`."""
    validate_fixture_scenario(scenario)
    identity_envelope = build_synthetic_club_identity_payload(
        scenario if scenario in ("wrong_club", "wrong_platform", "wrong_game_version") else "valid"
    )
    if scenario == "empty_history":
        return {**identity_envelope, "matches": []}

    missing_opt = scenario == "missing_optional_metrics"
    mtype_default = "mysteryCupMode99" if scenario == "unknown_match_type" else "leagueMatch"

    match_specs = [
        ("match_ecf_demo_001", "2026-10-29T20:00:00Z", "WIN", 3, 1, "4-3-3(2)", "Harbor Synthetic FC", "999101", 56.0, 11, 7),
        ("match_ecf_demo_002", "2026-10-29T18:00:00Z", "WIN", 2, 0, "4-3-3(2)", "Atlantic Synthetic XI", "999102", 54.5, 9, 5),
        ("match_ecf_demo_003", "2026-10-28T21:00:00Z", "DRAW", 1, 1, "4-2-3-1", "Harbor Synthetic FC", "999101", 50.0, 8, 4),
        ("match_ecf_demo_004", "2026-10-28T19:00:00Z", "LOSS", 1, 2, "4-2-3-1", "Bayfront Synthetic SC", "999103", 47.0, 7, 3),
        ("match_ecf_demo_005", "2026-10-27T21:00:00Z", "WIN", 3, 0, "4-3-3(2)", "Coastal Synthetic Athletic", "999104", 58.0, 12, 8),
        ("match_ecf_demo_006", "2026-10-27T19:00:00Z", "WIN", 3, 1, "4-3-3(2)", "Maritime Synthetic United", "999105", 55.0, 10, 6),
    ]

    matches: list[dict[str, Any]] = []
    for idx, (m_id, played_at, result, gf, ga, formation, opp_name, opp_id, poss, shots, sot) in enumerate(
        match_specs, start=1
    ):
        row: dict[str, Any] = {
            "matchId": m_id,
            "playedAt": played_at,
            "matchType": mtype_default,
            "result": result,
            "goalsFor": gf,
            "goalsAgainst": ga,
            "opponentName": opp_name,
            "opponentClubId": opp_id,
            "formation": formation,
            "players": _build_synthetic_players(idx, goals_for=gf, missing_optional=missing_opt),
        }
        if not missing_opt:
            row["possessionPct"] = poss
            row["shots"] = shots
            row["shotsOnTarget"] = sot
        if scenario == "schema_drift" and idx == 1:
            row["unexpectedMatchField"] = "schema_drift_detected"
        if scenario == "sensitive_field" and idx == 1:
            row["players"][0]["gamer_tag"] = "ProhibitedGamerTag99"
            row["players"][0]["nucleus_id"] = "nuc_999888777"
        matches.append(row)

    if scenario == "duplicates":
        # Append an exact duplicate of the first match
        matches.append(json.loads(json.dumps(matches[0])))
    elif scenario == "changed_replay_conflict":
        # Append a conflicting record with the same matchId but altered score
        conflicted = json.loads(json.dumps(matches[0]))
        conflicted["goalsFor"] = 5
        conflicted["goalsAgainst"] = 0
        matches.append(conflicted)

    return {**identity_envelope, "matches": matches}


class FC27DemoFixtureHTTPServer(ThreadingHTTPServer):
    """Loopback-only HTTP server serving deterministic synthetic FC 27 fixture payloads."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        *,
        bind_host: str = LOOPBACK_IPV4,
        port: int = 0,
        scenario: str = "valid",
        timeout_sleep_seconds: float = 0.35,
    ) -> None:
        validated_host = validate_loopback_bind_host(bind_host)
        validated_scenario = validate_fixture_scenario(scenario)
        if not isinstance(port, int) or isinstance(port, bool) or port < 0 or port > 65535:
            raise ValueError(f"Invalid port: {port}")
        self.scenario: str = validated_scenario
        self.timeout_sleep_seconds: float = float(timeout_sleep_seconds)
        self.request_count: int = 0
        self._lock = threading.Lock()
        super().__init__((validated_host, port), _FC27FixtureRequestHandler)

    @property
    def bind_host(self) -> str:
        return str(self.server_address[0])

    @property
    def bound_host(self) -> str:
        return str(self.server_address[0])

    @property
    def bound_port(self) -> int:
        return int(self.server_address[1])

    def set_scenario(self, scenario: str) -> None:
        with self._lock:
            self.scenario = validate_fixture_scenario(scenario)

    def increment_request_count(self) -> int:
        with self._lock:
            self.request_count += 1
            return self.request_count


class _FC27FixtureRequestHandler(BaseHTTPRequestHandler):
    """Fixed-route request handler with zero request body retention and zero cookies/sessions."""

    server_version = "FC27SyntheticFixture/1.0"
    sys_version = ""

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress raw HTTP access logging to stdout/stderr
        return

    def do_GET(self) -> None:  # noqa: N802
        srv: FC27DemoFixtureHTTPServer = self.server  # type: ignore[assignment]
        srv.increment_request_count()
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path

        if path not in FIXED_FIXTURE_PATHS:
            self._send_bytes(
                status=404,
                content_type="application/json",
                body=b'{"error":"NOT_FOUND"}',
            )
            return

        scenario = srv.scenario

        if scenario == "timeout":
            time.sleep(srv.timeout_sleep_seconds)

        if scenario == "redirect":
            self.send_response(302)
            self.send_header("Location", "https://example.invalid/redirect")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "22")
            self.end_headers()
            self.wfile.write(b'{"status":"redirect"}')
            return

        if scenario == "http_401":
            self._send_bytes(
                status=401,
                content_type="application/json",
                body=b'{"error":"UNAUTHORIZED"}',
            )
            return

        if scenario == "http_403":
            self._send_bytes(
                status=403,
                content_type="application/json",
                body=b'{"error":"FORBIDDEN"}',
            )
            return

        if scenario == "html_challenge":
            html_bytes = (
                b"<!DOCTYPE html><html><head><title>Security Challenge</title></head>"
                b"<body><h1>CAPTCHA Challenge Required</h1></body></html>"
            )
            self._send_bytes(
                status=200,
                content_type="text/html; charset=utf-8",
                body=html_bytes,
            )
            return

        if scenario == "malformed_json":
            self._send_bytes(
                status=200,
                content_type="application/json",
                body=b'{"330323": {"clubId": "330323", "name": "East Coasters"',
            )
            return

        if scenario == "duplicate_json_keys":
            dup_body = (
                b'{"330323": {"clubId": "330323", "clubId": "330323", '
                b'"name": "East Coasters", "platform": "common-gen5"}}'
            )
            self._send_bytes(
                status=200,
                content_type="application/json",
                body=dup_body,
            )
            return

        if scenario == "oversized_response":
            padding = "A" * 300_000
            big_body = json.dumps({"330323": {"clubId": "330323", "padding": padding}}).encode("utf-8")
            self._send_bytes(
                status=200,
                content_type="application/json",
                body=big_body,
            )
            return

        if path == "/api/fc/clubs/info":
            payload: Mapping[str, Any] = build_synthetic_club_identity_payload(scenario)
        elif path == "/api/fc/clubs/overallStats":
            payload = build_synthetic_club_statistics_payload(scenario)
        else:
            payload = build_synthetic_matches_payload(scenario)

        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status=200, content_type="application/json", body=encoded)

    def _send_bytes(self, *, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass


class EphemeralFixtureServerContext:
    """Context manager starting and stopping an `FC27DemoFixtureHTTPServer` on `127.0.0.1`."""

    def __init__(
        self,
        *,
        bind_host: str = LOOPBACK_IPV4,
        port: int = 0,
        scenario: str = "valid",
        timeout_sleep_seconds: float = 0.35,
    ) -> None:
        self._server = FC27DemoFixtureHTTPServer(
            bind_host=bind_host,
            port=port,
            scenario=scenario,
            timeout_sleep_seconds=timeout_sleep_seconds,
        )
        self._thread: Optional[threading.Thread] = None

    @property
    def server(self) -> FC27DemoFixtureHTTPServer:
        return self._server

    @property
    def host(self) -> str:
        return self._server.bind_host

    @property
    def port(self) -> int:
        return self._server.bound_port

    def start(self) -> FC27DemoFixtureHTTPServer:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.05},
                daemon=True,
            )
            self._thread.start()
        return self._server

    def stop(self) -> None:
        if self._thread is not None:
            self._server.shutdown()
            self._thread.join(timeout=3.0)
            self._thread = None
        self._server.server_close()

    def __enter__(self) -> FC27DemoFixtureHTTPServer:
        return self.start()

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the local loopback synthetic FC 27 fixture HTTP server."
    )
    parser.add_argument(
        "--host",
        default=LOOPBACK_IPV4,
        help="Loopback bind host (must be 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=0,
        help="Loopback TCP port (0 for ephemeral port).",
    )
    parser.add_argument(
        "--scenario",
        default="valid",
        choices=ALLOWED_FIXTURE_SCENARIOS,
        help="Deterministic synthetic fixture scenario.",
    )
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    validate_loopback_bind_host(args.host)
    with EphemeralFixtureServerContext(
        bind_host=args.host,
        port=args.port,
        scenario=args.scenario,
    ) as srv:
        print(f"FIXTURE_SERVER_STATUS=LISTENING")
        print(f"FIXTURE_BIND_HOST={srv.bind_host}")
        print(f"FIXTURE_BOUND_PORT={srv.bound_port}")
        print(f"FIXTURE_SCENARIO={srv.scenario}")
        sys.stdout.flush()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
