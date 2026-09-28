# VideoMind v0.1.0 — 发布说明

VideoMind 是支持长视频分析的中文 AI 视频工作台，把视频、ASR/OCR 时间轴、证据检索和 AI 回答连接到同一界面。

## 本版包含

- 本地媒体导入、媒体库、真实视频播放和分析工作台。
- Whisper ASR 与 Tesseract OCR 的时序来源建模；分页查看全片记录与多轨时间轴。
- 分块与混合检索，Planner / Executor / Critic 分阶段分析。
- 经过来源校验的答案级结构化引用，以及 Citation → Evidence → 视频时间点跳转。
- 模型调用前的累计 token 预算准入、阶段用量遥测和恢复记录。
- Pixel Future Academy 视觉体系与中文优先界面。
- FastAPI REST/SSE、Celery/RabbitMQ 异步任务以及 MySQL、Redis、MinIO、Qdrant 组合。

## 使用

参阅 [README](../README.md) 和 [完整启动指南](QUICK_START.md)。本地 API/界面探索不需要外部服务；完整视频分析需要媒体工具、五项基础服务及自行配置的聊天和 BGE-M3 Embedding Provider。

## 已知边界

- Citation 为答案级；并非逐句归因。
- 媒体库支持本地文件导入；远程视频网站 URL 导入未开放。
- 分析时延、token 使用量和费用受媒体内容及外部供应商影响；预算准入不保证货币成本上限。
- 当前提供本地/开发运行路径，没有托管部署承诺。X3 路由实验未证明自适应策略优于固定策略，见 [评估报告](X3_OPENROUTER_X3_E_REPORT.md)。

许可：MIT；前端上游声明见 [NOTICE](../client/NOTICE.md)。
