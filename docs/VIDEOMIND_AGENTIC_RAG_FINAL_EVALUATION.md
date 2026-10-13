# VideoMind Agentic RAG 最终真实评测

2026-10-14，Asia/Shanghai。**ENGINEERING FROZEN / QUALITY INCONCLUSIVE / HOLDOUT NOT_RUN**。

最终一轮完成 24 次真实业务请求：两题 Smoke，11 道 Development VIDEO 问题各一次 A/D。不重跑失败题，不重建视频、ASR/OCR、Chunk、Embedding 或索引，不追加优化轮次。工程封板后进入维护与面试准备。

## 实验身份与公平条件

基准 HEAD：`8a4ce040c01e085cb8771febd87b9addef0b7f90`。实际工作区包含已核验的 R1/R2/R3/Final Gate 未提交修复，不能把基准 HEAD 当作本轮生产源码。347 项上一轮冻结 Hash 全部匹配，运行期间生产源码、Prompt、Schema、Guard 不变。最终提交以仓库 Git 日志及本地推送回执为准。

SourceRevision：`4adfbf8117800f0616427026852e8088c9d02b91606d8800f2f107f539b6eec1`。复用同一媒体、8 个 Chunk、1024 维 BGE-M3 与既有 Qdrant 集合。私有 Dataset SHA256：`9dd12d76e214c4af14b33a301b72e6d29d702fe3b07c240df8a6df280814a5b5`。配对前冻结的评分文件 SHA256：`b106840dc6994d10fffbe1c1782a7d41dfc0fadce32a85bb973d7ceaffb9ab7d`。

源码、工具、Fixture、Registry、历史产物及 Prompt/Schema 所属模块均有逐文件 SHA256；模型配置另有 Digest。全部原始输入、响应、DTO、候选、自然 Planner Query、排名、Stage Trace、Usage 与评分材料只保存在被忽略的私有评测目录。本报告只公开聚合指标与匿名 Case ID。

两组共同使用 OpenRouter `deepseek/deepseek-v4.1-flash`、Provider only `siliconflow/fp8`、reasoning `none`、输出上限 2048；deny/ZDR/require_parameters/no fallback 保持。BGE-M3 Query Embedding 使用硅基流动直连，已有 Chunk 向量不再生成。每次调用最多一次 HTTP attempt；双方请求预算 65,000 Chat Token、Deadline 60 秒、cap8，相同基础 Hybrid 与 Evidence Guard，独立无历史会话，M2/Reranker OFF。

| 组 | Need | Reflection | Feedback |
|---|---|---|---|
| A：Baseline Hybrid | OFF | OFF | OFF |
| D：Final Agentic | ENABLED | SHADOW | ENABLED，最多一次 |

65k 只用于评测进程，生产默认值未修改。历史 MIXED 准入示例需要 54,206 Token（含 38,000 下游预留），50k 不足、65k 可准入；本轮 Smoke 的 Reflection 确实执行。Reflection 局部 Timeout 保持 5 秒，下游预留、重试结算和并发隔离未放宽。保留两组各自自然 Query；完整方案差异不能全部归因于 Feedback。

## 历史六题审核与证据等级

| Case | 用户确认的历史决策 | 边界 |
|---|---|---|
| v2-01 | PARTIAL | 历史独立知识回答 |
| v2-08 | INCORRECT | 历史业务失败与拒绝草稿语义分开 |
| tc-04 | CORRECT | 原题不额外强制 O(n) |
| tc-17 | INCORRECT | 1093700ms 不证明首次；缓存字面定位不证明 Feedback 收益 |
| tc-18 | INCORRECT，旧草稿另记 PARTIAL | 历史 DTO/业务失败与草稿质量分开 |
| tc-20 | NEEDS_REVIEW | 无全视频不存在证明，不作权威负 Gold |

来源为用户基于现有摘录与 AI 辅助分析的确认，未宣称独立逐帧审核。以上是历史判断，不是新模型答案标签。

新配对题的来源引用、必要事实与评分合同在 Pilot 前冻结，没有生成独立人工 Gold。7 题由工程评分者核对冻结原始来源，作为可评分子集；4 题因语义、额外断言或引用充分性有争议而单列。必要事实的 16 个原子项是对原冻结要求的拆分，未新增或改变要求。此为来源核对的工程评分，不能标为 HUMAN_APPROVED。可信来源不等于评分者判断绝对无误。

## 两题真实 Smoke

| Case | 业务/Guard | Reflection / Repair | 事实质量 |
|---|---|---|---|
| v2-08 MIXED | evidence_rejected；Knowledge HTTP 0 | ASSESSED / NOT_REQUIRED | 草稿 claim 与 content 不一致，强逐字合同正确拒绝；未回答作者解释是否正确 |
| tc-18 四类算法 | 交付，Guard 通过，4 条唯一引用 | ASSESSED / NO_VALID_REPAIR | 只覆盖搜索版本，披露候选缺口，缺另外三类算法，主要事实要求失败 |

v2-08 真实执行 Planner → BGE-M3 → Hybrid → Reflection → 视频抽取，未通过 Guard 后没有知识调用，不能宣称 MIXED 全链成功。tc-18 的 Grader 合法识别缺口，但应用没有准入有效 Repair；合法 DTO 不证明四事实完整性。两次分别耗时 12.728 / 14.517 秒、4 / 4 Provider 推理 HTTP；无确定性安全 P0，新模型不合规和可解释的覆盖限制归入 Known Limitations，未调 Prompt。

## 全部 A/D 配对与评分分母

| Case | A | D | 评分说明 |
|---|---|---|---|
| tc-03 | FAIL | FAIL | 自包含问题含“前者”，M1 提前要求澄清，未调用 Provider |
| tc-04 | DISPUTED | DISPUTED | 两组总式正确；A 额外语句编号/频度有误，严格与仅核心事实口径不同 |
| tc-05 | FAIL | FAIL | 指代规则提前澄清，未回答 |
| tc-06 | DISPUTED | DISPUTED | 原因解释完整性、短引用充分性待语义审核 |
| tc-07 | PASS | FAIL | A 给出正确乘法规则；D source_mismatch 被 Guard 拒绝 |
| tc-08 | DISPUTED | DISPUTED | 阶数结论正确，比较方法与推断的来源支持待审核 |
| tc-09 | PASS | PASS | 增长关系及不要求证明满足来源要求，OCR 歧义有局限说明 |
| tc-10 | DISPUTED | DISPUTED | A 未完整写最终式；D 推断式正确，范围措辞有歧义；最终 Prompt 无目标字面式 |
| tc-11 | FAIL | PASS | A 错答另一算法；D 还原嵌套次数与复杂度 |
| tc-14 | PASS | PASS | 搜索首末位置与最好/最坏条件正确 |
| tc-15 | FAIL | FAIL | 指代规则提前澄清，未回答 |

共有 22 次配对请求，14 次进入可信评分分母，8 次争议请求完整保留；另有 2 次诊断 Smoke。不是删掉失败 Case：tc-03/05/15 全部计失败，Provider/Guard 拒绝也计失败。无可信负样本或 DIRECT 样本，不能给出其成功率。

| 指标 | A | D |
|---|---:|---:|
| 配对业务请求 | 11 | 11 |
| 可评分请求 | 7 | 7 |
| Grounded Answer Success Rate | 3/7，42.86% | 3/7，42.86% |
| 必要事实覆盖（可信子集） | 6/16，37.50% | 6/16，37.50% |
| Delivery Rate（全部配对，含澄清文本） | 11/11，100% | 10/11，90.91% |
| Prompt Token（含 Query Embedding） | 75,956 | 141,590 |
| Completion Token | 4,454 | 5,190 |
| Reasoning Token | 0 | 0 |
| Total Token | 80,410 | 146,780 |
| 平均 Token（全部配对） | 7,310 | 13,343.64 |
| Chat HTTP / Embedding HTTP / Qdrant Query | 16 / 8 / 8 | 24 / 8 / 8 |
| 平均 E2E | 7.457 秒 | 8.501 秒 |
| 样本 E2E P50 | 9.794 秒 | 11.279 秒 |
| 已报告 Chat 美元费用 | 0.028054500 | 0.039522456 |
| Embedding 实际费用 | 未报告 | 未报告 |
| 无 Usage 的推理尝试保守预留 | 0 | 0 |

Delivery 包含没有事实回答的澄清文本，不能冒充成功率。可信子集核心事实完整正确（未答计失败）均为 3/7，必要事实逐项覆盖单列为 6/16；全答逐句 Factual Correctness 和独立 Semantic Grounding 未盲审，不给出伪精确比例。引用身份核验在 8/8 A、7/8 D 已生成且交付的视频 DTO 中通过，但不能据此推导逐 claim 自然语言蕴含正确率。D 的 invalid_response 为 0/11，Guard 拒绝为 1/11；A 两者均为 0/11。无权威负样本，Reasonable Abstention 不可量化；tc-18 的诚实部分答复仍因核心事实遗漏失败。

可信配对：双方成功 2，A 失败 D 成功 1（tc-11），A 成功 D 失败 1（tc-07），双方失败 3。GASR 变化 **0 个百分点**，事实覆盖变化 **0 个百分点**。平均 Token +82.54%，平均 E2E +1.043 秒，实报 Chat 成本 +40.88%。全配对 Token / 可信成功数为 26,803.33 / 48,926.67；Chat 费用 / 可信成功数为 US$0.0093515 / US$0.013174152，此口径包含争议题支出，不是已完整结算的单成功总费用。

tc-11 是自然检索输入变化产生的整套方案改善，没有实际 Repair，不能归因于 Feedback。tc-07 是自然生成的通道/来源不合规回退，Guard 保持拒绝。

敏感性：仅总式正确与严格额外事实口径会改变 tc-04；是否接受循环式原因解释会改变 tc-06；对推断与直接引用的要求会改变 tc-08/10。这四对均保留争议，不擅自指定 Gold。把其标签所有可能取值纳入时，两组各有 3–7/11 的宽界，差值最宽为 ±36.36 个百分点；这是不确定性界限，不是合理同一评分规则下的实测点估计。不能在此基础上宣称整体改善或统计显著泛化。保守来源核对子集净收益为零，整体结论 **QUALITY INCONCLUSIVE**。

## Agentic 实际漏斗

| 指标 | 全轮 D（11 配对 + 2 Smoke） |
|---|---:|
| 业务请求 | 13 |
| M1 在 Need 前澄清 | 3 |
| Need VIDEO_RAG / MIXED | 9 / 1 |
| Reflection 调用机会 / 合法 ASSESSED | 10 / 10 |
| TIMEOUT / SKIPPED_BUDGET / INPUT_UNAVAILABLE / SKIPPED_TIME | 0 / 0 / 0 / 0 |
| Repair NOT_REQUIRED / NO_VALID_REPAIR | 8 / 2 |
| Repair Eligible / Attempted / Added Evidence | 0 / 0 / 0 |
| 实际恢复必要事实 | 0 |

Need 是规则决策，本轮无额外 Need 模型 HTTP。A 的 Need OFF，不把缺 Trace 当失败；三对提前澄清发生在路由前。tc-18 的明确多事实缺口、tc-09 的 Grader 不足建议均未产生合法 Repair。Assessment 合法不等于判断正确，未宣称 10 次 Grader 都语义正确。未出现预算/字符限制跳过，不能推导其他输入均不会跳过。**FEEDBACK QUALITY BENEFIT NOT ESTABLISHED**。既有 C/D 固定输入 Replay 只用于结构诊断，不并入自然请求成功率。

## 全轮预算、Bad Case 与 Holdout

不可重置 Campaign Ledger：业务 **24/30**，Provider HTTP attempts **70/120**（64 次推理含 18 次 Embedding，6 次只读 Provider 预检，含沙箱连接失败），保守累计 **262,897/1,000,000 Token**，实报 OpenRouter Chat **US$0.079531056**，费用停止线 US$0.75。剩余账本额度：6 次业务、50 次 Provider HTTP、737,103 Token、实报费用停止线余量 US$0.670468944；不为用完额度增加题目。18 次 Qdrant 检索另计，只读元数据预检不属于 Provider 推理。

OpenRouter 当前余额经只读 API 核验；硅基流动旧余额 API 返回 410，使用用户最新确认的有效 16 元 BGE-M3 券，首个自然 Query 验证 Key，不另发付费探针。Embedding 18 次均有 Usage、无费用字段，费用仍是未知；不能把 Chat 小计当作完整账单。所有推理尝试有 Usage，Reasoning 不重复累加 Total。无自动充值、Provider 切换、fallback 或重试。

历史 Registry 29 条、18 条关联回归，均已核对。本轮在私有副本追加 12 条失败/限制记录，最终 **41 条、18 条关联回归**；22 个公开合成回归合同由现有测试发现。新增记录含真实 Trace、Case/Run、源码与配置身份、阶段及封板结论，模型不合规、前置规则限制与检索遗漏分开归档。没有新增确定性 P0，无权为每条模型失败声称新增回归已解决。

原 3 道 Holdout 仍缺题目专属可信评分依据，其中还包含 DIRECT/MIXED，故 **NOT_RUN**。11 道 Development 中只有 7 对可较可靠评分，未为凑 14 道伪造参考事实。零 Holdout 调参或污染。

V1 历史同视频 19 个正例 Draft 的 Recall@5：Dense **0.460526**、Hybrid **0.671053**、Hybrid+Reranker **0.855263**；这是历史检索召回，不是本轮回答成功率，也不是独立人审 Gold。不能写成 Agentic 回答正确率提升。

## 限制、保留决策与简历数据

M1 对自包含题的指代误判；MIXED 强抽取导致模型不合规时拒绝；短引文合法但语义支持不足；五引用与 cap8 对复杂事实覆盖有限；Grader 发现缺口不保证确定性 Repair Admission；OCR 误识别与采样缺口；完整视频首次/不存在均无法由局部证据证明。独立语义人审缺失、同视频小样本、全部配对固定 A 后 D 的顺序可能有缓存/时序影响，均限制结论。

保留已验证的来源身份、预算和一次 Repair 安全边界；Reflection/Feedback 继续默认关闭，作为可选实验能力，不为本轮表现继续改模型或 Prompt。未证明它们值得默认开启。

可信简历表述：

- 实现来源身份、revision、ASR/OCR 通道与时间校验的有界视频 RAG，补充 22 个合成回归合同，最终离线 1778 项测试通过、前端 107 项通过。
- 建立真实缓存复用与 A/D 配对评测，完成 24 次请求，记录 70 次 Provider HTTP、262,897 Token 及分供应商成本；7 题来源核对评分两组均 3/7，如实保留负结果与争议样本。
- 建立 41 条 Bad Case 的可追溯 Registry，18 条关联确定性回归；本轮 Reflection 10 次、实际 Repair 0 次，未将结构测试冒充自然质量收益。

项目正式停止 Agentic RAG 开发，转入维护与面试准备。质量限制不阻碍通过安全检查的工程版本封板。
