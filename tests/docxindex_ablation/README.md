# DocxIndex 执行级消融试验

测试脚本和报告均放在本目录；不修改生产检索源码、`config.yaml`、现有合同或审查标准。

固定输入：`tests/criteria_formal_recall/cases/D01.docx` 至 `D10.docx`，以及
`tests/criteria_formal_recall/expected_cases_10.csv` 的 180 条期望。
启动时校验合同、期望表、审查标准和配置的 SHA-256。

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
| E08 | 每次重排新建局部 semaphore | 保留真实 LLMClient 共享限制；12 请求、6 线程的并发探针 |
| F01–F03 | 重排提示词的 `node_type` / `token_estimate` / `sources` | 各删一个字段，固定候选准入；每合同选一个最大载荷调用，配对 3 次 |

F01–F03 是筛查，不是对全项目字段的删除证明。相同提示词的模型输出也有波动；
字段使结果改变，不直接等于提高或降低准确率。

`combined` 是离线 E01/E02/E05/E06 组合，固定重排响应；不能拿它代表正式主程序候选。
正式 `safe` 组合仅含 E01/E05/E06/E07/E08，不含改变候选集合或提示词字段的干预。

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
| `run capture` | 当前配置、DOCX、18 条标准 | 真实建索引和检索；调用 LLM/embedding；保存提示词、响应、节点树和 usage |
| `run replay` | 已完成合同的捕获文件 | 固定模型响应，7 次交替本地对照；无模型调用 |
| `analyze` | 已完成合同的索引及重排响应 | E07/E08、本地并发、内容边界探针；无模型调用 |
| `live` | 固定索引、路由、结构与向量结果 | 仅真实重排；保存每个配对的提示词和响应，可续跑 |
| `formal` | 同一套合同和两个隔离主程序 API | 完整审查、任务 ID、trace、带批注 DOCX |
| `judge` | 最终 DOCX、原始 DOCX、标准和期望表 | LLM 初判，保留理由和真实可见批注，**不是人工准确率** |
| `report` | 已有实验结果 | CSV 与 `recommendations.md`；不调用模型 |

`capture` 只允许跳过已经完整采集的合同。合同中途失败后禁止“重建索引却复用旧查询”；
使用新结果目录重采该合同。原失败记录保留，不覆盖。`live` 的续跑只能使用同一捕获目录。

正式任务失败后默认停止。调查原因后，如保持相同模型和配置重试，可显式使用 `formal --retry-failed`；
旧失败记录保存到 `formal/<variant>/attempts/`，原 API task_id 不被覆盖，汇总保留失败尝试。

## 正式主程序对照

测试启动器运行原项目 `app.app` 和 `mcp_service.server.server`，不是重写审查工作流。
在两个终端分别启动：

```powershell
python -m tests.docxindex_ablation.service --api --variant baseline --port 5101
python -m tests.docxindex_ablation.service --api --variant safe --port 5102
```

随后在测试终端设置已有、已注册的 `API_KEY` 环境变量（不要把密钥提交进脚本），再运行：

```powershell
python -m tests.docxindex_ablation.formal --output $experiment
python -m tests.docxindex_ablation.judge --output $experiment
python -m tests.docxindex_ablation.report --output $experiment
```

隔离边界：

- 开发机运行 Python 主程序，监听 `127.0.0.1:5101/5102`，不占用现有 5000 端口。
- 测试进程禁用 Celery 入队，使用原 API 已有的进程内执行分支；审查 workflow 不变。
- Redis 使用 Docker 发布端口 `127.0.0.1:6380`，保留现有全局模型限额和 key；Redis 容器必须已启动。
- 真实 API 任务存入 `data/api/docxindex_ablation_<variant>/api/<task_id>/`，不会在启动时扫描或改写原服务任务。
- 批注 DOCX 和评测结果复制到 `tests/docxindex_ablation/results/<run>/formal/<variant>/`。
- 两种配置按合同交替执行顺序，各一遍；这是配对筛查，不足以消除全部模型随机性。不能宣称显著提速。
- 结束后关闭两个测试终端；不要停止用户原有服务或 Redis。

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
python -m unittest tests.criteria_formal_recall.test_semantic_supplement tests.criteria_formal_recall.test_scoped_retrieval -v
python -m ruff check tests/docxindex_ablation
python -m compileall -q tests/docxindex_ablation
```
