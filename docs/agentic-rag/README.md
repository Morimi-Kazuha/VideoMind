# Bounded Agentic RAG

VideoMind 的独立 Follow-up 在既有 Hybrid、Evidence Guard 和 M1 上增加可关闭的三路线与一次证据反馈。工程状态为 **CODE FREEZE READY — LIVE GATE PENDING**；R6 真实完整端到端验收尚未完成，没有生产质量提升承诺。

## 模块与流程

| 模块 | 职责与边界 |
|---|---|
| M1 | 原问题、最近六轮与摘要；需要时改写指代，历史不能成为视频证据；receipt/lease/CAS/revision隔离 |
| Retrieval Need | 保守规则判断 DIRECT_LLM / VIDEO_RAG / VIDEO_PLUS_KNOWLEDGE，不调用分析模式分类模型 |
| DIRECT | 独立知识Adapter、MODEL_KNOWLEDGE来源；零检索/Reflection/Repair，仍检查身份与revision |
| VIDEO | 原Hybrid与来源Guard；检索候选不等于语义支持 |
| MIXED | 先核验视频事实，再独立生成知识块；来源分开保存与展示 |
| M2 | 复杂问题可提出2–3条抽取式初始子查询；不评证据充分性 |
| Reflection | 同一有界回答投影的一次严格Assessment；UNKNOWN或非法输出不授权补检索 |
| Feedback | 独立Gap Admission、确定性Query Repair、最多一次基础Hybrid；不递归M2，不再次Grader |
| Request budgets | 模型每HTTP attempt准入/结算；基础Hybrid最多initial3+repair1；60s总deadline |

```text
M1 → Need → DIRECT: knowledge answer
          → VIDEO/MIXED: optional M2 → Strict Hybrid → shared projection
              → optional Reflection → safe gap? → one baseline Hybrid
              → grounded answer → Guard → MIXED knowledge when selected
          → ownership/revision recheck → M1 CAS → Markdown API data
```

AnalysisMode 的 GENERAL/LEARNING/REVIEW/CREATION 由用户手选，默认 GENERAL；旧 POST /analysis/route 返回固定 GENERAL 兼容结果，无分类LLM。任务键和历史具体模式不变。Jev FAST/BALANCED/DEEP 只选择模型档位，与 Need 和 AnalysisMode 独立。

## 配置与关闭

配置示例在根目录 `.env.example`，仅服务端设置可开启能力，HTTP参数不能扩大执行权限。

| 配置 | 默认与约束 |
|---|---|
| DOVIDEO_RETRIEVAL_NEED_MODE | OFF / SHADOW / ENABLED，默认OFF；SHADOW只观察 |
| DOVIDEO_MIXED_FOLLOW_UP_ENABLED | false；Need ENABLED且来源Writer兼容才执行MIXED |
| DOVIDEO_MEMORY_SOURCE_WRITE_ENABLED | false；ENABLED Need要求兼容Writer，所有共享Reader先升级 |
| DOVIDEO_FOLLOW_UP_BUDGET_MODE | OFF / SHADOW / ENFORCED，默认OFF |
| DOVIDEO_FOLLOW_UP_MAX_CHAT_TOKENS | ENFORCED必须配置经过校准的正整数，无建议生产阈值 |
| DOVIDEO_EVIDENCE_REFLECTION_MODE | OFF / SHADOW，默认OFF；可选评价需要Token余量 |
| DOVIDEO_EVIDENCE_REFLECTION_REASONING_EFFORT | 默认空；可选none仅接受已经验证的model/provider组合 |
| DOVIDEO_FEEDBACK_RETRIEVAL_MODE | OFF / SHADOW / ENABLED，默认OFF；ENABLED要求Reflection SHADOW、model、ENFORCED预算与可计数Hybrid |
| DOVIDEO_ADAPTIVE_RETRIEVAL_ENABLED | false，M2独立开关 |

安全基线是上述默认值。要启用三路线，先完成共享Reader升级和来源Writer迁移；要评估反馈，再单独启用Reflection SHADOW与ENFORCED请求预算，最后Feedback SHADOW/ENABLED。非法值/依赖组合在启动或构造拒绝，不打包为不可解释的“智能模式”。实验额度与供应商设置不会自动用于生产。

回滚时先关闭Feedback/MIXED/Need执行，保留已经兼容标签的Reader与Writer。关闭Need不关闭来源Writer。保存过DIRECT/MIXED的共享Redis记录不能直接交给旧strict Reader；须保持兼容Reader或在另行迁移后降级。关闭反馈恢复原VIDEO路径，不改变Jev与手动模式。

## 来源、时间与预算

Repair仅允许原问题明确视频缺口，严格重验Assessment、query与可信scope；补回hit必须匹配媒体调用范围、revision、canonical Chunk/Segment、SourceItem和原文。合并最多8候选。M2、Critic、X1原独立边界保持；Feedback只在Follow-up接入。

最终Guard验证原文、来源与半开时间窗，不证明全部自然语言结论必然正确。ASR前400、OCR前2项各400，以及有界sourceObservations由Reflection与回答共用。命中区域仍可能因裁剪丢失关键公式；没有扩大上下文或修改Chunking/ranking来修补个例。

60s总deadline、Reflection局部5s、VIDEO至少40s/MIXED至少55s剩余时间准入保持。下游预留与第二次Repair门可能导致跳过，需要真实阶段Usage与延迟分布才能校准。缺少Usage保守保留预留；不把未报告成本填0。Budget/deadline终止不能保存成功历史。

## 公开复现

安装 `pip install -e ".[test]"` 与client的 `npm ci`，然后运行：

```powershell
python -m pytest -q -p no:cacheprovider
# client目录
npm.cmd test
npm.cmd run build
```

合成fixtures位于 `tests/fixtures/agentic-rag/`：保留精确span、union、MIXED边界、调用额度与输入hash断言；tc编号的公开输入是独立合成替代，不是私有视频原文。`historical-replay.json`仅保留历史回放结果元数据，没有模型Prompt/原始响应或视频摘录；历史结果不能追溯验证新的合成输入。完整原实验报告、日志、缓存和原sidecar留在本地且被忽略。

无需付费调用的Runner：

```powershell
python tools/run_retrieval_need_shadow.py --output work/public-need-run
python tools/run_r2b_direct_comparison.py --output work/public-direct-run
python tools/run_r4_feedback_comparison.py --synthetic-only --output work/public-feedback-run
python tools/analyze_r4_budget_scenarios.py --output work/public-budget.json
```

每次使用全新输出路径。Need公开数据集只含合成/歧义安全案例；R4 synthetic-only仍使用真实应用Hybrid/BM25/Guard与替身外部端口。真实视频runner（R3、R4缓存组、R5自然初始候选、MIXED缓存组）需要使用者自己的合法缓存与冻结sidecar，不随仓库分发。R3.6/3.7可用 `--plan tests/fixtures/agentic-rag/r36/experiment-plan.json` / `r37/experiment-plan.json` 指定合成计划；真实Provider必须另有明确授权，`--live`不代表仓库授权付费。

## 验证范围与剩余门槛

确定性测试证明零检索DIRECT、来源隔离MIXED、一次Repair、最多4次Hybrid、cap8、异常停止、Memory恢复、模式兼容与预算并发隔离。受控fixture中的缺口恢复不能证明真实自然召回收益；缓存自然候选对照未证明事实可见性改善。Draft未经独立人审，不是Gold。

2026-10-14 已完成最终 24 次真实 Follow-up 请求，包括 11 题 A/D 配对和两题 Smoke，工程封板并转入维护。7 题来源核对评分的成功率均为 3/7，另外 4 题保留语义争议，质量结论为 INCONCLUSIVE。Reflection 实际完成 10 次 Assessment，Repair 实际执行 0 次，不能宣称 Feedback 质量收益；Holdout 未运行。独立人审必要事实/引用、完整上传链路和本轮跳过的 live 集成未被冒充通过。当前不默认开启新能力、不发布托管服务、不创建 Release；详见 [最终评测](../VIDEOMIND_AGENTIC_RAG_FINAL_EVALUATION.md) 与 [封板验收](../VIDEOMIND_FINAL_FREEZE_REPORT.md)。
