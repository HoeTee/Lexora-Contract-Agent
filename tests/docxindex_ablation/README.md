# DocxIndex 执行级消融试验

测试脚本和报告均放在本目录；不修改生产检索源码、`config.yaml`、现有合同或审查标准。

固定输入：`tests/criteria_formal_recall/cases/D01.docx` 至 `D10.docx`，以及
`tests/criteria_formal_recall/expected_cases_10.csv` 的 180 条期望。
启动时校验合同、期望表、审查标准和配置的 SHA-256。

## 当前执行顺序：先核验效果，不先比较速度

旧组合 `safe` 尚未证明质量不下降，正式提交入口现已停用；下面的组合配置说明仅为历史记录。当前重排已改为独立 reranker，并统一使用 DB 2 模型控制；旧 LLM 排序响应不能充当当前版本的重排基线。新采集写入 `capture_protocol: dedicated_reranker_v1`，`analyze/live` 会拒绝旧协议目录，旧报告和证据保留用于历史核验。
不得将 180 次原文/锚点等价转换为 180 条问题全部正确召回。

1. 核验原标准、180 条期望的有效性、基线实际可见批注和总结。
2. 定义每条用例的验收条件；复合问题必须写明全部要求，部分召回单列，不算完整命中。
3. 基线全部核验后，才进入单操作消融。基线本身可以有漏检、误判，不能为了“通过”把它标成满分。
4. 默认不接受已正确召回用例丢失、新增事实误判或错误落点；争议、失败与未核验不视为通过。
5. 质量守住后才比较时间/调用成本，最后再验证通过的单项组合。

### 基线质量核验入口（不调用模型）

```powershell
python -m tests.docxindex_ablation.quality prepare --output $experiment
python -m tests.docxindex_ablation.quality check --output $experiment
```

`prepare` 冻结原文、标准、期望、基线任务及最终 DOCX 的版本，生成 `baseline_quality/`：

- `snapshot.json`：直接读取 DOCX XML 的全文位置和真实可见批注，不使用检索结果冒充完整原文。
- `review.json`：逐条评判记录；初始为 pending，必须填写核验者、标准依据、验收条件、命中/部分命中/未命中及实际批注引用。
- `cases.csv`、`comments.csv`：供审阅的用例与批注明细，含原文和批注文本；由 review.json 导出，不从 CSV 自动导入判断。
- `conflicts.csv`：标准与用例口径冲突的证据。第 3 条要求“预留”字段，而原测试把“未填写”视为错误，10 份合同均有此冲突。
- `gate.json`：未核验、争议、缺行、虚构引用、错误落点、版本变化均不放行；不自动删除争议用例缩小分母。

未通过时命令退出码为 2，召回率/准确率为 null，不伪造分数。
程序只检查版本、引用、覆盖和评判记录完整性，不能证明人工或 AI 判断本身正确。
`baseline_ready=true` 只表示基线已完整核验，不代表消融可部署；单项的质量对照与组合验证仍是后续步骤。
标准或测试合同的口径修订须先确认；本轮未改它们，后续单项正式复测暂不启动。

## 实验范围

| 编号 | 具体代码操作 | 验证方式 |
| --- | --- | --- |
| E01 | `retriever.py` 单候选仍调用 `rerank_matches` | 同一模型响应下跳过；比较最终原文和锚点 |
| E02 | 结构已覆盖允许检索范围，仍发起向量查询 | 比较候选字段；不能把“无新增节点”当成提示词不变 |
| E03 | `_merge_semantic_matches` 在重排前截断新节点 | 所有触发调用真实配对 3 次；最终仍选 10 个节点 |
| E04 | `content._prefer_specific_nodes` 父子节点过滤 | 去掉过滤，对比新增和丢失的原文 |
| E05 | `content.get_node` 重复线性扫描 | 替换成节点字典；离线时间包含字典构建成本 |
| E06 | `_format_anchor_context` 重复排序、全量扫描锚点 | 排序索引加范围查找；复用生产渲染函数保证文本和顺序 |
| E07 | 单次在线重排的内存缓存、哈希、锁操作 | 保留真实重排函数，只回放模型响应；不删除共享/落盘缓存 |
| E08 | 已移除的批次局部 semaphore | 已停用，显式报错；不得把关闭全局模型控制当成对照 |
| F01–F03 | 重排提示词的 `node_type` / `token_estimate` / `sources` | 各删一个字段，固定候选准入；每合同选一个最大载荷调用，配对 3 次 |

F01–F03 是筛查，不是对全项目字段的删除证明。相同提示词的模型输出也有波动；
字段使结果改变，不直接等于提高或降低准确率。

`combined` 是离线 E01/E02/E05/E06 组合，固定重排响应；不能拿它代表正式主程序候选。
历史 `safe` 组合曾包含 E01/E05/E06/E07/E08，当前不能启动；新版本需要重新验证单项，不能沿用旧组合结论。

## 运行命令

在项目根目录的 PowerShell 执行。结果目录自行选择一个新名称：

```powershell
$experiment = 'tests/docxindex_ablation/results/my-run'
python -m tests.docxindex_ablation.run capture --output $experiment
python -m tests.docxindex_ablation.run replay --output $experiment
python -m tests.docxindex_ablation.analyze --output $experiment
python -m tests.docxindex_ablation.live --output $experiment --workers 2
python -m tests.docxindex_ablation.report --output $experiment
```

| 命令 | 输入 | 输出 / 外部调用 |
| --- | --- | --- |
| `run capture` | 当前配置、DOCX、18 条标准 | 真实工具级建索引和检索；调用 LLM/embedding/独立 reranker；全部模型请求经过 DB 2，保存请求、响应、节点树和 usage；不代表整合同 Celery 验证 |
| `run replay` | 已完成合同的捕获文件 | 固定模型响应，7 次交替本地对照；无模型调用 |
| `analyze` | 新协议的索引及重排请求、索引分数 | E07 本地成本及内容边界探针；E08 标为停用，全局并发标为未测量；无模型调用 |
| `live` | 新协议的固定索引、路由、结构与向量结果 | 仅调用配置的独立 reranker，通过 DB 2 限流；保存每个配对的请求与分数，可续跑 |
| `formal` | 同一套合同和两个隔离主程序 API | 完整审查、任务 ID、trace、带批注 DOCX |
| `judge` | 最终 DOCX、原始 DOCX、标准和期望表 | LLM 初判，保留理由和真实可见批注，**不是人工准确率** |
| `report` | 已有实验结果 | CSV 与 `recommendations.md`；不调用模型 |

`capture` 只允许跳过已经完整采集的合同。合同中途失败后禁止“重建索引却复用旧查询”；
使用新结果目录重采该合同。原失败记录保留，不覆盖。`live` 的续跑只能使用同一捕获目录。

正式任务失败后默认停止。调查原因后，如保持相同模型和配置重试，可显式使用 `formal --retry-failed`；
旧失败记录保存到 `formal_verified/<variant>/attempts/`，原 API task_id 不被覆盖，汇总保留失败尝试。

## 正式主程序对照

测试启动器运行原项目 `app.app` 和 `mcp_service.server.server`，不是重写审查工作流。
当前只启动基线采集。正式任务固定进入 Celery，不能关闭队列。已有完整旧基线仍可做对应版本的质量核验，但不能作为新重排协议的对照。以下命令在同一 Linux/WSL 测试项目中、不同终端分别运行 API 与配套 worker；两个进程必须能通过 `config.yaml` 连接 DB 0 和 DB 2，数据路径必须一致：

```powershell
python -m tests.docxindex_ablation.service --api --variant baseline --port 5101
python -m tests.docxindex_ablation.service --worker --variant baseline
```

随后在测试终端设置已有、已注册的 `API_KEY` 环境变量（不要把密钥提交进脚本），再运行：

```powershell
python -m tests.docxindex_ablation.formal --output $experiment --variants baseline
python -m tests.docxindex_ablation.report --output $experiment
```

隔离边界：

- Linux/WSL 测试开发机运行源码及已安装依赖，API 监听 `127.0.0.1:5101`；不要求远端镜像宿主机具备源码或 Python。
- API 将任务投递至独立队列 `contract_agent_ablation_baseline`，必须启动匹配的 `--worker`；测试 worker 使用 `prefork/concurrency=1`，不会消费生产默认队列。生产部署仍保持三个 worker。
- Redis 地址完全读取当前 `config.yaml`；DB 0 存任务、DB 2 控制模型名额。不替换成发布端口，不修改 key、容量、租约，不关闭限流。测试环境需要自己满足所配置主机名和网络的可达性。
- 真实 API 任务存入 `data/api/docxindex_ablation_<variant>/api/<task_id>/`，不会在启动时扫描或改写原服务任务。
- 批注 DOCX 和评测结果复制到 `tests/docxindex_ablation/results/<run>/formal_verified/<variant>/`。
- 目前只运行基线，不能拿单次结果宣称消融提速。后续配对筛查也不足以消除全部模型随机性。
- API、worker 和 MCP 都必须使用同一项目位置、数据根目录和模型配置；不要让宿主机 API 将本地绝对路径传给看不到该目录的容器 worker。
- 结束后关闭测试 API 和测试 worker；不要停止用户原有服务或 Redis。

普通部署的日志核验也可使用 `scripts/verify_real_contract_logging.py --help`，它访问已运行 API，并通过 `--task-root` 显式指定开发机看到的共享 `data/api`，不启动应用、不覆写配置、不关闭队列或模型限流。真正执行该脚本会提交真实合同；`--help` 不调用模型。

### 干预生效校验与本轮纠正

MCP SDK 不会默认继承全部环境变量。早期 `formal/safe/` 中的任务虽然带有候选标签，
但子进程实际运行了基线；这些任务不能作为候选对照，保留原始文件并列入
`invalid_treatment_runs.json`，不参与新报告。早期基线任务通过启动器源码审计确认后复用，
其记录明确标注 `legacy baseline source/launcher audit; no runtime marker`，不伪造日志。

新入口使用 `baseline_server` / `safe_server` 显式模块选择，不依赖环境变量传递。
每次实际建索引会在本任务 `events.log` 写入 `docxindex_ablation variant=... operation=installed`；
缺少、错误或混合标签都不能通过实验有效性校验。单候选跳过另记录 `skip_single_rerank`。
旧 `--variants safe` 组合入口已停用，不能跳过基线质量核验和单项验证。

旧 `judge` 输出包含无效候选标签，而且事实抽检失败，仅保留为失败的自动评判尝试；
新报告不导入这些召回率/准确率数字，质量结论须审阅实际 DOCX 后确认。

## 结果口径

1. 原文及锚点等价不等于“审查准确率 100%”。
2. 单候选重排被省掉的时间合计是工具调用耗时，不是并发工作流墙钟时间节省。
3. 最终召回同时考虑普通批注和 AI 审查总结；普通批注准确率与总结质量分开。
4. `quote_observations.csv` 只是字面证据覆盖观察，不用它计算问题召回率。
5. `judge` 优先依据标准，再看合同事实、实际锚点和可见批注；重复本身不算错误。
6. 错误期望、无法确认的样本和模型初判要单列，不能偷偷剔除后宣称更高准确率。
7. 生成结果包含合同原文及提示词，已被 Git 忽略；不得分享给无权访问合同的人。
8. 本次对自动评判的抽检已发现事实误判，`llm_draft_*` 指标不能用作发布门禁或质量提升结论；最终正确率仍需逐条人工核验。

## 回归验证

```powershell
python -m unittest tests.docxindex_ablation.test_experiments -v
python -m unittest tests.docxindex_ablation.test_quality -v
python -m unittest tests.criteria_formal_recall.test_semantic_supplement tests.criteria_formal_recall.test_scoped_retrieval -v
python -m ruff check tests/docxindex_ablation
python -m compileall -q tests/docxindex_ablation
```
