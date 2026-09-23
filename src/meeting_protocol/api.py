import asyncio
import hmac
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field, ValidationError

from .audio import SetupError, sanitize_filename
from .config import Settings
from .exports import export_docx, export_pdf
from .extraction import display_name
from .models import (
    Meeting,
    Model,
    ModelSelection,
    Participant,
    ProcessingStatus,
    SpeakerMapping,
    TaskStatus,
)
from .processor import Processor, check_audio_setup
from .profiles import ProfileError
from .runtime_lock import RuntimeLock
from .store import Store

ACTIVE = {
    ProcessingStatus.queued,
    ProcessingStatus.normalizing,
    ProcessingStatus.transcribing,
    ProcessingStatus.diarizing,
    ProcessingStatus.extracting,
}


class CreateMeeting(Model):
    title: str = Field(min_length=1, max_length=300)
    meeting_date: date | None = None
    participants: list[Participant] = Field(default_factory=list, max_length=100)
    model_selection: ModelSelection = Field(default_factory=ModelSelection)


class ModelSelectionPatch(Model):
    model_selection: ModelSelection = Field(default_factory=ModelSelection)


class ProcessBody(Model):
    model_selection: ModelSelection | None = None


class TaskPatch(Model):
    status: TaskStatus


class MappingPatch(Model):
    speaker: str
    name: str | None = Field(default=None, max_length=200)


def create_app(settings: Settings | None = None, processor_factory=Processor) -> FastAPI:
    settings = settings or Settings()
    store = Store(settings.database_path)
    processor = processor_factory(settings, store)
    jobs: set[asyncio.Task] = set()
    runtime_lock = RuntimeLock(settings.database_path)

    @asynccontextmanager
    async def lifespan(app):
        runtime_lock.acquire()
        try:
            store.recover()
            yield
        finally:
            try:
                if jobs:
                    await asyncio.gather(*jobs, return_exceptions=True)
            finally:
                runtime_lock.release()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.processor = store, processor

    @app.middleware("http")
    async def security(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            if settings.agent_api_key and not hmac.compare_digest(
                request.headers.get("X-Agent-API-Key", ""), settings.agent_api_key
            ):
                return JSONResponse(
                    {"detail": "Invalid or missing X-Agent-API-Key"}, status_code=401
                )
            # Same-origin browser writes only, including loopback deployments without a key.
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse(
                    {"detail": "Cross-origin requests are disabled"}, status_code=403
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    def get(mid):
        try:
            return store.get(mid)
        except KeyError:
            raise HTTPException(404, "Meeting not found") from None

    def editable(meeting):
        if meeting.status in ACTIVE:
            raise HTTPException(409, "Wait for processing to finish")

    def public(meeting):
        data = meeting.model_dump(mode="json")
        for task in data["tasks"]:
            task["assignee_display"] = display_name(meeting, task["assignee"])
            task["assigner_display"] = display_name(meeting, task["assigner"])
        for segment in data["transcript"]:
            segment["speaker_name"] = display_name(meeting, segment["speaker"])
        data["has_audio"] = store.audio(meeting.id) is not None
        data["attempts"] = public_attempts(meeting.id)
        return data

    def public_attempts(mid):
        return [
            {"attempt_id": item.id, "status": item.status,
             "bindings": item.snapshot.bindings.model_dump(mode="json")}
            for item in store.list_attempts(mid)
        ]

    def validate_selection(selection):
        # Compatibility is checked independently of local artifact availability.
        for slot in ("asr", "diarization", "extract_tasks", "verify_tasks", "resolve_speakers", "generate_summary"):
            profile_id = getattr(selection, slot)
            if profile_id is None:
                continue
            profile = processor.catalog.profiles.get(profile_id)
            if profile is None or slot not in profile.compatible_slots:
                raise HTTPException(422, "Invalid model selection")

    def profile_validation_error(request, exc):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)

    @app.exception_handler(ValidationError)
    async def pydantic_error(request: Request, exc: ValidationError):
        return profile_validation_error(request, exc)

    @app.exception_handler(RequestValidationError)
    async def request_validation_error(request: Request, exc: RequestValidationError):
        return profile_validation_error(request, exc)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/model-profiles")
    async def model_profiles():
        return processor.catalog.public_catalog()

    @app.post("/api/v1/meetings", status_code=201)
    async def create(body: CreateMeeting):
        validate_selection(body.model_selection)
        meeting = Meeting(**body.model_dump())
        store.save(meeting)
        return public(meeting)

    @app.get("/api/v1/meetings")
    async def meetings():
        return [
            {"id": m.id, "title": m.title, "meeting_date": m.meeting_date, "status": m.status}
            for m in store.list()
        ]

    @app.get("/api/v1/meetings/{mid}")
    async def detail(mid: str):
        return public(get(mid))

    @app.get("/api/v1/meetings/{mid}/transcript")
    async def transcript(mid: str):
        return public(get(mid))["transcript"]

    @app.get("/api/v1/meetings/{mid}/tasks")
    async def tasks(mid: str):
        return public(get(mid))["tasks"]

    @app.post("/api/v1/meetings/{mid}/audio")
    async def upload(mid: str, file: UploadFile):
        meeting = get(mid)
        editable(meeting)
        if meeting.status == ProcessingStatus.completed:
            raise HTTPException(409, "Create a new meeting to replace processed audio")
        previous_audio = store.audio(mid)
        expected_body = meeting.model_dump_json()
        folder = settings.data_root / meeting.id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / (str(uuid4()) + "_" + sanitize_filename(file.filename or "audio"))
        total = 0
        try:
            with target.open("wb") as stream:
                while block := await file.read(1024 * 1024):
                    total += len(block)
                    if total > settings.max_upload_bytes:
                        raise HTTPException(413, "Audio exceeds MAX_UPLOAD_BYTES")
                    stream.write(block)
            if not total:
                raise HTTPException(400, "Empty audio file")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        finally:
            await file.close()
        try:
            meeting = store.attach_audio(mid, target, expected_body=expected_body,
                                         expected_audio=previous_audio)
        except KeyError:
            target.unlink(missing_ok=True)
            raise HTTPException(404, "Meeting not found") from None
        except ValueError:
            target.unlink(missing_ok=True)
            raise HTTPException(409, "Meeting cannot replace audio now") from None
        return public(meeting)

    @app.get("/api/v1/meetings/{mid}/audio")
    async def audio(mid: str):
        meeting = get(mid)
        source = store.audio(mid)
        normalized = None
        if meeting.results_attempt_id and meeting.status == ProcessingStatus.completed and source:
            attempt = store.get_attempt(meeting.results_attempt_id)
            if attempt.status == "completed" and attempt.source_audio == source:
                normalized = settings.data_root / meeting.id / f"normalized-{attempt.id}.wav"
        source = normalized if normalized is not None and normalized.exists() else source
        if source is None or not source.exists():
            raise HTTPException(404, "Audio unavailable")
        return FileResponse(source)

    @app.post("/api/v1/meetings/{mid}/process", status_code=202)
    async def process(mid: str, body: ProcessBody | None = None):
        meeting = get(mid)
        editable(meeting)
        source = store.audio(mid)
        if source is None:
            raise HTTPException(400, "Upload audio first")
        selection = body.model_selection if body is not None and body.model_selection is not None else meeting.model_selection
        validate_selection(selection)
        try:
            check_audio_setup(source, settings.ffmpeg_path)
            snapshot = processor.catalog.resolve(selection)
            await asyncio.wait_for(processor.catalog.preflight(snapshot), timeout=4.0)
        except SetupError as exc:
            raise HTTPException(503, str(exc)) from None
        except ProfileError as exc:
            if exc.code in {"invalid_profile", "conflicting_server_models", "conflicting_server_alias"}:
                raise HTTPException(422, "Invalid model selection") from None
            safe_code = exc.code if exc.code in {
                "unavailable", "missing_files", "incompatible", "not_verified",
                "artifact_missing", "server_unavailable", "preflight_timeout"
            } else "setup_unavailable"
            raise HTTPException(503, {"code": safe_code, "message": "Selected model setup is unavailable"}) from None
        except TimeoutError:
            raise HTTPException(503, {"code": "preflight_timeout", "message": "Selected model setup is unavailable"}) from None
        try:
            attempt = store.admit_attempt(mid, snapshot, selection,
                                          expected_body=meeting.model_dump_json(), expected_audio=source)
        except KeyError:
            raise HTTPException(404, "Meeting not found") from None
        except ValueError as exc:
            message = str(exc)
            if message == "Upload an audio file first":
                raise HTTPException(400, message) from None
            raise HTTPException(409, "Meeting changed or processing is already active") from None
        job = asyncio.create_task(processor.run(attempt.id))
        jobs.add(job)
        job.add_done_callback(jobs.discard)
        return {"status": "queued", "attempt_id": attempt.id,
                "bindings": snapshot.bindings.model_dump(mode="json")}

    @app.patch("/api/v1/meetings/{mid}/model-selection")
    async def model_selection(mid: str, body: ModelSelectionPatch):
        meeting = get(mid)
        editable(meeting)
        validate_selection(body.model_selection)
        expected_body = meeting.model_dump_json()
        meeting.model_selection = body.model_selection
        try:
            store.save(meeting, expected_body=expected_body)
        except ValueError:
            raise HTTPException(409, "Meeting changed; refresh and try again") from None
        return public(meeting)

    @app.patch("/api/v1/meetings/{mid}/speaker-mappings")
    async def mappings(mid: str, body: list[MappingPatch]):
        meeting = get(mid)
        editable(meeting)
        expected_body = meeting.model_dump_json()
        known = {s.speaker for s in meeting.transcript}
        current = {m.speaker: m for m in meeting.speaker_mappings}
        for item in body:
            if item.speaker not in known:
                raise HTTPException(422, "Unknown transcript speaker")
            current[item.speaker] = SpeakerMapping(
                speaker=item.speaker, name=item.name or None, confidence=1, manual=True
            )
        meeting.speaker_mappings = list(current.values())
        try:
            store.save(meeting, expected_body=expected_body)
        except ValueError:
            raise HTTPException(409, "Meeting changed; refresh and try again") from None
        return public(meeting)

    @app.patch("/api/v1/tasks/{tid}")
    async def update_task(tid: str, body: TaskPatch):
        for meeting in store.list():
            for task in meeting.tasks:
                if task.id == tid:
                    editable(meeting)
                    expected_body = meeting.model_dump_json()
                    task.status = body.status
                    try:
                        store.save(meeting, expected_body=expected_body)
                    except ValueError:
                        raise HTTPException(409, "Meeting changed; refresh and try again") from None
                    return public(meeting)
        raise HTTPException(404, "Task not found")

    @app.get("/api/v1/meetings/{mid}/export.{kind}")
    async def export(mid: str, kind: str):
        meeting = get(mid)
        if meeting.status != ProcessingStatus.completed:
            raise HTTPException(409, "Complete processing before exporting")
        try:
            if kind == "docx":
                content = await asyncio.to_thread(export_docx, meeting)
                media = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            elif kind == "pdf":
                content = await asyncio.to_thread(export_pdf, meeting, settings.pdf_font_path)
                media = "application/pdf"
            else:
                raise HTTPException(404, "Unknown export format")
        except SetupError as exc:
            raise HTTPException(503, str(exc)) from None
        return Response(
            content,
            media_type=media,
            headers={"Content-Disposition": f'attachment; filename="protocol.{kind}"'},
        )

    app.mount("/", StaticFiles(directory=Path(__file__).parent / "web", html=True), name="web")
    return app
