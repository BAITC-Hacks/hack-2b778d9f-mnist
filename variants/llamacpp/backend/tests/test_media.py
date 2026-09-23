import subprocess
import wave
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from app.audio import transcribe_turns
from app.main import create_app
from app.process import process_recording
from app.store import Store


def test_mp3_upload_is_decoded_to_playable_wav_before_processing(tmp_path, short_wav):
    mp3 = tmp_path / "sample.mp3"
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(short_wav), str(mp3)], check=True)
    with TestClient(create_app(tmp_path / "db", tmp_path / "files")) as client:
        uploaded = client.post("/meetings", data={"meeting_date": "2026-09-23"},
                               files={"audio": ("sample.mp3", mp3.read_bytes(), "audio/mpeg")})
        assert uploaded.status_code == 201
        audio = client.get(f"/meetings/{uploaded.json()['id']}/audio")
        assert audio.headers["content-type"] == "audio/wav"
        with wave.open(BytesIO(audio.content)) as decoded:
            assert decoded.getframerate() == 16000
            assert decoded.getnchannels() == 1
            assert decoded.getnframes() > 0
        assert client.delete(f"/meetings/{uploaded.json()['id']}").status_code == 204


def test_fake_mp3_is_rejected_without_creating_record(tmp_path):
    with TestClient(create_app(tmp_path / "db", tmp_path / "files")) as client:
        response = client.post("/meetings", data={"meeting_date": "2026-09-23"},
                               files={"audio": ("fake.mp3", b"not audio", "audio/mpeg")})
        assert response.status_code == 422
        assert not list((tmp_path / "files").iterdir())


def test_missing_local_model_fails_without_downloading(short_wav, monkeypatch):
    monkeypatch.delenv("ASR_MODEL_PATH", raising=False)
    monkeypatch.delenv("ASR_MODEL_ARTIFACT", raising=False)
    with pytest.raises(RuntimeError, match="provisioned local"):
        transcribe_turns(short_wav)


def test_same_speaker_across_pauses_remains_identifiable(short_wav):
    result = process_recording(
        short_wav, "2026-09-23", [],
        lambda _: [{"start": 0, "end": 1, "text": "Аудит поручен"}],
        lambda _: [(0, 0.4, "s1"), (0.5, 0.95, "s1")],
        lambda *_: {"summary": "", "actions": []},
    )
    assert result["transcript"][0]["speaker_id"] == "s1"
    assert result["transcript"][0]["speaker_uncertain"] is False


def test_no_recognized_speech_is_visible_failure(short_wav):
    with pytest.raises(ValueError, match="No speech"):
        process_recording(short_wav, "2026-09-23", [], lambda _: [],
                          lambda _: [], lambda *_: {"summary": "", "actions": []})


def test_repetitive_asr_hallucination_never_reaches_extraction(short_wav):
    def should_not_extract(*args):
        raise AssertionError("Corrupt ASR must not become a protocol")
    with pytest.raises(ValueError, match="repetitive"):
        process_recording(
            short_wav, "2026-09-23", [],
            lambda _: [{"start": 0, "end": 1, "text": "сенат есеп " * 12}],
            lambda _: [(0, 1, "s1")], should_not_extract,
        )


def test_export_docx_works_without_pdf_converter(tmp_path, monkeypatch):
    store = Store(tmp_path / "db", tmp_path / "files")
    meeting_id = store.create("2026-09-23", [], b"audio", "")
    store.finish(meeting_id, {"summary": "Қауіпсіздік", "transcript": [], "actions": []})
    def no_converter(*args, **kwargs):
        raise FileNotFoundError("libreoffice")
    monkeypatch.setattr("app.export.subprocess.run", no_converter)
    with TestClient(create_app(tmp_path / "db", tmp_path / "files")) as client:
        partial = client.post(f"/meetings/{meeting_id}/export")
        assert partial.status_code == 200
        assert partial.json()["pdf"] is None
        assert "PDF" in partial.json()["warning"]
        assert client.get(partial.json()["docx"]).content.startswith(b"PK")
        docx = client.post(f"/meetings/{meeting_id}/export?format=docx")
        assert docx.status_code == 200
        assert docx.json()["pdf"] is None
        assert "warning" not in docx.json()
