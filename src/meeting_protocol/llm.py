import json
import re
from pathlib import Path
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from .config import ROOT, Settings
from .models import Extraction, MappingResult, MeetingSummary

T = TypeVar("T", bound=BaseModel)


def parse_json[T: BaseModel](text: str, schema: type[T]) -> T:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    # Only accept a complete JSON object; never evaluate Python or model output.
    return schema.model_validate_json(text)


class LocalLLM:
    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport
        self.structured = True

    async def request(self, operation: str, payload: dict, schema: type[T]) -> T:
        prompt_file = ROOT / "prompts" / f"{operation}.md"
        if not prompt_file.exists():
            prompt_file = Path(__file__).parent / "prompt_templates" / f"{operation}.md"
        prompt = prompt_file.read_text(encoding="utf-8-sig")
        messages = [
            {
                "role": "system",
                "content": prompt
                + "\nReturn JSON matching: "
                + json.dumps(schema.model_json_schema()),
            },
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        async with httpx.AsyncClient(
            timeout=self.settings.llm_timeout_seconds,
            trust_env=False,
            follow_redirects=False,
            transport=self.transport,
        ) as client:
            for attempt in range(2):
                body = {
                    "model": self.settings.llm_model,
                    "messages": messages,
                    "temperature": self.settings.llm_temperature,
                    "max_tokens": self.settings.llm_max_output_tokens,
                    "chat_template_kwargs": {"enable_thinking": False},
                }
                if self.structured:
                    body["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema.__name__,
                            "strict": True,
                            "schema": schema.model_json_schema(),
                        },
                    }
                response = await client.post(
                    self.settings.llm_base_url.rstrip("/") + "/chat/completions", json=body
                )
                if response.status_code in {400, 422} and self.structured:
                    self.structured = False
                    body.pop("response_format")
                    response = await client.post(
                        self.settings.llm_base_url.rstrip("/") + "/chat/completions", json=body
                    )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                try:
                    return parse_json(content, schema)
                except (ValidationError, ValueError):
                    if attempt:
                        raise ValueError(
                            "Local model returned invalid structured JSON after one repair"
                        ) from None
                    messages += [
                        {"role": "assistant", "content": content},
                        {
                            "role": "user",
                            "content": "Repair your JSON to the supplied schema. JSON only. Preserve evidence; do not invent facts.",
                        },
                    ]
        raise RuntimeError("Unreachable")

    async def extract_tasks(self, payload: dict) -> Extraction:
        return await self.request("extract_tasks", payload, Extraction)

    async def verify_tasks(self, payload: dict) -> Extraction:
        return await self.request("verify_tasks", payload, Extraction)

    async def resolve_speakers(self, payload: dict) -> MappingResult:
        return await self.request("resolve_speakers", payload, MappingResult)

    async def generate_summary(self, payload: dict) -> MeetingSummary:
        return await self.request("generate_summary", payload, MeetingSummary)
