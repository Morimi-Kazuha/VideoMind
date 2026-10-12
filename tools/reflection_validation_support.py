"""Offline validation utilities; no production configuration changes."""
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from dovideo.infrastructure.providers.http import StdlibAsyncJsonPostClient

CHAT_NAMES = {
    "DOVIDEO_MODEL_TRANSPORT", "DOVIDEO_MODEL_BASE_URL", "DOVIDEO_MODEL_API_KEY",
    "DOVIDEO_MODEL_MODEL", "DOVIDEO_MODEL", "DOVIDEO_MODEL_NAME", "DOVIDEO_LLM_MODEL",
    "DOVIDEO_OPENROUTER_API_KEY", "DOVIDEO_MODEL_PROVIDER_TAG", "DOVIDEO_BALANCED_MODEL",
    "DOVIDEO_MODEL_TIMEOUT_SECONDS", "DOVIDEO_MODEL_MAX_ATTEMPTS", "DOVIDEO_MODEL_RETRY_DELAY_SECONDS",
}


def load_chat_environment(paths):
    """Only explicitly supplied project configuration, inherited values win.

    Load chat names only, never write files or serialize values/credentials.
    """
    inherited = set(os.environ)
    sources = []
    for path in paths:
        loaded, preserved = [], []
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
            match = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", line)
            if not match or match[1] not in CHAT_NAMES:
                continue
            name, value = match.groups()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if name in inherited:
                preserved.append(name)
            else:
                os.environ[name] = value
                loaded.append(name)
        sources.append({"file": str(Path(path).resolve()), "loaded_names": loaded, "preserved_names": preserved})
    return sources


def public_config(config):
    return None if config is None else {"endpoint_host": urlsplit(config.base_url).hostname,
        "model": config.model, "transport": config.transport, "provider_only": config.provider_only,
        "data_collection": config.provider_data_collection, "zdr": config.provider_zdr,
        "credential_present": bool(config.api_key)}


class RecordingHttp:
    """Retain structured model response and numeric usage, never headers/keys."""
    def __init__(self, secret=None):
        self.client = StdlibAsyncJsonPostClient()
        self.records = []
        self.secret = secret

    async def post(self, url, **kwargs):
        record = {"status_code": None, "raw_structured_response": None, "usage": None, "finish_reason": None,
                  "refusal": None, "safe_provider_error": None,
                  "reasoning_tokens": None}
        self.records.append(record)
        response = await self.client.post(url, **kwargs)
        record["status_code"] = response.status_code
        body = response.body
        if isinstance(body, dict):
            error = body.get("error")
            if isinstance(error, dict):
                message = str(error.get("message", ""))[:600]
                if self.secret: message = message.replace(self.secret, "[REDACTED]")
                record["safe_provider_error"] = {"code": error.get("code"), "message": message}
            usage = body.get("usage")
            if isinstance(usage, dict):
                record["usage"] = {k: v for k, v in usage.items() if k in {"prompt_tokens","completion_tokens","total_tokens","cost"} and type(v) in {int,float}}
                detail = usage.get("completion_tokens_details")
                if isinstance(detail, dict) and type(detail.get("reasoning_tokens")) in {int,float}:
                    record["reasoning_tokens"] = detail["reasoning_tokens"]
            choices = body.get("choices")
            if isinstance(choices, list) and choices:
                message = choices[0].get("message", {})
                raw = message.get("content")
                if isinstance(raw, str) and self.secret:
                    raw = raw.replace(self.secret, "[REDACTED]")
                record["raw_structured_response"] = raw
                record["finish_reason"] = choices[0].get("finish_reason")
                refusal = message.get("refusal")
                if isinstance(refusal, str):
                    record["refusal"] = refusal.replace(self.secret, "[REDACTED]") if self.secret else refusal
        return response


def attach_review(sidecar):
    """Freeze review before calling the model; no claims of independent review."""
    requirements = {
        "complete": ["明确 B 的选择原因"], "related": ["Redis 缓存失效的处理方法"],
        "cause": ["选择 B 的原因，不能用采用 B 的结果代替"],
        "comparison": ["A 与 B 各自的内存开销"], "temporal": ["初期、中期、后期的方案"],
        "multi": ["开头与结尾各自的推荐对象"], "clipped": ["B 的选择原因必须在裁剪后的文本中"],
        "empty": ["当前没有供生成使用的证据；不能断言整个视频缺失"],
        "mixed": ["视频中被评价的 Redis 解释内容；通用知识正确性不要求来自视频"],
        "legal_unrelated": ["红黑树删除旋转的具体实现"],
        "tc-17-partial-cache": ["3n+3 与 3n+1003 两处画面及不同时间位置"],
        "tc-18-partial-cache": ["四个 loveYou 版本的复杂度与搜索位置依赖"],
        "tc-20-partial-cache": ["红黑树删除修复代码和逐行推导时间；视频整体有无尚未确认"],
    }
    for case in sidecar["cases"]:
        case["review"] = {"kind": "DEVELOPER_LITERAL_PROJECTION_CHECK" if case["expected_status"] else "NEEDS_HUMAN_REVIEW_REAL_DRAFT",
            "independent_human_review": False, "necessary_facts": requirements[case["id"]],
            "scope": "provided bounded candidates only", "reviewed_before_model": True}
    return sidecar
