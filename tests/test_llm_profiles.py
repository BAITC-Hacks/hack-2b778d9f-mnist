import json
from types import SimpleNamespace

import httpx
import pytest

from meeting_protocol.config import Settings
from meeting_protocol.llm import LocalLLM
from meeting_protocol.models import Extraction


def profile(profile_id, alias, base_url, *, context=8192, output=512):
    return SimpleNamespace(
        id=profile_id,
        backend="llama.cpp",
        base_url=base_url,
        served_alias=alias,
        context_size=context,
        max_output_tokens=output,
        temperature=0.2,
        timeout_seconds=17.0,
        model_identity="private-model-identity",
        runtime_artifact="/private/model.gguf",
    )


class Snapshot:
    def __init__(self, profiles):
        self.profiles = profiles

    def profile_for(self, operation):
        return self.profiles[operation]


@pytest.fixture
def settings(tmp_path):
    return Settings(_env_file=None, data_root=tmp_path / "data", database_path=tmp_path / "db")


def ok_response():
    return httpx.Response(
        200, json={"choices": [{"message": {"content": '{"tasks": [], "decisions": []}'}}]}
    )


async def test_operation_profiles_select_frozen_endpoint_alias_and_budget(settings):
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        return ok_response()

    snapshot = Snapshot(
        {
            "extract_tasks": profile("one", "alias-one", "http://127.0.0.1:27362/v1", output=321),
            "verify_tasks": profile("two", "alias-two", "http://127.0.0.1:27363/v1", output=654),
        }
    )
    llm = LocalLLM(settings, httpx.MockTransport(handler), snapshot=snapshot)
    await llm.extract_tasks({"transcript": []})
    await llm.verify_tasks({"tasks": []})

    assert [url for url, _ in seen] == [
        "http://127.0.0.1:27362/v1/chat/completions",
        "http://127.0.0.1:27363/v1/chat/completions",
    ]
    assert [body["model"] for _, body in seen] == ["alias-one", "alias-two"]
    assert [body["max_tokens"] for _, body in seen] == [321, 654]
    assert all(body["temperature"] == 0.2 for _, body in seen)
    assert all("private-model-identity" not in json.dumps(body) for _, body in seen)
    assert all("/private/model.gguf" not in json.dumps(body) for _, body in seen)


async def test_structured_fallback_isolated_by_profile(settings):
    seen = []

    def handler(request):
        body = json.loads(request.content)
        seen.append((str(request.url), body))
        if str(request.url).endswith("27362/v1/chat/completions") and "response_format" in body:
            return httpx.Response(400, json={"error": "private server detail"})
        return ok_response()

    snapshot = Snapshot(
        {
            "extract_tasks": profile("profile-a", "a", "http://127.0.0.1:27362/v1"),
            "verify_tasks": profile("profile-b", "b", "http://127.0.0.1:27363/v1"),
        }
    )
    llm = LocalLLM(settings, httpx.MockTransport(handler), snapshot=snapshot)
    await llm.extract_tasks({})
    await llm.extract_tasks({})
    await llm.verify_tasks({})

    assert ["response_format" in body for _, body in seen] == [True, False, False, True]
    assert "private server detail" not in repr(seen)


async def test_payload_and_repair_are_bounded(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "x" * 1800}}]}
        )

    small = profile("small", "small", "http://127.0.0.1:27362/v1", context=4096, output=3500)
    llm = LocalLLM(settings, httpx.MockTransport(handler), snapshot=Snapshot({"extract_tasks": small}))
    with pytest.raises(ValueError, match="context budget"):
        await llm.extract_tasks({"transcript": "x" * 10000})
    assert calls == []

    # A response too large to safely append to a repair prompt is never sent as a second request.
    roomy = profile("roomy", "roomy", "http://127.0.0.1:27362/v1", context=4096, output=512)
    llm = LocalLLM(settings, httpx.MockTransport(handler), snapshot=Snapshot({"extract_tasks": roomy}))
    with pytest.raises(ValueError, match="repair exceeds"):
        await llm.extract_tasks({})
    assert len(calls) == 1


async def test_legacy_settings_request_behavior(settings):
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return ok_response()

    llm = LocalLLM(settings, httpx.MockTransport(handler))
    result = await llm.extract_tasks({"transcript": []})
    assert result == Extraction(tasks=[], decisions=[])
    assert seen[0]["model"] == settings.llm_model
    assert seen[0]["max_tokens"] == settings.llm_max_output_tokens


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/v1",
        "http://example.com/v1",
        "http://user:pass@127.0.0.1/v1",
        "http://127.0.0.1/v1?secret=value",
    ],
)
async def test_reject_nonlocal_or_credentialed_endpoints(settings, url):
    llm = LocalLLM(
        settings,
        httpx.MockTransport(lambda request: ok_response()),
        snapshot=Snapshot({"extract_tasks": profile("bad", "alias", url)}),
    )
    with pytest.raises(ValueError, match="loopback HTTP"):
        await llm.extract_tasks({})
