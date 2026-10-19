from fastapi.testclient import TestClient

from matchpulse.service import create_app
from matchpulse.synthetic import generate_match


def test_health_and_profiles():
    with TestClient(create_app()) as client:
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["synthetic_only"] is True
        assert len(client.get("/v1/profiles").json()) == 4


def test_ingest_state_and_recap():
    with TestClient(create_app()) as client:
        events = list(generate_match(match_id="api-demo", seed=11, events=24))
        for event in events:
            response = client.post("/v1/events", json=event.model_dump(mode="json"))
            assert response.status_code == 202, response.text
            assert response.json()["latency_ms"] >= 0
        state = client.get("/v1/matches/api-demo/state")
        assert state.status_code == 200
        assert state.json()["status"] == "finished"
        recap = client.get("/v1/matches/api-demo/recap", params={"profile_id": "broadcaster-es"})
        assert recap.status_code == 200
        assert recap.json()["locale"] == "es"
        assert recap.json()["evidence_event_ids"]


def test_non_synthetic_source_is_rejected_at_schema_boundary():
    with TestClient(create_app()) as client:
        payload = {"match_id": "bad", "sequence": 0, "clock_ms": 0, "event_type": "match_start", "source": "real"}
        response = client.post("/v1/events", json=payload)
        assert response.status_code == 422


def test_out_of_order_event_returns_conflict():
    with TestClient(create_app()) as client:
        first = {"match_id": "order", "sequence": 2, "clock_ms": 1000, "event_type": "match_start"}
        second = {"match_id": "order", "sequence": 1, "clock_ms": 1200, "event_type": "match_end"}
        assert client.post("/v1/events", json=first).status_code == 202
        response = client.post("/v1/events", json=second)
        assert response.status_code == 409


def test_websocket_receives_personalized_overlay():
    with TestClient(create_app()) as client:
        with client.websocket_connect("/ws/matches/ws-demo?profile_id=fan-fr") as websocket:
            assert websocket.receive_json()["type"] == "welcome"
            event = {"match_id": "ws-demo", "sequence": 1, "clock_ms": 45000, "event_type": "shot",
                     "team_id": "Riverside-Athletic", "player_id": "RA-9", "outcome": "on_target",
                     "speed_kmh": 108, "source": "synthetic"}
            assert client.post("/v1/events", json=event).status_code == 202
            overlay = websocket.receive_json()
            assert overlay["locale"] == "fr"
            assert overlay["profile_id"] == "fan-fr"
            assert overlay["evidence"]["event_ids"]
