import asyncio
import hmac
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import Field

from .audio import SetupError, sanitize_filename
from .config import Settings
from .exports import export_docx, export_pdf
from .extraction import display_name
from .models import Meeting, Model, Participant, ProcessingStatus, SpeakerMapping, TaskStatus
from .processor import Processor
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

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        yield
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)

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
        return data

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/api/v1/meetings", status_code=201)
    async def create(body: CreateMeeting):
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
        # Recheck after awaits to prevent racing a newly started processing job.
        editable(get(mid))
        store.set_audio(mid, target)
        meeting.status, meeting.error = ProcessingStatus.uploaded, None
        store.save(meeting)
        return public(meeting)

    @app.get("/api/v1/meetings/{mid}/audio")
    async def audio(mid: str):
        meeting = get(mid)
        normalized = settings.data_root / meeting.id / "normalized.wav"
        source = (
            normalized if normalized.exists() and meeting.status not in ACTIVE else store.audio(mid)
        )
        if source is None or not source.exists():
            raise HTTPException(404, "Audio unavailable")
        return FileResponse(source)

    @app.post("/api/v1/meetings/{mid}/process", status_code=202)
    async def process(mid: str):
        meeting = get(mid)
        editable(meeting)
        if store.audio(mid) is None:
            raise HTTPException(400, "Upload audio first")
        meeting.status, meeting.error = ProcessingStatus.queued, None
        store.save(meeting)
        job = asyncio.create_task(processor.run(mid))
        jobs.add(job)
        job.add_done_callback(jobs.discard)
        return {"status": "queued"}

    @app.patch("/api/v1/meetings/{mid}/speaker-mappings")
    async def mappings(mid: str, body: list[MappingPatch]):
        meeting = get(mid)
        editable(meeting)
        known = {s.speaker for s in meeting.transcript}
        current = {m.speaker: m for m in meeting.speaker_mappings}
        for item in body:
            if item.speaker not in known:
                raise HTTPException(422, "Unknown transcript speaker")
            current[item.speaker] = SpeakerMapping(
                speaker=item.speaker, name=item.name or None, confidence=1, manual=True
            )
        meeting.speaker_mappings = list(current.values())
        store.save(meeting)
        return public(meeting)

    @app.patch("/api/v1/tasks/{tid}")
    async def update_task(tid: str, body: TaskPatch):
        for meeting in store.list():
            for task in meeting.tasks:
                if task.id == tid:
                    editable(meeting)
                    task.status = body.status
                    store.save(meeting)
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
