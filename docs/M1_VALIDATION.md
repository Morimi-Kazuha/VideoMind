# M1 验收记录（2026-10-08，Asia/Shanghai）

状态：**PASS WITH NOTES**。四项核心能力均有代码及确定性测试；真实Redis已验证。真实模型+真实视频端到端未执行，不能把Mock成功当成真实Provider效果。

## Git 与环境

- 工作区：`D:\Agent Learning\VideoMind-upload-finalization`；分支：`main`。
- 实施前HEAD、本地origin/main、实时远端main：`e75e8387afa1f5bf6c2c4b8ab10b1087fd66e32b`。
- 原有唯一工作区变更：未跟踪 `docs/interview-guide/`，保留原样，不进入M1提交；已逐文件校验SHA-256未改变。
- Python 3.13.3、Node v24.15.0、Docker 29.8.0；复用项目Windows `.venv`、现有node_modules和五个健康Docker基础设施服务。
- 沙箱内Python定位及Docker权限受限，批准提升执行权限后现有环境可用。没有替换`.venv`，没有安装新依赖。
- pytest默认临时目录有Windows权限问题；改用仓库外专用可写basetemp和忽略的work/cache，不改变测试标准。
- 未执行reset/clean/强推；提交范围只包含M1文件。最终commit及远端SHA在交付回复中报告，避免文档自引用提交哈希。

## 原始测试基线

指定原Follow-up命令：

```powershell
.venv\Scripts\python.exe -m pytest tests/application/test_p3_grounded_follow_up.py tests/infrastructure/test_p3_grounded_follow_up_provider.py tests/presentation/test_p3_grounded_follow_up_api.py -q
```

结果：56 passed。首个完整基线因临时目录权限产生166个setup错误；从原始HEAD导出测试副本，给它提供只读Git历史信任配置，并使用仓库外专用临时目录，最终未修改源码完整基线为 **1131 passed / 32 skipped**。前端原始基线 **79 passed**，原始构建成功。

干净基线的完整命令和输出保留在忽略的 `work/m1-baseline-correct.log`、XML中。副本为 `git archive HEAD` 导出的 `work/m1-original`，没有切换工作区或覆盖本地用户文件。

## 最终真实执行结果

| 检查 | 命令 | 结果 |
| --- | --- | --- |
| 原Follow-up兼容 | 上述三个测试文件，追加专用basetemp/cache | 56 passed |
| 新M1测试 | `.venv\Scripts\python.exe -m pytest tests/application/test_m1_conversation_memory.py tests/infrastructure/test_m1_conversation_provider.py tests/presentation/test_m1_conversation_memory_api.py -q`，追加专用basetemp/cache | 50 passed |
| 完整后端 | `.venv\Scripts\python.exe -m pytest -q --basetemp='D:/Agent Learning/tmp/m1-release-check-20261008' -o cache_dir=work/m1-cache --junitxml=work/m1-final-backend.xml` | 1181 passed / 36 skipped |
| 真实Redis | 设置 `DOVIDEO_MEMORY_REDIS_LIVE=1` 和现有Redis URL后，运行 `.venv\Scripts\python.exe -m pytest tests/infrastructure/test_m1_conversation_redis_live.py -q` | 4 passed |
| 前端 | 在client执行 `npm test` | 86 passed |
| 构建 | 在client执行 `npm run build` | 成功，40 modules |
| diff | `git diff --check` | 通过 |
| 演示 | `.venv\Scripts\python.exe tools/demo_conversation_memory.py --output docs/m1-demo-results.json` | A Guard PASS；B第10轮摘要+保留6轮；C evidence_rejected |

36项跳过包括原基线32项有条件Live测试，以及4项新增Redis opt-in测试；新增Redis测试已在显式Live运行中全部通过。后端只有原有Starlette/httpx弃用警告，没有新增断言失败或setup错误。没有重新运行不相关的大规模检索算法对照实验。

## 核心验收与限制

近期窗口、六个身份维度、TTL、重复请求、同会话顺序、旧CAS/旧租约拒绝、摘要触发/合并/失败保留/容量恢复、JSON边界、改写真实进入检索、原问题保留、注入数据隔离、错误历史和错误摘要引用拒绝、来源变化、删除、模型/检索失败、取消、写入降级提示均有测试。

前端覆盖关闭重开/刷新恢复、重建不重复追加、账号切换清空、新对话、goal/mode/media隔离、迟到历史和回答、稳定requestId重试、临时失败保留标识、删除清理与时间戳解析。没有改变已有SSE契约。

三组演示采用合成ASR与确定性Provider Mock，通过实际M1服务及Evidence Guard；实际输出见[m1-demo-results.json](m1-demo-results.json)。模型端点和密钥未配置，真实Provider+真实视频未执行。LocalR1也明确保留为确定性本地模式，不声称拥有真实LLM摘要或改写。

原有Evidence Guard、来源标识、revision和检索算法未弱化或重构。Redis是七天短期上下文；20轮摘要故障积压后的回答会明确提示未保存，压缩过的完整老问答只保留摘要；64条回执是有界去重，不是永久幂等账本。这些是已实现并有测试的范围取舍。

面试讲解、核心源码、五个追问和准确简历表述见[CONVERSATION_MEMORY.md](CONVERSATION_MEMORY.md)。
