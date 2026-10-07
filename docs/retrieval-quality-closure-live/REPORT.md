# VideoMind Retrieval Quality Closure — Live Provider Report

## 1. Status

**当前 Retrieval 未证明优于 Baseline；真实 A/B/C 在本次 12-case 合成数据集上打平。Status：PASS WITH NOTES。**
36/36 个 case 执行成功，真实 BGE-M3/Qdrant/reranker 路径完成，全部 fallback delta=0，评估 namespace 清理通过。历史两个 rank-6 case 在真实路径中均为 rank1。本轮完成该冻结合成数据集的质量闭环；不声称真实长视频泛化或 Final 超越旧 Baseline。

## 2. Environment

HEAD before / after：`da5b2ffc5c2d1cec65874c0c0627b70ebc34a4f4`；分支 main。未创建 commit，未 push、Release、rebase 或 squash。
Docker：既有 dovideo-r2 MySQL / Redis / MinIO / Qdrant / RabbitMQ 均 healthy；没有重建容器或清空基础设施。Qdrant 容器 dovideo-r2-qdrant-1，endpoint http://127.0.0.1:6333。
Collection：`video_chunks_r4_bge_m3`。按现有项目 loader 顺序读取 .env.r2.local、.env.r4.local；r4 的显式 collection 覆盖 r2，进程既有配置优先。
Embedding：api.siliconflow.cn / BAAI/bge-m3 / 1024维。Reranker：同一现有 Provider / BAAI/bge-reranker-v2-m3；URL 使用已有 transport 默认 https://api.siliconflow.cn/v1/rerank。
本轮明确授权后，缺失的 reranker key 仅在评估进程中继承 embedding key；没有回写任何 .env、输出密钥或修改默认生产配置。Reranker configuredEnabled 仍为 false；仅 ARM C 启用。
Dataset digest：`f1820acf0b0aec90a64b0bcd9d7f4a8a8d204e70fe55cfd667e5da04ae569b71`；sourceRevision：`f4ca4519bde75e16aff66bd4df0976a55e56a6d021b5952df85fd9dd6fddb600`。

## 3. Provider Preflight

| Component | Status | Evidence |
|---|---|---|
| BGE-M3 | PASS | 正确模型，真实返回1024维 finite vector |
| Qdrant | PASS | readyz、authenticated read、isolated write/search、collection dimension=1024、media/revision/chunking scope、两个 stale probes 排除、cleanup |
| Reranker | PASS | 真实短合成探针成功，指定模型存在，返回合法 candidate mapping |
每个评估 mediaId 在写入前检查无现有点；只删除本轮自己的 namespace。ARM A/B/C 的剩余评估点均为0。B/C各验证了 wrong sourceRevision 与 wrong chunkingVersion 不会进入 scoped candidates，negative mediaId 查询为空。
36个 case 均为 LIVE_QDRANT_SUCCESS，vectorStoreFallbacks delta=0；embedding/sparse/reranker fallback也均为0。没有将 local cosine 降级计入 Final live 成功。
ARM A 保留冻结旧算法语义：Qdrant Top6，未命中 remote-score 的 chunk 可使用旧算法自带 cosine。该数据集 Baseline 总共6个 chunk，健康查询均返回6个，因此未发生旧算法补分。A不注入 stale points，使用空白且独占的 media namespace。

## 4. Experimental Arms

| Arm | Algorithm | Provider | Cases |
|---|---|---|---|
| A | c7c0270 固定5min + 旧 .60/.25/.15 Chunk加权 + 旧Segment排序 | 真 BGE-M3 / Qdrant | 12/12 PASS |
| B | 当前5min滑窗/1min overlap + Dense8/BM25Top8/RRF + Final3 + Segment排序；reranker OFF | 真 BGE-M3 / Qdrant | 12/12 PASS |
| C | 与B相同，唯一算法开关为 reranker ON | 真 BGE-M3 / Qdrant / Cross-Encoder | 12/12 PASS |
同一12-case retrieval-focused-v2、同一query/source artifact/revision/annotations；semantic-01、distractor-02保持原样。A从冻结Git源码加载，生产代码未保留或模拟旧算法。B/C重用完全相同的8个已构造Chunk及embedding vectors。
Summary/planner使用与历史相同的冻结 LocalChunkSummaryAdapter / LocalRetrievalPlanner，使provider ablation可比；这不是生产LLM summary/planner质量测试。Embedding输入仍为 summary + newline + keywords，没有将全部ASR/OCR塞入dense representation。
所有参数冻结：Dense8、Sparse8、Fusion10、Final3、RRF k60、BM25 k1=1.2/b=.75、.55/.25/.20、overlap1min/stride4min。
B/C 的 Dense/BM25/RRF candidate identity及排序在12/12 case均一致；BM25/RRF绝对score也一致。重复远程查询中3个case出现极小dense score浮动，最大 0.00067311，具体浮动来源未进一步测量；没有改变任何上游rank或RRF结果。
既有X3 runner因仓库ownership检查保留gitSha=null/workingTreeState=UNKNOWN；每个arm旁新增 closure-run-metadata.json 保存已核实SHA、完整working tree、Provider身份及settings，没有将UNKNOWN改写成CLEAN。

## 5. Aggregate Metrics

**REMOTE BGE-M3 RESULT / SYNTHETIC**：

| Metric | A | B | C |
|---|---:|---:|---:|
| recall@1 | 0.916667 | 0.916667 | 0.916667 |
| recall@3 | 1.000000 | 1.000000 | 1.000000 |
| recall@5 | 1.000000 | 1.000000 | 1.000000 |
| precision@1 | 1.000000 | 1.000000 | 1.000000 |
| precision@3 | 0.388889 | 0.388889 | 0.388889 |
| precision@5 | 0.233333 | 0.233333 | 0.233333 |
| mrr | 1.000000 | 1.000000 | 1.000000 |
| temporal_hit | 1.000000 | 1.000000 | 1.000000 |
| temporal_coverage | 1.000000 | 1.000000 | 1.000000 |

Recall@1=.916667是当前标注的上限：两个 boundary case 各有两个 expected Segment，单个Top1 hit只能覆盖其中一个。所有case的第一hit均正确，所以Precision@1=1、MRR=1。Precision@3/@5受每case仅1或2个标注相关Segment及当前固定返回长度影响；这里没有未标注相关性判定。三路在当前返回长度与标注规则下均已达到这些指标的上限，因此无法证明严格优越。

历史负实验继续保留，**OFFLINE LOCAL RESULT**：

| Metric | Offline A | Offline B | Live A | Live B | Live C |
|---|---:|---:|---:|---:|---:|
| Recall@3 | 1.000000 | 0.833333 | 1.000000 | 1.000000 | 1.000000 |
| MRR | 0.958333 | 0.861111 | 1.000000 | 1.000000 | 1.000000 |
TF-IDF负结果不能直接代表BGE-M3 Hybrid Retrieval退化。真实实验也没有证明新架构必然更好：A/B/C最终指标相同。

## 6. Chunk-Level Metrics

Expected coverage按每个gold Segment是否至少有一个parent进入该stage TopK计算，再按case平均。边界Segment的多个parent全部保留。

| Chunk metric | A | B | C |
|---|---:|---:|---:|
| Dense Recall@1 | .750000 | .750000 | .750000 |
| Dense Recall@3 | .750000 | .916667 | .916667 |
| Dense Recall@5 | .916667 | 1.000000 | 1.000000 |
| Dense configured TopK recall | 1.000000 (Top6) | 1.000000 (Top8) | 1.000000 (Top8) |
| BM25 Recall@1 | N/A | 1.000000 | 1.000000 |
| RRF Chunk Recall@1 / @3 / @10 | N/A | 1.000000 | 1.000000 |
| Reranker Final3 Chunk Recall | OFF | OFF | 1.000000 |
| Final Chunk Recall@1 | .916667 | 1.000000 | 1.000000 |
| Final Chunk Recall@3 | 1.000000 | 1.000000 | 1.000000 |
A只有6个Chunk，B/C只有8个，因此Dense配置TopK覆盖整个corpus，Dense Recall@TopK=1并不是大规模candidate recall能力的证明。Fusion上限是10，但本次实际fusion候选只有8个。

## 7. Segment-Level Metrics

三路 Segment Recall@1=.916667、Recall@3/@5=1、MRR=1。B/C虽然将Final Chunk Recall@1从A的.916667提高到1，最终Segment指标没有超过A；boundary Top1的上限来自一个hit无法覆盖两个独立Segment，并非fine ranking失误。
本次真实路径没有发现 coarse recall成功而gold Segment被压到rank6的case；历史离线曾有该问题。Temporal hit/coverage按所有返回EvidenceHit计算，不等价于Recall@3。

## 8. Category Results

| Category | A Recall@1 / @3 / MRR | B Recall@1 / @3 / MRR | C Recall@1 / @3 / MRR |
|---|---|---|---|
| Semantic paraphrase | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 |
| Exact technical terms | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 |
| OCR-only | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 |
| ASR-only | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 |
| Boundary-crossing | 0.500000 / 1.000000 / 1.000000 | 0.500000 / 1.000000 / 1.000000 | 0.500000 / 1.000000 / 1.000000 |
| Distractor | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 | 1.000000 / 1.000000 / 1.000000 |

## 9. semantic-01 Deep Dive

Expected Segment：`seg_c40a98b083b5027aaeb50438a953a8f04b785fc6561ce05aff4dbcaf41ee916f`；范围420000–480000ms（07:00–08:00）。
Final滑窗parent：`chunk_480042a984ecd04eaaeca520521295cef6955b6f1de8fef79bfbe52cb039bbf2`。

| Stage | Offline Final OFF | Live A | Live B | Live C |
|---|---:|---:|---:|---:|
| Dense | 3 | 1 | 2 | 2 |
| BM25 | 1 | N/A / OFF | 1 | 1 |
| RRF | 2 | N/A / OFF | 1 | 1 |
| Reranker | N/A / OFF | N/A / OFF | N/A / OFF | 1 |
| FinalChunk | 2 | 1 | 1 | 1 |
| Segment / EvidenceHit | 6 / 6 | 1 / 1 | 1 / 1 | 1 / 1 |

首次差异发生于Dense：历史TF-IDF的目标parent rank3，真实BGE-M3为rank2；BM25仍为rank1，RRF随后从rank2变为rank1。最终parent_relevance从.5变为1，Segment从rank6变为rank1。
BGE-M3 Dense本身没有把滑窗parent提升到rank1；BM25/RRF组合完成了rank1选择。Reranker保持正确parent在rank1，没有进一步救回。
| Segment metadata | Live A | Live B | Live C |
|---|---:|---:|---:|
| parentRank | 1.000000 | 1.000000 | 1.000000 |
| parentRelevance | 0.538818 | 1.000000 | 1.000000 |
| asrMatch | 0.500000 | 0.500000 | 0.500000 |
| ocrMatch | 0.000000 | 0.000000 | 0.000000 |
| finalSegmentScore | 0.421350 | 0.675000 | 0.675000 |

B候选数：Dense=8、BM25=8、RRF=8、Final=3；C reranker候选=8。B/C Recall@1/@3/@5均为true。
A使用绝对weighted Chunk score作为parent contribution；B/C使用1/parent_rank，两个parentRelevance的数值不可直接按同一score scale比较。

## 10. distractor-02 Deep Dive

Expected Segment：`seg_c40a98b083b5027aaeb50438a953a8f04b785fc6561ce05aff4dbcaf41ee916f`；范围420000–480000ms（07:00–08:00）。
Final滑窗parent：`chunk_480042a984ecd04eaaeca520521295cef6955b6f1de8fef79bfbe52cb039bbf2`。

| Stage | Offline Final OFF | Live A | Live B | Live C |
|---|---:|---:|---:|---:|
| Dense | 3 | 1 | 2 | 2 |
| BM25 | 1 | N/A / OFF | 1 | 1 |
| RRF | 2 | N/A / OFF | 1 | 1 |
| Reranker | N/A / OFF | N/A / OFF | N/A / OFF | 1 |
| FinalChunk | 2 | 1 | 1 | 1 |
| Segment / EvidenceHit | 6 / 6 | 1 / 1 | 1 / 1 | 1 / 1 |

首次差异发生于Dense：历史TF-IDF的目标parent rank3，真实BGE-M3为rank2；BM25仍为rank1，RRF随后从rank2变为rank1。最终parent_relevance从.5变为1，Segment从rank6变为rank1。
BGE-M3 Dense本身没有把滑窗parent提升到rank1；BM25/RRF组合完成了rank1选择。Reranker保持正确parent在rank1，没有进一步救回。
| Segment metadata | Live A | Live B | Live C |
|---|---:|---:|---:|
| parentRank | 1.000000 | 1.000000 | 1.000000 |
| parentRelevance | 0.611369 | 1.000000 | 1.000000 |
| asrMatch | 1.000000 | 1.000000 | 1.000000 |
| ocrMatch | 0.000000 | 0.000000 | 0.000000 |
| finalSegmentScore | 0.586253 | 0.800000 | 0.800000 |

B候选数：Dense=8、BM25=5、RRF=8、Final=3；C reranker候选=8。B/C Recall@1/@3/@5均为true。
A使用绝对weighted Chunk score作为parent contribution；B/C使用1/parent_rank，两个parentRelevance的数值不可直接按同一score scale比较。

## 11. Reranker Value

| Measure | Count | Cases |
|---|---:|---|
| changedTop3（按顺序比较） | 9 | semantic-02, technical-01, technical-02, ocr-01, asr-01, asr-02, boundary-01, distractor-01, distractor-02 |
| membership changed | 9 | 同上；不只是同一集合内部重排 |
| order-only changed | 0 | 无 |
| rescued | 0 | 无 |
| harmed | 0 | 无 |
| unchanged | 3 | semantic-01, ocr-02, boundary-02 |
| displayed EvidenceHit Top8 changed | 6 | semantic-02, technical-01, asr-01, asr-02, boundary-01, distractor-01 |
B的所有gold Segment parent已在Final rank1，所以C没有需要救回的gold parent。C改变了非目标候选组成，6个case的展示EvidenceHit集合/顺序也变化，但12/12 case的最终质量指标均与B相同。没有隐藏negative case；本数据集未观察到harmed，不等价于一般情况下不会伤害。
诊断中的rerankerScore/finalScore是生产service在排序后保留的RRF candidate score，不能解释为Cross-Encoder绝对relevance；本轮依据真实reranker返回所决定的rank及合法mapping评价价值。原始Cross-Encoder score未持久化。

## 12. Root Cause

**历史退化根因：Fine-Ranking Parent Dominance。真实差异：Dense排名改善 + Sparse/RRF使正确parent进入rank1；live没有新的失败层。**
两个历史case的ASR/OCR match未变，变化的是parent rank及parent relevance。semantic-01从.400000变为.675000；distractor-02从.525000变为.800000。
当前权重下，parent rank2且OCR=0的Segment即便ASR=1，最高也只有.525，低于rank1 parent零文本匹配Segment的.55。这一结构性限制仍存在，历史离线trace已证明它可以触发。但本次B/C所有gold都有rank1 parent，缺少真实parent-rank2样本，因此**未实测确认它是本次live路径的bottleneck**，也不能宣布该策略已对所有视频稳健。
未发现wrong identity、wrong parent、mapping、scope、dedup或rank off-by-one导致的production correctness bug；没有修改生产代码。

## 13. Production Decision

**Quality Decision：D — Final仍未证明优于Baseline（本次是打平，并非退化）。**
**KEEP RERANKER OPTIONAL**：保持生产默认OFF。当前测量没有支持默认启用Cross-Encoder的最终质量收益。此结论限于这个已有指标饱和的数据集，不代表reranker在更难case无价值。
本轮没有证据要求先修改.55再允许发布，也没有理由回退默认ranking。可将本报告作为真实Provider已跑通、冻结case不回退的审查依据；不能以此宣传Final已超越Baseline或完成真实长视频泛化验证。
是否push仍由用户在审查后决定；本轮没有push或commit。后续应扩大真实/困难样本，尤其正确parent只能排第2的case，另测成本与延迟；本轮不做调参或新增RAG功能。

## 14. Tests

Full backend：**1090 passed、17 skipped、0 failures/errors**（43.92s）。跳过的是要求显式opt-in的通用基础设施live tests；本轮检索Provider验证在preflight及36-case实验中独立完成。
Targeted retrieval/evaluation/Qdrant/reranker/diagnostics：**95 passed**（6.30s），含新增的runtime inheritance/显式配置优先级测试。
Frontend：**79 passed、0 failed**；production build：**PASS**（932ms）。
测试使用正常执行环境及新的workspace basetemp，避免之前沙箱事件循环和旧temp ACL问题。

## 15. Changed Files

**NO PRODUCTION ALGORITHM CHANGE。**

- tools/run_retrieval_quality_closure.py：新增显式进程内reranker credential inheritance开关，以及preflight阶段的stale scope/cleanup验证。
- tests/application/test_retrieval_quality_closure.py：新增runtime inheritance与显式设置优先级测试。
- docs/retrieval-quality-closure-live/：全新真实Provider preflight、A/B/C原始X3数值产物、脱敏ranking diagnostics、每arm元数据、比较/失败分析、审计与本报告。
历史retrieval-evaluation-v1/v2、旧X3 artifacts、上一轮retrieval-quality-closure及datasets/x3未覆盖。原有6个interview-guide文件保持未跟踪、未提交；没有向Provider发送其内容。

测量边界：12个SYNTHETIC case、单一30min合成source。真实用户视频case为0；真实长视频泛化、cross-video scale、no-answer、provider cost、provider latency分布、LLM summary/planner质量均为NOT_MEASURED。
仅保存了本轮arm wall time：A=6.437s、B=8.334s、C=9.367s。该数值混合chunk construction、embedding、Qdrant验证和检索；C重用B的Chunk，不能据此把C-B当作纯reranker latency或推断成本。

## 16. Interview Story

初版采用固定五分钟Chunk与semantic/keyword/OCR加权检索，职责简单、易于验证。为了处理边界证据和混合检索，升级为滑窗、Dense/BM25、RRF与可选Cross-Encoder。升级后没有假设复杂架构一定更好，而是冻结旧Git源码和12个版本化合成case做对照。
离线TF-IDF中Final Recall@3从1降到.833333。stage trace定位两个case的正确Chunk已在Final rank2，Segment却排到6；父排名贡献压制了局部文本相关性。没有盲目调低.55。
恢复基础设施和明确合成数据授权后，使用真实BGE-M3/Qdrant/reranker运行36个A/B/C case。两个历史失败case在Dense阶段先从rank3升到2，BM25/RRF再把正确parent提升到1，Segment也回到1。说明TF-IDF负结果不能直接代表真实embedding路径。
三路最终Recall@3和MRR均为1，Final没有超越Baseline。Cross-Encoder改变了9个Top3 Chunk集合，但没有rescued/harmed或最终质量收益。因此保持reranker可选、默认OFF，保留历史负结果与父排名策略的结构性风险；当前无需为了这12个case修fine ranking，未来需在更难且真实的数据上验证。
真实Provider合成评测章节可以按此限定范围封板；真实性、可比性和负结果解释均有产物支撑。真实长视频泛化仍不能声称封板。

复现（PowerShell，使用新的空output目录）：

```powershell
.\.venv\Scripts\python.exe tools/run_retrieval_quality_closure.py --inherit-reranker-key --env-file 'D:\Agent Learning\dovideo-python\.env.r2.local' --env-file 'D:\Agent Learning\dovideo-python\.env.r4.local' --output work/closure-live-next
```

需要与本轮相同的合成数据发送及隔离Qdrant写入授权。真实密钥只在原有私有env及进程内存在；报告和产物不含凭据或完整query/ASR/OCR文本。
