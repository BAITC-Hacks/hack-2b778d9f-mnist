from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import TypeVar, cast
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ValidationError

from .config import ROOT, Settings
from .models import Extraction, MappingResult, MeetingSummary
from .profiles import AttemptSnapshot, LLMProfile

T = TypeVar("T", bound=BaseModel)
_OPERATIONS = {"extract_tasks", "verify_tasks", "resolve_speakers", "generate_summary"}
_INPUT_RESERVE_TOKENS = 256


def parse_json[T: BaseModel](text: str, schema: type[T]) -> T:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    # Only accept a complete JSON object; never evaluate Python or model output.
    return schema.model_validate_json(text)


def _safe_loopback_url(base_url: str) -> str:
    try:
        parsed = urlsplit(base_url)
        hostname = parsed.hostname
        loopback = hostname in {"localhost", "::1"} or (
            hostname is not None and ipaddress.ip_address(hostname).is_loopback
        )
        if (
            parsed.scheme != "http"
            or not loopback
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or "?" in base_url
            or "#" in base_url
        ):
            raise ValueError
        return base_url.rstrip("/") + "/chat/completions"
    except (ValueError, TypeError):
        raise ValueError(
            "LLM endpoint must be a loopback HTTP URL without credentials or query"
        ) from None


class LocalLLM:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
        snapshot: AttemptSnapshot | None = None,
    ):
        self.settings = settings
        self.transport = transport
        self.snapshot = snapshot
        # Retain the legacy public attribute, while keeping capability state scoped per profile.
        self.structured = True
        self._structured_by_profile: dict[tuple[str, str], bool] = {}

    def _profile(self, operation: str) -> LLMProfile | None:
        if operation not in _OPERATIONS:
            raise ValueError("Unknown local LLM operation")
        if self.snapshot is None:
            return None
        return cast(LLMProfile, self.snapshot.profile_for(operation))

    def _profile_values(
        self, profile: LLMProfile | None
    ) -> tuple[str, str, int, int, float, float, str]:
        if profile is None:
            return (
                self.settings.llm_base_url,
                self.settings.llm_model,
                self.settings.llm_context_size,
                self.settings.llm_max_output_tokens,
                self.settings.llm_temperature,
                self.settings.llm_timeout_seconds,
                "legacy",
            )
        return (
            profile.base_url,
            profile.served_alias,
            profile.context_size,
            profile.max_output_tokens,
            profile.temperature,
            profile.timeout_seconds,
            profile.id,
        )

    async def request(self, operation: str, payload: dict, schema: type[T]) -> T:
        profile = self._profile(operation)
        base_url, alias, context_size, max_output_tokens, temperature, timeout, profile_id = (
            self._profile_values(profile)
        )
        endpoint = _safe_loopback_url(base_url)
        prompt_file = ROOT / "prompts" / f"{operation}.md"
        if not prompt_file.exists():
            prompt_file = Path(__file__).parent / "prompt_templates" / f"{operation}.md"
        prompt = prompt_file.read_text(encoding="utf-8-sig")
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
        try:
            payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            raise ValueError("Local model request payload is not serializable") from None
        messages = [
            {
                "role": "system",
                "content": prompt + "\nReturn JSON matching: " + schema_json,
            },
            {"role": "user", "content": payload_json},
        ]
        input_budget_chars = context_size - max_output_tokens - _INPUT_RESERVE_TOKENS
        initial_payload_size = sum(len(item["content"].encode("utf-8")) for item in messages)
        if input_budget_chars <= 0 or initial_payload_size > input_budget_chars:
            raise ValueError("Local model request exceeds the configured context budget")
        state_key = (base_url.rstrip("/"), profile_id)
        structured = self._structured_by_profile.get(state_key, True)

        async with httpx.AsyncClient(
            timeout=timeout,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            for attempt in range(2):
                body = {
                    "model": alias,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_output_tokens,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
                if structured:
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema.__name__,
                            "strict": True,
                            "schema": schema.model_json_schema(),
                        },
                    }
                try:
                    response = await client.post(endpoint, json=body)
                    if response.status_code in {400, 422} and structured:
                        structured = False
                        self._structured_by_profile[state_key] = False
                        self.structured = False
                        body.pop("response_format")
                        response = await client.post(endpoint, json=body)
                    response.raise_for_status()
                    response_data = response.json()
                    content = response_data["choices"][0]["message"]["content"]
                    if not isinstance(content, str) or len(content) > max_output_tokens * 4:
                        raise ValueError
                except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                    raise RuntimeError("Local model request failed safely") from None
                try:
                    return parse_json(content, schema)
                except (ValidationError, ValueError):
                    if attempt:
                        raise ValueError(
                            "Local model returned invalid structured JSON after one repair"
                        ) from None
                    repair_messages = messages + [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": "Repair your JSON to the supplied schema. JSON only. Preserve evidence; do not invent facts.",
                        },
                    ]
                    repair_size = sum(
                        len(item["content"].encode("utf-8")) for item in repair_messages
                    )
                    if repair_size > input_budget_chars:
                        raise ValueError(
                            "Local model repair exceeds the configured context budget"
                        ) from None
                    messages = repair_messages
        raise RuntimeError("Local model request failed safely")

    async def extract_tasks(self, payload: dict) -> Extraction:
        return await self.request("extract_tasks", payload, Extraction)

    async def verify_tasks(self, payload: dict) -> Extraction:
        return await self.request("verify_tasks", payload, Extraction)

    async def resolve_speakers(self, payload: dict) -> MappingResult:
        return await self.request("resolve_speakers", payload, MappingResult)

    async def generate_summary(self, payload: dict) -> MeetingSummary:
        return await self.request("generate_summary", payload, MeetingSummary)
