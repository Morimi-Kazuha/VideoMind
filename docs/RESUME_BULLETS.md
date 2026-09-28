# VideoMind 简历要点

以下是可按岗位裁剪的表述。先核对本人实际负责的代码与当前测试结果，再用于简历；不要把实验负面结果包装为性能提升。

## 精简版

- 构建 VideoMind 长视频 AI 分析工作台，连接视频播放、ASR/OCR 时间轴、Evidence 检索与答案级结构化引用。
- 设计 Planner / Executor / Critic 分层分析和确定性来源校验，使引用可回溯到原始视频时间段。
- 实现跨重试累计 token 预算准入与阶段用量遥测，控制长视频分析的上下文与调用开销。

## 技术版

- 将 Whisper ASR、Tesseract OCR 统一为带来源身份的时序 `VideoContext`，提供 60 秒浏览窗口、逐条分页记录和五分钟检索分块。
- 使用 Qdrant 向量索引与关键词混合检索，向 Agent 各阶段提供不同粒度的上下文投影，减少重复 Prompt，同时保留可绑定的精确证据行。
- 在模型输出后验证来源文本、身份与时间覆盖；通过 Citation → Evidence → Timeline → Video seek 完成可核查交互。
- 以 FastAPI、Celery/RabbitMQ、MySQL、Redis、MinIO 组织异步处理和持久恢复，保留不调用外部 Provider 的历史 replay。

## AI / Agent 岗位版

- 将长视频 ASR/OCR 组织为分层时间语义表示，结合 Embedding/关键词检索为 Planner、Executor、Critic 提供角色适配上下文。
- 实现受控只读工具调用与确定性 Evidence Guard，防止模型自报来源直接成为可信引用。
- 增加模型调用前累计 token 准入、重试账本和按阶段 usage 来源标注；用真实媒体验收验证默认预算下的完成与引用绑定。

## 后端岗位版

- 构建 FastAPI REST/SSE + Celery/RabbitMQ 异步视频分析链路，使用 MySQL 持久执行记录与 checkpoint、Redis 运行态、MinIO 媒体对象和 Qdrant 检索索引。
- 为任务引入幂等键、受限重试、失败移交和无 Provider 调用的历史 replay，隔离恢复状态与历史事实。
- 提供可分页时序读取 API、来源校验与脱敏错误边界，并以离线测试覆盖核心应用和基础设施契约。

可核对的公开验证数据见 [发布说明](RELEASE_NOTES_v0.1.0.md) 与 [X3 评估](X3_OPENROUTER_X3_E_REPORT.md)。任何测试数量只应采用发布候选最终验证的实测值。
