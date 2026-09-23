# Meeting Minutes MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development` (recommended) or `executing-plans` task by task. Steps use checkbox (`- [ ]`) syntax. Do not commit or push unless the user explicitly requests it.

**Goal:** A persistent web demo that processes a permitted meeting recording into a human-correctable draft with speaker turns, summary, confirmed поручения, and matching DOCX/PDF exports.

**Architecture:** React/Vite calls a FastAPI app on one Brev GPU VM. A single in-process worker runs local ASR, diarization, and extraction sequentially; SQLite persists meeting records and corrections, while permitted audio and generated documents live on persistent disk. External inference APIs are forbidden.

**Tech Stack:** Python 3.11, FastAPI, sqlite3, pytest, ffmpeg, transformers/PyTorch, pyannote.audio Community-1, Ollama/Qwen3-8B, python-docx, LibreOffice, React/Vite/TypeScript, Node 22.

**Spec:** `docs/superpowers/specs/2026-09-23-meeting-minutes-mvp-design.md`; read `CONTEXT.md` and `spec-meeting-minutes-assistant.md` before implementation.

## Global Constraints

- Recordings: up to 10 minutes; only consented/synthetic or anonymized audio on Brev. Keep all inference on the VM; download weights before offline verification.
- A **предложение** is not a **поручение** without evidence of authorized confirmation. **Ответственный** is the assigned person or department, not necessarily the speaker. Until external approval, every exported document is a **черновик протокола**.
- A speaker cluster is anonymous; no voice-based naming. Preserve the original deadline phrase, leave ambiguous identities/dates unresolved, and keep approximate segment times distinct from word-level alignment.
- Access the demo through authenticated Brev access/SSH forwarding, not an unauthenticated internet endpoint. Persist data under the VM's persistent workspace; deleting a meeting removes audio and exports. No user accounts, audit log, message broker, or WhisperX for the MVP.
- Keep tasks locally testable without downloading multi-GB models. Test process boundaries with fakes; run one real model/audio smoke on the target GPU and a network-denied processing check before claiming the demo works.
- The current `.gitignore` excludes `docs/`, so this design and plan are local artifacts unless the user explicitly chooses to track them. Do not commit or push without explicit request.

## File map and dependencies

| Area | Files | Responsibility |
| --- | --- | --- |
| API/persistence | `backend/app/__init__.py`, `backend/app/main.py`, `backend/app/store.py`, `backend/tests/test_api.py`, `backend/requirements.txt` | Upload/lookup/update/delete, persistent statuses and corrections, file access. |
| Media and models | `backend/app/audio.py`, `backend/app/process.py`, `backend/tests/conftest.py`, `backend/tests/test_process.py` | Validate audio, extract anonymous timed turns, run one processing job. |
| Semantic extraction | `backend/app/extract.py`, `backend/tests/test_extract.py` | JSON schema, source-grounded поручения, dates and unresolved fields. |
| Documents | `backend/app/export.py`, `backend/tests/test_export.py` | Generate DOCX and convert the same reviewed content to PDF. |
| Browser | `frontend/src/App.tsx`, `frontend/src/App.test.tsx`, `frontend/src/api.ts`, `frontend/src/main.tsx`, `frontend/package.json` | Upload, poll, edit/reopen, seek original audio and download exports. |
| Reproduction | `backend/app/serve.py`, `backend/tests/conftest.py`, `backend/tests/test_end_to_end.py`, `README.md` | Brev setup, offline smoke and example workflow. |

### Task 1: Persisted meeting API

**Files:** Create `backend/app/__init__.py`, `backend/app/store.py`, `backend/app/main.py`, `backend/tests/test_api.py`, `backend/requirements.txt`; extend `backend/app/main.py` only in later tasks.

**Interfaces:** `Store(db_path: Path, storage_dir: Path)` exposes `create(meeting_date: str, roster: list[str], audio: bytes, filename: str) -> str`, `get(meeting_id: str) -> dict`, `update(meeting_id: str, changes: dict) -> dict`, `delete(meeting_id: str) -> None`. `create_app(db_path: Path, storage_dir: Path, processor: Callable | None = None) -> FastAPI` exposes `POST /meetings`, `GET /meetings/{id}`, `PATCH /meetings/{id}`, `DELETE /meetings/{id}`, and `GET /meetings/{id}/audio`. Use UUIDs for IDs and server-chosen filenames, never user-provided paths. Store roster/transcript/actions as JSON, summary/date/status/error as columns. Only allow edits to roster, transcript, summary and actions; validate the request schema and dates. Files stay under `storage_dir`.

**Representative red check:**

```python
from io import BytesIO
import wave
from fastapi.testclient import TestClient
from app.main import create_app

def test_correction_survives_restart(tmp_path):
    db, files = tmp_path / "meetings.sqlite", tmp_path / "files"
    sample = BytesIO()
    with wave.open(sample, "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    with TestClient(create_app(db, files)) as client:
        created = client.post("/meetings", data={"meeting_date": "2026-09-23", "roster": "[]"},
                              files={"audio": ("example.wav", sample.getvalue(), "audio/wav")})
        assert created.status_code == 201
        meeting_id = created.json()["id"]
        assert client.patch(f"/meetings/{meeting_id}", json={"summary": "Проверено"}).status_code == 200
    with TestClient(create_app(db, files)) as client:
        assert client.get(f"/meetings/{meeting_id}").json()["summary"] == "Проверено"
```

**Implementation shape (single persistent record):**

```python
connection.execute("""CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY, meeting_date TEXT NOT NULL, roster_json TEXT NOT NULL,
    transcript_json TEXT NOT NULL DEFAULT '[]', actions_json TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'queued',
    error TEXT, audio_path TEXT NOT NULL
)""")
```

- [ ] **Step 1: Write an API test** in `backend/tests/test_api.py` for a small valid WAV upload with meeting date, a `201` response, re-opening `Store` on the same SQLite file, `PATCH`ing an action correction, re-opening again, and deleting the meeting plus audio. Also assert invalid date, empty file and a traversal-like filename cannot create a meeting outside storage. Use `TestClient(create_app(tmp_path/'db.sqlite', tmp_path/'files'))`.
- [ ] **Step 2: Install `backend/requirements.txt` into an isolated environment, then run `PYTHONPATH=backend python3 -m pytest backend/tests/test_api.py -q`; expect failure** because the module does not exist. Dependencies at this stage: `fastapi`, `uvicorn`, `python-multipart`, `pytest`, `httpx`, `python-docx`; GPU dependencies enter Task 2.
- [ ] **Step 3: Implement the smallest schema and routes.** In `Store.__init__`, create one `meetings` table with `id`, `meeting_date`, `roster_json`, `transcript_json`, `actions_json`, `summary`, `status`, `error`, `audio_path`; write files as `<uuid>.wav`-style server-owned names under the storage directory. Enable SQLite foreign-safe atomic writes via connection context managers. Return `404` for unknown IDs and `422` for malformed date/upload. `DELETE` removes stored exports as well when Task 4 adds them. Bind `create_app` through a single store instance, with no global DB path.
- [ ] **Step 4: Rerun the test, then `PYTHONPATH=backend python3 -m pytest backend/tests -q`; expect pass.** Verify a new Store instance reads a persisted correction.

### Task 2: Bounded local audio pipeline and job states

**Files:** Create `backend/app/audio.py`, `backend/app/process.py`, `backend/tests/conftest.py`, `backend/tests/test_process.py`; modify `backend/app/main.py` and `backend/app/store.py`.

**Interfaces:** `prepare_audio(source: Path, target: Path) -> float` uses `ffprobe` for seconds and `ffmpeg` for 16 kHz mono WAV; reject non-audio or duration > 600 seconds before model invocation. `transcribe_turns(wav: Path) -> list[dict]` returns `{id,start,end,text,speaker_id,overlap}`. `process_recording(source: Path, date: str, roster: list[str], asr: Callable, diarizer: Callable, extract: Callable) -> dict` returns `transcript`, `summary`, `actions`. `POST /meetings/{id}/process` returns `202` with status; `GET /meetings/{id}` exposes status `queued|processing|review|failed`. One worker at a time; a restart marks unfinished jobs failed and allows retry. The test injects callables; production model adapters are lazy-loaded only when a job runs.

**Representative red check and adapter contract:**

```python
def fake_asr(_wav):
    return [{"id": "t1", "start": 0.0, "end": 0.5, "text": "Предлагаю провести аудит."},
            {"id": "t2", "start": 0.5, "end": 1.0, "text": "Фиксируем: аудит до пятницы."}]

def fake_diarizer(_wav):
    return [(0.0, 0.7, "speaker_0"), (0.3, 1.0, "speaker_1")]

def test_overlap_stays_uncertain(short_wav):
    def fake_extract(_turns, _date, _roster):
        return {"summary": "", "actions": []}
    result = process_recording(short_wav, "2026-09-23", [], fake_asr,
                               fake_diarizer, fake_extract)
    assert result["transcript"][0]["overlap"] is True
    assert result["transcript"][0]["speaker_id"] is None
```

Define `short_wav` in `backend/tests/conftest.py` with Python's `wave` module: one second, mono, 16 kHz, 16-bit silence written to `tmp_path / "demo.wav"`.

```python
@pytest.fixture
def short_wav(tmp_path):
    path = tmp_path / "demo.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 16000)
    return path

@pytest.fixture
def permitted_wav(short_wav):
    return short_wav.read_bytes()
```

Import `pytest` and `wave` at the top of `conftest.py`.

Use `ThreadPoolExecutor(max_workers=1)` to serialize processing in a single FastAPI worker; test injection uses `create_app(..., processor=fake_processor)`. Run exactly one `uvicorn` worker for the MVP; multiple app processes would bypass this guard.

- [ ] **Step 1: Add a failing integration test**: supply a short WAV, fake `asr` returning two timed segments and fake `diarizer` returning distinct anonymous intervals with one overlap. Poll until `review`, then assert both turns and the overlap flag remain visible, and after re-opening the database the results persist. Add a separate >600-second WAV-header case and a fake extractor that raises, asserting a visible `failed` status and successful retry.
- [ ] **Step 2: Run `PYTHONPATH=backend python3 -m pytest backend/tests/test_process.py -q`; expect missing-interface failures.**
- [ ] **Step 3: Implement ffprobe/ffmpeg wrappers with bounded subprocess execution and exit-status checks.** Use the original audio for review and the converted copy for models. For production, load `shyngys879/kazakh-whisper-large-v3-turbo` locally through a Transformers ASR pipeline with chunked transcription; load `pyannote/speaker-diarization-community-1` from an already provisioned model cache. Keep pyannote's ordinary overlapping annotation rather than discarding overlap with exclusive labels. Assign an anonymous speaker to a segment only when unambiguous; otherwise leave `speaker_id` null and `overlap` true. Never infer a real name from the roster.
- [ ] **Step 4: Wire one background worker and persistent status changes.** On startup mark `queued`/`processing` from the previous process as `failed` with a retryable interruption reason; `POST .../process` clears failure and enqueues only once. Skip GPU allocation for invalid media. Keep model imports/load inside the worker so API tests run CPU-only.
- [ ] **Step 5: Run `PYTHONPATH=backend python3 -m pytest backend/tests/test_process.py -q` and the full backend suite.** On Brev, process one permitted short recording with the actual model adapters and record wall time and peak GPU use; do not substitute this for the fake-based repeatable check.

### Task 3: Source-grounded поручения and editable summary

**Files:** Create `backend/app/extract.py`, `backend/tests/test_extract.py`; modify `backend/app/process.py`, `backend/app/main.py` and `backend/app/store.py` for editable persisted results.

**Interfaces:** `extract_draft(turns: list[dict], meeting_date: str, roster: list[str], chat: Callable) -> dict` returns `summary: str` and `actions: list[dict]`. Each action has `text`, `assignee` (`str|null`), `deadline_phrase` (`str|null`), `due_date` (`YYYY-MM-DD|null`), `source_turn_ids: list[str]`, `confirmation_turn_ids: list[str]`, `needs_review: bool`. `chat` is the local Ollama `/api/chat` adapter with JSON Schema in `format`, no external URL. Reject references to nonexistent turn IDs; keep an action as a review candidate rather than calling it confirmed if evidence of authorized confirmation is absent. Do not fabricate an assignee or date. Edits and manually added actions persist through `PATCH /meetings/{id}`.

**Representative red check and model response:**

```python
def fake_chat(_messages, _schema):
    return {"summary": "Обсудили аудит.", "actions": [
        {"text": "Провести аудит", "assignee": None,
         "deadline_phrase": "до пятницы", "due_date": None,
         "source_turn_ids": ["t1"], "confirmation_turn_ids": ["t2"],
         "needs_review": True}]}

def test_keeps_unresolved_owner():
    turns = [{"id": "t1", "text": "Предлагаю провести аудит."},
             {"id": "t2", "text": "Фиксируем аудит."}]
    draft = extract_draft(turns, "2026-09-23", [], fake_chat)
    assert draft["actions"][0]["assignee"] is None
    assert draft["actions"][0]["source_turn_ids"] == ["t1"]
```

Also test a missing `t2`. If the confirming speaker's authority is not established by supplied meeting context, keep `needs_review=True` even if the words appear to confirm. The schema constrains shape; application checks source references. Store review candidates separately from confirmed поручения in the draft; a human can clear `needs_review` only after checking confirmation evidence. This review does not approve the protocol.

- [ ] **Step 1: Write `backend/tests/test_extract.py` with a fake JSON response** covering a suggestion followed by a chair's confirmation, a second unconfirmed suggestion, an assigned non-speaker, and an ambiguous deadline. Assert only confirmed work can be marked as поручение, every cited ID exists, unresolved dates/owners remain null, and malformed JSON or a nonexistent source ID produces a visible error/review flag rather than a fabricated result. Use a fixed meeting date for relative dates.
- [ ] **Step 2: Run `PYTHONPATH=backend python3 -m pytest backend/tests/test_extract.py -q`; expect failure.**
- [ ] **Step 3: Implement an explicit JSON schema and local HTTP adapter.** Send stable turn IDs and meeting date to Ollama, request a schema with nullable assignee/date and evidence IDs, parse/validate the reply with Pydantic, then verify IDs and roster aliases in code. Preserve spoken deadline text; normalize only deterministic cases supported by tests (explicit full date and unambiguous `завтра`/weekday relative to the meeting date). Leave all other dates null for review. Generate the summary from cited turns; do not treat model-reported confidence as a calibrated probability.
- [ ] **Step 4: Persist and edit the draft.** Allow the secretary to add, remove or correct an action and speaker-name mapping without overwriting original segment times or source evidence; require a visible unresolved marker for blank owner/date. Provide an explicit review control for a suggested action: it enters the confirmed поручения list only with checked confirmation evidence; otherwise retain it as a separately labelled candidate. An edited action remains a draft, not an approved protocol.
- [ ] **Step 5: Run extraction, API and backend tests.** Try one actual Ollama/Qwen3-8B response on Brev; inspect an unconfirmed suggestion and an ambiguous date manually before claiming correctness.

### Task 4: One reviewed source, two document formats

**Files:** Create `backend/app/export.py`, `backend/tests/test_export.py`; modify `backend/app/main.py`, `backend/app/store.py`.

**Interfaces:** `export_draft(meeting: dict, output_dir: Path) -> tuple[Path, Path]` writes DOCX using `python-docx`, converts that exact file using local `libreoffice --headless --convert-to pdf --outdir <dir> <docx>`, checks both outputs exist/nonempty, and returns paths. `POST /meetings/{id}/export` creates both from persisted edits; `GET /meetings/{id}/export/{docx|pdf}` downloads generated files. Label documents `Черновик протокола` and include meeting date, summary, and a table with поручение, ответственный, срок. Unconfirmed candidates, if shown, go under a separate `На уточнение` heading—not into the поручения table. Confirmed поручения with an unknown owner/date remain in the table with an explicit uncertainty label. Never label it approved.

**Representative red check and conversion:**

```python
from docx import Document

def test_docx_contains_reviewed_action(tmp_path, monkeypatch):
    reviewed_meeting = {"id": "demo", "meeting_date": "2026-09-23",
                        "summary": "Қауіпсіздік талқыланды.",
                        "actions": [{"text": "Провести аудит", "assignee": "Юр. отдел",
                                     "deadline_phrase": None, "due_date": None}]}
    def fake_convert(args, **_kwargs):
        (tmp_path / "draft.pdf").write_bytes(b"%PDF demo")
    monkeypatch.setattr("app.export.subprocess.run", fake_convert)
    docx_path, pdf_path = export_draft(reviewed_meeting, tmp_path)
    doc = Document(docx_path)
    text = " ".join(p.text for p in doc.paragraphs)
    cells = " ".join(c.text for row in doc.tables[0].rows for c in row.cells)
    assert "Черновик протокола" in text
    assert "Провести аудит" in cells
    assert pdf_path.read_bytes().startswith(b"%PDF")
```

The fake replaces only `subprocess.run`; use `draft.docx` as the output name in `export_draft` so the expected PDF is `draft.pdf`. The required real LibreOffice smoke later checks that the actual PDF opens and renders Kazakh glyphs. Run conversion without shell interpolation:

```python
subprocess.run(["libreoffice", "--headless", "--convert-to", "pdf",
                "--outdir", str(output_dir), str(docx_path)],
               check=True, timeout=120)
```

- [ ] **Step 1: Write a failing export test**: create a reviewed meeting with Cyrillic and Kazakh text, one null deadline and corrected responsible person, then assert DOCX paragraphs/table contain the corrected values and draft marker. Stub only the local LibreOffice subprocess in the unit test; separately check a real generated PDF opens on the target machine and contains the same values.
- [ ] **Step 2: Run `PYTHONPATH=backend python3 -m pytest backend/tests/test_export.py -q`; expect failure.**
- [ ] **Step 3: Implement one DOCX renderer, conversion, existence/content checks and download routes.** On conversion failure leave DOCX downloadable, mark PDF unavailable and report an explicit error; do not silently serve an old PDF after a correction. Delete prior exports when content is edited.
- [ ] **Step 4: Run export and full backend tests; run the real LibreOffice smoke with Kazakh glyphs on Brev.**

### Task 5: Browser review workflow

**Files:** Create `frontend/package.json`, `frontend/index.html`, `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/App.test.tsx`, `frontend/src/api.ts`, `frontend/tsconfig.json`, `frontend/vite.config.ts`; modify `backend/app/main.py` only for same-origin static delivery/CORS in local development.

**Interfaces:** `api.ts` wraps the already implemented endpoints; `App.tsx` needs only four views/states: upload/date/roster, queued/processing progress, review/edit, failure/retry. The review view shows audio playback with segment-seek controls, anonymous speaker IDs with optional user-confirmed names, transcript and evidence-linked actions, a control for checking confirmation evidence, editable summary, and DOCX/PDF downloads. Both formats remain draft-labelled.

**Representative red check and API type:**

```tsx
// App.test.tsx (Vitest + Testing Library; mock API requests, not internal state)
import { render, screen } from '@testing-library/react';
import { it, expect, vi } from 'vitest';
import App from './App';

vi.mock('./api', () => ({
  getMeeting: vi.fn().mockResolvedValue({
    id: 'demo', status: 'review', summary: '', transcript: [{id: 't1', start: 0, end: 1, text: 'Провести аудит'}],
    actions: [{text: 'Провести аудит', assignee: null, deadline_phrase: null,
               due_date: null, source_turn_ids: ['t1'], confirmation_turn_ids: ['t1'], needs_review: true}]
  })
}));

it('shows uncertain owner and evidence before exporting', async () => {
  window.history.pushState(null, '', '/?meeting=demo');
  render(<App />);
  await screen.findByText('Провести аудит');
  expect(screen.getByText('Ответственный не указан')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Прослушать фрагмент' })).toBeTruthy();
});
```

```ts
export type Action = {
  text: string; assignee: string | null; deadline_phrase: string | null;
  due_date: string | null; source_turn_ids: string[];
  confirmation_turn_ids: string[]; needs_review: boolean;
};
```

`api.ts` exports `getMeeting(id: string): Promise<Meeting>`, `createMeeting(form: FormData): Promise<Meeting>`, `startProcessing(id: string): Promise<void>`, `saveMeeting(id: string, edits: Partial<Meeting>): Promise<Meeting>`, and `exportMeeting(id: string): Promise<void>`; `Meeting` includes the API fields in this test plus `meeting_date`, `roster`, and `error`. Configure Vite to proxy `/meetings` to `http://127.0.0.1:8000` during development; production FastAPI serves the built `dist` and the API under the same origin. Install `vitest`, `@testing-library/react`, and `jsdom` for this one browser-facing check.

- [ ] **Step 1: Scaffold a Vite React/TypeScript frontend and add a browser-facing behavior check** that a mocked API meeting in `review` renders one source-linked action, a null deadline as needing review, and save/download controls. Set `test: "vitest run"` in `frontend/package.json`; keep the check focused on rendered behavior rather than component internals.
- [ ] **Step 2: Run `npm test` from `frontend/`; expect failure because the review screen is absent.**
- [ ] **Step 3: Implement upload/poll/review/save/retry using only the API contracts above.** Seek the browser's native audio element to a cited segment start; do not claim word-level timing or automatically name an anonymous speaker. Disable export while unsaved edits exist, or save them before invoking export.
- [ ] **Step 4: Run `npm run build && npm test` from `frontend/`.** Manually inspect a narrow and wide browser viewport, keyboard access to upload/save/download and clear error/processing feedback. UI layout and interaction polish belong to the designer lane; preserve its visual decisions in later integration.

### Task 6: Brev reproduction and end-to-end evidence

**Files:** Create `backend/app/serve.py`, `backend/tests/test_end_to_end.py`; modify `README.md`; add only minimal config scripts needed to run API, Vite and model pre-download on the selected instance. Do not add a production orchestrator.

**Interfaces:** `backend/app/serve.py` creates `app = create_app(Path(os.environ["DB_PATH"]), Path(os.environ["STORAGE_DIR"]))`; `PYTHONPATH=backend uvicorn app.serve:app --host 127.0.0.1 --port 8000 --workers 1` runs the backend. `npm run dev -- --host 127.0.0.1` (from `frontend/`) runs the development frontend. README gives exact setup, disk path, model IDs/licenses/access step, local Ollama setup, ffmpeg/LibreOffice/fonts prerequisites, authorized Brev port-forward, teardown and approximate measured processing time. Do not publish secrets or actual confidential audio.

**Representative red check:**

```python
import time
from fastapi.testclient import TestClient
from app.main import create_app

def test_reopen_and_export(tmp_path, permitted_wav, monkeypatch):
    db, files = tmp_path / "db.sqlite", tmp_path / "files"
    def fake_processor(_source, _date, _roster):
        return {"transcript": [{"id": "t1", "start": 0, "end": 1, "text": "Фиксируем аудит"}],
                "summary": "Обсудили аудит",
                "actions": [{"text": "Провести аудит", "assignee": "Отдел А",
                             "deadline_phrase": None, "due_date": None,
                             "source_turn_ids": ["t1"], "confirmation_turn_ids": ["t1"],
                             "needs_review": False}]}
    def fake_convert(args, **_kwargs):
        from pathlib import Path
        out = Path(args[args.index("--outdir") + 1])
        (out / "draft.pdf").write_bytes(b"%PDF demo")
    monkeypatch.setattr("app.export.subprocess.run", fake_convert)
    with TestClient(create_app(db, files, processor=fake_processor)) as client:
        created = client.post("/meetings", data={"meeting_date": "2026-09-23", "roster": "[]"},
                              files={"audio": ("demo.wav", permitted_wav, "audio/wav")})
        meeting_id = created.json()["id"]
        assert client.post(f"/meetings/{meeting_id}/process").status_code == 202
        for _ in range(100):
            if client.get(f"/meetings/{meeting_id}").json()["status"] == "review":
                break
            time.sleep(0.01)
        changes = {"summary": "Проверено", "actions": [{"text": "Провести аудит",
                   "assignee": "Отдел Б", "deadline_phrase": None, "due_date": None,
                   "source_turn_ids": ["t1"], "confirmation_turn_ids": ["t1"],
                   "needs_review": False}]}
        assert client.patch(f"/meetings/{meeting_id}", json=changes).status_code == 200
    with TestClient(create_app(db, files, processor=fake_processor)) as client:
        assert client.get(f"/meetings/{meeting_id}").json()["summary"] == "Проверено"
        assert client.post(f"/meetings/{meeting_id}/export").status_code == 200
        docx = client.get(f"/meetings/{meeting_id}/export/docx")
        assert docx.status_code == 200
        from docx import Document
        from io import BytesIO
        assert "Отдел Б" in " ".join(c.text for row in Document(BytesIO(docx.content)).tables[0].rows for c in row.cells)
        assert client.get(f"/meetings/{meeting_id}/export/pdf").content.startswith(b"%PDF")
```

Reuse the one-second valid `permitted_wav` bytes fixture from `backend/tests/conftest.py`; the test waits for `review` rather than relying on a fixed processing delay. Add assertions for the corrected summary in both exports and for file deletion. The real-GPU check is a separate manual run whose measurements go into `README.md`.

- [ ] **Step 1: Write a failing end-to-end test** using the API with fake ASR/diarizer/chat: upload short WAV and date; wait for review; correct an action; restart the app against the same SQLite/disk; assert corrections persisted; export and assert DOCX/PDF contents. Add a delete check for record, source audio and exports.
- [ ] **Step 2: Run `python3 -m pytest backend/tests/test_end_to_end.py -q`; expect failure until all routes and persistence are connected.**
- [ ] **Step 3: Complete the route wiring and README with exact commands for the selected Brev GPU.** Pre-download Hugging Face weights to persistent workspace; pyannote requires prior accepted model terms. Keep Ollama bound locally and use Brev authentication or SSH port forwarding for the UI; document how to stop the instance and avoid lingering credit use.
- [ ] **Step 4: Run all backend tests, frontend check/build, and the real demo scenario** on the two supplied samples plus a separately consented simulated mixed-language meeting. Record actual GPU/VRAM, duration, output inspection and observed errors. Run one inference pass with egress denied and model artifacts already cached. If errors prevent a credible demo, fix the smallest failing stage and repeat the same scenario; do not claim language/overlap quality from the tiny external benchmark.

## Execution handoff

Read the design and glossary before Task 1. Gate each task on its own runnable check; Task 5's UX implementation should be delegated to a designer. The document remains local/ignored under the current `.gitignore`; changing that policy requires a user decision. No commits or pushes without explicit request.
