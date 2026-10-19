import json

import httpx

from matchpulse.foundry import FoundryNarrativeClient, FoundrySettings
from matchpulse.models import Evidence, PersonalizedInsight


def item():
    return PersonalizedInsight(insight_id="i", match_id="m", profile_id="fan", locale="en", persona="fan",
        priority=.8, title="Shot", body="A shot reached 101 km/h.", overlay_text="101 km/h shot",
        clock_ms=1, sequence=1, severity="notable", evidence=Evidence(event_ids=["e1"], inputs={"speed": 101},
        formula="measured speed", confidence=.9), stats={"speed_kmh": 101}, expires_in_ms=12000)


def test_foundry_guard_rejects_new_numeric_claim(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None
        def json(self):
            return {"choices": [{"message": {"content": json.dumps({"title": "Shot", "body": "A 130 km/h shot", "overlay_text": "130 km/h"})}}]}
    monkeypatch.setattr(httpx, "post", lambda *args, **kwargs: Response())
    source = item()
    output, warning = FoundryNarrativeClient(FoundrySettings("https://example.test", "model", "secret")).rewrite(source)
    assert output == source
    assert warning and "ValueError" in warning
