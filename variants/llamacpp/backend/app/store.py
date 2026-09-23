import json
import sqlite3
import uuid
import shutil
from pathlib import Path


class Store:
    def __init__(self, db_path: Path, storage_dir: Path):
        self.db_path = Path(db_path)
        self.storage_dir = Path(storage_dir)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS meetings (
                    id TEXT PRIMARY KEY,
                    meeting_date TEXT NOT NULL,
                    roster_json TEXT NOT NULL,
                    transcript_json TEXT NOT NULL DEFAULT '[]',
                    actions_json TEXT NOT NULL DEFAULT '[]',
                    summary TEXT NOT NULL DEFAULT '',
                    speaker_names_json TEXT NOT NULL DEFAULT '{}',
                    export_docx_ready INTEGER NOT NULL DEFAULT 0,
                    export_pdf_ready INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'queued',
                    error TEXT,
                    audio_path TEXT NOT NULL
                )"""
            )
            columns = {row[1] for row in connection.execute("PRAGMA table_info(meetings)")}
            if "speaker_names_json" not in columns:
                connection.execute("ALTER TABLE meetings ADD COLUMN speaker_names_json TEXT NOT NULL DEFAULT '{}' ")
            if "export_docx_ready" not in columns:
                connection.execute("ALTER TABLE meetings ADD COLUMN export_docx_ready INTEGER NOT NULL DEFAULT 0")
            if "export_pdf_ready" not in columns:
                connection.execute("ALTER TABLE meetings ADD COLUMN export_pdf_ready INTEGER NOT NULL DEFAULT 0")

    def create(self, meeting_date: str, roster: list[str], audio: bytes, filename: str) -> str:
        meeting_id = str(uuid.uuid4())
        audio_path = self.storage_dir / f"{meeting_id}.wav"
        audio_path.write_bytes(audio)
        try:
            with sqlite3.connect(self.db_path) as connection:
                connection.execute(
                    "INSERT INTO meetings (id, meeting_date, roster_json, audio_path) VALUES (?, ?, ?, ?)",
                    (meeting_id, meeting_date, json.dumps(roster, ensure_ascii=False), audio_path.name),
                )
        except Exception:
            audio_path.unlink(missing_ok=True)
            raise
        return meeting_id

    def get(self, meeting_id: str) -> dict:
        with sqlite3.connect(self.db_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if row is None:
            raise KeyError(meeting_id)
        return {
            "id": row["id"],
            "meeting_date": row["meeting_date"],
            "roster": json.loads(row["roster_json"]),
            "transcript": json.loads(row["transcript_json"]),
            "actions": json.loads(row["actions_json"]),
            "summary": row["summary"],
            "speaker_names": json.loads(row["speaker_names_json"]),
            "status": row["status"],
            "error": row["error"],
        }

    def mark_interrupted(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE meetings SET status = 'failed', error = 'Processing interrupted by server restart' "
                "WHERE status IN ('queued', 'processing')"
            )

    def queue(self, meeting_id: str) -> bool:
        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.execute(
                "UPDATE meetings SET status = 'queued', error = NULL, export_docx_ready = 0, export_pdf_ready = 0 "
                "WHERE id = ? AND status IN ('failed', 'queued')",
                (meeting_id,),
            )
            return cursor.rowcount > 0

    def set_status(self, meeting_id: str, status: str, error: str | None = None) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE meetings SET status = ?, error = ? WHERE id = ?",
                (status, error, meeting_id),
            )

    def audio_file(self, meeting_id: str) -> Path:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute("SELECT audio_path FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if row is None:
            raise KeyError(meeting_id)
        return self.storage_dir / row[0]

    def finish(self, meeting_id: str, result: dict) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE meetings SET transcript_json = ?, summary = ?, actions_json = ?, "
                "status = 'review', error = NULL, export_docx_ready = 0, export_pdf_ready = 0 WHERE id = ?",
                (json.dumps(result["transcript"], ensure_ascii=False), result["summary"],
                 json.dumps(result["actions"], ensure_ascii=False), meeting_id),
            )

    def update(self, meeting_id: str, changes: dict) -> dict:
        columns = {
            "roster": ("roster_json", lambda value: json.dumps(value, ensure_ascii=False)),
            "transcript": ("transcript_json", lambda value: json.dumps(value, ensure_ascii=False)),
            "actions": ("actions_json", lambda value: json.dumps(value, ensure_ascii=False)),
            "summary": ("summary", lambda value: value),
            "speaker_names": ("speaker_names_json", lambda value: json.dumps(value, ensure_ascii=False)),
        }
        if not changes or any(key not in columns for key in changes):
            raise ValueError("No editable meeting fields supplied")
        assignments = ", ".join(f"{columns[key][0]} = ?" for key in changes)
        values = [columns[key][1](value) for key, value in changes.items()]
        with sqlite3.connect(self.db_path) as connection:
            cursor = connection.execute(
                f"UPDATE meetings SET {assignments}, export_docx_ready = 0, export_pdf_ready = 0 "
                "WHERE id = ? AND status != 'processing'", (*values, meeting_id)
            )
            if cursor.rowcount == 0:
                row = connection.execute("SELECT status FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
                if row is None:
                    raise KeyError(meeting_id)
                raise RuntimeError("Cannot edit while processing")
        self.clear_exports(meeting_id)
        return self.get(meeting_id)

    def export_dir(self, meeting_id: str) -> Path:
        self.get(meeting_id)
        return self.storage_dir / meeting_id

    def clear_exports(self, meeting_id: str) -> None:
        path = self.storage_dir / meeting_id
        if path.exists():
            shutil.rmtree(path)

    def set_export_ready(self, meeting_id: str, docx: bool, pdf: bool) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute("UPDATE meetings SET export_docx_ready = ?, export_pdf_ready = ? WHERE id = ?",
                               (int(docx), int(pdf), meeting_id))

    def export_ready(self, meeting_id: str, format: str) -> bool:
        column = "export_docx_ready" if format == "docx" else "export_pdf_ready"
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(f"SELECT {column} FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if row is None:
            raise KeyError(meeting_id)
        return bool(row[0])

    def delete(self, meeting_id: str) -> None:
        with sqlite3.connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT audio_path FROM meetings WHERE id = ?", (meeting_id,)
            ).fetchone()
            if row is None:
                raise KeyError(meeting_id)
            self.clear_exports(meeting_id)
            (self.storage_dir / row[0]).unlink(missing_ok=True)
            connection.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
