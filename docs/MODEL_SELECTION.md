# Task-specific model selection

User-requested defaults, recorded 2026-09-23. The contract below now has a configured and offline/fake-tested implementation; it is **not** a report of installed models, real inference, or a successful meeting demo. Before implementation the application had one ASR setting, one diarization setting and one shared LLM configuration. The earlier instructions and acceptance list are retained as the handoff contract; actual coverage and limitations are recorded at the end.

## Default task assignments

| Pipeline task | Default model or implementation | Runtime |
| --- | --- | --- |
| Audio normalization | ffmpeg, no AI model | Local executable, mono 16 kHz WAV |
| Speech recognition / transcription | `openai/whisper-large-v3-turbo` | faster-whisper with a compatible CTranslate2 artifact; CPU/int8 initially |
| Speaker diarization | `pyannote/speaker-diarization-3.1` | Compatible local pyannote pipeline; CPU initially |
| Word/segment-to-speaker alignment | Deterministic overlap logic | Python |
| Task and decision extraction | `Qwen3.5-4B-UD-Q6_K_XL` | Existing GGUF via loopback llama-server |
| Task and decision verification | `Qwen3.5-4B-UD-Q6_K_XL` | Same default server/profile |
| Speaker-to-participant inference | `Qwen3.5-4B-UD-Q6_K_XL` | Same default server/profile |
| Summary generation and reduction | `Qwen3.5-4B-UD-Q6_K_XL` | Same default server/profile |
| Deadline normalization | Deterministic resolver | Python; do not delegate calendar arithmetic to the LLM |
| Persistence and DOCX/PDF exports | SQLite, python-docx, ReportLab | No AI model |

The four LLM operations can be selected independently, but all inherit the same Qwen profile by default. Do not load four copies of Qwen. A meeting task (an extracted assignment) is not the unit of model selection: pipeline operations are.

## Model identity is different from its runtime artifact

### Whisper

`openai/whisper-large-v3-turbo` identifies the selected original model. Its model card provides Transformers usage; faster-whisper uses CTranslate2 weights. Keep the chosen identity in metadata and resolve a compatible converted local artifact separately. Do not put the original repository ID into `WhisperModel(...)` and assume format compatibility. [Official Whisper model card](https://huggingface.co/openai/whisper-large-v3-turbo), [faster-whisper conversion instructions](https://github.com/SYSTRAN/faster-whisper#model-conversion).

Prefer an explicit local conversion of the selected checkpoint or a verified compatible conversion with documented source/revision. Record quantization, tokenizer/config artifacts and conversion version. Any setup download/conversion is separate from meeting processing. During processing require local cached files, automatic/multilingual recognition and timestamps. No OpenAI API call is involved: the `openai/` prefix denotes the model publisher.

### Pyannote

Use exactly the requested `pyannote/speaker-diarization-3.1` as the default identity. Its model card describes gated access, including access to segmentation dependencies, and a pyannote 3.1-era API. Read the current official instructions and establish a compatible Python 3.12/Windows dependency set; do not infer the needed library version from the pipeline name alone. [Official diarization model card](https://huggingface.co/pyannote/speaker-diarization-3.1).

The preceding implementation specified `pyannote.audio>=4,<5` and recommended community-1; the current `pyproject.toml` instead specifies `pyannote.audio>=3.4,<4` and defaults to 3.1. This heavy dependency has **not** been installed or validated with the actual pipeline on Windows. Metadata-only preflight rejects pyannote versions outside 3.x; the YAML config must have local, existing segmentation and embedding weights. A pipeline config alone is not a complete offline model. Never silently substitute community-1 if 3.1 fails. Download-time gated access must not become a required runtime API token.

### Qwen

The selected artifact is the existing root `Qwen3.5-4B-UD-Q6_K_XL.gguf`. Keep it in place. The current server alias `qwen3.5-4b-local` may remain as an API-facing identifier; it is not a different model. Inspect installed llama-server help and verify the served model. Preflight reads `/v1/models` for that alias and read-only `/props` at the origin/API prefix for an absolute `model_path` matching the selected runtime artifact; GET needs no `--props` flag (that flag enables POST mutation). This compares a **server-reported path**, not content, a hash or model accuracy. Never hash the whole GGUF for provenance.

A normal single-model llama-server does not switch weights merely because a request uses another `model` value. Supporting an alternate LLM selection requires a verified matching local server/artifact configuration. If it is unavailable, show an actionable error. Do not automatically launch or swap heavyweight servers from browser-supplied commands.

## Bounded profile configuration

The implementation uses a small typed catalog of known local profiles, not a generic plugin registry or autonomous router. It keeps existing Settings and `.env` keys and adds optional `MODEL_PROFILES_JSON`, `MODEL_DEFAULT_BINDINGS_JSON`, `ASR_MODEL_ARTIFACT` and `DIARIZATION_MODEL_ARTIFACT`. See [README](../README.md#configure) for an operator-only example.

Each profile needs a safe stable ID (internal dots allowed, no path traversal), display label, supported operation(s), original model identity, backend, local runtime reference and device/compute settings. Paths and endpoint configuration are operator-controlled and never accepted as browser profile selections. Public labels/identities reject absolute and dot-segment paths (`../`), URLs, credentials, control characters and known token prefixes. An LLM profile also resolves an HTTP `127.0.0.1` base URL (not `localhost` or alternate loopbacks), served alias, finite bounded context/output budgets with input reserve, temperature and timeout.

Suggested default profile IDs:

- `whisper-turbo-local`
- `pyannote-3.1-local`
- `qwen-4b-local`

Required operation slots: `asr`, `diarization`, `extract_tasks`, `verify_tasks`, `resolve_speakers`, `generate_summary`. No speculative alternative models need to be prepopulated. A single configured choice per slot is valid; make additional configured compatible profiles selectable later without editing domain logic.

Resolution precedence: explicit meeting/process selection, then operator-configured defaults, then built-ins (or identity-unverified legacy defaults when old explicit speech paths are set). Operator default IDs must exist and support their slots. Resolved snapshots are stored per processing attempt; old snapshots do not change when defaults change. Preserve existing `.env`; do not overwrite it. Speech adapters validate supported backend and complete local artifact structure, not a whitelist of upstream identities: custom/legacy identities remain operator metadata, not proof of weight provenance. Unsupported formats, incomplete weight files, Hub references and missing dependencies fail rather than download.

## API and browser behavior

- Expose a safe model catalog, for example `GET /api/v1/model-profiles`, protected by the same API authentication. Return profile IDs, labels, operation compatibility and coarse availability states.
- Accept profile IDs only in meeting configuration/processing input. Reject unsupported operations, unconfigured IDs and arbitrary paths, remote endpoints or download identifiers supplied by the browser.
- Show ASR and diarization choices plus an advanced section for the four LLM operations. Display defaults without making selection a required extra step.
- Use clear states such as configured, missing files, server unavailable and not yet verified; do not label a model ready solely because a setting contains a name. The catalog shares the audio adapters' CT2 and parsed pyannote YAML/weight validation. A local GGUF is `not_verified`; no catalog state promises an inference test.
- Keep private filesystem paths, tokens and full internal profile objects out of HTTP responses, validation errors and public metadata. Return safe profile/run identifiers instead.
- Show the selected profiles for each completed/failed processing attempt. Configuration changes apply to future attempts, never silently to existing results. Attempt IDs identify executions, not a meeting's mutable configuration; SQL freezes source and snapshot across execution. A single-process OS lock is obtained before restart recovery.
- Preflight must not download models, process an entire recording or call external services. Admission validates CT2 model/config/tokenizer/preprocessor and pyannote YAML local weights; checks installed dependency versions without importing heavy modules; and probes the loopback server `/models` alias plus root/API-prefix GET `/props` `model_path`. Absent/relative/nonmatching identity fails safely. This is a server-reported path check, **not** complete weight loading, hash verification, an offline traffic audit or an accuracy check. Missing setup gives a safe path-free error, not a fallback to another model.

## Resource defaults

CPU/int8 ASR, CPU diarization, one heavy processing job at a time, sequential stages. The four LLM operations reuse one local Qwen server by default. Keep offload, context and generation settings configurable; measure practical settings for the RTX 4050 6 GB rather than promising that maximum offload fits. Selection of different profiles does not authorize parallel GPU loading.

## Acceptance tests

1. Omitted selections resolve to all requested defaults.
2. Each LLM operation can independently resolve a configured profile, with no accidental cross-operation override.
3. Original Whisper identity and its converted artifact remain distinct; unsupported artifact/backend combinations fail clearly.
4. Pyannote 3.1 is not silently replaced; missing dependent weights produce a safe setup error.
5. Unknown IDs, incompatible tasks, browser paths and remote LLM endpoints are rejected.
6. Profile selection survives SQLite save/load and each run keeps its original snapshot after default changes.
7. Catalog/meeting/error responses contain no model paths, credentials or hidden runtime configuration.
8. No downloads or network/model dependencies occur in ordinary tests. Use fake profiles/adapters for selection tests.
9. A separate real integration run proves the selected artifacts actually load and complete a short recording offline. Until then describe defaults as configured/requested, not validated.

## Implementation evidence and boundaries (2026-09-23)

`profiles.py` resolves six slots, rejects unknown/incompatible IDs and conflicting GGUFs on one endpoint, keeps original model identity separate from the private runtime artifact, and snapshots settings for SQLite processing attempts. `api.py` exposes an authenticated safe catalog, meeting settings, process overrides and public attempt IDs/status/resolved bindings. `processor.py` uses the frozen attempt snapshot; `audio.py` accepts complete local CT2 and pyannote artifacts; `llm.py` routes its four calls by selected profile. The dashboard shows choices and latest-attempt IDs/bindings, preserving selected IDs on auth reconnect. Private SQLite holds paths and full snapshots; protect its filesystem access. New diarization results do not automatically inherit manual speaker mappings. See README for exact JSON shape and keys.

Before the feature, 33 offline tests passed; an intermediate run had 64 passing. The final offline suite has **117 passed**, one Starlette/httpx deprecation warning; a legacy direct meeting-ID processor test was migrated to admitted attempts with fake inference and public metadata dot-segment regressions were added. Prescribed Ruff, `python -m mypy` (14 source files), JS syntax, four Node behavioral tests, original-data and whitespace checks passed. An optional `mypy .` including tests found missing YAML stubs; source-only mypy is not global strict typing. PyYAML 6.0.3 was installed as a lightweight parser; `.[models]` and actual weights were **not** installed. These checks use fakes and local fixtures, **not** real Qwen/pyannote/Whisper startup, live browser visual tests or supplied recordings. Neither the server-reported `model_path` nor complete preflight file checks prove the content of loaded weights, successful decoding, 3.1 runtime compatibility or accuracy. Complete acceptance item 9 and the real-world aspects of items 3–4 and 7–8 during a separate measured integration; keep correctness and visual checks on the [remaining-work list](REMAINING_WORK.md).
