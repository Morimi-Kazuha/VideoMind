# VideoMind 本地启动指南

本文区分轻量界面探索与完整视频分析。命令均从仓库根目录执行，除非特别注明。系统要求：Python 3.12+、Node.js 22.12+、npm；完整分析还需要 Docker Compose、FFmpeg/ffprobe、Tesseract 和可用的 OpenAI Whisper Python 包及模型权重。外部聊天与 Embedding Provider 需由使用者自行配置并承担调用费用。

## 1. 安装应用依赖

```bash
python -m venv .venv
# macOS / Linux
source .venv/bin/activate
# Windows PowerShell：运行 .\.venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
```

前端在 `client/` 中执行 `npm ci`。若只查看本地 API 和界面，可跳到第 4 步，保持 `DOVIDEO_PROFILE=local`，暂不启动 Compose 或 worker。本地默认组合是开发模式；它不是下述完整媒体分析组合。

## 2. 配置完整分析环境

复制 [.env.example](../.env.example) 为仓库根目录下的 `.env.r2.local`。该文件已由 `.gitignore` 排除。将其中所有 `replace-me` 换成独立的本地密码或密钥，保持 `DOVIDEO_PROFILE=production`，确认 `DOVIDEO_R2_DATA_ROOT` 是可写目录，并替换聊天与 Embedding 的示例 endpoint/model。Compose 用同一文件配置 MySQL、Redis、MinIO、Qdrant、RabbitMQ；**API 与 worker 进程也必须加载这个文件**。Compose 的 `--env-file` 不会自动设置宿主机终端的进程环境。

聊天模型有两种受支持配置，选一种：

- OpenRouter：设置 `DOVIDEO_MODEL_TRANSPORT=openrouter`、`DOVIDEO_OPENROUTER_API_KEY`、`DOVIDEO_MODEL_PROVIDER_TAG` 和 `DOVIDEO_MODEL_MODEL`。仓库已验证的 X3 模型配置示例见 [.env.example](../.env.example)，固定供应商的实时可用性仍需在账户中确认。代码会固定供应商并禁用回退。
- 其他 OpenAI 兼容接口：设置 `DOVIDEO_MODEL_TRANSPORT=openai-compatible`、`DOVIDEO_MODEL_BASE_URL`、`DOVIDEO_MODEL_API_KEY` 和 `DOVIDEO_MODEL_MODEL`。选择支持项目所需结构化输出的模型。

完整 R4 路径还要求 `DOVIDEO_EMBEDDING_API_KEY`、`DOVIDEO_EMBEDDING_BASE_URL` 和 `DOVIDEO_EMBEDDING_MODEL=BAAI/bge-m3`。Embedding 接口应与 OpenAI Embeddings 格式兼容。聊天与 Embedding 密钥可以来自不同供应商。自动模型路由默认关闭；它是单独的可选配置，不是完成一次分析的前提。

安装本机媒体工具后，用 `ffmpeg -version`、`ffprobe -version`、`tesseract --version` 确认命令可调用。在虚拟环境中运行 `python -m pip install -U openai-whisper`，再用 `python -c "import whisper"` 验证导入；PyTorch 等平台依赖按 [Whisper 官方安装说明](https://github.com/openai/whisper#setup) 安装。首次加载模型可能下载权重。可通过 `DOVIDEO_FFMPEG_DIR`、`DOVIDEO_TESSERACT_PATH`、`DOVIDEO_WHISPER_MODEL_ROOT` 指向已有本地安装。`DOVIDEO_*` 是保留的配置接口名称。

## 3. 启动基础服务

```bash
docker compose --env-file .env.r2.local -f docker-compose.r2.yml config --quiet
docker compose --env-file .env.r2.local -f docker-compose.r2.yml up -d
docker compose --env-file .env.r2.local -f docker-compose.r2.yml ps
```

请在启动 API 和 worker 的**每个**终端导入同一份私有配置。POSIX shell 示例：

```bash
set -a
. ./.env.r2.local
set +a
```

PowerShell 示例，处理模板中简单的 `KEY=value` 行；复杂含引号、换行的密钥应在当前进程中单独设置：

```powershell
Get-Content .env.r2.local | ForEach-Object {
    if ($_ -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
```

不要把实际配置内容打印到终端记录、截图或提交中。

首次部署和每次升级，在加载环境变量后、启动任何 API/worker 前执行一次：

```bash
alembic upgrade head
alembic current
alembic check
```

全新库和已有 VideoMind 库使用同一迁移路径；不要对旧库直接 `stamp head`。
API/worker 启动只检查 revision，不运行 DDL。迁移失败时停止部署。
MySQL 迁移使用 30 秒部署锁等待及 metadata lock 等待；生产发布可再加外部总时限。
备份、历史兼容范围及预处理版本规则见 [工程化说明](BACKEND_ENGINEERING.md)。

## 4. 启动 API、worker 与前端

终端 A（已激活虚拟环境；完整分析时已导入私有配置）：

```bash
python -m dovideo api
```

终端 B（仅完整分析需要；同样已激活虚拟环境并导入配置）：

```bash
python -m celery -A dovideo.infrastructure.celery_worker:celery_app worker --loglevel=WARNING --pool=solo --concurrency=1 -Q dovideo.analysis
```

终端 C：

```bash
cd client
npm ci
npm run dev
```

浏览器打开 `http://127.0.0.1:5173`。运行 `curl http://127.0.0.1:8000/health` 验证 API；前端开发代理默认指向该地址。若端口不同，可设置 `VITE_DEV_PROXY_TARGET`。数据库结构由上一步 Alembic 迁移建立；API/worker 只检查 revision，基础设施组合会确保 MinIO bucket 存在。生产配置缺失时启动直接报错，不会静默退回本地组合。

## 5. 演示与验证

注册/登录测试账户，导入**有权使用**的视频，在媒体库打开分析工作台，提交问题。等待 worker 完成后检查 ASR/OCR 记录、证据检索、AI 回答与引用跳转。参考 [README 演示路径](../README.md#五分钟演示路径)。处理时长取决于媒体、机器与供应商。

```bash
python -m pytest -q
cd client
npm test
npm run build
```

测试套件使用离线替身，不需要真实 Provider 密钥。完整 R4 live acceptance 是单独的付费验证流程，不属于日常启动或 CI。

### M1 / M2 与 F1 验收

生产 R4 会将 M1 接入 Redis；工作台保留当前会话标识，重开或刷新后恢复近期问答。指代追问先由已配置的聊天 Provider 改写再检索，回答保留原始问题。历史与摘要不能作为视频证据；默认 local 模式的确定性替身不能用于证明真实 LLM 能力。

M2 默认 `DOVIDEO_ADAPTIVE_RETRIEVAL_ENABLED=false`。仅在需要验证复杂检索的受控 API/worker 进程中显式设为 `true`，两边使用相同配置，记录决策、子查询与 Hybrid 次数；验证后停止验收进程或恢复 `false`。不要改写生产默认文件，也不要为测试切换 Provider、降低 Guard 或无限重试。

F1 工作台的“核心结论”读取现有 Citation API 的结构化投影，点击相关证据复用原播放器跳转；没有合法引用时显示缺失状态，Markdown 导出保持原样。验收应选用获准的视频和项目私有配置，限制为一次主线分析、至少两轮 M1 追问，预算允许时再运行真实摘要。逐项记录 REAL_VIDEO + REAL_PROVIDER、REAL_INFRASTRUCTURE、DETERMINISTIC_TEST、MOCK 或 NOT_RUN，保留 M2 单查询对照。

本轮实测、阻塞与封板门槛见 [FINAL_FREEZE_REPORT.md](FINAL_FREEZE_REPORT.md)。Windows pytest 临时目录受限时使用独立的新目录，例如：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp='D:/Agent Learning/tmp/f1-check-new' -o cache_dir=work/f1-cache
```

`basetemp` 必须是专供本次测试的目录，不要指定已有业务数据或用户目录。

## 6. 可选检索 Reranker

默认 `DOVIDEO_RERANKER_ENABLED=false`，长视频采用 Dense + BM25 → RRF。
启用时配置独立的 `DOVIDEO_RERANKER_URL`、`DOVIDEO_RERANKER_API_KEY`、
`DOVIDEO_RERANKER_MODEL`；默认目标为 `BAAI/bge-reranker-v2-m3`。超时、
最大尝试次数和重试延迟见 `.env.example`。该 adapter 实现 SiliconFlow
text `/rerank` 协议，不能把任意 OpenAI chat 地址当成 rerank endpoint。
在 API/worker 各终端加载同一配置，真实凭据仅放在忽略的私有环境文件中。
本地 offline composition 保持 Reranker 关闭，不依赖远端精排。

启用但失败时，base application 记录 `rerankerFallbacks` 并保持 RRF 顺序；
canonical Strict R4 会拒绝此次降级。关闭时不记录失败。检索评测仍用
现有 X3 runner；离线复现命令如下（输出目录必须为空）：

```bash
python tools/run_retrieval_comparison.py --output work/retrieval-comparison-new
```

案例明确标为 SYNTHETIC，使用本地 TF-IDF，默认关闭 Reranker；它不验证
真实 BGE-M3 或 Cross-Encoder 的效果。结果与限制见 [检索说明](RETRIEVAL.md)。
