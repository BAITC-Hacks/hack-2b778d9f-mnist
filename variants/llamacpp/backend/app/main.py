import json
import tempfile
import subprocess
import wave
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from datetime import date
from pathlib import Path
from typing import Callable

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.store import Store
from app.audio import diarize_turns, prepare_audio, transcribe_turns
from app.process import process_recording
from app.extract import extract_draft, llama_chat
from app.export import export_draft
from app.review import validate_review

MAX_AUDIO_BYTES = 200 * 1024 * 1024
MAX_WAV_SECONDS = 600
WAV_READ_CHUNK_BYTES = 1024 * 1024


def _valid_audio(audio_file) -> bool:
    try:
        audio_file.seek(0)
        with wave.open(audio_file, "rb") as wav:
            frame_rate = wav.getframerate()
            frame_count = wav.getnframes()
            frame_size = wav.getnchannels() * wav.getsampwidth()
            if frame_rate <= 0 or frame_count <= 0 or frame_count > MAX_WAV_SECONDS * frame_rate:
                return False
            if frame_size <= 0 or frame_size > WAV_READ_CHUNK_BYTES:
                return False
            while frame_count:
                frames = min(frame_count, WAV_READ_CHUNK_BYTES // frame_size)
                if len(wav.readframes(frames)) != frames * frame_size:
                    return False
                frame_count -= frames
        return True
    except (wave.Error, EOFError):
        return False


def _editable_changes(changes: object) -> dict:
    if not isinstance(changes, dict) or not changes:
        raise HTTPException(status_code=422, detail="Expected a non-empty JSON object")
    allowed = {"roster", "transcript", "summary", "actions", "speaker_names"}
    if set(changes) - allowed:
        raise HTTPException(status_code=422, detail="Only roster, transcript, summary and actions are editable")
    for key in ("roster", "transcript", "actions"):
        if key in changes and (
            not isinstance(changes[key], list)
            or (key == "roster" and any(not isinstance(person, str) for person in changes[key]))
        ):
            raise HTTPException(status_code=422, detail=f"{key} must be a list")
    if "summary" in changes and not isinstance(changes["summary"], str):
        raise HTTPException(status_code=422, detail="summary must be a string")
    if "speaker_names" in changes and (
        not isinstance(changes["speaker_names"], dict)
        or any(not isinstance(key, str) or not isinstance(value, str)
               for key, value in changes["speaker_names"].items())
    ):
        raise HTTPException(status_code=422, detail="speaker_names must map speakers to names")
    return changes


def create_app(db_path: Path, storage_dir: Path, processor: Callable | None = None) -> FastAPI:
    app = FastAPI()
    store = Store(db_path, storage_dir)
    worker = ThreadPoolExecutor(max_workers=1)
    in_flight: set[str] = set()
    in_flight_lock = Lock()

    def run_job(meeting_id: str) -> None:
        try:
            store.set_status(meeting_id, "processing")
            meeting = store.get(meeting_id)
            source = store.audio_file(meeting_id)
            if processor is not None:
                result = processor(source, meeting["meeting_date"], meeting["roster"])
            else:
                with tempfile.TemporaryDirectory() as temporary:
                    wav = Path(temporary) / "model-input.wav"
                    prepare_audio(source, wav)
                    speaker_intervals = diarize_turns(wav)
                    if not speaker_intervals:
                        raise ValueError("No speech detected; inspect the recording")
                    result = process_recording(
                        wav, meeting["meeting_date"], meeting["roster"],
                        lambda _: transcribe_turns(wav, speaker_intervals),
                        lambda _: speaker_intervals,
                        lambda turns, day, roster: extract_draft(turns, day, roster, llama_chat),
                    )
            # Validate machine output at the same persisted seam as human edits.
            # Use its own transcript as the immutable timestamp baseline.
            result = validate_review({**meeting, **result}, result)
            store.finish(meeting_id, result)
        except Exception as error:
            store.set_status(meeting_id, "failed", str(error)[:1000])
        finally:
            with in_flight_lock:
                in_flight.discard(meeting_id)

    @app.on_event("startup")
    def recover_jobs():
        store.mark_interrupted()

    @app.on_event("shutdown")
    def stop_worker():
        worker.shutdown(wait=True, cancel_futures=False)

    @app.post("/meetings", status_code=201)
    def create_meeting(
        meeting_date: str = Form(...),
        roster: str = Form("[]"),
        audio: UploadFile = File(...),
    ):
        try:
            parsed_date = date.fromisoformat(meeting_date)
            if parsed_date.isoformat() != meeting_date:
                raise ValueError
            parsed_roster = json.loads(roster)
            if not isinstance(parsed_roster, list) or any(not isinstance(person, str) for person in parsed_roster):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            raise HTTPException(status_code=422, detail="Invalid meeting date or roster") from None
        audio.file.seek(0, 2)
        audio_size = audio.file.tell()
        audio.file.seek(0)
        if audio_size == 0 or audio_size > MAX_AUDIO_BYTES:
            raise HTTPException(status_code=422, detail="Upload a non-empty WAV or MP3, up to 200 MiB and 10 minutes")
        header = audio.file.read(12)
        audio.file.seek(0)
        if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
            if not _valid_audio(audio.file):
                raise HTTPException(status_code=422, detail="Invalid or truncated WAV, or duration exceeds 10 minutes")
            audio.file.seek(0)
            audio_bytes = audio.file.read(MAX_AUDIO_BYTES + 1)
        else:
            # Validate and normalize MP3 locally before allocating any model.
            with tempfile.TemporaryDirectory() as temporary:
                source = Path(temporary) / "upload.mp3"
                normalized = Path(temporary) / "recording.wav"
                source.write_bytes(audio.file.read(MAX_AUDIO_BYTES + 1))
                try:
                    prepare_audio(source, normalized, allowed_formats={"mp3"})
                except ValueError as error:
                    raise HTTPException(status_code=422, detail=str(error)) from error
                except (OSError, subprocess.TimeoutExpired) as error:
                    raise HTTPException(status_code=503, detail="Local audio decoder unavailable") from error
                audio_bytes = normalized.read_bytes()
        meeting_id = store.create(parsed_date.isoformat(), parsed_roster, audio_bytes, audio.filename or "")
        return store.get(meeting_id)

    @app.get("/meetings/{meeting_id}")
    def get_meeting(meeting_id: str):
        try:
            return store.get(meeting_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Meeting not found") from None

    @app.post("/meetings/{meeting_id}/process", status_code=202)
    def process_meeting(meeting_id: str):
        try:
            meeting = store.get(meeting_id)
            with in_flight_lock:
                if meeting["status"] in {"queued", "failed"} and meeting_id not in in_flight:
                    if store.queue(meeting_id):
                        in_flight.add(meeting_id)
                        worker.submit(run_job, meeting_id)
                        return store.get(meeting_id)
            return meeting
        except KeyError:
            raise HTTPException(status_code=404, detail="Meeting not found") from None

    @app.patch("/meetings/{meeting_id}")
    async def update_meeting(meeting_id: str, changes: object = Body(...)):
        editable = _editable_changes(changes)
        try:
            with in_flight_lock:
                if meeting_id in in_flight:
                    raise HTTPException(status_code=409, detail="Cannot edit while processing")
                meeting = store.get(meeting_id)
                editable = validate_review(meeting, editable)
                return store.update(meeting_id, editable)
        except KeyError:
            raise HTTPException(status_code=404, detail="Meeting not found") from None
        except RuntimeError:
            raise HTTPException(status_code=409, detail="Cannot edit while processing") from None
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=422, detail=str(error)) from error

    @app.delete("/meetings/{meeting_id}", status_code=204)
    def delete_meeting(meeting_id: str):
        try:
            with in_flight_lock:
                meeting = store.get(meeting_id)
                if meeting_id in in_flight or meeting["status"] == "processing":
                    raise HTTPException(status_code=409, detail="Cannot delete while processing")
                store.delete(meeting_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Meeting not found") from None
        return Response(status_code=204)

    @app.get("/meetings/{meeting_id}/audio")
    def get_audio(meeting_id: str):
        try:
            meeting = store.get(meeting_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="Meeting not found") from None
        path = Path(storage_dir) / f"{meeting['id']}.wav"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Audio not found")
        return FileResponse(path, media_type="audio/wav", filename=path.name)

    @app.post("/meetings/{meeting_id}/export")
    def create_export(meeting_id: str, format: str = "pdf"):
        if format not in {"docx", "pdf"}:
            raise HTTPException(status_code=422, detail="Choose docx or pdf")
        try:
            with in_flight_lock:
                meeting = store.get(meeting_id)
                if meeting["status"] != "review":
                    raise HTTPException(status_code=409, detail="Meeting is not ready for review")
                store.set_export_ready(meeting_id, False, False)
                output = store.export_dir(meeting_id)
                try:
                    _, pdf = export_draft(meeting, output, include_pdf=format == "pdf")
                except RuntimeError as error:
                    docx_ready = (output / "draft.docx").is_file()
                    store.set_export_ready(meeting_id, docx_ready, False)
                    if not docx_ready:
                        raise
                    return {"docx": f"/meetings/{meeting_id}/export/docx", "pdf": None,
                            "warning": str(error)}
                store.set_export_ready(meeting_id, True, pdf is not None)
                return {"docx": f"/meetings/{meeting_id}/export/docx",
                        "pdf": f"/meetings/{meeting_id}/export/pdf" if pdf else None}
        except KeyError:
            raise HTTPException(status_code=404, detail="Meeting not found") from None
        except RuntimeError as error:
            raise HTTPException(status_code=500, detail=str(error)) from error

    @app.get("/meetings/{meeting_id}/export/{format}")
    def download_export(meeting_id: str, format: str):
        if format not in {"docx", "pdf"}:
            raise HTTPException(status_code=404, detail="Unknown export format")
        try:
            if not store.export_ready(meeting_id, format):
                raise HTTPException(status_code=404, detail="Export not available")
            path = store.export_dir(meeting_id) / f"draft.{format}"
        except KeyError:
            raise HTTPException(status_code=404, detail="Meeting not found") from None
        if not path.is_file():
            raise HTTPException(status_code=404, detail="Export not available")
        return FileResponse(path, filename=f"meeting-{meeting_id}-draft.{format}")

    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")

    return app
