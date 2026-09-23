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

The cleaned implementation and handoff were committed and pushed to `origin/main` as `8b0a9eb`. The audit reran all 33 tests, Ruff, mypy and JavaScript syntax checks successfully. Earlier references to untracked implementation/no commits describe the state before this publication.

## Multi-agent continuation and requested model defaults

The next documentation update expanded the OpenCode prompt into coordinator, speech, extraction, API/dashboard and QA/documentation roles with file ownership and sequential heavy-model experiments. Added `MODEL_SELECTION.md` specifying selectable pipeline-operation profiles and defaults: `pyannote/speaker-diarization-3.1`, `openai/whisper-large-v3-turbo` with a compatible local CTranslate2 artifact, and `Qwen3.5-4B-UD-Q6_K_XL` for all LLM operations. **At this documentation-only stage**, runtime model selection, dependency migration and real-model validation remained work for the continuation; this update changed the handoff, not the running application.

## Model-selection implementation continuation (after the handoff above)

The implementation now includes `profiles.py` with built-in and optional operator JSON profiles; `config.py` / `.env.example` expose local artifact settings and slot defaults. The requested speech identities are separate from their runtime directories. The existing GGUF is selected by profile, not swapped by changing a chat request's alias. `models.py`, `api.py`, `processor.py` and `store.py` resolve six pipeline-operation slots, provide a safe catalog/meeting selection interface, preflight before admitting a processing attempt, and persist an attempt-scoped snapshot (including private local runtime values in SQLite, never in public attempt bindings). `audio.py` loads local CT2/pyannote artifacts; `llm.py` routes four operations through the resolved local profile; the dashboard exposes selection and attempt bindings. Built-in defaults use `openai/whisper-large-v3-turbo`, `pyannote/speaker-diarization-3.1` and `Qwen3.5-4B-UD-Q6_K_XL`. No runtime fallback to community-1 or cloud inference was added.

At this **intermediate** stage, coordinator-reported validation was **64 offline tests passed**, Ruff, mypy, `node --check src/meeting_protocol/web/app.js`, and `git diff --exit-code -- initial_data`. Before this feature the published baseline was 33 tests. These additional tests used fakes; they did not establish real weight loading. `pyproject.toml` declared `pyannote.audio>=3.4,<4` in optional `.[models]`, without an installed heavy stack or demonstrated pyannote 3.x/3.1 runtime compatibility. No llama-server startup, real speech inference, supplied-recording processing, browser visual test or rendered export review was performed. This is a historical intermediate check, not the final count or a publication/commit report.

## Bounded correctness and final offline validation (2026-09-23)

Follow-up changes implemented shared complete local CT2 and parsed pyannote YAML/weight preflight (absolute or localized relative references, no process-wide working-directory changes), metadata-only dependency checks and pyannote 3.x restriction. Speech adapters now accept compatible custom/legacy local artifacts without relabelling their unverified model identities. Operator JSON defaults validate ID/slot compatibility; public labels/identities reject paths, URLs, credentials and control characters. The six slots resolve independently with strict HTTP `127.0.0.1` LLM endpoints and bounded finite context/output budgets. Local GGUF availability is `not_verified`; preflight checks both `/models` alias and root/API-prefix read-only GET `/props` absolute `model_path`. GET needs no `--props` flag (that flag allows POST mutation). A matching **server-reported path** is not proof of weight contents, a hash, actual inference or accuracy.

Admission now freezes a source and resolved snapshot in SQLite for each execution attempt; only execution status changes afterward. A single-process OS lock is acquired before restart recovery. Attempt IDs identify executions, not editable meeting selections. Browser authentication reconnect preserves selected profile IDs; only configured IDs, not arbitrary browser paths or endpoints, are accepted. New diarization results are fresh: manual mappings and task status edits are **not** automatically carried across attempts. A previously failing obsolete direct meeting-ID processor test was migrated to admitted attempts while keeping fake inference.

Final coordinator evidence: **117 offline pytest tests passed**, with one Starlette/httpx deprecation warning; the last four regressions cover public metadata dot-segment paths (such as `../`). Prescribed Ruff, `python -m mypy` for 14 source files, JS syntax, four Node behavioral tests, original-data diff and whitespace checks passed. An extra `mypy .` over tests found missing YAML stubs; the prescribed source check is not a claim of globally strict typing. PyYAML 6.0.3 was the sole newly installed dependency (lightweight pipeline-config parser); heavy models/dependencies, live llama-server, real inference, browser visual tests and rendered exports were **not** run. See [remaining work](REMAINING_WORK.md) and [review risks](DESIGN_REVIEW.md) before describing any real-world acceptance. These changes are not a new commit/publication report.
