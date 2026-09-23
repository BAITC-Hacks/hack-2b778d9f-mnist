import sqlite3
from pathlib import Path

from .models import Meeting, ProcessingStatus


class Store:
    """One JSON document per meeting; private audio path lives in a separate column."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS meetings (id TEXT PRIMARY KEY, body TEXT NOT NULL, audio TEXT)"
            )

    def connect(self):
        return sqlite3.connect(self.path, timeout=30)

    def save(self, meeting: Meeting) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO meetings(id, body) VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (meeting.id, meeting.model_dump_json()),
            )

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

    def audio(self, meeting_id: str) -> Path | None:
        with self.connect() as db:
            row = db.execute("SELECT audio FROM meetings WHERE id=?", (meeting_id,)).fetchone()
        return Path(row[0]) if row and row[0] else None

    def recover(self):
        for meeting in self.list():
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
