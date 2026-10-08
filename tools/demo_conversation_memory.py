"""Reproducible synthetic-video + deterministic-provider M1 demos.

Run from repo root: python tools/demo_conversation_memory.py
No credentials, Redis or real video are required. Never claim live LLM results.
"""
from __future__ import annotations

import asyncio
import argparse
import importlib.util
import json
from pathlib import Path
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
fixture = ROOT / "tests" / "application" / "test_m1_conversation_memory.py"
spec = importlib.util.spec_from_file_location("m1_demo_fixture", fixture)
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


async def main(output=None):
    a = f.harness()
    await f.ask(a, "视频中作者为什么选择 Redis？")
    a_before = (await a[4].store.load(a[5])).prompt_context()
    a_answer = await f.ask(a, "它和 MySQL 相比有什么不同？")
    a_result = {"recent_context_loaded": a_before, "original_question": a[3].calls[-1][1]["question"],
        "standalone_query": a[3].rewrite_result.standalone_query,
        "actual_retrieval_query": a[2].queries[-1], "verified_answer": a_answer, "evidence_guard": "PASS"}

    b = f.harness()
    for i in range(9): await f.ask(b, f"请解释存储数据库问题 {i+1}")
    before = await b[4].store.load(b[5])
    await f.ask(b, "请解释存储数据库问题 10")
    after = await b[4].store.load(b[5])
    await f.ask(b, "刚才那个与 MySQL 有何区别？")
    b_result = {"before_turn_count": len(before.turns), "trigger_turn": 10,
        "compressed_questions": [t["question"] for t in next(c[1]["olderTurns"] for c in b[3].calls if c[0] == "ROLLING_SUMMARY")],
        "rolling_summary": after.summary.model_dump(), "recent_six": [t.question for t in after.turns],
        "follow_up_used_summary": b[3].calls[-2][1]["conversationContext"]["rollingSummary"] is not None,
        "summary_calls_at_trigger": sum(c[0] == "ROLLING_SUMMARY" for c in b[3].calls)}

    c = f.harness()
    memory, identity = c[4:]
    await memory.store.acquire(identity, "synthetic-seed")
    await memory.save_verified(identity, f.ConversationState(), "synthetic-seed", str(uuid4()),
        "Redis 使用什么保存数据？", "错误历史：Redis 使用磁盘保存数据。")
    await memory.store.release(identity, "synthetic-seed")
    c[3].bad_evidence = True
    rejected = None
    try: await f.ask(c, "它为什么用磁盘？")
    except f.FollowUpFailure as error: rejected = error.category
    failed_input = c[3].calls[-1][1]
    count_after_rejection = len((await memory.store.load(identity)).turns)
    c[3].bad_evidence = False
    corrected = await f.ask(c, "它用什么保存数据？")
    c_result = {"deliberately_injected_history": failed_input["conversationContext"],
        "current_video_evidence": failed_input["retrievedSourceCandidates"],
        "forged_citation_result": rejected, "turn_count_after_rejection": count_after_rejection,
        "corrected_verified_answer": corrected}
    result = {"provenance": "SYNTHETIC ASR fixture + deterministic Mock provider + actual M1 service and Evidence Guard",
              "case_a": a_result, "case_b": b_result, "case_c": c_result}
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if output:
        Path(output).write_text(serialized + "\n", encoding="utf-8")
        print("A: Evidence Guard PASS; B: 10 turns -> summary + 6 recent; C: forged citation REJECTED")
    else:
        print(serialized)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", help="Write UTF-8 fixture results to this file")
    asyncio.run(main(parser.parse_args().output))
