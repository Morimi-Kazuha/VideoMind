"""Freeze R3.7 data and replay historical outputs without any network calls."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from run_r36_reflection_optimization import digest, read, save
from reflection_prompt_snapshots import R3_SYSTEM, HistoricalReflectionAdapter
from dovideo.infrastructure.providers.evidence_reflection import EvidenceReflectionModelAdapter, SYSTEM
from dovideo.application.follow_up_budget import estimate_chat_input

OUT = ROOT / "docs/agentic-rag/r37-validation-v1"


def synthetic(id, question, text, status, facts, gap=None, *, mixed=False, ocr=None):
    return {"id": id, "split": "HOLDOUT_NOT_FOR_PARAMETER_SELECTION",
        "label_origin": "DEVELOPER_LITERAL_CHECK_NOT_INDEPENDENT_HUMAN", "human_approved": False,
        "input": {"question": question, "mixed": mixed,
            "candidates": [{"candidateIndex": 0, "startMs": 0, "endMs": 10000,
                "source": "ASR", "asrExcerpt": text, "ocrExcerpts": ocr or []}]},
        "expected_status": status, "expected_gap": gap,
        "review": {"kind": "DEVELOPER_LITERAL_PROJECTION_CHECK", "independent_human_review": False,
            "necessary_facts": facts, "scope": "provided bounded candidates only", "reviewed_before_model": True}}


def freeze():
    old = read(ROOT / "docs/agentic-rag/r36-validation-v1/frozen-sidecar-v1.json")
    lookup = {c["id"]: c for c in old["cases"]}
    ids = ["mixed", "h-mixed-video-boundary", "h-comparison-complete", "tc-17-partial-cache",
           "related", "cause", "multi", "clipped"]
    development = [deepcopy(lookup[id]) for id in ids]
    for case in development:
        case["split"] = "DEVELOPMENT_PREVIOUSLY_SEEN_NOT_HOLDOUT"
    negative = synthetic("d-mixed-incomplete", "作者描述的缓存命中、未命中及回填流程合理吗？",
        "作者说命中就返回缓存值；未命中就查询数据库。", "INSUFFICIENT",
        ["命中时的动作", "未命中时的动作", "查询数据库后的回填步骤（当前缺失）"],
        "PARTIAL_FACT_COVERAGE", mixed=True)
    negative["split"] = "DEVELOPMENT_REVERSE_MIXED_REGRESSION"
    # Keep the reverse control adjacent to the two known MIXED failures.
    development.insert(2, negative)
    holdout = [
        synthetic("n-tripod-cause", "讲者为什么给相机加三脚架？", "讲者用了三脚架，照片随后变清晰。",
            "INSUFFICIENT", ["讲者明确描述加三脚架的原因，结果不能代替原因"], "MISSING_CAUSAL_EVIDENCE"),
        synthetic("n-panel-comparison", "讲者给出的单晶和薄膜的转换率分别是多少？",
            "讲者说单晶转换率为百分之二十，薄膜转换率为百分之十。", "SUFFICIENT",
            ["单晶转换率百分之二十", "薄膜转换率百分之十；允许共用完整比较句 span"]),
        synthetic("n-film-temporal", "讲者在拍摄前、拍摄中和拍摄后分别怎样校准颜色？",
            "拍摄前用灰卡设白平衡，拍摄后用色卡做校色。", "INSUFFICIENT",
            ["拍摄前校准", "拍摄中校准（缺失）", "拍摄后校准"], "MISSING_TEMPORAL_CONTEXT"),
        synthetic("n-bread-mixed-full", "讲者先称面粉和水、再混合、最后盖住静置的做法合理吗？",
            "讲者先称面粉和水，再混合，最后盖住静置。", "SUFFICIENT",
            ["称面粉和水", "混合", "盖住静置；无需视频给出合理性 verdict 或烘焙通用原理"], mixed=True),
        synthetic("n-photo-mixed-gap", "讲者测光、设定曝光再拍摄的流程正确吗？",
            "讲者设定曝光后按快门拍摄。", "INSUFFICIENT",
            ["测光步骤（缺失）", "设定曝光", "拍摄；不要求视频给正确性 verdict"],
            "PARTIAL_FACT_COVERAGE", mixed=True),
        synthetic("n-ocr-clipped", "讲者为什么把灯调到低档？", "讲者把灯调到低档。", "INSUFFICIENT",
            ["低档选择的原因必须出现在有界 OCR/ASR；裁剪后没有原因"], "INSUFFICIENT_SOURCE_TEXT",
            ocr=["设置说明：" + "灯光设置。" * 79]),
    ]
    holdout[-1]["raw_segment_tail_only"] = "因为高档使样品反光。此尾部不在模型输入，禁止发送。"
    cases = development + holdout
    sidecar = {"version": "r37-frozen-v1", "prompt_fixed_before_new_holdout": True,
               "independent_human_review": False, "cases": cases}
    for name in ("frozen-sidecar-v1.json", "experiment-plan.json", "prompt-system.txt"):
        if (OUT / name).exists():
            raise FileExistsError("never overwrite frozen inputs")
    save(OUT / "frozen-sidecar-v1.json", sidecar)
    system = EvidenceReflectionModelAdapter.messages("", ())[0]["content"]
    (OUT / "prompt-system.txt").write_text(system, encoding="utf-8")
    planned = []
    for c in cases:
        inp = c["input"]
        messages = EvidenceReflectionModelAdapter.messages(inp["question"], tuple(inp["candidates"]), mixed=inp["mixed"])
        planned.append({"id": c["id"], "split": c["split"], "input_sha256": digest(inp),
            "messages_sha256": digest(messages), "estimated_input_tokens": estimate_chat_input(messages),
            "output_reserve": 1024, "worst_unreported_charge": estimate_chat_input(messages) + 1024})
    save(OUT / "experiment-plan.json", {"status": "PREPARED_NOT_AUTHORIZED_FOR_LIVE",
        "calibration_ids": [c["id"] for c in development], "holdout_ids": [c["id"] for c in holdout], "draft_ids": [],
        "model": "deepseek/deepseek-v4.1-flash", "provider_tag": "siliconflow/fp8", "reasoning_effort": "none",
        "completion_cap": 1024, "offline_timeout_seconds": 20, "production_parameters_changed": False,
        "prompt_sha256": hashlib.sha256(SYSTEM.encode()).hexdigest(),
        "full_system_sha256": hashlib.sha256(system.encode()).hexdigest(),
        "frozen_sidecar_sha256": hashlib.sha256((OUT / "frozen-sidecar-v1.json").read_bytes()).hexdigest(),
        "max_proposed_attempts": 15, "proposed_token_ceiling": 50000, "planned_cases": planned,
        "all_unreported_estimate_tokens": sum(p["worst_unreported_charge"] for p in planned),
        "stop_conditions": ["No retry/fallback; fixed provider/cap/effort", "Provider rejection or reasoning>0 stops",
            "Two consecutive unavailable results stop", "Global calls/tokens include BOTH cohorts; skip if headroom missing",
            "No prompt tuning from holdout; no output repair", "No paid calls without NEW explicit approval"]})


class ReplayChat:
    def __init__(self, raw):
        self.raw, self.calls = raw, 0
    async def complete(self, messages, *, stage):
        self.calls += 1
        assert stage == "EVIDENCE_REFLECTION"
        return self.raw


async def replay():
    old = ROOT / "docs/agentic-rag/r36-validation-v1"
    lookup = {c["id"]: c for c in read(old / "frozen-sidecar-v1.json")["cases"]}
    plan = read(old / "experiment-plan.json")
    assert hashlib.sha256(R3_SYSTEM.encode()).hexdigest() == plan["prompt_sha256"]
    rows = []
    for path in sorted(old.glob("*-none-1024/results.json")):
        for row in read(path):
            inp = lookup[row["id"]]["input"]
            raw = row["http"][0]["raw_structured_response"]
            chat = ReplayChat(raw)
            try:
                proposal = await EvidenceReflectionModelAdapter(chat).assess(inp["question"], tuple(inp["candidates"]), mixed=inp["mixed"])
                result, error = proposal.status, None
            except (ValueError, TypeError) as exc:
                result, error = "INVALID_RESPONSE", str(exc)[:1400]
            previous = HistoricalReflectionAdapter.messages(inp["question"], tuple(inp["candidates"]), mixed=inp["mixed"])
            assert digest(list(previous)) == row["http"][0]["sent_parameters"]["messages_sha256"]
            rows.append({"id": row["id"], "old_prediction": row["prediction"], "replay": result,
                "error": error, "fixture_calls": chat.calls, "network_calls": 0,
                "raw_sha256": hashlib.sha256(raw.encode()).hexdigest()})
    save(OUT / "historical-replay.json", {"mode": "DETERMINISTIC_NO_NETWORK_NO_SEMANTIC_REGRADING",
        "historical_wire_hashes_match": True, "strict_valid": sum(r["error"] is None for r in rows),
        "invalid": sum(r["error"] is not None for r in rows), "rows": rows,
        "note": "Prompt changes cannot retroactively fix old semantic output. Same strict DTO rejects the same two failures."})


if __name__ == "__main__":
    freeze()
    asyncio.run(replay())
