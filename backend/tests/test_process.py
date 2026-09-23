import sqlite3
import wave
import sys
import types
from threading import Event

from fastapi.testclient import TestClient

from app.main import create_app
from app.process import process_recording
from app.audio import transcribe_turns
from app.store import Store


def fake_asr(_wav):
    return [
        {"id": "t1", "start": 0.0, "end": 0.5, "text": "Предлагаю провести аудит."},
        {"id": "t2", "start": 0.5, "end": 1.0, "text": "Фиксируем: аудит до пятницы."},
    ]


def fake_diarizer(_wav):
    return [(0.0, 0.7, "speaker_0"), (0.3, 1.0, "speaker_1")]


def fake_extract(_turns, _date, _roster):
    return {"summary": "", "actions": []}


def test_overlap_stays_uncertain(short_wav):
    result = process_recording(
        short_wav, "2026-09-23", [], fake_asr, fake_diarizer, fake_extract
    )
    assert result["transcript"][0]["overlap"] is True
    assert result["transcript"][0]["speaker_id"] is None
    assert result["transcript"][1]["overlap"] is True
    assert result["transcript"][0]["speaker_uncertain"] is True


def test_sequential_speakers_are_uncertain_without_overlap(short_wav):
    result = process_recording(
        short_wav, "2026-09-23", [],
        lambda _: [{"start": 0.0, "end": 1.0, "text": "Реплика"}],
        lambda _: [(0.0, 0.5, "a"), (0.5, 1.0, "b")], fake_extract,
    )
    turn = result["transcript"][0]
    assert turn["overlap"] is False
    assert turn["speaker_uncertain"] is True
    assert turn["speaker_id"] is None


def test_incomplete_asr_timestamps_keep_text(short_wav, monkeypatch):
    monkeypatch.setenv("ASR_MODEL_PATH", str(short_wav.parent))
    chunks = [
        {"text": "Начало", "timestamp": (None, 0.4)},
        {"text": "Конец", "timestamp": (0.4, None)},
    ]
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False), float32="float32", float16="float16",
    ))
    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(
        AutoModelForSpeechSeq2Seq=types.SimpleNamespace(from_pretrained=lambda *a, **kw:
            types.SimpleNamespace(generation_config=types.SimpleNamespace(language="kk"))),
        AutoProcessor=types.SimpleNamespace(from_pretrained=lambda *a, **kw:
            types.SimpleNamespace(tokenizer=object(), feature_extractor=object())),
        pipeline=lambda *args, **kwargs: lambda *a, **kw: {"chunks": chunks}
    ))
    turns = transcribe_turns(short_wav)
    assert [turn["text"] for turn in turns] == ["Начало", "Конец"]
    assert [(turn["start"], turn["end"]) for turn in turns] == [(0.0, 0.4), (0.4, 1.0)]
    assert all(turn["timestamp_uncertain"] for turn in turns)


def test_asr_text_without_chunks_is_kept(short_wav, monkeypatch):
    monkeypatch.setenv("ASR_MODEL_PATH", str(short_wav.parent))
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(
        cuda=types.SimpleNamespace(is_available=lambda: False), float32="float32", float16="float16",
    ))
    monkeypatch.setitem(sys.modules, "transformers", types.SimpleNamespace(
        AutoModelForSpeechSeq2Seq=types.SimpleNamespace(from_pretrained=lambda *a, **kw:
            types.SimpleNamespace(generation_config=types.SimpleNamespace(language="kk"))),
        AutoProcessor=types.SimpleNamespace(from_pretrained=lambda *a, **kw:
            types.SimpleNamespace(tokenizer=object(), feature_extractor=object())),
        pipeline=lambda *args, **kwargs: lambda *a, **kw: {"text": "Важное решение"}
    ))
    turns = transcribe_turns(short_wav)
    assert turns[0]["text"] == "Важное решение"
    assert turns[0]["timestamp_uncertain"] is True
    assert (turns[0]["start"], turns[0]["end"]) == (0.0, 1.0)


def _wait_for_status(client, meeting_id, status):
    import time

    for _ in range(200):
        meeting = client.get(f"/meetings/{meeting_id}").json()
        if meeting["status"] == status:
            return meeting
        time.sleep(0.01)
    raise AssertionError(f"meeting did not reach {status}")


def test_process_persists_review_and_restart_marks_running_job_failed(tmp_path, permitted_wav):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    app = create_app(db, files, processor=lambda source, date, roster: process_recording(
        source, date, roster, fake_asr, fake_diarizer, fake_extract
    ))
    with TestClient(app) as client:
        created = client.post(
            "/meetings",
            data={"meeting_date": "2026-09-23", "roster": "[]"},
            files={"audio": ("recording.wav", permitted_wav, "audio/wav")},
        ).json()
        queued = client.post(f"/meetings/{created['id']}/process")
        assert queued.status_code == 202
        assert queued.json()["status"] in {"queued", "processing", "review"}
        reviewed = _wait_for_status(client, created["id"], "review")
        assert len(reviewed["transcript"]) == 2
        assert reviewed["transcript"][0]["overlap"] is True

    assert Store(db, files).get(created["id"])["transcript"] == reviewed["transcript"]
    with sqlite3.connect(db) as connection:
        connection.execute("UPDATE meetings SET status = 'processing' WHERE id = ?", (created["id"],))
    restarted = create_app(db, files, processor=lambda *_: {})
    with TestClient(restarted) as client:
        recovered = client.get(f"/meetings/{created['id']}").json()
        assert recovered["status"] == "failed"
        assert "interrupted" in recovered["error"].lower()


def test_long_audio_fails_before_processor_and_can_retry(tmp_path):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    invoked = []

    def processor(*args):
        invoked.append(args)
        def fail_extract(_turns, _date, _roster):
            raise RuntimeError("extractor failed")

        return process_recording(args[0], args[1], args[2], fake_asr, fake_diarizer, fail_extract)

    client = TestClient(create_app(db, files, processor=processor))
    long_wav = tmp_path / "long.wav"
    with wave.open(str(long_wav), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(1)
        wav.writeframes(b"\0\0" * 601)
    created = client.post(
        "/meetings",
        data={"meeting_date": "2026-09-23", "roster": "[]"},
        files={"audio": ("long.wav", long_wav.read_bytes(), "audio/wav")},
    )
    assert created.status_code == 422
    assert not invoked

    short = tmp_path / "short.wav"
    with wave.open(str(short), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    meeting = client.post(
        "/meetings",
        data={"meeting_date": "2026-09-23", "roster": "[]"},
        files={"audio": ("short.wav", short.read_bytes(), "audio/wav")},
    ).json()
    client.post(f"/meetings/{meeting['id']}/process")
    failed = _wait_for_status(client, meeting["id"], "failed")
    assert "extractor failed" in failed["error"]
    client.post(f"/meetings/{meeting['id']}/process")
    assert _wait_for_status(client, meeting["id"], "failed")["status"] == "failed"
    import time
    for _ in range(200):
        if len(invoked) == 2:
            break
        time.sleep(0.01)
    assert len(invoked) == 2


def test_patch_rejected_during_processing_and_retry_succeeds(tmp_path, permitted_wav):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    entered, release = Event(), Event()
    attempts = 0

    def processor(*_args):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            entered.set()
            assert release.wait(5)
            raise RuntimeError("temporary failure")
        return {"transcript": [], "summary": "Ready", "actions": []}

    with TestClient(create_app(db, files, processor=processor)) as client:
        meeting = client.post(
            "/meetings", data={"meeting_date": "2026-09-23"},
            files={"audio": ("demo.wav", permitted_wav, "audio/wav")},
        ).json()
        meeting_id = meeting["id"]
        client.post(f"/meetings/{meeting_id}/process")
        assert entered.wait(5)
        try:
            response = client.patch(f"/meetings/{meeting_id}", json={"summary": "My edit"})
            assert response.status_code == 409
        finally:
            release.set()
        _wait_for_status(client, meeting_id, "failed")
        client.post(f"/meetings/{meeting_id}/process")
        assert _wait_for_status(client, meeting_id, "review")["summary"] == "Ready"
        assert attempts == 2
