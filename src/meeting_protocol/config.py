from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    app_env: str = "development"
    public_host: str = "127.0.0.1"
    public_port: int = 27361
    agent_api_key: str = ""
    llm_base_url: str = "http://127.0.0.1:27362/v1"
    llm_model: str = "qwen3.5-4b-local"
    llm_context_size: int = Field(8192, ge=4096)
    llm_temperature: float = 0.1
    llm_max_output_tokens: int = Field(2048, ge=256)
    llm_timeout_seconds: float = 120
    ffmpeg_path: str = "ffmpeg"
    asr_model: str = "large-v3-turbo"
    asr_device: str = "cpu"
    asr_compute_type: str = "int8"
    diarization_model: str = ""
    diarization_device: str = "cpu"
    data_root: Path = Path("data")
    database_path: Path = Path("data/app.db")
    max_upload_bytes: int = Field(524288000, gt=0)
    processing_concurrency: int = Field(1, ge=1, le=1)
    pdf_font_path: str = ""

    @model_validator(mode="after")
    def validate_local(self):
        url = urlsplit(self.llm_base_url)
        if (
            url.scheme != "http"
            or url.hostname != "127.0.0.1"
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise ValueError("LLM_BASE_URL must be an HTTP 127.0.0.1 URL")
        if self.public_host not in {"127.0.0.1", "localhost", "::1"} and (
            len(self.agent_api_key) < 16 or self.agent_api_key.startswith("replace-")
        ):
            raise ValueError(
                "LAN binding requires a non-placeholder AGENT_API_KEY (16+ characters)"
            )
        if self.llm_max_output_tokens >= self.llm_context_size - 2000:
            raise ValueError("Reserve at least 2000 context tokens for instructions and transcript")
        return self
