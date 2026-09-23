# OpenCode multi-agent continuation prompt

Copy the text below into OpenCode, opened at this repository root.

---

You are working directly in the existing `hack-2b778d9f-mnist` repository root (use its actual local checkout path) on the HackAlem AI local meeting protocol application. Continue the existing implementation. Do not create a nested project or rebuild it as a generic agent framework.

First run `git status --short` and `git ls-files`. Read these files in order:

1. `docs/IMPLEMENTATION_HISTORY.md`
2. `docs/REMAINING_WORK.md`
3. `docs/DESIGN_REVIEW.md`
4. `docs/MODEL_SELECTION.md` (latest requested model defaults and selection requirements)
5. `README.md`, `pyproject.toml`, `.env.example`
6. The implementation under `src/meeting_protocol/`, domain prompts and tests.

Treat these documents as a handoff, not proof that the application works with real models. Inspect the current code and rerun checks. The initial implementation and handoff were committed and pushed to `main` as `8b0a9eb`; inspect `git status` and recent commits for the continuation's publication state. Do not remove current work with git clean/reset or overwrite it as disposable scaffolding. The original implementation history describes an earlier state; the model defaults in MODEL_SELECTION.md supersede earlier community-1 recommendations. Historical runs had 33, then 64 passing offline tests. The final full suite had **117 passed** with one Starlette/httpx deprecation warning; the obsolete direct meeting-ID processor test now exercises admitted attempts with fake inference and public metadata rejects `../` segments. Prescribed Ruff, source-only `python -m mypy` (14 files), JS syntax, four Node behavioral tests and original-data/whitespace checks passed. An extra `mypy .` including tests reported missing YAML stubs; do not mistake source-only mypy for global strict typing. PyYAML 6.0.3 was the only newly installed dependency; no heavy-model or real-inference run was performed.

## Agent organization

Use a lead coordinator and specialized coding agents where OpenCode supports them. These agents are for development; do not add a multi-agent framework to the meeting application. Use available configured coding models for the agents. The application models below are inference defaults, not instructions to use Whisper/pyannote as coding agents. Do not invent unavailable OpenCode commands or model providers.

| Agent | Concrete responsibility | Owned files |
| --- | --- | --- |
| Coordinator / integrator | Inspect baseline, define profile/config contracts, integrate changes, run whole-project checks, resolve conflicts | `config.py`, `models.py`, `processor.py`, `store.py`, `.env.example`, `pyproject.toml`, dependency pins and shared integration tests |
| Speech integration | Implement selected Whisper and pyannote defaults, validate offline loading and compatible versions, improve speaker alignment | `audio.py`, new speech-only helpers/setup scripts, `tests/test_audio_*.py` |
| Extraction and grounding | Per-operation LLM selection, bounded requests, grounded verification, overlap/deduplication, conservative dates and summary correctness | `llm.py`, `extraction.py`, `deadlines.py`, `prompts/`, `tests/test_extraction_*.py`, `tests/test_deadline_*.py` |
| API and dashboard | Safe model catalog/selection API, settings controls, preflight display, evidence playback, upload lifecycle fixes | `api.py`, `web/`, `tests/test_api_*.py` |
| QA and documentation | Independent regression review, real-run evidence, rendered export checks, accurate Markdown updates | `docs/`, `README.md`, `exports.py`, export tests, `scripts/manual_integration.py` |

Use as many agents as the runtime supports; queue roles when slots are limited. If delegation is unavailable, perform these roles sequentially and state that limitation.

Working rules:

1. The coordinator first publishes a small contract: model profile IDs, selected-profile fields, safe API representations, resolution precedence, immutable per-run snapshot and compatibility/error behavior. Other agents may inspect in parallel but must not guess incompatible schemas.
2. After that contract, speech, extraction and API work can proceed independently in owned files. One owner per file. Request cross-owner changes through the coordinator rather than editing the same files concurrently.
3. QA reviews completed changes and integration evidence; it is the sole writer of shared Markdown during the run. Agents send findings to QA rather than overwriting one another's docs. The coordinator owns the existing `tests/test_meeting.py` unless explicitly reassigned.
4. Each agent reports changed files, tests, unresolved assumptions, setup requirements and integration requests. Keep reports factual and distinguish fake tests from real inference.
5. Run only one heavy model experiment at a time on this 6 GB GPU machine. Development agents may work in parallel; speech/LLM benchmarks may not compete for GPU memory. Do not independently start multiple llama servers.
6. The coordinator integrates and reruns the full suite. Do not commit or push as part of this prompt unless the user explicitly requests it for this continuation.

## Implemented task-specific model selection (offline/fake-tested)

The code implements the profile contract in `docs/MODEL_SELECTION.md`, with these configured defaults:

- Diarization: `pyannote/speaker-diarization-3.1`.
- Speech recognition: `openai/whisper-large-v3-turbo`, using a compatible local CTranslate2 conversion when retaining faster-whisper.
- Task/decision extraction, verification, speaker-to-person inference and summary: the existing local `Qwen3.5-4B-UD-Q6_K_XL.gguf` through llama-server.
- Audio normalization, timestamp merge, date normalization, persistence and exports remain deterministic code, not model choices.

The catalog and UI select profiles per pipeline operation and preserve original identity separately from runtime artifact/path and server alias. Do not pass the OpenAI Transformers checkpoint directly to faster-whisper or assume changing a llama request's `model` string loads different GGUF weights. Do not silently replace the user's selected pyannote 3.1 with community-1. Speech adapters accept compatible local CT2 and pyannote artifacts for custom and identity-unverified legacy profiles; they do not certify a model's claimed upstream provenance. Defaults are CPU/int8 ASR and CPU diarization; no model downloads occur during processing.

Resolved selections and source audio are frozen per execution attempt in private SQLite; API responses expose safe bindings/attempt IDs only. A single-process OS lock precedes recovery. Browser inputs are configured IDs, never arbitrary paths or endpoints; auth reconnect preserves choices. Public labels/identities exclude paths, URLs, credentials and control characters. Preflight shares CT2/YAML-weight validation with speech adapters, checks dependency metadata without importing heavy modules and checks the HTTP `127.0.0.1` server's `/models` alias and read-only root/API-prefix GET `/props` absolute `model_path`. GET does not need `--props` (the flag permits POST mutation). The matching server-reported path is not a hash, proof of GGUF contents, successful model loading or accuracy. Missing/incompatible setup must remain a safe error before heavy processing, not a cloud fallback or automatic download.

## Constraints

- Preserve every original recording and reference protocol under `initial_data/`. Read originals only; put generated output under ignored `data/`.
- Never overwrite `.env`, print its secrets, or put secrets in URLs. Read only settings needed for the current task and redact diagnostic output.
- The existing GGUF is `Qwen3.5-4B-UD-Q6_K_XL.gguf` at the repository root. Never modify, move, copy, hash the whole file, commit it, or expose its full path through HTTP.
- All meeting processing must remain local. Never send audio, transcripts, participant data or summaries to external APIs. Explicit dependency/model downloads may use the network without uploading meeting data. Respect model license/account requirements; do not accept agreements on the user's behalf.
- Python 3.12, Windows 10/11, RTX 4050 Laptop with 6 GB VRAM, 24 GB RAM. Use `.venv-meeting`, not the pre-existing broken `.venv`.
- Keep llama-server separate and bound only to `127.0.0.1`; inspect its installed `--help` before changing launch flags. Public FastAPI defaults to port 27361, llama-server to 27362.
- Keep SQLite, plain HTML/CSS/JS, one heavy job at a time, configurable devices and sequential heavy stages. No Redis/Celery, generic tool registry, autonomous shell tools or agent recursion.
- LAN binding requires `X-Agent-API-Key`; browser secrets belong only in sessionStorage. `/health` must return only `{"status":"ok"}` without authentication.
- Do not commit automatically. If a commit is later explicitly requested, do not add AI contributor attribution or co-author trailers.

## Immediate objective

Turn the implemented but only partly validated application into a demonstrated real-model meeting workflow. Prioritize integration and factual correctness before styling or architectural expansion.

1. Establish the current baseline using the checks below. Confirm installed Python, ffmpeg, llama-server and speech model caches without hashing the GGUF or exposing secrets.
2. Inspect the implemented selection/attempt contract and rerun the latest full suite; do **not** redo it as if absent. Resolve missing local dependencies and model setup using official sources and the user's selected defaults. Verify Qwen model compatibility, actual server flags and structured-output behavior. Start CPU ASR and CPU diarization first. The current `.[models]` specifies `pyannote.audio>=3.4,<4` but was not installed; prove (or correct) compatibility with the requested 3.1 pipeline rather than assuming it.
3. Test a short real audio sample through normalization, ASR, diarization, task extraction/verification, deadlines, summary, persistence and exports. Never substitute fake inference and describe it as real-model success.
4. Investigate the high-priority correctness issues in `DESIGN_REVIEW.md`, especially context budgeting, evidence/assignee semantics, silent candidate loss, stale audio after replacement and preservation of manual edits during reprocessing. Add targeted regression tests for fixes.
5. Process both supplied recordings, without modifying the originals, once the short integration test succeeds. Use an actual known meeting date or leave it unspecified. Compare results with the reference protocols separately; do not feed reference answers into extraction and call that evaluation.
6. Verify the browser flow, evidence seeking, manual name changes, task statuses, authentication and export downloads. Visually inspect DOCX/PDF output with Cyrillic and Kazakh text, long tasks and multipage transcripts.
7. Record exact model/dependency versions, working settings, timing, failures and remaining limitations. Update the handoff files with new evidence, preserving the distinction between historical and newly verified results.

If a required dependency, model license, credential or tool is unavailable, complete independent work and report the exact blocker. Do not claim all acceptance criteria are met merely because fake-model tests pass.

## Baseline commands

Run from the repository root in PowerShell:

```powershell
.\.venv-meeting\Scripts\python.exe -m pytest
.\.venv-meeting\Scripts\python.exe -m ruff check src/meeting_protocol tests scripts/manual_integration.py
.\.venv-meeting\Scripts\python.exe -m mypy
node --check src/meeting_protocol/web/app.js
node --test tests/test_web_models.js
git diff --exit-code -- initial_data
git diff --check
```

Launch in separate terminals after configuration:

```powershell
.\scripts\start_llama_server.ps1
```

```powershell
.\.venv-meeting\Scripts\python.exe -m meeting_protocol
```

Open `http://127.0.0.1:27361`. Read `scripts/manual_integration.py` before using it; it creates a meeting and saves results under `data/manual/`.

## Final report

Report actual changes, checks and results; exact launch commands; model/dependency setup still needed; real-model quality and timing if measured; remaining defects and limitations. Distinguish implemented, fake-tested, live-HTTP-tested, visually verified and real-model-verified. Do not claim more than the evidence supports.
