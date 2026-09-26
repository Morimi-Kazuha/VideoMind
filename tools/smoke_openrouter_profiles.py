"""Synthetic, non-golden connectivity check for the three frozen X3 profiles."""

from __future__ import annotations

import asyncio
import argparse
import json
import os
from dataclasses import replace
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from run_r4_live import load_local_environment

from dovideo.infrastructure.providers.config import ModelRequestSettings, ProviderConfig
from dovideo.infrastructure.providers.http import StdlibAsyncJsonPostClient, response_parts
from dovideo.infrastructure.providers.model import OpenAICompatibleChatClient


class CapturingClient:
    def __init__(self, *, include_tools: bool = False) -> None:
        self.delegate = StdlibAsyncJsonPostClient()
        self.include_tools = include_tools
        self.request: dict | None = None
        self.response: dict | None = None
        self.status: int | None = None

    async def post(self, url, *, headers, json, timeout):
        if self.include_tools:
            json = dict(json)
            json["tools"] = [{"type": "function", "function": {
                "name": "noop", "description": "No operation",
                "parameters": {"type": "object", "properties": {}},
            }}]
            json["tool_choice"] = "none"
        self.request = json
        response = await self.delegate.post(url, headers=headers, json=json, timeout=timeout)
        self.status, body = response_parts(response)
        self.response = body if isinstance(body, dict) else None
        return response


def generation_metadata(generation_id: str, api_key: str) -> dict:
    """Read OpenRouter's non-content generation metadata when available."""

    url = "https://openrouter.ai/api/v1/generation?" + urlencode({"id": generation_id})
    request = Request(url, headers={"Authorization": f"Bearer {api_key}"})
    for attempt in range(4):
        try:
            with urlopen(request, timeout=20) as response:
                body = json.load(response)
            data = body.get("data", {}) if isinstance(body, dict) else {}
            if data.get("provider_name"):
                return data
        except Exception:
            pass
        if attempt < 3:
            time.sleep(1)
    return {}


def endpoint_provider_name(model: str, tag: str) -> str:
    url = f"https://openrouter.ai/api/v1/models/{model}/endpoints"
    for attempt in range(3):
        try:
            with urlopen(url, timeout=30) as response:
                endpoints = json.load(response)["data"]["endpoints"]
            break
        except (OSError, TimeoutError):
            if attempt == 2:
                raise
    matches = [item for item in endpoints if item.get("tag") == tag]
    if len(matches) != 1:
        raise RuntimeError("provider tag does not identify one model endpoint")
    return matches[0]["provider_name"]


def record(result: dict) -> None:
    artifact = Path(__file__).resolve().parents[1] / "work" / "x3-openrouter-provider-smokes.jsonl"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    with artifact.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, sort_keys=True) + "\n")
    print(json.dumps(result, sort_keys=True))


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lane", choices=("FAST", "BALANCED", "DEEP"), required=True)
    parser.add_argument("--provider-tag", required=True)
    args = parser.parse_args()
    load_local_environment()
    os.environ["DOVIDEO_MODEL_TRANSPORT"] = "openrouter"
    os.environ["DOVIDEO_MODEL_PROVIDER_TAG"] = args.provider_tag
    os.environ["DOVIDEO_BALANCED_MODEL"] = "deepseek/deepseek-v4.1-flash"
    base = ProviderConfig.from_environment(required=True)
    assert base is not None
    if base.provider_only != (args.provider_tag,) or base.provider_data_collection != "deny" or base.provider_zdr is not True:
        raise RuntimeError("synthetic smoke requires pinned no-collection ZDR policy")
    checks = (
        ("FAST", "deepseek/deepseek-v4.1-flash", ModelRequestSettings(reasoning_effort="none")),
        ("BALANCED", "deepseek/deepseek-v4.1-flash", ModelRequestSettings()),
        ("DEEP", "deepseek/deepseek-v4-pro-0813", ModelRequestSettings(reasoning_effort="max", max_tokens=65_536)),
    )
    for lane, model, settings in checks:
        if lane != args.lane:
            continue
        provider_name = endpoint_provider_name(model, args.provider_tag)
        capture = CapturingClient(include_tools=lane == "DEEP")
        config = replace(base, model=model, max_attempts=1)
        client = OpenAICompatibleChatClient(config, request_settings=settings, client=capture)
        try:
            try:
                content = await client.complete(
                    ({"role": "user", "content": 'Return exactly one JSON object with key "ok" set to true.'},),
                    stage="NON_GOLDEN_SMOKE",
                )
            except Exception as exc:
                error = (capture.response or {}).get("error") or {}
                metadata = error.get("metadata") if isinstance(error, dict) else None
                record({
                    "lane": lane, "httpStatus": capture.status,
                    "requestedModel": model,
                    "requestedProvider": args.provider_tag,
                    "errorCode": error.get("code") if isinstance(error, dict) else None,
                    "errorMessage": error.get("message") if isinstance(error, dict) else None,
                    "errorProvider": metadata.get("provider_name") if isinstance(metadata, dict) else None,
                    "errorRaw": str(metadata.get("raw"))[:600] if isinstance(metadata, dict) else None,
                    "exceptionType": type(exc).__name__,
                })
                raise
            parsed = json.loads(content)
            body = capture.response or {}
            usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
            generation = (
                await asyncio.to_thread(generation_metadata, body["id"], config.api_key)
                if isinstance(body.get("id"), str) and config.api_key else {}
            )
            returned_provider = body.get("provider") or generation.get("provider_name")
            result = {
                "lane": lane, "httpStatus": capture.status,
                "jsonObject": isinstance(parsed, dict), "schemaOk": isinstance(parsed, dict) and parsed.get("ok") is True,
                "generationId": body.get("id"),
                "requestedModel": model, "returnedModel": body.get("model"),
                "requestedProvider": args.provider_tag, "returnedProvider": returned_provider,
                "reasoningEffort": settings.reasoning_effort,
                "reasoningRequestField": (capture.request or {}).get("reasoning_effort"),
                "toolsRequestField": "tools" in (capture.request or {}),
                "toolChoiceRequestField": (capture.request or {}).get("tool_choice"),
                "usage": {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
                "cost": usage.get("cost"),
                "generationCost": generation.get("total_cost"),
            }
            record(result)
            if capture.status != 200 or not isinstance(parsed, dict) or parsed.get("ok") is not True:
                raise RuntimeError(f"{lane} smoke did not satisfy JSON contract")
            if body.get("model") != model:
                raise RuntimeError(f"{lane} response model identity mismatch")
            if returned_provider not in (provider_name, args.provider_tag):
                raise RuntimeError(f"{lane} response provider identity mismatch")
            if any(usage.get(name) is None for name in ("prompt_tokens", "completion_tokens", "total_tokens")):
                raise RuntimeError(f"{lane} usage telemetry missing")
        finally:
            await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
