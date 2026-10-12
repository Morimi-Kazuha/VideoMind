import json
import pytest
from fastapi.testclient import TestClient
from test_m1_conversation_memory_api import app_with_memory, params
from dovideo.application.evidence_reflection import ReflectionMode, capture_evidence_reflection
from dovideo.application.follow_up_budget import FollowUpBudgetMode, FollowUpBudgetSettings
from dovideo.application.retrieval_need import RetrievalNeedMode, RetrievalNeedSettings
from dovideo.infrastructure.providers import OpenAICompatibleChatClient, ProviderConfig, ProviderHttpResponse


@pytest.mark.parametrize("route", ["VIDEO", "MIXED", "DIRECT"])
@pytest.mark.parametrize("bad", [False, True])
def test_actual_production_shadow_stage_guard_memory_and_auth(monkeypatch,route,bad):
    app,services,retrieval,_=app_with_memory(monkeypatch)
    services.evidence_reflection_mode=ReflectionMode.SHADOW
    services.follow_up_budget_settings=FollowUpBudgetSettings(FollowUpBudgetMode.ENFORCED,500000)
    services.retrieval_need_settings=RetrievalNeedSettings(RetrievalNeedMode.ENABLED)
    services.mixed_follow_up_enabled=True
    services.conversation_memory.store.write_sources=True
    calls=[]
    class Http:
        async def post(self,url,**kw):
            system=kw["json"]["messages"][0]["content"]
            if "Grade semantic sufficiency" in system:
                calls.append("EVIDENCE_REFLECTION")
                content={} if bad else {"status":"UNKNOWN","reason_code":"UNCERTAIN_SEMANTIC_SUPPORT","covered_requirements":[],"missing_requirements":[],"candidate_references":[]}
            elif "You have not viewed" in system:
                calls.append("DIRECT_FOLLOW_UP"); content={"answer":"Redis 常用于缓存。"}
            elif "source quotes" in system:
                calls.append("MIXED_KNOWLEDGE"); content={"knowledge_blocks":[{"kind":"INFERENCE","text":"单次遍历通常是线性复杂度。"}]}
            else:
                calls.append("FOLLOW_UP")
                text="算法的时间复杂度是 O(n)"
                content={"answer":text,"evidence":[{"candidateIndex":0,"timestampMs":1200,"source":"ASR","content":text,"claim":text}]}
            return ProviderHttpResponse(200,{"choices":[{"message":{"content":json.dumps(content,ensure_ascii=False)}}],"usage":{"total_tokens":20}})
    services.providers.chat_client=OpenAICompatibleChatClient(ProviderConfig(base_url="https://example.test/v1",model="fixture",max_attempts=1),client=Http())
    question={"VIDEO":"作者解释了什么？","MIXED":"作者解释的时间复杂度正确吗？","DIRECT":"不考虑当前视频，解释 Redis。"}[route]
    p={**params(),"question":question}; headers={"Authorization":"Bearer 7"}
    with TestClient(app) as client:
        assert client.post("/analysis/follow-up",params=p).status_code==401
        assert client.post("/analysis/follow-up",params=p,headers={"Authorization":"Bearer 8"}).status_code==403
        assert not calls
        result=client.post("/analysis/follow-up",params=p,headers=headers)
        assert result.status_code==200 and isinstance(result.json()["data"],str)
        assert client.post("/analysis/follow-up",params=p,headers=headers).json()==result.json()
        history=client.get("/analysis/follow-up/history",params=p,headers=headers).json()["data"]
        assert history["turns"][0]["answer"]==result.json()["data"]
        assert "missing_requirements" not in result.json()["data"]
    assert calls=={"VIDEO":["EVIDENCE_REFLECTION","FOLLOW_UP"],"MIXED":["EVIDENCE_REFLECTION","FOLLOW_UP","MIXED_KNOWLEDGE"],"DIRECT":["DIRECT_FOLLOW_UP"]}[route]
    assert len(retrieval.calls)==int(route!="DIRECT")
