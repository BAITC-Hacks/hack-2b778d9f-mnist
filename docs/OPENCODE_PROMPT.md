# OpenCode continuation prompt

Copy the text below into OpenCode, opened at this repository root.

---

You are working directly in the existing `hack-2b778d9f-mnist` repository root (use its actual local checkout path) on the HackAlem AI local meeting protocol application. Continue the existing implementation. Do not create a nested project or rebuild it as a generic agent framework.

First run `git status --short` and `git ls-files`. Read these files in order:

1. `docs/IMPLEMENTATION_HISTORY.md`
2. `docs/REMAINING_WORK.md`
3. `docs/DESIGN_REVIEW.md`
4. `README.md`, `pyproject.toml`, `.env.example`
5. The implementation under `src/meeting_protocol/`, domain prompts and tests.

Treat these documents as a handoff, not proof that the application works with real models. Inspect the current code and rerun checks. Much of the implementation is untracked; do not remove it with git clean/reset or overwrite it as disposable scaffolding. No implementation commit was made.

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
2. Resolve missing local dependencies and model setup using official sources and the user's existing model. Verify Qwen model compatibility, actual server flags and structured-output behavior. Start CPU ASR and CPU diarization first.
3. Test a short real audio sample through normalization, ASR, diarization, task extraction/verification, deadlines, summary, persistence and exports. Never substitute fake inference and describe it as real-model success.
4. Investigate the high-priority correctness issues in `DESIGN_REVIEW.md`, especially context budgeting, evidence/assignee semantics, silent candidate loss, stale audio after replacement and preservation of manual edits during reprocessing. Add targeted regression tests for fixes.
5. Process both supplied recordings, without modifying the originals, once the short integration test succeeds. Use an actual known meeting date or leave it unspecified. Compare results with the reference protocols separately; do not feed reference answers into extraction and call that evaluation.
6. Verify the browser flow, evidence seeking, manual name changes, task statuses, authentication and export downloads. Visually inspect DOCX/PDF output with Cyrillic and Kazakh text, long tasks and multipage transcripts.
7. Record exact model/dependency versions, working settings, timing, failures and remaining limitations. Update the handoff files with new evidence, preserving the distinction between historical and newly verified results.

If a required dependency, model license, credential or tool is unavailable, complete independent work and report the exact blocker. Do not claim all acceptance criteria are met merely because fake-model tests pass.

## Baseline commands

Run from the repository root in PowerShell:

```powershell
$env:PATH = "$PWD\.venv-meeting\Scripts;$env:PATH"
python -m pytest
python -m ruff check src/meeting_protocol tests scripts/manual_integration.py
python -m mypy
node --check src/meeting_protocol/web/app.js
git diff --exit-code -- initial_data
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
