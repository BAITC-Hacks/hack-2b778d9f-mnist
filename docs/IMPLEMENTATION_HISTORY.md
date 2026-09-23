# Implementation history

Handoff recorded on 2026-09-23. This describes the preceding implementation session and a subsequent read-only code review for this handoff. It is not a Git commit history. No tests were rerun solely to write these documents.

## Starting state

- Existing root: the local `hack-2b778d9f-mnist` checkout; machine-specific paths are omitted.
- Git tracked `README.md` and the original material under `initial_data/`. README already had local modifications; a generic `agentic_core` scaffold and its supporting files were untracked.
- Inspected Git status/tracked files, project configuration, source, scripts, tests, prompts and initial-data filenames. Read `.env` configuration selectively without displaying secrets.
- Found two MP3 recordings and corresponding reference protocol PDFs in `initial_data/recordings/audio1` and `audio2`. Their contents were not used for an accuracy evaluation.
- ffmpeg was available. `python` and `llama-server` were not on PATH; the existing `.venv` referenced a missing Python installation.

## Changes made

### Environment and project

- Installed Python 3.12.12 through uv with approval and created `.venv-meeting`. Left the old `.venv` untouched.
- Installed the application and lightweight development/export dependencies. Did not install/download the heavy speech stack or llama-server.
- Replaced project metadata with `meeting-protocol`, retaining Python `>=3.12,<3.13`; `.python-version` remains `3.12`.
- Added the focused environment template and ignored runtime/model directories. Preserved the existing `.env`; legacy keys are ignored by new settings.
- Captured lightweight installed package versions in `requirements-tested.txt`. This is not a complete lock for heavy-model dependencies.

### Domain pipeline

- Added `src/meeting_protocol/models.py`: meeting, participant, transcript, task, decision, summary, mapping and status models. Extra fields are forbidden; some scalar type coercion still follows Pydantic defaults.
- Added `config.py`: local-only LLM URL validation, LAN key requirements, device/path/limit settings.
- Added `llm.py`: asynchronous httpx requests to local chat completions, schema output requests, unsupported-format fallback and one JSON repair attempt. No model-generated code execution.
- Added four root `prompts/*.md` files for extraction, verification, speaker resolution and summary; wheel configuration includes copies of these prompts.
- Added `extraction.py`: complete-segment chunks, task/decision verification, exact evidence matching, conservative deduplication, deadline resolution, speaker inference and hierarchical summary reduction.
- Added `deadlines.py`: initial deterministic Russian date rules. Missing dates and unsupported phrases remain reviewable; event dependencies do not become invented dates.
- Added `audio.py`: ffmpeg normalization, faster-whisper ASR, pyannote diarization, offline settings, filename sanitization and timestamp alignment. Concrete adapters exist but were not run against real speech models.
- Added `processor.py`: sequential heavy stages, one-job semaphore, persisted stage/error reporting, and injectable adapters for tests.

### Application and outputs

- Added `store.py`: SQLite meeting JSON records with a separate private audio-path column and restart recovery for interrupted jobs.
- Added `api.py` and `__main__.py`: meeting creation/list/detail, upload, processing, transcript/tasks, manual speaker mapping, status updates, audio delivery and exports.
- Added `web/index.html`, `web/style.css`, `web/app.js`: basic dashboard, progress polling, transcript and tasks, speaker correction, audio source seeking, exports and sessionStorage key handling.
- Added `exports.py`: python-docx protocol generation and ReportLab PDF using installed Unicode fonts. No font binaries were added to Git.
- Reworked PowerShell launchers. The llama launcher resolves quoted paths, checks the installed help output, uses an argument array and forces loopback binding.
- Added `scripts/manual_integration.py` for explicit real-model runs through the local API.
- Rewrote README with architecture, setup, run instructions, offline model preparation, VRAM guidance and limitations.

### Removed scaffold

- Removed `src/agentic_core/`, including generic agent loops, tool registries, filesystem/demo tools and approval abstractions.
- Replaced old generic-agent tests and prompts with meeting-specific files.
- Removed demo `examples/` and obsolete `scripts/start_app.sh` / `scripts/start_llama_server.sh`.
- These were largely untracked files, so Git's tracked diff alone does not show the full change history. Existing `LICENSE` and `workspace/.gitkeep` were retained.

## Validation actually performed

| Check | Recorded result | What it does not prove |
| --- | --- | --- |
| `python -m pytest` | Final run: 33 passed, one Starlette/httpx deprecation warning | Real model accuracy or compatibility |
| Ruff on source, tests, manual runner | Passed | Runtime behavior of optional models |
| mypy | Passed for 12 source files | Full strict typing; several interfaces remain loosely typed |
| `node --check` on dashboard JS | Passed | Browser interaction correctness |
| PowerShell parser on llama launcher | Passed | Actual llama-server flags/model loading |
| Real Uvicorn startup | Passed on loopback with isolated smoke database | LAN behavior under a real remote client |
| Live HTTP health, HTML delivery, meeting creation, authenticated upload | Passed | Visual UI operation or completed inference |
| Real ffmpeg on generated silent WAV | Passed; mono, 16 kHz output inspected | Recognition or processing of the supplied recordings |
| DOCX/PDF fixture generation and Unicode extraction | Passed | Rendered pagination, layout or font appearance |
| `git diff --exit-code -- initial_data` | Passed | An accuracy comparison with the references |

Tests cover dates, missing dates, absent/event deadlines, JSON/schema errors and bounded repair, chunks, timestamp alignment, evidence rejection, mapping correction, exports, health/auth/origin handling, upload sanitization/limits, persistence, restart recovery, missing diarization setup and a processor run with fake adapters.

## Corrections during implementation

- Fixed a UTF-8 BOM in pyproject.toml that initially prevented package installation.
- Fixed lint and mypy findings, then reran checks.
- The first live API upload returned 401 because the preserved `.env` required a key. Repeated the check using the existing key without printing it; it passed.
- Added canonical speaker identities for uniquely resolved names so later manual name corrections update task displays.
- Fixed a PowerShell-to-Python Unicode issue in the newly added mapping test; the final 33-test run passed.

## State left behind

- No automatic commits, no added contributor attribution, no nested project.
- `.env`, the GGUF and original recordings/reference files were not changed. The GGUF was not copied or hashed.
- Ignored `data/smoke/` contains generated smoke-test audio and a temporary database. The smoke-test server was stopped.
- Browser automation had no available browser. Visual UI testing and rendered export review were not completed.
- llama-server, real ASR, real diarization and the supplied-recording workflow remain unverified.
- The handoff documents were added afterward; their review findings are separated into [DESIGN_REVIEW.md](DESIGN_REVIEW.md), not presented as fixes already made.

## Pre-publication cleanup

A subsequent repository audit removed the obsolete `workspace/.gitkeep` placeholder, kept legacy scratch/runtime directories ignored, replaced machine-specific documentation paths with portable examples, and expanded ignore rules for credentials, model weights, local media and databases. Existing tracked `initial_data` files remain unchanged. This audit does not change the earlier real-model validation limitations.
