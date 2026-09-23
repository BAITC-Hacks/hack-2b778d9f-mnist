import json
from datetime import date
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader

from meeting_protocol.api import create_app
from meeting_protocol.audio import merge_speakers, sanitize_filename
from meeting_protocol.config import Settings
from meeting_protocol.deadlines import normalize_deadline
from meeting_protocol.exports import export_docx, export_pdf
from meeting_protocol.extraction import chunk_transcript, extract_meeting
from meeting_protocol.llm import LocalLLM, parse_json
from meeting_protocol.models import (
    DeadlineType,
    Extraction,
    MappingResult,
    Meeting,
    MeetingSummary,
    MeetingTask,
    ModelSelection,
    ProcessingStatus,
    SpeakerTurn,
    TranscriptSegment,
)
from meeting_protocol.processor import Processor
from meeting_protocol.profiles import ProfileError
from meeting_protocol.store import Store


@pytest.fixture
def transcript():
    return [
        TranscriptSegment.model_validate(s)
        for s in json.loads(
            (Path(__file__).parent / "fixtures/transcript.json").read_text(encoding="utf-8-sig")
        )
    ]


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None,
        public_host="127.0.0.1",
        agent_api_key="",
        data_root=tmp_path / "data",
        database_path=tmp_path / "app.db",
    )


def candidate(**changes):
    return dict(
        action="Подготовить отчет",
        assignee="Тимур",
        deadline_raw="до пятницы",
        deadline_type="relative",
        evidence_start=1,
        evidence_end=5,
        evidence_text="Тимур, подготовьте отчет до пятницы.",
        confidence=0.9,
        **changes,
    )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("30 сентября", "2026-09-30"),
        ("15 октября", "2026-10-15"),
        ("до пятницы", "2026-09-25"),
        ("к среде", "2026-09-23"),
        ("на этой неделе", "2026-09-27"),
        ("на следующей неделе", "2026-10-04"),
        ("через две недели", "2026-10-07"),
        ("завтра", "2026-09-24"),
    ],
)
def test_deadlines(raw, expected):
    result, _, review = normalize_deadline(raw, DeadlineType.relative, date(2026, 9, 23))
    assert str(result) == expected and not review


@pytest.mark.parametrize("raw", ["до пятницы", "30 сентября", "через две недели"])
def test_unknown_date(raw):
    assert normalize_deadline(raw, DeadlineType.relative, None)[::2] == (None, True)


def test_no_invented_deadline():
    assert normalize_deadline(None, DeadlineType.exact, None) == (None, DeadlineType.absent, False)
    assert (
        normalize_deadline("после совещания с подрядчиками", DeadlineType.event, date(2026, 9, 23))[
            0
        ]
        is None
    )
    assert normalize_deadline("31 февраля", DeadlineType.exact, date(2026, 1, 1))[2]
    assert normalize_deadline("30 сентября 2027", DeadlineType.exact, None)[0] == date(2027, 9, 30)


def test_json():
    text = json.dumps({"tasks": [candidate()]})
    assert parse_json("```json\n" + text + "\n```", Extraction).tasks[0].assignee == "Тимур"
    with pytest.raises(ValueError):
        parse_json("{invalid", Extraction)
    with pytest.raises(ValueError):
        parse_json('{"tasks": [], "execute": "shell"}', Extraction)


async def test_llm_fallback_repair(settings):
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if len(calls) == 1:
            return httpx.Response(400, json={"error": "unsupported response_format"})
        content = "{broken" if len(calls) == 2 else '{"tasks": [], "decisions": []}'
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    result = await LocalLLM(settings, httpx.MockTransport(handler)).extract_tasks(
        {"transcript": []}
    )
    assert result.tasks == [] and len(calls) == 3
    assert "response_format" not in calls[1]


async def test_llm_bounded_repair(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "invalid"}}]})

    with pytest.raises(ValueError):
        await LocalLLM(settings, httpx.MockTransport(handler)).extract_tasks({})
    assert len(calls) == 2


def test_chunking(transcript):
    chunks = chunk_transcript(transcript, 240)
    assert [s for chunk in chunks for s in chunk] == transcript
    assert len(chunks) > 1
    with pytest.raises(ValueError):
        chunk_transcript(transcript, 20)


def test_merge():
    segments = [
        TranscriptSegment(start=0, end=1, text="One"),
        TranscriptSegment(start=1, end=2, text="Two"),
        TranscriptSegment(start=5, end=6, text="Gap"),
    ]
    turns = [SpeakerTurn(start=0, end=1, speaker="a"), SpeakerTurn(start=1, end=3, speaker="b")]
    assert [s.speaker for s in merge_speakers(segments, turns)] == [
        "SPEAKER_00",
        "SPEAKER_01",
        "UNKNOWN",
    ]


class FakeLLM:
    async def extract_tasks(self, payload):
        return Extraction(tasks=[candidate(), candidate()])

    async def verify_tasks(self, payload):
        return Extraction(tasks=payload.get("tasks", []), decisions=payload.get("decisions", []))

    async def generate_summary(self, payload):
        return MeetingSummary(text="Обсудили отчет. Қазақша хат.")

    async def resolve_speakers(self, payload):
        return MappingResult()


async def test_extraction(transcript):
    meeting = await extract_meeting(
        Meeting(title="Test", meeting_date=date(2026, 9, 23), transcript=transcript), FakeLLM()
    )
    assert len(meeting.tasks) == 1
    assert meeting.tasks[0].deadline_normalized == date(2026, 9, 25)
    assert meeting.summary.text


async def test_unsupported_evidence(transcript):
    class Bad(FakeLLM):
        async def extract_tasks(self, payload):
            data = candidate()
            data["evidence_text"] = "Invented statement"
            return Extraction(tasks=[data])

    meeting = await extract_meeting(Meeting(title="Test", transcript=transcript), Bad())
    assert not meeting.tasks


@pytest.mark.parametrize(
    "filename", ["../../secret.mp3", r"C:\private\audio.mp3", "CON", "...", "a:bad?.wav"]
)
def test_filename(filename):
    cleaned = sanitize_filename(filename)
    assert cleaned and "/" not in cleaned and "\\" not in cleaned and ":" not in cleaned
    assert cleaned != "CON"


def test_api(settings, transcript):
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.get("/").status_code == 200
        response = client.post("/api/v1/meetings", json={"title": "Demo"})
        assert response.status_code == 201
        mid = response.json()["id"]
        assert client.post(f"/api/v1/meetings/{mid}/process").status_code == 400
        uploaded = client.post(
            f"/api/v1/meetings/{mid}/audio", files={"file": ("../../test.wav", b"fake audio")}
        )
        assert uploaded.status_code == 200
        assert "audio_path" not in uploaded.json()
        meeting = app.state.store.get(mid)
        meeting.transcript = transcript
        task = candidate()
        task["assignee"] = "SPEAKER_01"
        meeting.tasks = [MeetingTask(**task, meeting_id=mid)]
        meeting.status = ProcessingStatus.completed
        app.state.store.save(meeting)
        mapped = client.patch(
            f"/api/v1/meetings/{mid}/speaker-mappings",
            json=[{"speaker": "SPEAKER_01", "name": "Тимур"}],
        ).json()
        assert mapped["tasks"][0]["assignee_display"] == "Тимур"
        assert mapped["transcript"][1]["speaker_name"] == "Тимур"
        assert (
            client.patch(
                "/api/v1/tasks/" + meeting.tasks[0].id, json={"status": "completed"}
            ).status_code
            == 200
        )
        assert client.get(f"/api/v1/meetings/{mid}/export.docx").content[:2] == b"PK"
        assert client.get(f"/api/v1/meetings/{mid}/export.pdf").content[:4] == b"%PDF"
        assert (
            client.post(
                "/api/v1/meetings", json={"title": "x"}, headers={"Origin": "https://evil.example"}
            ).status_code
            == 403
        )


def test_auth(settings):
    settings.agent_api_key = "a-secret-long-enough"
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/api/v1/meetings").status_code == 401
        assert (
            client.get(
                "/api/v1/meetings", headers={"X-Agent-API-Key": settings.agent_api_key}
            ).status_code
            == 200
        )


def test_local_only():
    with pytest.raises(ValueError):
        Settings(_env_file=None, llm_base_url="https://api.example.com/v1")
    with pytest.raises(ValueError):
        Settings(_env_file=None, public_host="0.0.0.0", agent_api_key="")


def test_upload_limit(settings):
    settings.max_upload_bytes = 3
    with TestClient(create_app(settings)) as client:
        mid = client.post("/api/v1/meetings", json={"title": "Test"}).json()["id"]
        assert (
            client.post(
                f"/api/v1/meetings/{mid}/audio", files={"file": ("test.wav", b"1234")}
            ).status_code
            == 413
        )
        assert not list(settings.data_root.rglob("*.wav"))


def test_exports(transcript):
    meeting = Meeting(title="Қазақша кеңес Ә Ғ Қ Ң Ө Ұ Ү Һ І", transcript=transcript)
    doc = Document(BytesIO(export_docx(meeting)))
    assert meeting.title in "\n".join(p.text for p in doc.paragraphs)
    pdf = PdfReader(BytesIO(export_pdf(meeting)))
    assert "Қазақша" in "".join(p.extract_text() for p in pdf.pages)


async def test_processor(settings, transcript, monkeypatch):
    import meeting_protocol.processor as module

    normalized = []

    def normalize(source, target, executable):
        normalized.append(source)
        target.write_bytes(b"normalized")

    monkeypatch.setattr(module, "normalize_audio", normalize)
    monkeypatch.setattr(module.shutil, "which", lambda executable: executable)

    class ASR:
        def transcribe(self, path):
            return transcript

    class Diarizer:
        def diarize(self, path):
            return [SpeakerTurn(start=s.start, end=s.end, speaker=s.speaker) for s in transcript]

    store = Store(settings.database_path)
    meeting = Meeting(title="Full pipeline", meeting_date=date(2026, 9, 23))
    store.save(meeting)
    processor = Processor(settings, store, ASR(), Diarizer(), FakeLLM())
    await processor.run(meeting.id)  # Meeting IDs are not executable attempts, even with injected adapters.
    assert store.get(meeting.id).status == ProcessingStatus.created
    source = settings.data_root.parent / "source.wav"
    source.write_bytes(b"audio")
    store.attach_audio(meeting.id, source)
    selection = ModelSelection()
    snapshot = processor.catalog.resolve(selection)

    async def preflight(accepted):
        assert accepted == snapshot

    monkeypatch.setattr(processor.catalog, "preflight", preflight)
    attempt = store.admit_attempt(meeting.id, snapshot, selection,
                                  expected_body=store.get(meeting.id).model_dump_json(),
                                  expected_audio=source)
    await processor.run(attempt.id)
    await processor.run(attempt.id)  # A completed attempt is not claimed twice.
    result = store.get(meeting.id)
    assert result.status == ProcessingStatus.completed
    assert result.results_attempt_id == attempt.id
    assert store.get_attempt(attempt.id).snapshot == snapshot
    assert store.get_attempt(attempt.id).source_audio == source.resolve()
    assert normalized == [source.resolve()]
    assert result.transcript and result.summary.text and len(result.tasks) == 1
    assert Store(settings.database_path).get(meeting.id).summary == result.summary


def test_recover(settings):
    store = Store(settings.database_path)
    meeting = Meeting(title="Interrupted", status=ProcessingStatus.transcribing)
    store.save(meeting)
    store.recover()
    assert store.get(meeting.id).status == ProcessingStatus.failed


async def test_inferred_names_remain_correctable(transcript):
    from meeting_protocol.extraction import display_name
    from meeting_protocol.models import Participant, SpeakerMapping

    class Named(FakeLLM):
        async def resolve_speakers(self, payload):
            return MappingResult(
                mappings=[SpeakerMapping(speaker="SPEAKER_01", name=candidate()["assignee"], confidence=0.95)]
            )

    meeting = Meeting(
        title="Names", transcript=transcript, participants=[Participant(name=candidate()["assignee"])]
    )
    await extract_meeting(meeting, Named())
    assert meeting.tasks[0].assignee == "SPEAKER_01"
    mapping = next(m for m in meeting.speaker_mappings if m.speaker == "SPEAKER_01")
    mapping.name, mapping.manual = "Corrected participant", True
    assert display_name(meeting, meeting.tasks[0].assignee) == "Corrected participant"


async def test_missing_diarization_is_actionable(settings, monkeypatch):
    import meeting_protocol.processor as module
    from meeting_protocol import profiles

    settings.diarization_model_artifact = settings.data_root.parent / "missing-pipeline"
    store = Store(settings.database_path)
    meeting = Meeting(title="No model")
    store.save(meeting)
    source = settings.data_root.parent / "source.wav"
    source.write_bytes(b"audio")
    store.attach_audio(meeting.id, source)
    processor = Processor(settings, store)
    selection = ModelSelection()
    snapshot = processor.catalog.resolve(selection)
    # Skip unrelated ASR dependencies but run the real diarization artifact validation.
    monkeypatch.setattr(profiles, "_validate_ct2_artifact", lambda path: None)
    monkeypatch.setattr(profiles, "check_model_dependencies", lambda kind: None)
    monkeypatch.setattr(module.shutil, "which", lambda executable: executable)
    heavy_calls = []
    monkeypatch.setattr(module, "normalize_audio", lambda *args: heavy_calls.append(args))
    monkeypatch.setattr(processor.asr, "transcribe", lambda path: heavy_calls.append(path))
    with pytest.raises(ProfileError) as missing:
        await processor.catalog.preflight(snapshot)
    assert missing.value.code == "missing_files"
    assert missing.value.slot == "diarization"
    attempt = store.admit_attempt(meeting.id, snapshot, selection,
                                  expected_body=store.get(meeting.id).model_dump_json(),
                                  expected_audio=source)
    await processor.run(attempt.id)
    saved = store.get(meeting.id)
    assert saved.status == ProcessingStatus.failed
    assert saved.error == "Selected model setup is unavailable"
    assert store.get_attempt(attempt.id).status == "failed"
    assert heavy_calls == []
