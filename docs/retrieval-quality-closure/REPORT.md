# VideoMind Retrieval Quality Closure Report

## 1. Status

当前 Retrieval 尚未证明优于 Baseline；真实 BGE-M3 → Qdrant 路径受环境阻塞，状态为 **NOT READY**。
本轮完成源码审计、真实 embedding 预检、脱敏诊断工具、历史负实验分层定位及回归测试；真实 A/B/C 质量闭环尚未完成。

## 2. Baseline

HEAD before / after：`da5b2ffc5c2d1cec65874c0c0627b70ebc34a4f4`（未创建 commit，未 push）。分支 `main`。
开始时只有 `?? docs/interview-guide/`；6 个原有文件保持未跟踪、未暂存、未提交。新增内容仅为评估工具、测试和本目录。
Frozen algorithm baseline：`c7c02707911c8cb823d7f7a46c328469689256b7`，由既有工具从 Git 加载完整旧 chunking/retrieval/provenance 源码；没有手工模拟旧算法。
配置来源：同项目原工作树的 `.env.r2.local`、`.env.r4.local`，只加载检索设置到评估进程；进程原有配置优先。没有复制、修改或提交环境文件。
Python 3.13.3；全部项目依赖及测试依赖已核实。最终后端：1089 passed / 17 skipped。初次临时目录 ACL 错误保留在 tests.json。
Dataset digest：`f1820acf0b0aec90a64b0bcd9d7f4a8a8d204e70fe55cfd667e5da04ae569b71`。两个 v2 文件的 SHA-256 见 audit-manifest.json；历史 v1/v2/X3 数据与产物未改动。

既有 X3 runner 在当前 ownership 检查下写入 gitSha=null / workingTreeState=UNKNOWN；每套实验旁的 run-metadata.json 和本轮 audit-manifest.json 通过仅针对当前仓库的 safe.directory 命令核实并保存了真实 SHA 与完整 working tree，不把 UNKNOWN 隐藏为 CLEAN。

## 3. Live Preflight

| Component | Status | Evidence |
|---|---|---|
| BGE-M3 | PASS | api.siliconflow.cn；BAAI/bge-m3；真实响应为 1024 维有限值向量 |
| Qdrant health/read/write/dimension | FAILED / NOT_RUN | 127.0.0.1；连接失败（URLError）；collection 为 video_chunks_r4_bge_m3；未测读写及维度 |
| Reranker endpoint/model | NOT_CONFIGURED / NOT_RUN | 缺少 DOVIDEO_RERANKER_API_KEY；配置默认 disabled；模型可用性未测 |
Docker 引擎管道不存在。尝试启动已安装 Docker Desktop 后，backend 在初始化 Ingest server 时无法重命名 `sailor-ingest.sock`，取消启动；WSL docker-desktop 为 Stopped。
未通过本地 cosine 替代 Qdrant，没有把 TF-IDF 或测试替身结果归类为 live PASS。没有执行 live case，因此 vectorStoreFallbacks delta、collection scope 和 stale point 排除均为 NOT_RUN。

最终完整实验命令重试被自动审批拒绝，理由是可能向外部 embedding Provider 与 Qdrant 传送数据集文本。随后获准执行固定短探针的 --preflight-only；没有绕过拒绝发送数据集文本。此前获准的完整尝试也在 Qdrant 预检失败后停止，未执行任何 live case。

## 4. Experimental Arms

| Arm | Frozen/current path | Real-provider execution |
|---|---|---|
| A | Frozen fixed 5min + old semantic/keyword/OCR weighted chunk scores + old Segment ranking | NOT_RUN：Qdrant 不可达 |
| B | Sliding 5min/1min overlap + BGE-M3/Qdrant + BM25/RRF + reranker OFF + Segment ranking | NOT_RUN：Qdrant 不可达 |
| C | 与 B 相同，唯一变化为启用 BAAI/bge-reranker-v2-m3 | NOT_RUN：Qdrant 不可达且 reranker 未配置 |
所有 12 个 v2 case 原样保留。新增工具使用现有 EvaluationRunner；冻结相同 source artifact、revision、query 和本地 summary/planner，仅切换检索算法及指定 Provider。
真实 BGE/Qdrant 测量仍将保留确定性的本地 summary/planner，与历史输入口径一致；不代表生产 LLM summary/planner 的端到端质量。旧 baseline 的 Top6 之外使用 cosine 是冻结算法本身的语义，会明确记录，不能当成 Final Qdrant fallback。
固定参数：Dense8 / Sparse8 / Fusion10 / Final3；RRF k=60；BM25 k1=1.2、b=.75；Segment .55/.25/.20；stride 4min、overlap 1min。没有调参。

## 5. Aggregate Results

真实 Provider 路径：

| Metric | Baseline | Final OFF | Final ON |
|---|---|---|---|
| recall@1 | NOT_RUN | NOT_RUN | NOT_RUN |
| recall@3 | NOT_RUN | NOT_RUN | NOT_RUN |
| recall@5 | NOT_RUN | NOT_RUN | NOT_RUN |
| precision@1 | NOT_RUN | NOT_RUN | NOT_RUN |
| precision@3 | NOT_RUN | NOT_RUN | NOT_RUN |
| precision@5 | NOT_RUN | NOT_RUN | NOT_RUN |
| mrr | NOT_RUN | NOT_RUN | NOT_RUN |
| temporal_hit | NOT_RUN | NOT_RUN | NOT_RUN |
| temporal_coverage | NOT_RUN | NOT_RUN | NOT_RUN |

**OFFLINE LOCAL RESULT**（TF-IDF + InMemoryVectorIndex + local summary/planner；复现指标与历史文件逐项相同）：

| Metric | Frozen Baseline | Final OFF | Final ON |
|---|---|---|---|
| recall@1 | 0.833333 | 0.750000 | NOT_RUN |
| recall@3 | 1.000000 | 0.833333 | NOT_RUN |
| recall@5 | 1.000000 | 0.833333 | NOT_RUN |
| precision@1 | 0.916667 | 0.833333 | NOT_RUN |
| precision@3 | 0.388889 | 0.333333 | NOT_RUN |
| precision@5 | 0.233333 | 0.200000 | NOT_RUN |
| mrr | 0.958333 | 0.861111 | NOT_RUN |
| temporal_hit | 1.000000 | 1.000000 | NOT_RUN |
| temporal_coverage | 1.000000 | 1.000000 | NOT_RUN |

两个层级必须分开：离线 Final 的 Dense Recall@8=1、RRF Recall@3=1、Final Chunk Recall@3=1；Segment Recall@3=0.833333、MRR=0.861111。
Chunk Recall 定义为每个 expected Segment 是否至少有一个所属 parent 被该阶段的 TopK 覆盖，先按 case 平均再总体平均。边界 case 的多个 parent 全部保留。
Precision@K、MRR、temporal 指标沿用既有 EvaluationRunner。Temporal hit/coverage 以所有返回 EvidenceHit 计算，因此 rank 6 仍可得到 temporal hit=1，不代表 Recall@3 成功。

## 6. Category Results

真实 A/B/C 的全部六类指标均为 NOT_RUN。以下仅为离线负对照（每类两个 case）：

| Category | Baseline Recall@3 / MRR | Final OFF Recall@3 / MRR | Final ON |
|---|---|---|---|
| asr_only | 1.000000 / 1.000000 | 1.000000 / 1.000000 | NOT_RUN |
| boundary_crossing | 1.000000 / 1.000000 | 1.000000 / 1.000000 | NOT_RUN |
| distractor | 1.000000 / 1.000000 | 0.500000 / 0.583333 | NOT_RUN |
| exact_technical_terms | 1.000000 / 1.000000 | 1.000000 / 1.000000 | NOT_RUN |
| ocr_only | 1.000000 / 1.000000 | 1.000000 / 1.000000 | NOT_RUN |
| semantic_paraphrase | 1.000000 / 0.750000 | 0.500000 / 0.583333 | NOT_RUN |

## 7. semantic-01 Deep Dive

真实 A/B/C：NOT_RUN。以下全部为新生成的离线结构性 trace：

Expected segment：`seg_c40a98b083b5027aaeb50438a953a8f04b785fc6561ce05aff4dbcaf41ee916f`；时间范围 420000–480000ms（07:00–08:00）。
Final parent：`chunk_480042a984ecd04eaaeca520521295cef6955b6f1de8fef79bfbe52cb039bbf2`。

| Stage | Expected rank / count |
|---|---|
| Dense | 3 / 8 |
| BM25 | 1 / 8 |
| RRF | 2 / 8 |
| Final Chunk | 2 / 3 |
| Reranker | OFF；rank NOT_RUN |
| Segment / EvidenceHit | 6 / 6 |

parent_rank=2；parent_relevance=0.500000；ASR match=0.500000；OCR match=0.000000；final score=0.400000。
Frozen Baseline 的目标 EvidenceHit rank 为 2。Final 的 Recall@1/@3/@5 均为 false。
目标 parent 进入 Dense Top8、Sparse Top8、RRF Top10、Final3。Dense3 → BM25 支持后 RRF2，融合改善该 parent 的排名。**首次掉出 Recall@3 目标范围发生在 Segment Fine Ranking**。

## 8. distractor-02 Deep Dive

真实 A/B/C：NOT_RUN。以下全部为新生成的离线结构性 trace：

Expected segment：`seg_c40a98b083b5027aaeb50438a953a8f04b785fc6561ce05aff4dbcaf41ee916f`；时间范围 420000–480000ms（07:00–08:00）。
Final parent：`chunk_480042a984ecd04eaaeca520521295cef6955b6f1de8fef79bfbe52cb039bbf2`。

| Stage | Expected rank / count |
|---|---|
| Dense | 3 / 8 |
| BM25 | 1 / 5 |
| RRF | 2 / 8 |
| Final Chunk | 2 / 3 |
| Reranker | OFF；rank NOT_RUN |
| Segment / EvidenceHit | 6 / 6 |

parent_rank=2；parent_relevance=0.500000；ASR match=1.000000；OCR match=0.000000；final score=0.525000。
Frozen Baseline 的目标 EvidenceHit rank 为 1。Final 的 Recall@1/@3/@5 均为 false。
目标 parent 进入 Dense Top8、Sparse Top8、RRF Top10、Final3。Dense3 → BM25 支持后 RRF2，融合改善该 parent 的排名。**首次掉出 Recall@3 目标范围发生在 Segment Fine Ranking**。

## 9. Reranker Value

changed Top3 / rescued / harmed / unchanged：全部 NOT_RUN；没有可归因的实际 case ID。没有把 missing config 统计成 0 rescued 或 0 harmed。
工具已用单元测试验证 rescue/harm 统计、A/B/C 编排及 stale scope/cleanup；测试替身输出只存在于 pytest 临时目录，不属于真实实验产物。

## 10. Root Cause

**历史离线根因：Fine-Ranking Parent Dominance。真实 Provider 根因：NOT_MEASURED。**
两个 case 的 parent 为 Final rank2，Segment rank6；第一 parent 的 5 个 Segment 都排在目标之前。semantic-01 的目标得分 .55×.5+.25×.5=.400000；第一 parent 的零文本匹配候选也有 .55。
distractor-02 的目标 ASR match 已是 1，得分仍为 .55×.5+.25×1=.525000，低于第一 parent 的零文本匹配候选 .550000。因此不是这两个 case 的 candidate recall 或 Final3 cutoff 失败，也不是 dedup 丢失目标。
.55 与 parent_relevance=1/rank 的组合在这两个离线 ASR-only、parent-rank2 条件下构成可计算的排序瓶颈；尚未证明它是 BGE-M3 路径的实测瓶颈。没有调低权重或进行反事实调参。
源码确认 embedding 输入仍为 summary + newline + keywords。真实 summary representation failure、Dense recall、fusion 和 reranker failure 均未测得，不进行猜测性归因。未发现本轮可由数据支持的 production correctness bug。

## 11. Quality Decision

**E. Provider 环境不足，无法得出结论。**
TF-IDF 的负结果不能代表 BGE-M3 Hybrid Retrieval；embedding preflight PASS 也不能代表 Dense Recall PASS。严格 live A/B/C 都尚未执行，因此本章节不能封板，不能选择 A/B/C 的生产质量结论。

## 12. Production Recommendation

**FURTHER MEASUREMENT REQUIRED**。保持本轮生产参数与配置原状。没有证据支持默认开启 reranker、回退 production ranking 或推广新默认。
恢复现有 Docker/Qdrant collection 的健康与可读写状态；在本地安全配置 DOVIDEO_RERANKER_*（凭据不提交），然后使用新增工具重新执行全部 12 个 A/B/C case。不要只重跑两个失败 case。
本轮没有足够的真实 Provider 质量证据建议将这一质量结论 push 到公开 GitHub；没有 push、Release、rebase、squash。

## 13. Tests

最终 full backend：**1089 passed、17 skipped、0 failed / errors**；耗时 39.46s。17 项跳过的是要求显式 opt-in 的基础设施 live tests。
检索/evaluation/Qdrant/reranker/新增 diagnostics 定向回归：94 passed（包含 7 个新测试）。frontend：79 passed、0 failed；frontend production build：PASS。
初次 full backend：918 passed / 17 skipped / 164 setup errors；原因是旧 pytest temp ACL。以新的 workspace basetemp 重跑全部测试解决，没有因此修改业务代码。受限环境最小 asyncio 挂起，正常执行环境最小测试成功；所有有效 Python 测试和预检均在正常环境运行。

## 14. Changed Files

**NO PRODUCTION ALGORITHM CHANGE**。

- tools/run_retrieval_quality_closure.py：既有 runner 的真实 Provider preflight、冻结 A/B/C 编排、实例边界 stage trace、strict fallback 判定及独立离线负对照。
- tests/application/test_retrieval_quality_closure.py：原排序等价、重叠 parent/dedup、日志脱敏、环境优先级、rescue/harm、A/B/C scope/cleanup、fallback 拒绝。
- docs/retrieval-quality-closure/：本报告、audit/tests/blockers/failure-analysis、真实预检/未运行记录、离线 12-case 两路结果及排名诊断。
历史 docs/retrieval-evaluation-v1、v2、X3 artifacts 与 datasets/x3 保持原样；docs/interview-guide 保持未跟踪。没有提交。

## 15. Remaining Limits

| Item | State |
|---|---|
| Sample size | 12 SYNTHETIC cases，单一 30min 合成 source；真实视频 case 数为 0 |
| Live provider latency distribution | NOT_MEASURED（只有 embedding preflight，没有查询级统计） |
| Provider cost | NOT_MEASURED |
| Cross-video scale | NOT_MEASURED |
| No-answer behavior | NOT_MEASURED |
| Real long-video generalization | NOT_MEASURED |
| Live reranker quality / model availability | NOT_RUN |
| Live Qdrant scope / stale isolation / collection read-write | NOT_RUN |
| Production LLM summary/planner quality | NOT_MEASURED；实验采用冻结本地 summary/planner |

## 16. Interview Story

初版使用固定五分钟 Chunk 与简单 semantic/keyword/OCR 加权排序，易于解释和验证。为了处理跨边界 evidence、稀疏技术词与多阶段检索职责，升级为滑窗、Dense/BM25、RRF 和可选 reranker。架构职责更完整，并不自动意味着排序更好。
因此冻结旧 Git 源码与 12-case v2 输入，先做可比 A/B，再计划只开关 reranker 的第三路 ablation。历史 TF-IDF 实验中 Recall@3 从 1.000000 降到 .833333；新增 stage trace 显示两个退化 case 的 Chunk 已在 Final rank2，目标 Segment 却排到6，实际问题在父排名压制文本匹配的 fine ranking。没有因为小样本负结果盲目调整 .55。
真实 BGE-M3 返回1024维，说明 embedding endpoint 可用；但 Docker/Qdrant 环境阻塞，reranker缺少配置，无法诚实评价真实 Dense recall 或 reranker救回能力。最终决策是继续测量、保持本轮参数、不 push 质量封板结论。可解释的负结果和明确的未测边界，比宣称更复杂的 RAG 必然更好更可信。

复现（从项目根目录；PowerShell）：

```powershell
.\.venv\Scripts\python.exe tools/run_retrieval_quality_closure.py --output work/closure-next --env-file 'D:\Agent Learning\dovideo-python\.env.r2.local' --env-file 'D:\Agent Learning\dovideo-python\.env.r4.local'
.\.venv\Scripts\python.exe tools/run_retrieval_quality_closure.py --offline-diagnostics-only --output work/closure-offline-next
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp work/closure-pytest-next
```

使用空 output 目录；工具拒绝覆盖已有实验。遇到当前沙箱事件循环阻塞时，需要在正常执行环境运行。配置凭据只保存在原有本地环境或进程中。
