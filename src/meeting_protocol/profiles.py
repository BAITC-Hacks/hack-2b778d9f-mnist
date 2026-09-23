"""Model profile catalog and immutable, attempt-scoped runtime snapshots."""

import json
import re
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from .audio import (
    SetupError,
    _validate_ct2_artifact,
    _validate_diarization_artifact,
    check_model_dependencies,
)
from .config import ROOT, Settings
from .models import ModelSelection, ProfileId, ResolvedBindings

OPERATIONS = ("asr", "diarization", "extract_tasks", "verify_tasks", "resolve_speakers", "generate_summary")


def _public_text(value: str) -> str:
    lowered = value.lower()
    if (not value.strip() or re.search(r"[\x00-\x1f\x7f]", value)
            or value.startswith(("/", "\\", "~")) or re.search(r"[a-zA-Z]:[/\\]", value)
            or "://" in value or "\\" in value or "@" in value
            or re.search(r"(?:^|[\s/])\.\.?(?:/|$)", value)
            or re.search(r"(?:sk-|ghp_|gho_|github_pat_|hf_|bearer\s|api[_-]?key\s*[:=]|password\s*[:=]|token\s*[:=])", lowered)):
        raise ValueError("Public model metadata must not contain paths, endpoints or credentials")
    return value


class ProfileError(RuntimeError):
    def __init__(self, code: str, slot: str | None, message: str):
        super().__init__(message)
        self.code, self.slot, self.message = code, slot, message


class _Profile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)
    id: ProfileId
    label: str = Field(min_length=1, max_length=120)
    model_identity: str = Field(min_length=1, max_length=240)
    compatible_slots: tuple[str, ...]
    availability: Literal["configured", "missing_files", "server_unavailable", "not_verified", "incompatible"]
    reason: str | None = None
    runtime_artifact: str = Field(default="", repr=False)

    @field_validator("label", "model_identity")
    @classmethod
    def safe_metadata(cls, value: str) -> str:
        return _public_text(value)

    @property
    def _artifact(self) -> str:
        return self.runtime_artifact

    @property
    def _url(self) -> str:
        return self.base_url if isinstance(self, LLMProfile) else ""

    def runtime(self, artifact: str = "", url: str = ""):
        if not isinstance(artifact, str) or (artifact and not Path(artifact).is_absolute()):
            raise ValueError("Runtime artifact must be an absolute local path")
        if url and (not isinstance(self, LLMProfile) or url != self.base_url):
            raise ValueError("Runtime endpoint does not match profile endpoint")
        return type(self).model_validate({**self.model_dump(), "runtime_artifact": artifact})


class ASRProfile(_Profile):
    kind: Literal["asr"] = "asr"
    backend: Literal["faster-whisper"] = "faster-whisper"
    device: str = "cpu"
    compute_type: str = "int8"


class DiarizationProfile(_Profile):
    kind: Literal["diarization"] = "diarization"
    backend: Literal["pyannote"] = "pyannote"
    device: str = "cpu"


class LLMProfile(_Profile):
    kind: Literal["llm"] = "llm"
    backend: Literal["llama-server"] = "llama-server"
    max_context: int = Field(8192, ge=4096, le=131072, strict=True)
    max_output_tokens: int = Field(2048, ge=256, le=32768, strict=True)
    base_url: str = "http://127.0.0.1:27362/v1"
    served_alias: str = "qwen3.5-4b-local"
    context_size: int = Field(8192, ge=4096, le=131072, strict=True)
    temperature: float = Field(0.1, ge=0, le=2)
    timeout_seconds: float = Field(120, ge=0.1, le=600)

    @model_validator(mode="after")
    def validate_budget(self):
        if self.context_size != self.max_context or self.context_size - self.max_output_tokens < 2256:
            raise ValueError("Reserve at least 2000 input tokens and 256 request tokens")
        ProfileCatalog._validate_loopback_url(self.base_url)
        if not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", self.served_alias):
            raise ValueError("Invalid local model alias")
        return self


class AttemptSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version: Literal[1] = 1
    bindings: ResolvedBindings
    profiles: tuple[ASRProfile | DiarizationProfile | LLMProfile, ...]

    def profile_for(self, operation: str):
        if operation not in OPERATIONS:
            raise KeyError(operation)
        profile_id = getattr(self.bindings, operation)
        return next(profile for profile in self.profiles if profile.id == profile_id)


class _CustomBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: ProfileId
    label: str = Field(min_length=1, max_length=120)
    model_identity: str = Field(min_length=1, max_length=240)
    operations: tuple[str, ...]
    artifact_path: str

    @field_validator("label", "model_identity")
    @classmethod
    def safe_metadata(cls, value: str) -> str:
        return _public_text(value)


class _CustomASR(_CustomBase):
    kind: Literal["asr"]
    operations: tuple[str, ...]
    device: str = "cpu"
    compute_type: str = "int8"


class _CustomDiarization(_CustomBase):
    kind: Literal["diarization"]
    operations: tuple[str, ...]
    device: str = "cpu"


class _CustomLLM(_CustomBase):
    kind: Literal["llm"]
    operations: tuple[str, ...]
    base_url: str
    served_alias: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")
    context_size: int = Field(8192, ge=4096, le=131072, strict=True)
    max_output_tokens: int = Field(2048, ge=256, le=32768, strict=True)
    temperature: float = Field(0.1, ge=0, le=2, allow_inf_nan=False)
    timeout_seconds: float = Field(120, ge=0.1, le=600, allow_inf_nan=False)


class ProfileCatalog:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.profiles: dict[str, _Profile] = {}
        self.defaults = {slot: ("whisper-turbo-local" if slot == "asr" else
                                "pyannote-3.1-local" if slot == "diarization" else "qwen-4b-local")
                         for slot in OPERATIONS}
        asr_path = self._path(settings.asr_model_artifact)
        diar_path = self._path(settings.diarization_model_artifact)
        gguf = self._path(settings.model_path)
        asr_ready = self._has_ct2(asr_path)
        diar_ready = self._has_diarization(diar_path)
        self.profiles["whisper-turbo-local"] = ASRProfile(
            id="whisper-turbo-local", label="Whisper Turbo (local)", model_identity="openai/whisper-large-v3-turbo",
            compatible_slots=("asr",), availability="configured" if asr_ready else "missing_files",
            device=settings.asr_device, compute_type=settings.asr_compute_type,
            reason=None if asr_ready else "Local CTranslate2 artifact is unavailable").runtime(str(asr_path))
        self.profiles["pyannote-3.1-local"] = DiarizationProfile(
            id="pyannote-3.1-local", label="Pyannote 3.1 (local)", model_identity="pyannote/speaker-diarization-3.1",
            compatible_slots=("diarization",), availability="configured" if diar_ready else "missing_files",
            device=settings.diarization_device,
            reason=None if diar_ready else "Local diarization pipeline config or weights are unavailable").runtime(str(diar_path))
        self.profiles["qwen-4b-local"] = LLMProfile(
            id="qwen-4b-local", label="Qwen 4B (local)", model_identity="Qwen3.5-4B-UD-Q6_K_XL",
            compatible_slots=OPERATIONS[2:], availability="not_verified" if gguf.is_file() else "missing_files",
            reason=None if gguf.is_file() else "Local GGUF artifact is unavailable",
            max_context=settings.llm_context_size,
            context_size=settings.llm_context_size, max_output_tokens=settings.llm_max_output_tokens,
            temperature=settings.llm_temperature, timeout_seconds=settings.llm_timeout_seconds,
            base_url=settings.llm_base_url, served_alias=settings.llm_model).runtime(str(gguf), settings.llm_base_url)
        # Explicit legacy paths receive separate identities; no known upstream identity is inferred.
        if "asr_model" in settings.model_fields_set and settings.asr_model != "large-v3-turbo":
            legacy = self._path(Path(settings.asr_model))
            ready = self._has_ct2(legacy)
            self.profiles["legacy-asr"] = ASRProfile(
                id="legacy-asr", label="Legacy ASR (identity unverified)",
                model_identity="unknown (legacy ASR_MODEL)", compatible_slots=("asr",),
                availability="configured" if ready else "missing_files",
                reason=None if ready else "Legacy local CTranslate2 artifact is unavailable",
                device=settings.asr_device, compute_type=settings.asr_compute_type).runtime(str(legacy))
            self.defaults["asr"] = "legacy-asr"
        if "diarization_model" in settings.model_fields_set and settings.diarization_model:
            legacy = self._path(Path(settings.diarization_model))
            ready = self._has_diarization(legacy)
            self.profiles["legacy-diarization"] = DiarizationProfile(
                id="legacy-diarization", label="Legacy diarization (identity unverified)",
                model_identity="unknown (legacy DIARIZATION_MODEL)", compatible_slots=("diarization",),
                availability="configured" if ready else "missing_files",
                reason=None if ready else "Legacy local pipeline config or weights are unavailable",
                device=settings.diarization_device).runtime(str(legacy))
            self.defaults["diarization"] = "legacy-diarization"
        self._load_custom(settings.model_profiles_json)
        if settings.model_default_bindings_json:
            try:
                data = json.loads(settings.model_default_bindings_json)
                if not isinstance(data, dict) or set(data) - set(OPERATIONS):
                    raise ValueError
                for slot, profile_id in data.items():
                    if (not isinstance(profile_id, str) or len(profile_id) > 64
                            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]|\.[a-z0-9])*[a-z0-9]", profile_id)
                            or profile_id not in self.profiles or slot not in self.profiles[profile_id].compatible_slots):
                        raise ValueError
                self.defaults.update(data)
            except (ValueError, TypeError):
                raise ProfileError("invalid_configuration", None, "Model default bindings are invalid") from None

    @classmethod
    def from_settings(cls, settings: Settings) -> "ProfileCatalog":
        try:
            return cls(settings)
        except (ValidationError, ValueError, TypeError):
            raise ProfileError("invalid_configuration", None, "Model profiles configuration is invalid") from None

    def _path(self, path: Path) -> Path:
        return path if path.is_absolute() else (ROOT / path).resolve()

    @staticmethod
    def _has_ct2(path: Path) -> bool:
        try:
            _validate_ct2_artifact(path)
            return True
        except SetupError:
            return False

    @staticmethod
    def _has_diarization(path: Path) -> bool:
        try:
            _validate_diarization_artifact(path)
            return True
        except SetupError:
            return False

    def _load_custom(self, raw: str) -> None:
        if not raw:
            return
        try:
            entries = json.loads(raw)
            if not isinstance(entries, list):
                raise TypeError
            for entry in entries:
                if not isinstance(entry, dict):
                    raise TypeError
                kind = entry.get("kind")
                if not isinstance(kind, str):
                    raise TypeError
                input_class = {"asr": _CustomASR, "diarization": _CustomDiarization, "llm": _CustomLLM}.get(kind)
                if input_class is None:
                    raise ValueError
                spec = cast(Any, input_class).model_validate(entry)
                if spec.id in self.profiles:
                    raise ValueError
                allowed = {"asr": {"asr"}, "diarization": {"diarization"}, "llm": set(OPERATIONS[2:])}[kind]
                if not spec.operations or len(set(spec.operations)) != len(spec.operations) or not set(spec.operations) <= allowed:
                    raise ValueError
                artifact = self._safe_local_path(spec.artifact_path)
                if kind == "asr":
                    exists = self._has_ct2(artifact)
                    profile = ASRProfile(id=spec.id, label=spec.label, model_identity=spec.model_identity,
                        compatible_slots=spec.operations, availability="configured" if exists else "missing_files",
                        reason=None if exists else "Local CTranslate2 artifact is unavailable",
                        device=spec.device, compute_type=spec.compute_type).runtime(str(artifact))
                elif kind == "diarization":
                    exists = self._has_diarization(artifact)
                    profile = DiarizationProfile(id=spec.id, label=spec.label, model_identity=spec.model_identity,
                        compatible_slots=spec.operations, availability="configured" if exists else "missing_files",
                        reason=None if exists else "Local diarization pipeline config is unavailable",
                        device=spec.device).runtime(str(artifact))
                else:
                    self._validate_loopback_url(spec.base_url)
                    exists = artifact.is_file()
                    profile = LLMProfile(id=spec.id, label=spec.label, model_identity=spec.model_identity,
                        compatible_slots=spec.operations, availability="not_verified" if exists else "missing_files",
                        reason=None if exists else "Local GGUF artifact is unavailable",
                        base_url=spec.base_url, served_alias=spec.served_alias, context_size=spec.context_size,
                        max_context=spec.context_size, max_output_tokens=spec.max_output_tokens,
                        temperature=spec.temperature, timeout_seconds=spec.timeout_seconds).runtime(str(artifact), spec.base_url)
                self.profiles[spec.id] = profile
        except (ValidationError, ValueError, KeyError, TypeError, OSError):
            raise ProfileError("invalid_configuration", None, "Model profiles configuration is invalid") from None

    @staticmethod
    def _safe_local_path(raw_path: str) -> Path:
        if not raw_path or "\x00" in raw_path or "\n" in raw_path or "\r" in raw_path:
            raise ValueError
        normalized = raw_path.replace("\\", "/")
        if normalized.startswith("//") or any(part == ".." for part in normalized.split("/")):
            raise ValueError
        if "://" in normalized:
            raise ValueError
        return ProfileCatalog._normalize_path(Path(raw_path))

    @staticmethod
    def _normalize_path(path: Path) -> Path:
        return path if path.is_absolute() else (ROOT / path).resolve()

    @staticmethod
    def _validate_loopback_url(value: str) -> str:
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            if parsed.scheme != "http" or hostname != "127.0.0.1" or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError
            _ = parsed.port
            if not re.fullmatch(r"[A-Za-z0-9._~/-]*", parsed.path) or any(segment in {".", ".."} for segment in parsed.path.split("/")):
                raise ValueError
            return value.rstrip("/")
        except (ValueError, TypeError):
            raise ProfileError("invalid_configuration", None, "LLM endpoint must be a safe loopback HTTP URL") from None

    def resolve(self, selection: ModelSelection) -> AttemptSnapshot:
        ids = {}
        for slot in OPERATIONS:
            profile_id = getattr(selection, slot) or self.defaults[slot]
            profile = self.profiles.get(profile_id)
            if profile is None or slot not in profile.compatible_slots:
                raise ProfileError("invalid_profile", slot, "Selected model profile is invalid for this operation")
            ids[slot] = profile_id
        chosen = {profile_id: self.profiles[profile_id] for profile_id in ids.values()}
        llms = [p for p in chosen.values() if isinstance(p, LLMProfile)]
        for profile in llms:
            self._validate_loopback_url(profile.base_url)
        for index, profile in enumerate(llms):
            for other in llms[index + 1:]:
                if profile.base_url.rstrip("/") == other.base_url.rstrip("/"):
                    if Path(profile._artifact).resolve() != Path(other._artifact).resolve():
                        raise ProfileError("conflicting_server_models", None, "Selected profiles require different local models on one server")
                    if profile.served_alias != other.served_alias or profile.model_identity != other.model_identity:
                        raise ProfileError("conflicting_server_alias", None, "Selected profiles cannot use different aliases for one loaded model")
        typed_profiles = tuple(cast(ASRProfile | DiarizationProfile | LLMProfile, p.model_copy(deep=True)) for p in chosen.values())
        return AttemptSnapshot(bindings=ResolvedBindings(**ids), profiles=typed_profiles)

    def public_catalog(self) -> dict:
        return {"schema_version": 1, "defaults": {slot: self.defaults[slot] for slot in OPERATIONS},
                "profiles": [{"id": p.id, "label": p.label, "model_identity": p.model_identity,
                              "operations": list(p.compatible_slots), "availability": p.availability,
                              **({"reason_code": {"missing_files": "local_artifact_missing",
                                                   "server_unavailable": "local_server_unavailable",
                                                   "not_verified": "not_verified",
                                                   "incompatible": "incompatible"}.get(p.availability, "configured")}
                                 if p.availability != "configured" else {})}
                             for p in self.profiles.values()]}

    async def preflight(self, snapshot: AttemptSnapshot) -> None:
        for profile in snapshot.profiles:
            path = Path(profile.runtime_artifact) if profile.runtime_artifact else None
            if isinstance(profile, ASRProfile):
                try:
                    if path is None or profile.backend != "faster-whisper":
                        raise SetupError("Invalid local ASR artifact")
                    _validate_ct2_artifact(path)
                except SetupError:
                    raise ProfileError("missing_files", "asr", "Local CTranslate2 model files are unavailable") from None
                try:
                    check_model_dependencies("asr")
                except SetupError:
                    raise ProfileError("incompatible", "asr", "Install compatible ASR model dependencies (faster-whisper and CTranslate2)") from None
            if isinstance(profile, DiarizationProfile):
                try:
                    if path is None or profile.backend != "pyannote":
                        raise SetupError("Invalid local diarization artifact")
                    _validate_diarization_artifact(path)
                except SetupError:
                    raise ProfileError("missing_files", "diarization", "Local diarization config or weights are unavailable") from None
                try:
                    check_model_dependencies("diarization")
                except SetupError:
                    raise ProfileError("incompatible", "diarization", "Install compatible pyannote.audio 3.x, torch, torchaudio and soundfile") from None
            if isinstance(profile, LLMProfile):
                if path is None or not path.is_file():
                    raise ProfileError("missing_files", None, "Local GGUF artifact is unavailable")
                base_url = self._validate_loopback_url(profile.base_url)
                url = urlsplit(base_url)
                try:
                    async with httpx.AsyncClient(timeout=1.5, trust_env=False, follow_redirects=False) as client:
                        response = await client.get(f"{url.scheme}://{url.netloc}{url.path.rstrip('/')}/models")
                        response.raise_for_status()
                        body = response.json()
                        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
                            raise TypeError
                        data = body["data"]
                        if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in data):
                            raise ValueError
                        aliases = {item["id"] for item in data}
                        if profile.served_alias not in aliases:
                            raise ProfileError("server_alias_missing", None, "Local server does not report the selected model alias")
                        api_prefix = url.path.rstrip("/").removesuffix("/v1")
                        props = await client.get(f"{url.scheme}://{url.netloc}{api_prefix}/props")
                        props.raise_for_status()
                        properties = props.json()
                        if not isinstance(properties, dict):
                            raise TypeError
                        reported = properties.get("model_path")
                        if not isinstance(reported, str) or not reported.strip() or reported.strip().lower() == "none":
                            raise ProfileError("identity_unverified", None, "Local server model identity could not be verified")
                        if not Path(reported).is_absolute() or Path(reported).resolve() != path.resolve():
                            raise ProfileError("identity_unverified", None, "Local server model artifact does not match the selected profile")
                except httpx.HTTPError:
                    raise ProfileError("server_unavailable", None, "Local model server is unavailable") from None
                except (ValueError, TypeError, AttributeError):
                    raise ProfileError("identity_unverified", None, "Local server model identity could not be verified") from None
