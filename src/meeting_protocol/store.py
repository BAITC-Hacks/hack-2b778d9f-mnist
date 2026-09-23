from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from .models import Meeting, ModelSelection, ProcessingStatus
from .profiles import AttemptSnapshot


class ProcessingAttempt(BaseModel):
    id: str
    meeting_id: str
    snapshot: AttemptSnapshot
    source_audio: Path
    status: str
    error: str | None = None
    created_at: str
    updated_at: str


type AttemptList = list[ProcessingAttempt]


class Store:
    """One JSON document per meeting; private audio path lives in a separate column."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS meetings (id TEXT PRIMARY KEY, body TEXT NOT NULL, audio TEXT)"
            )
            db.execute("CREATE TABLE IF NOT EXISTS processing_attempts (id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, snapshot_json TEXT NOT NULL, source_audio TEXT NOT NULL, status TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_attempt_provenance
                BEFORE UPDATE OF snapshot_json, source_audio, meeting_id ON processing_attempts
                WHEN NEW.snapshot_json IS NOT OLD.snapshot_json OR NEW.source_audio IS NOT OLD.source_audio
                    OR NEW.meeting_id IS NOT OLD.meeting_id
                BEGIN SELECT RAISE(ABORT, 'Attempt provenance is immutable'); END""")

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    @staticmethod
    def _snapshot_json(snapshot: AttemptSnapshot) -> str:
        return snapshot.model_dump_json()

    @staticmethod
    def _snapshot_read(raw: str) -> AttemptSnapshot:
        payload = json.loads(raw)
        for item in payload["profiles"]:
            runtime = item.pop("_runtime", None)
            if runtime:
                item["runtime_artifact"] = runtime.get("artifact", "")
                if runtime.get("url"):
                    item["base_url"] = runtime["url"]
        return AttemptSnapshot.model_validate(payload)

    def save(self, meeting: Meeting, expected_body: str | None = None) -> None:
        with self.connect() as db:
            if expected_body is None:
                db.execute("INSERT INTO meetings(id, body) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                           (meeting.id, meeting.model_dump_json()))
            elif not db.execute("UPDATE meetings SET body=? WHERE id=? AND body=?",
                                (meeting.model_dump_json(), meeting.id, expected_body)).rowcount:
                raise ValueError("Meeting changed; refresh and try again")

    def get(self, meeting_id: str) -> Meeting:
        with self.connect() as db:
            row = db.execute("SELECT body FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        if row is None:
            raise KeyError(meeting_id)
        return Meeting.model_validate_json(row[0])

    def list(self) -> list[Meeting]:
        with self.connect() as db:
            return [
                Meeting.model_validate_json(row[0])
                for row in db.execute("SELECT body FROM meetings ORDER BY rowid DESC")
            ]

    def set_audio(self, meeting_id: str, path: Path):
        with self.connect() as db:
            db.execute("UPDATE meetings SET audio=? WHERE id=?", (str(path.resolve()), meeting_id))

    def attach_audio(self, meeting_id: str, path: Path, expected_body: str | None = None,
                     expected_audio: Path | None = None) -> Meeting:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body, audio FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if row is None:
                raise KeyError(meeting_id)
            if expected_body is not None and expected_body != row[0]:
                raise ValueError("Meeting changed; refresh and try again")
            if expected_audio is not None and str(expected_audio.resolve()) != (row[1] or ""):
                raise ValueError("Audio changed; refresh and try again")
            if expected_audio is None and expected_body is not None and row[1]:
                raise ValueError("Audio changed; refresh and try again")
            meeting = Meeting.model_validate_json(row[0])
            if meeting.active_attempt_id or meeting.status in {
                ProcessingStatus.queued, ProcessingStatus.normalizing, ProcessingStatus.transcribing,
                ProcessingStatus.diarizing, ProcessingStatus.extracting, ProcessingStatus.completed,
            }:
                raise ValueError("Audio cannot be changed while processing or after results are completed")
            meeting.status, meeting.error = ProcessingStatus.uploaded, None
            meeting.results_attempt_id = None
            meeting.transcript, meeting.tasks, meeting.decisions, meeting.speaker_mappings = [], [], [], []
            meeting.summary = type(meeting.summary)()
            db.execute("UPDATE meetings SET audio=?, body=? WHERE id=?",
                       (str(path.resolve()), meeting.model_dump_json(), meeting_id))
            return meeting

    def audio(self, meeting_id: str) -> Path | None:
        with self.connect() as db:
            row = db.execute("SELECT audio FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return Path(row[0]) if row and row[0] else None

    def recover(self):
        with self.connect() as db:
            rows = db.execute("SELECT id, meeting_id FROM processing_attempts WHERE status NOT IN ('completed','failed')").fetchall()
            for attempt_id, meeting_id in rows:
                now = datetime.now(UTC).isoformat()
                db.execute("UPDATE processing_attempts SET status='failed', error=?, updated_at=? WHERE id=?", ("Processing was interrupted by application restart.", now, attempt_id))
                row = db.execute("SELECT body FROM meetings WHERE id=?", (meeting_id,)).fetchone()
                if row:
                    meeting = Meeting.model_validate_json(row[0])
                    if meeting.active_attempt_id == attempt_id:
                        meeting.status, meeting.error, meeting.active_attempt_id = ProcessingStatus.failed, "Processing was interrupted by application restart. Start processing again.", None
                        db.execute("UPDATE meetings SET body=? WHERE id=?", (meeting.model_dump_json(), meeting_id))
        for meeting in self.list():
            if meeting.active_attempt_id:
                continue
            if meeting.status not in {
                ProcessingStatus.created,
                ProcessingStatus.uploaded,
                ProcessingStatus.completed,
                ProcessingStatus.failed,
            }:
                meeting.status = ProcessingStatus.failed
                meeting.error = (
                    "Processing was interrupted by application restart. Start processing again."
                )
                self.save(meeting)

    def admit_attempt(self, meeting_id: str, snapshot: AttemptSnapshot, selection: ModelSelection,
                      expected_body: str | None = None, expected_audio: Path | None = None) -> ProcessingAttempt:
        from uuid import uuid4
        attempt_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT body, audio FROM meetings WHERE id=?", (meeting_id,)).fetchone()
            if row is None:
                raise KeyError(meeting_id)
            meeting = Meeting.model_validate_json(row[0])
            if expected_body is not None and expected_body != row[0]:
                raise ValueError("Meeting changed; refresh and try again")
            if expected_audio is not None and str(expected_audio.resolve()) != (row[1] or ""):
                raise ValueError("Audio changed; refresh and try again")
            if not row[1]:
                raise ValueError("Upload an audio file first")
            if meeting.status in {ProcessingStatus.queued, ProcessingStatus.normalizing, ProcessingStatus.transcribing, ProcessingStatus.diarizing, ProcessingStatus.extracting}:
                raise ValueError("Meeting processing is already active")
            meeting.model_selection = selection
            meeting.status, meeting.error = ProcessingStatus.queued, None
            meeting.active_attempt_id = meeting.latest_attempt_id = attempt_id
            db.execute("INSERT INTO processing_attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                       (attempt_id, meeting_id, self._snapshot_json(snapshot), row[1], "queued", None, now, now))
            db.execute("UPDATE meetings SET body=? WHERE id=?", (meeting.model_dump_json(), meeting_id))
        return self.get_attempt(attempt_id)

    def claim_attempt(self, attempt_id: str) -> ProcessingAttempt | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT meeting_id, status FROM processing_attempts WHERE id=?",
                             (attempt_id,)).fetchone()
            if row is None or row[1] != ProcessingStatus.queued:
                return None
            meeting_row = db.execute("SELECT body FROM meetings WHERE id=?", (row[0],)).fetchone()
            if meeting_row is None:
                return None
            meeting = Meeting.model_validate_json(meeting_row[0])
            if meeting.active_attempt_id != attempt_id or meeting.status != ProcessingStatus.queued:
                return None
            meeting.status = ProcessingStatus.normalizing
            db.execute("UPDATE processing_attempts SET status=?, updated_at=? WHERE id=? AND status=?",
                       (ProcessingStatus.normalizing, datetime.now(UTC).isoformat(), attempt_id,
                        ProcessingStatus.queued))
            db.execute("UPDATE meetings SET body=? WHERE id=?", (meeting.model_dump_json(), row[0]))
        return self.get_attempt(attempt_id)

    def get_attempt(self, attempt_id: str) -> ProcessingAttempt:
        with self.connect() as db:
            row = db.execute("SELECT id, meeting_id, snapshot_json, source_audio, status, error, created_at, updated_at FROM processing_attempts WHERE id=?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return ProcessingAttempt(id=row[0], meeting_id=row[1], snapshot=self._snapshot_read(row[2]), source_audio=Path(row[3]), status=row[4], error=row[5], created_at=row[6], updated_at=row[7])

    def list_attempts(self, meeting_id: str) -> AttemptList:
        with self.connect() as db:
            ids = db.execute("SELECT id FROM processing_attempts WHERE meeting_id=? ORDER BY created_at, rowid", (meeting_id,)).fetchall()
        return [self.get_attempt(row[0]) for row in ids]

    def stage_attempt(self, attempt_id: str, status: ProcessingStatus) -> bool:
        now = datetime.now(UTC).isoformat()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT meeting_id, status FROM processing_attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None: raise KeyError(attempt_id)
            meeting_row = db.execute("SELECT body FROM meetings WHERE id=?", (row[0],)).fetchone()
            meeting = Meeting.model_validate_json(meeting_row[0])
            if meeting.active_attempt_id != attempt_id or row[1] not in {
                ProcessingStatus.normalizing, ProcessingStatus.transcribing,
                ProcessingStatus.diarizing, ProcessingStatus.extracting,
            }:
                return False
            db.execute("UPDATE processing_attempts SET status=?, updated_at=? WHERE id=?", (status.value, now, attempt_id))
            meeting.status = status
            db.execute("UPDATE meetings SET body=? WHERE id=?", (meeting.model_dump_json(), row[0]))
            return True

    def finish_attempt(self, attempt_id: str, result: Meeting, error: str | None = None) -> bool:
        now = datetime.now(UTC).isoformat()
        status = "failed" if error else "completed"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT meeting_id, status FROM processing_attempts WHERE id=?", (attempt_id,)).fetchone()
            if row is None: raise KeyError(attempt_id)
            current_row = db.execute("SELECT body FROM meetings WHERE id=?", (row[0],)).fetchone()
            current = Meeting.model_validate_json(current_row[0])
            if current.active_attempt_id != attempt_id or row[1] not in {
                ProcessingStatus.normalizing, ProcessingStatus.transcribing,
                ProcessingStatus.diarizing, ProcessingStatus.extracting,
            }:
                return False
            db.execute("UPDATE processing_attempts SET status=?, error=?, updated_at=? WHERE id=?", (status, error, now, attempt_id))
            if error:
                current.status, current.error, current.active_attempt_id = ProcessingStatus.failed, error, None
            else:
                result.id = current.id
                result.title, result.meeting_date = current.title, current.meeting_date
                result.participants = current.participants
                result.tasks = [task.model_copy(update={"status": next(
                    (old.status for old in current.tasks if old.id == task.id), task.status)})
                                for task in result.tasks]
                result.model_selection = current.model_selection
                result.active_attempt_id = None
                result.latest_attempt_id = attempt_id
                result.results_attempt_id = attempt_id
                result.status, result.error = ProcessingStatus.completed, None
                current = result
            db.execute("UPDATE meetings SET body=? WHERE id=?", (current.model_dump_json(), row[0]))
            return True
