import shutil
import subprocess

import pytest

from src.thread_ids import format_thread_id, is_valid_club_id, parse_thread_id


@pytest.mark.parametrize("value", ["330323", "0", "123456789012"])
def test_club_id_accepts_ascii_digits(value):
    assert is_valid_club_id(value)


@pytest.mark.parametrize("value", ["٣٣٠٣٢٣", "３３０３２３", "-330323", "+330323", " 330323", "330323 ",
                                   "330.323", "33a0323", "", "330323\n", "1234567890123", None, 330323])
def test_club_id_rejects_unicode_digits(value):
    assert not is_valid_club_id(value)


def test_thread_id_format_roundtrip():
    from datetime import date
    tid = format_thread_id("330323", date(2026, 10, 1), 7)
    assert tid == "fc27-330323-run-20261001-007"
    assert parse_thread_id(tid) == {"club": "330323", "date": "20261001", "seq": "007"}
    assert parse_thread_id(tid + "\n") is None


def test_tts_command_uses_option_terminator():
    from voice.tts_service import build_command
    cmd = build_command("-f /etc/passwd", "default", "/tmp/out.wav")
    assert cmd[-2:] == ["--", "-f /etc/passwd"]
    with pytest.raises(ValueError):
        build_command("hi", "--voice=evil", "/tmp/out.wav")


@pytest.mark.skipif(shutil.which("espeak-ng") is None, reason="espeak-ng not installed")
def test_tts_dash_prefixed_text_is_literal(tmp_path):
    from fastapi.testclient import TestClient
    from voice.tts_service import app
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET CONTENT " * 50)
    client = TestClient(app)
    r = client.post("/synthesize", json={"text": f"-f {secret}"})
    assert r.status_code == 200 and r.content[:4] == b"RIFF"
    # Spoken literally: the audio for the short literal string is far shorter
    # than reading the file would produce.
    file_read = subprocess.run(["espeak-ng", "-w", str(tmp_path / "f.wav"), "-f", str(secret)], capture_output=True)
    assert file_read.returncode == 0
    assert len(r.content) < (tmp_path / "f.wav").stat().st_size / 4
