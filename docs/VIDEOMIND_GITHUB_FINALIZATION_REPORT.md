# VideoMind GitHub 最终整理报告

日期：2026-10-15（Asia/Shanghai）。范围为正式产品成果、README、原始真实截图与GitHub main同步。

## Final Status

**READY TO PUSH**。代码审计、完整回归、README与Mermaid渲染、图片原图核对及敏感信息检查完成；本节在实际推送与远端验证后更新。

## Git 基线与审计范围

工作区为VideoMind-upload-finalization，分支main，起始HEAD `5da65dd5453b94646ebcfa4ba51cc1aa8382b3d4`。
已执行status、log、diff、diff --cached、fetch origin与双向提交比较；开始时main与origin/main完全一致，无已提交但未推送的本地Commit。起始29个已跟踪文件修改、137个未跟踪文件；无已暂存变更。

按实际diff交付UI-R1、Product R2/R3、WEB LINK成果；正式Agent/RAG主体此前已在起始提交中同步，不虚构本轮新增算法或未推Commit。未重做业务功能、Prompt、Agent策略或检索策略。

## 实际同步的产品成果

| 阶段 | 本轮纳入 |
| --- | --- |
| UI-R1 | Cyber Cyan模板/主题、UploadPanels/MediaCard/CyberMark、动画/焦点/窄屏适配、品牌图标、本地字体及许可证 |
| Product R2 | 生产短视频/单Chunk索引接入、真实转录状态、短片M1所需durable chunks、受限Query Rewrite Provider兼容、生产Demo隔离与Preview代理 |
| Product R3 | 累计Token显式开关与三处预算判断、实际遥测和恢复保留、Deadline/轮次/成本保护独立测试 |
| WEB LINK | 公开HTTPS MP4真实下载、固定DNS与SSRF拦截、有界重定向、100MiB/45秒边界、归属/失败清理与前端交互测试 |
| 运行与测试 | 本地PowerShell启动器、共用安全环境加载器、可公开设置模板、5个新增后端测试文件及相关现有回归、前端WEB LINK测试 |
| 文档 | 按指定正文重写README、可移植QUICK_START、公开产品验证摘要、字体来源和第三方NOTICE、四张用户指定真实截图 |

上传幂等/租约/Checkpoint/Token Bucket、M1和现有正式Agent/RAG主体已经在Git基线中，已读取相关源码核对README的能力描述。本轮未跟踪source目录没有额外尚未提交的Agent算法。

## README 落地与最小事实修正

完整采用用户指定正文与章节顺序，居中标题、简洁Badge、四张靠前产品图、六项能力、Mermaid架构、技术栈和本地运行说明。没有把默认实验开关、演示视频时长、未证明的性能提升或内部评测状态放到首页；当前UI不再描述为Pixel Future Academy。

仅做事实/路径补充：

1. 证据时间校验描述改为“对应来源记录时间范围”，避免把canonical窗口尾端误称为已解码媒体长度。
2. Reranker说明为可配置接入，不宣称每次都调用模型重排。
3. 补充WEB LINK真实支持范围：无需认证的公开HTTPS MP4，并说明安全/大小/超时边界。
4. 新工作区启动前须从安全模板创建本地settings，修复QUICK_START中旧README锚点、机器专属启动说明与私人报告引用。

README未介绍UI来源；原有LICENSE、client/NOTICE.md与新增THIRD_PARTY_NOTICES/字体许可完整保留，版权归属未删除。

## 四张真实截图

优先检查的work/readme-kit-input/docs/assets不存在；使用用户本轮直接附上的四张真实产品截图。
用户最后明确要求“截图原样上传”，因此最终文件与附件逐字节相同，**无裁剪、遮挡或重绘**。
目视检查没有API Key、JWT、密码、Cookie、私有授权链接或开发控制台。公开页面昵称及课件画面按用户明确指示保留；没有将授权扩展到其它私人原始数据。

| 文件 | 尺寸 | 原图与发布图相同的SHA-256 |
| --- | --- | --- |
| `docs/assets/videomind-live-home.png` | 2549×1266 | `1aca0c22a87b19cb99e2534aff6a181af559872328cde3b839b499f5eb764c58` |
| `docs/assets/videomind-live-library.png` | 2549×1266 | `a5b0ca2c4611ade3fb1c4aa9c1c9ba4800c2730bb9314e8df0c1c8f1359f03be` |
| `docs/assets/videomind-live-agent.png` | 1600×1000 | `ce44b69302c74e1a29fa642760f98955ab5b6536a4b83504b05ab7edb3f93a8d` |
| `docs/assets/videomind-live-evidence.png` | 1600×1000 | `09e0ded542a0da451b5f912b78801f97342dc38ee97d4bb922474fdbf67c07be` |

四图分别展示首页、媒体库、真实双链表Agent回答和9:00知识回顾证据定位。原始回答、Citation、时间戳和业务状态未修改。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| 完整后端pytest | **1835 passed、38 skipped、21 warnings**；81.28秒 |
| 完整前端npm test | **112 passed、0 failed** |
| npm run build | PASS；1.49秒 |
| Git diff --check | PASS；新文件尾随空白已清理，许可正文保持；保留既有LF/CRLF提示 |
| Markdown/相对链接 | 指定正文排版、README全部本地链接和四图引用通过 |
| Mermaid | 本地浏览器真实生成SVG，未仅检查代码块字符串 |
| 浏览器图片 | 四图全部loaded，naturalWidth/naturalHeight与原图一致 |
| 许可证 | 项目MIT、原client NOTICE、新增UI MIT、Dela/Space OFL、Syncopate Apache完整保留 |
| 敏感信息检查 | 提交候选文件无真实密钥/JWT/私钥命中；6处credential URL命中均人工核实为replace-me或测试合成值 |

38个skipped是显式opt-in集成用例，不算本轮Live通过；21个warnings包含既有TestClient弃用与Windows子进程文本解码告警，没有隐去测试失败。本轮发布整理没有新发起付费模型分析；历史真实业务范围见PRODUCT_VERIFICATION.md，原始数据仅留本地。

## 明确暂存/提交文件清单（58个）

使用明确路径清单进行暂存，未使用git add .或git add -A。

- `.env.example`
- `README.md`
- `THIRD_PARTY_NOTICES.md`
- `client/index.html`
- `client/public/THIRD_PARTY_NOTICES.txt`
- `client/public/favicon.svg`
- `client/public/fonts/Dela-Gothic-One-OFL.txt`
- `client/public/fonts/Dela-Gothic-One.woff2`
- `client/public/fonts/Space-Grotesk-OFL.txt`
- `client/public/fonts/Space-Grotesk.woff2`
- `client/public/fonts/Syncopate-LICENSE.txt`
- `client/public/fonts/Syncopate.woff2`
- `client/src/AnalysisWorkspace.vue`
- `client/src/App.vue`
- `client/src/analysis-workspace.css`
- `client/src/components/CyberMark.vue`
- `client/src/components/MediaCard.vue`
- `client/src/components/UploadPanels.vue`
- `client/src/cyber-theme.css`
- `client/src/dovideo.css`
- `client/src/production.css`
- `client/src/style.css`
- `client/src/webLink.test.js`
- `client/vite.config.js`
- `docs/PRODUCT_VERIFICATION.md`
- `docs/QUICK_START.md`
- `docs/VIDEOMIND_GITHUB_FINALIZATION_REPORT.md`
- `docs/assets/videomind-live-agent.png`
- `docs/assets/videomind-live-evidence.png`
- `docs/assets/videomind-live-home.png`
- `docs/assets/videomind-live-library.png`
- `docs/ui-r1/font-sources.json`
- `pyproject.toml`
- `scripts/local-product.ps1`
- `scripts/local-product.settings.example.json`
- `scripts/local_product_runtime.py`
- `src/dovideo/application/agent.py`
- `src/dovideo/application/long_context.py`
- `src/dovideo/domain/budget.py`
- `src/dovideo/infrastructure/media/url.py`
- `src/dovideo/infrastructure/providers/model.py`
- `src/dovideo/infrastructure/r4_runtime.py`
- `src/dovideo/presentation/api/app.py`
- `src/dovideo/presentation/api/r2_runtime.py`
- `src/dovideo/presentation/api/r4_runtime.py`
- `src/dovideo/presentation/api/runtime.py`
- `src/dovideo/presentation/composition.py`
- `tests/application/test_agent_loop_7f.py`
- `tests/application/test_task_lease.py`
- `tests/domain/test_budget.py`
- `tests/infrastructure/test_direct_mp4.py`
- `tests/infrastructure/test_direct_mp4_api.py`
- `tests/infrastructure/test_product_r2_rewrite_settings.py`
- `tests/infrastructure/test_product_r2_short_index.py`
- `tests/infrastructure/test_r4_content_reuse.py`
- `tests/infrastructure/test_r5_model_call_budget.py`
- `tests/presentation/test_api_r1.py`
- `tests/presentation/test_product_r2_transcript.py`

## 保留在本地的未跟踪文件（114个）

以下逐项列出，不删除、不覆盖、不暂存。它们并非本轮漏推的正式源代码；相关已完成产品能力已由上面的代码和公开摘要纳入。

| 路径 | 保留原因 |
| --- | --- |
| `docs/VIDEOMIND_AGENTIC_RAG_V2_EVALUATION.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_OPTIMIZATION_BENCHMARK_V1.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_PRODUCT_R2_LIVE_REPORT.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_PRODUCT_R3_DOUBLY_REVIEW_CONTINUATION_REPORT.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_PRODUCT_R3_DOUBLY_REVIEW_REPORT.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_PRODUCT_R3_REAL_DEMO_REPORT.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_UI_R1_MIGRATION_REPORT.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_UI_R2_REPORT.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_V1_PILOT_EVALUATION_REPORT.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_V2_BADCASE_ROOT_CAUSE.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_V2_FINAL_DEVELOPMENT_GATE.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_V2_FINAL_HUMAN_REVIEW.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_V2_R2_LIVE_VALIDATION.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/VIDEOMIND_V2_R3_CORRECTNESS_FINALIZATION.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/agentic-rag/r1-evaluation-final-v1/decisions.jsonl` | 原始实验/评测产物，含来源与运行数据，未确认适合公开。 |
| `docs/agentic-rag/r1-evaluation-final-v1/r1-only-existing-files.diff` | 原始实验/评测产物，含来源与运行数据，未确认适合公开。 |
| `docs/agentic-rag/r1-evaluation-final-v1/run.json` | 原始实验/评测产物，含来源与运行数据，未确认适合公开。 |
| `docs/agentic-rag/r1-evaluation-final-v1/summary.json` | 原始实验/评测产物，含来源与运行数据，未确认适合公开。 |
| `docs/agentic-rag/r1-evaluation-final-v1/verification.json` | 原始实验/评测产物，含来源与运行数据，未确认适合公开。 |
| `docs/agentic-rag/r1-evaluation-v1/decisions.jsonl` | 原始实验/评测产物，含来源与运行数据，未确认适合公开。 |
| `docs/agentic-rag/r1-evaluation-v1/run.json` | 原始实验/评测产物，含来源与运行数据，未确认适合公开。 |
| `docs/agentic-rag/r1-evaluation-v1/summary.json` | 原始实验/评测产物，含来源与运行数据，未确认适合公开。 |
| `docs/assets/videomind-real-analysis.png` | 旧验收截图；本次仅发布用户指定四张原图，避免重复或混用历史场景。 |
| `docs/assets/videomind-real-evidence.png` | 旧验收截图；本次仅发布用户指定四张原图，避免重复或混用历史场景。 |
| `docs/assets/videomind-real-library.png` | 旧验收截图；本次仅发布用户指定四张原图，避免重复或混用历史场景。 |
| `"docs/interview-guide/04-Celery\344\270\216RabbitMQ\345\274\202\346\255\245\344\273\273\345\212\241\347\263\273\347\273\237.md"` | 不属于已验证正式产品发布内容，保留本地。 |
| `"docs/interview-guide/05-\351\225\277\344\273\273\345\212\241\345\271\202\347\255\211\344\270\216TaskLease.md"` | 不属于已验证正式产品发布内容，保留本地。 |
| `"docs/interview-guide/06-Worker\345\264\251\346\272\203\346\201\242\345\244\215\344\270\216\344\270\232\345\212\241\351\207\215\350\257\225.md"` | 不属于已验证正式产品发布内容，保留本地。 |
| `"docs/interview-guide/07\357\275\234DLQ \344\270\216 Durable Dead-letter Handoff.md"` | 不属于已验证正式产品发布内容，保留本地。 |
| `"docs/interview-guide/08-ASR\344\270\216OCR\345\252\222\344\275\223\351\242\204\345\244\204\347\220\206.md"` | 不属于已验证正式产品发布内容，保留本地。 |
| `"docs/interview-guide/09-ContentAddressedVideoContext\345\244\215\347\224\250.md"` | 不属于已验证正式产品发布内容，保留本地。 |
| `docs/m1-first-turn-continuity-report.md` | 历史评测/验收报告，关联本机或私人原始证据，未确认整份适合公开；正式产品成果已由公开验证摘要覆盖。 |
| `docs/product-r2/accounting-and-admission.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/build-product-checks.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/build-restored.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/long-attempt-1791987087935.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/long-attempt-1791987326798.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/long-attempt-1791988365736.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/long-attempt-1791988515238.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/long-live.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/long-running-restored.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/persistence-product-checks.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/persistence-restored.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-analysis.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-attempt-1791973064433.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-attempt-1791986868781.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-attempt-1791988243212.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-evidence.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-first-attempt-181022.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-follow-up.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-live.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-restored.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/short-running-restored.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r2/upload-product-checks.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly-resume/active-budget.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly-resume/analysis.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly-resume/answer.md` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly-resume/before-retry.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly-resume/evidence.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly-resume/live.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly-resume/question.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly-resume/summary.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly/failed.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly/failure-restored.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly/live.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly/question.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3-doubly/summary.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3/accounting-and-admission.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3/browser-checks.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3/mobile-library.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3/nine-minute-analysis.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3/nine-minute-evidence.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3/nine-minute-live.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3/nine-minute-running-restored.png` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/product-r3/persistence-and-provenance.json` | 原始产品验收产物，含完整回答、ASR/OCR摘录、trace或非本次指定截图；公开摘要已另行纳入。 |
| `docs/ui-r1/browser-results.json` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/build.txt` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/frontend-tests.txt` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/reference-manifest.json` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/reference/analysis.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/reference/library.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/reference/login.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/reference/overview.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/reference/preview.gif` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/reference/transcript.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/analysis-error.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/analysis-evidence.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/analysis-processing.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/library-1280.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/library-1440.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/library-1920.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/library-390.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/library-empty.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/library-error.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/library-processing.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/library-upload.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/login.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/mock-upload-completed-processing.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/mock-upload-progress.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/register.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/upload-hover.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/workspace-1280.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/workspace-1440.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/workspace-1920.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r1/screenshots/workspace-390.png` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `docs/ui-r2-live.json` | 历史 UI/Live 原始证据，未逐项授权公开；所需字体来源清单已单独纳入。 |
| `scripts/product-r2-budget.py` | 本机受控 Live/付费验收脚本，依赖私有账户、媒体及本机路径；正式启动器已另行纳入。 |
| `scripts/product-r2-smoke.mjs` | 本机受控 Live/付费验收脚本，依赖私有账户、媒体及本机路径；正式启动器已另行纳入。 |
| `scripts/product-r3-cost.py` | 本机受控 Live/付费验收脚本，依赖私有账户、媒体及本机路径；正式启动器已另行纳入。 |
| `scripts/product-r3-live.mjs` | 本机受控 Live/付费验收脚本，依赖私有账户、媒体及本机路径；正式启动器已另行纳入。 |
| `scripts/product-r3-observe.mjs` | 本机受控 Live/付费验收脚本，依赖私有账户、媒体及本机路径；正式启动器已另行纳入。 |
| `scripts/ui-r1-browser.mjs` | 本机受控 Live/付费验收脚本，依赖私有账户、媒体及本机路径；正式启动器已另行纳入。 |
| `scripts/ui-r2-live.mjs` | 本机受控 Live/付费验收脚本，依赖私有账户、媒体及本机路径；正式启动器已另行纳入。 |
| `scripts/ui_r2_url_live.py` | 本机受控 Live/付费验收脚本，依赖私有账户、媒体及本机路径；正式启动器已另行纳入。 |

另外，既有忽略规则下的work/、私有.env、媒体、数据库/备份、Provider原始数据、Docker持久化目录、.venv/node_modules与个人interview-guide学习材料均保留本地；不为发布而枚举或公开其中私人内容。

## Commit、Push 与远端检查

待实际提交推送后更新本节。目标确认：origin=https://github.com/Morimi-Kazuha/VideoMind.git，branch=main。
禁止Force Push、Reset --hard、Clean与无关Release/Tag；没有删除用户历史数据。
