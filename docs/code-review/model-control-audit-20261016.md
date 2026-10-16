# 模型控制复查与真实合同验证

日期：2026-10-16。基线提交：`b493eb513`。

## 本轮范围与执行方案

1. 复查三个 Celery worker 及其 MCP 子进程的 Agent、DocxIndex、LlamaIndex 模型入口，确认统一经过 `execute_model`。
2. 修正取消发生在名额释放阶段时的生命周期：等待同一次、有界的 Redis 清理结束，然后传播取消；不让任务退出打断后台清理。
3. 移除摘要压缩的独立三轮预算；模型超时、Redis 故障和发送次数耗尽后不再通过摘要兜底重新调用模型。
4. 补充超时、重复取消、真实 Redis 名额释放和摘要发送次数的回归验证。
5. 核对配置说明与实现，修正旧日志文档，明确列出 BGE reranker 映射与 OpenAI 兼容的 BGE embedding 配置方式。
6. 使用当前源码、三个独立 worker、隔离 Redis DB 0/DB 2 和 `deploy/test_contract.docx` 运行一个真实任务，保存任务、日志和报告证据。

## 已发现的问题

- 原 `model_semaphore` 退出仅调用 `asyncio.shield`。总截止时间在释放期间到期时，外层先返回，后台清理会受事件循环关闭影响。
- `llm_summary.py` 批量摘要捕获全部 RuntimeError 后启动单节点请求；单节点压缩固定重启三次逻辑调用，扩大时间和发送预算。
- 专门配置说明与基线实现一致，但 `docs/LOGGER_DESIGN.md` 仍记载旧限流和 LLM 重排行为。
- `deploy/config.yaml` 仍含新版本拒绝的旧字段。测试需使用迁移后的隔离配置，保留模型地址和凭据来源。

## 真实服务边界

本地现有部署镜像早于本次修改，不能用它验证当前源码。测试使用其已安装依赖及当前源码挂载，独立队列、名额和输出目录，不替换现有服务。当前本地配置的模型地址为阿里云域名；测试结果不代表公司内网模型服务性能。

## 执行结果

复查及修正完成。所有生产模型 HTTP 入口都经过公共控制器；真实合同任务完成并生成报告，但一个审查要点失败，不能解释为整份合同全部审查成功。

### 修正内容

| 位置 | 本轮修正 |
| --- | --- |
| `endpoints/runtime/llm_semaphore.py:47` | 申请异常和释放名额都等待同一次有界清理结束；重复取消不重启清理时限，不遗留后台释放任务。 |
| `endpoints/runtime/llm_semaphore.py:182` | 已发生的超时/取消优先于后续 Redis 清理错误；正常收到响应后的释放故障仍明确失败，不重发模型请求。 |
| `endpoints/runtime/model_calls.py:49` | 同步输出校验跨过截止时间后，校验异常归类为模型超时，不能被摘要截断兜底掩盖。 |
| `tools/docxindex/indexing/llm_summary.py:119` | 批量摘要调用失败直接上抛；只有成功响应缺少其他节点时才生成相应节点摘要。 |
| `tools/docxindex/indexing/llm_summary.py:170` | 同节点摘要长度修复纳入一次公共调用的预算；修复耗尽后仅截断已经收到的候选，不追加请求。 |
| `config.py:312`、`config.example.yaml` | 显式列出 BGE 映射，保留已有名称，增加测试网 `qwen3-reranker` 的 Higress 协议别名。 |

### 调用入口核对

| 调用方 | 统一控制路径 |
| --- | --- |
| Agent，包括 Planner/SubAgent/Reflector/Summarizer | `agents/base_agent.py` → `execute_model` |
| DocxIndex 索引、路由、扫描及摘要 LLM | `tools/docxindex/llm/client.py` → `execute_model` |
| DocxIndex embedding | `tools/docxindex/vector/client.py` → `execute_model` |
| 两种检索后端的专用 reranker | `tools/docxindex/reranker.py` → `execute_model` |
| LlamaIndex 同步/异步 chat、completion | `tools/retrieval/llamaindex/limited_llm.py` → `execute_model` |
| LlamaIndex 六个 embedding 入口 | `tools/retrieval/llamaindex/limited_embedding.py` → `execute_model` |

SDK 自身重试关闭，未使用的 streaming 入口明确拒绝。三个 worker 共用同一 Redis DB 2 和相同资源键；MCP 子进程继承任务截止时间。

60 秒是一次逻辑模型操作的预算，包含等待、请求、退避及同一结果的格式修复。不同节点、工具返回后的继续生成和新业务反馈后的修订是独立操作，仍受整份任务的 1800 秒硬时限控制。`_validate_and_revise_output` 的固定 `max_revisions=2` 是每次确定性业务检查最多追加两次新结论修订，并非整个要点最多调用两次模型。

普通模型超时会先主动清理自身名额。Redis 不可用或进程被强杀时不能保证主动释放，残留名额仍按租约到期回收；本轮没有把租约回收描述为立即删除。

### 自动验证

- 正式回归套件：260 项，Windows 下 259 项通过、1 项 Linux 专项跳过；该 Linux 专项已在 Docker 单独运行通过。
- 上述 Linux 专项：真实 Celery prefork，11.223 秒通过；2 秒硬时限下持锁任务在 2.168 秒记录超时，执行子进程 PID `9 → 12 → 15`，工具进程停止，后续任务成功。
- 消融兼容套件：22 项通过。
- 模型控制使用真实隔离 Redis 和本地 HTTP，覆盖三个独立进程共享三类容量、等待/请求/释放超时、重复取消、清理故障、硬杀持有者后的租约回收，以及不得因释放失败重发模型。
- 摘要专项覆盖发送预算、共享截止、批量故障不扩散请求和同步校验跨截止。
- BGE/Qwen 协议测试覆盖生产 `bge-m3`、`bge-reranker-v2-m3` 和测试 `qwen3-embedding`、`qwen3-reranker`；这些使用 MockTransport，不代表内网服务连通性已验证。
- Ruff、Python 编译及差异检查通过。

### 真实合同任务

输入为 `deploy/test_contract.docx`，通过正常 HTTP API 上传，在挂载当前源码的隔离集群中运行。三个 worker 均为 prefork、并发 1，本次一份合同由 worker 2 处理；并非三份真实合同同时压力测试。依赖版本为 Celery 5.6.3、Kombu 5.6.2、redis-py 8.1.0。

| 项目 | 实测结果 |
| --- | --- |
| 任务 ID | `20261016-164723-9e72` |
| 客户端提交至终态 | 222.532 秒，约 3 分 43 秒 |
| API 任务状态 | `succeeded`，已生成并下载报告 |
| 审查质量字段 | 18 项中 17 项完成，H18 错误 |
| DOCX 批注 | 44 条 |
| 实际发送 | 104 次：LLM 82、embedding 19、reranker 3 |
| 逻辑操作和重试 | 95 个操作，9 个操作各额外发送 1 次；单操作最多 2 次 |
| 统一控制字段缺失 | 0 次；所有已发送调用都有类型、名额 key、预算、剩余时间和发送编号 |
| 最大记录累计耗时 | `timing.total_seconds=57.941402`；相对逻辑操作预算起点，非单次 HTTP 耗时 |
| 模型超时 | 本次未发生；超时清理结论来自上述专项测试 |
| Redis 结束状态 | LLM、embedding、reranker 的 owners/waiting/deadlines 均为 0 |
| 实跑采样占用峰值 | LLM 4、embedding 3、reranker 1，未越过 5/3/10 上限 |

H18 初始响应修复为合法 JSON 后，业务锚点检查触发新的结论修订。该修订两次响应均为 HTTP 200，但 `comparison_decisions` 中证据锚点的类型和 ID 不成对，最终产生 `AgentOutputValidationError`。这不是超时、Redis 排队或名额释放失败，也不能通过把报告生成状态解释为全部要点完成来忽略。

所有尝试中已收到的输入用量为 490620 token，输出为 49253 token；3 次 reranker 响应未提供 usage，因此不是完整成本统计。日志中的 0 或未知字段不代表没有消耗。

本次真实调用沿用本地已配置的阿里云域名服务。没有改成未知的内网地址，也没有验证生产 BGE 或测试网 Qwen embedding/reranker 的实际推理。

证据保留在被 Git 忽略的 `deploy/control-audit-20261016/`：

- `real-run-summary.json`：汇总及公开质量状态。
- `review-report.docx`：下载的报告。
- `data/api/20261016-164723-9e72/`：任务、调用、要点进度及错误。
- `redis-samples.jsonl`：每 0.2 秒名额采样。
- `service-logs.txt`：API 和三个 worker 日志。
- `formal-tests-final.log`、`ablation-tests.log`、`linux-celery-tests.log`：回归记录。

测试容器在验证后停止并移除，以上文件保留在宿主机。原有服务和原始 deploy 配置不被替换；旧 deploy 配置仍需按[配置说明](../CONCURRENCY_TIMEOUT_CONFIGURATION.md)迁移后才能运行新镜像。
