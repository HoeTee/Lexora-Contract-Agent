# 模型请求统一控制实施与验收记录

实施日期：2026-10-25—2026-10-26。当前状态：已完成。最终 r5 镜像同批混合六份流式合同全部 18/18，两份非流式合同全部 18/18；日志、API 和报告状态一致。500 项自动化回归均有对应平台的通过证据。

方案：[MODEL_REQUEST_CONTROL_PLAN.md](C:/Users/lenovo/Desktop/contract_agent/docs/MODEL_REQUEST_CONTROL_PLAN.md)。配置逐项含义、默认值、null 行为和时间关系：[CONCURRENCY_TIMEOUT_CONFIGURATION.md](C:/Users/lenovo/Desktop/contract_agent/docs/CONCURRENCY_TIMEOUT_CONFIGURATION.md)。

## 1. 已实现内容

| 范围 | 实际行为 |
| --- | --- |
| 三种客户端 | LLM、embedding、reranker 客户端位于 endpoints/runtime；旧检索适配层调用这些客户端。model_calls 负责共同的准入、预算、记录和清理；不再维护 model_http 工厂。 |
| 输入限制 | 三种模型使用各自 max_input_tokens；LLM 的 max_context_tokens、max_prompt_tokens、max_result_tokens 已移除。没有隐藏的官方容量取最小值，没有输出 token 上限。 |
| tokenizer | 六套固定版本的原版 tokenizer 随镜像离线提供，支持一个 tokenizer 对应多个模型服务名称。必须显式配置 cl100k_base: [other]；未匹配名称有估算标记。 |
| Agent 历史 | 超限时在发送副本中移除较旧完整消息组，保留必需任务与当前工具事务。原始历史、工具返回与日志不做字符截断。 |
| 向量与重排 | 临时切片最高分归并到原 node，返回完整原文和原 node_id；保留 embedding 重叠配置及原 reranker 请求格式。 |
| 全局准入 | Redis 原子处理跨三个 worker 的 FIFO、瞬时并发、此前 60 秒滚动 RPM。RPM 等待不占用模型并发名额，重试逐次计数。 |
| 时间 | 每次实际 HTTP 请求独立计时；非流式限制总时长，流式限制连续无新增内容时长。名额等待单独累计，合同硬时限仍是外层约束。 |
| 租约 | 获取名额后开始有限 lease，运行中自动续期；正常结束或请求取消主动释放，进程崩溃依靠有限租约回收。 |
| 流式结束 | 识别 finish_reason 和真实 [DONE]，不伪造结束信号。仅 [DONE] 没有可用响应或无结束依据的 EOF 均失败。传输结束后仍执行原 JSON、Pydantic 和复核规则。 |
| 失败统计 | 发送前保留本地输入计数，失败和部分流式输出不清空消耗。服务 usage 与本地估算分别标识；未知输出不伪装成零。API、任务与日志汇总按 call_id 去重。 |
| MCP | 专用 stdio 入口在导入依赖阶段将普通打印转入 stderr，之后恢复 stdout 的 JSONRPC 通信。保留父进程退出时的子进程保护。 |
| 可空限制 | 资源上限和可关闭的时限支持 null/省略；轮询、心跳、lease 等算法运行参数仍需要有限正数，详见配置文档。 |

保留三个 Celery worker、每个 concurrency=1，以及 Web/API 两个入口。未修改 checkpoint，未为任何 H 编号增加特殊判断。

## 2. 验收期间发现并修复的通用问题

1. 比较审计的证据范围未完整传给复核模型。正文中出现“附件”标题时，模型会将正文目录当作实际附件。现将真实 source_region 以及同一份 targets/evidence 清单传给初始审查、复核、修复和确定性校验；保留 evidence=[]，不放宽证据归属规则。
2. 服务以 finish_reason=length 结束且 JSON 不合法时，后续修复会再次发送整段无效输出，使输入从约 5.4K 增至约 72K token。现仅从发送副本排除这条完整的无效截断回复，保留原任务、证据、校验反馈和全部原始日志；正常完整回复的修复仍使用原流程。
3. 对未闭合 JSON 使用最后一个右花括号裁剪后，再次解析失败会覆盖原始错误。现保留完整对象首次解析错误，避免将裁剪引入的“缺逗号”反馈给模型。
4. SubAgent 补充检索的展示分隔符与原文解析边界不同，第一条补检证据会被并入上一条原文。合并时增加统一原文边界，保留展示标签；专项验证首条附件证据、多次补检及原文内容不会相互污染，证据范围校验仍严格执行。
5. 补充非流式测试首次查询状态时，read_task 的 exists→read 之间发生 FileNotFoundError，API 500 导致测试脚本提前关闭服务。该次尚无实际模型请求。去掉先检查后读取的窗口，仅对文件暂时不存在做短暂、有界的读取重试；真正不存在仍返回 None，JSON 损坏与权限错误不被吞掉。底层文件短暂不可见的具体原因不确定，失败证据保留。

这些修复不增加配置、不调整 60 秒、不增加输出上限，不以合同风险判断是否通过程序验收。

## 3. 测试环境与配置

真实请求运行在本地 Docker，使用当前可访问的外网模型服务；这不是公司内网实测。API 与三个 worker 使用同一构建镜像、同一配置与离线资源。测试使用独立 Compose 项目、独立 Redis，以及宿主机挂载的 data 总目录。

正式批次冻结配置：LLM/embedding/reranker 输入上限 88000/1024/1024，并发 5/3/10；LLM RPM 30，另外两种 RPM null；模型等待 180 秒，单次模型时限 60 秒，额外发送次数 5，要点总尝试次数 3；合同硬时限 1800 秒。没有为得到通过结果而提高这些值。

本地实际模型为 qwen3.6-35b-a3b、text-embedding-v4、qwen3-rerank。后两种名称未映射到原版资源，按 other 使用 cl100k_base，计数结果属于估算；内网部署应按实际名称配置 tokenizer 映射。

## 4. 已完成专项

- 六套 tokenizer 在构建镜像时断网加载并校验固定 revision/SHA；不下载模型权重。
- Redis 三进程测试覆盖并发、滚动窗口、等待、取消、异常、租约续期和释放。
- 流式专项覆盖结束信号、空响应、EOF、心跳、内容空闲、工具参数、断流、超时重试和部分输出统计。
- 真正的 MCP 子进程完成 initialize/list_tools，返回 10 个工具，JSONRPC 解析错误为零。
- Linux Celery 真实子进程测试覆盖硬时限与 worker 中断后的任务状态、子进程停止及替补进程。

本地 unittest 是自动化回归，不是 CI；真实模型合同测试另列。

最终代码全套 unittest：500 项，499 项通过，1 项因 Windows 不支持 Linux prefork 硬时限测试而跳过；该项随后在最终 Linux 镜像单独执行通过（13.552 秒）。因此 500 项均有对应平台的通过证据。编译与相关文件空白检查通过。

回归过程中曾出现 Windows 临时 task.json 重命名被拒绝的 WinError 5；具体占用进程不确定。隔离该用例连续 30 次、480 次并发更新均通过，最终全套也通过。没有放宽断言或为此修改任务锁。状态读取竞争另外新增 8 项测试，修复前复现 API 500，修复后 200；仅文件缺失重试 3 次，两次 10ms 等待，总计 20ms。原根目录测试控制台日志已按用户要求清理；本节保留当时测试结论，逐合同调用记录与验收结果仍在下列任务目录中。

## 5. 真实合同批次

以下任务 token 来自 API 与全部已发送调用日志的去重汇总：输入包括三类模型的输入，输出只统计 LLM 文本；embedding 向量维度和 reranker 分数不当作输出 token。本地估算回退、服务 usage、失败和未知输出均保留来源与完整性标记。

### 非流式专项：两份用户指定合同

批次 mc202610252317287677b2，321.437 秒，两个任务均 18/18，API、任务记录、报告一致。

| 合同 | task_id | 要点完成 | 实际请求数 | 输入 token | 输出 token |
| --- | --- | --- | --- | --- | --- |
| deploy/test_contract.docx | 20261025-231736-06c6 | 18/18 | 94 | 533302 | 53235 |
| 2025 年度算力资源采购合同 1223 | 20261025-231736-8ef1 | 18/18 | 102 | 522960 | 60049 |

两任务各有 5 次中间校验失败并恢复，消耗保留在总数中。LLM 并发峰值 5、滚动分钟峰值 30；embedding 并发峰值 3；reranker 并发峰值 1。结束时三种模型活动名额、等待者和等待截止项均为零。保留 RPM 窗口记录是正常计数，不是未释放的名额。

证据：[nonstream/result.json](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/nonstream/result.json)。Windows 长路径读取问题修复后重新核对同一批原始产物，没有重跑或更换成功任务。

### 首轮流式六份：未通过，完整保留失败证据

批次 mc2026102523313184374e，一次性提交六个不同合同，提交屏障为 6；三个先执行，另外三个排队接手。整批 2192.219 秒，严格通过 4/6。C6 虽然流水线状态为 succeeded，但 H18 仅完成 17/18，因此不计为验收成功。C1 的 H04 出现服务输出到 65536 token 后 JSON 未闭合的反复修复问题，最终触发原配置 1800 秒合同硬时限，API 与任务记录均为 failed。

| 合同 | task_id | 严格验收 | 要点完成 | 输入 token | 输出 token |
| --- | --- | --- | --- | --- | --- |
| C1 | 20261025-233138-de09 | 失败，合同硬时限 | 17/18 | 952804 | 405580 |
| C2 | 20261025-233138-a981 | 通过 | 18/18 | 572925 | 51906 |
| C3 | 20261025-233138-2c01 | 通过 | 18/18 | 401496 | 42660 |
| C4 | 20261025-233137-e04f | 通过 | 18/18 | 187588 | 27421 |
| C5 | 20261025-233137-122f | 通过 | 18/18 | 535743 | 48708 |
| C6 | 20261025-233137-1586 | 失败，H18 校验 | 17/18 | 292261 | 35682 |

C1 最后一次请求被硬时限中止，实际输出未知，stats_complete=false、total_is_lower_bound=true；该行是已知消耗的下界，不是服务实际生成量的完整统计。其余五行计数完整。全批结束时三种模型的活动名额、等待者和等待截止项均为零。

取证中前三次长输出分别约 305/300/313 秒，最长内容间隔约 0.92/3.97/4.28 秒。这证明连续流式输出没有被 60 秒总时长提前截断；它不能证明模型一定生成合法业务结果。失败输入、输出及重试全部保留。

证据目录：[stream-six-round1](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-round1)。不得与后续批次拼接成六份成功。

### 中间流式复测：六份全部通过，尚未作为最终镜像验收

批次 mc20261026000921cd43e8，镜像 lexora:model-control-20261026-r3，846.547 秒，六份均 18/18；API、日志和报告一致，全部严格检查通过。LLM 407 次实际发送，滚动分钟峰值 30、并发峰值 5；embedding 102 次、并发峰值 3；reranker 21 次、并发峰值 1。三类最终活动名额、等待者与截止项均为零。

该批期间只读审计发现补检边界问题，随后已作通用修复。因此这次通过作为中间证据保留，不用它替代最终 r5 镜像的整批验收。

证据：[stream-six-round2/result.json](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-round2/result.json)。

### r4 六份复测：模型控制与连接失败恢复通过

当轮镜像 lexora:model-control-20261026-r4；代码提交 f81b3f90。批次 mc20261026002438417e3d，一次屏障同时提交六份不同合同，耗时 946.453 秒（约 15 分 46 秒）。六份均 18/18，API、任务记录、最终事件、汇总与报告检查全部通过，没有拼接其他批次。

| 合同 | task_id | 完成 | 输入 token | 已知输出 token | 额外发送 | 报告 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 test_contract | 20261026-002444-a633 | 18/18 | 398,959 | 39,818 | 2 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r4/data/api/20261026-002444-a633/output/【已AI审查】test_contract.docx>) |
| C2 2025 算力采购 | 20261026-002445-4192 | 18/18 | 813,289 | 82,691† | 19 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r4/data/api/20261026-002445-4192/output/【已AI审查】【20251224已审查】2025年度算力资源服务租赁项目（算力资源一）采购合同1223.docx>) |
| C3 2026 GPU 算力 | 20261026-002444-f136 | 18/18 | 474,039 | 53,230† | 5 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r4/data/api/20261026-002444-f136/output/【已AI审查】【已审查】（2026年058号）2026年度GPU算力服务租赁采购合同-20260617_批注版.docx>) |
| C4 办公场地租赁 | 20261026-002445-265d | 18/18 | 191,624 | 22,084 | 1 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r4/data/api/20261026-002445-265d/output/【已AI审查】【已审查】（456号）农商财富大厦办公场地租赁合同11.17.docx>) |
| C5 IPv6 设备采购 | 20261026-002444-4ff5 | 18/18 | 547,493 | 54,226 | 10 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r4/data/api/20261026-002444-4ff5/output/【已AI审查】【已审查】（524号）IPv6 上网行为管理设备采购合同.docx>) |
| C6 医疗合作协议 | 20261026-002444-6685 | 18/18 | 319,270 | 39,691 | 3 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r4/data/api/20261026-002444-6685/output/【已AI审查】【已审查】（528号）2026年至2028年贵宾医疗服务合作协议-邵逸夫医院.docx>) |

† C2/C3 分别有 2/4 次连接失败未取得服务输出 usage，已知输出与总 token 为下界；stats_complete=false，不将未知消耗写成零。两份最终审查和报告均成功。

| 类型 | 实际发送 | 中间失败 | 请求平均秒数 | 请求最长秒数 | 单条本地输入计数最大值 | 并发峰值 | 滚动分钟峰值 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| LLM | 401 | 40 | 5.000 | 34.547 | 26,072 | 5 | 30 |
| embedding | 107 | 0 | 0.543 | 1.345 | 1,024 | 3 | 38 |
| reranker | 25 | 0 | 0.341 | 0.484 | 1,024 | 1 | 7 |

请求耗时包括单次发送后的接收过程，不包含名额等待；包含成功与失败尝试。单条输入最大值按实际选定 tokenizer 的应用端计数，embedding/reranker 按每条输入或每组 query+候选计算，不把批次总和当单条上限。LLM/embedding/reranker 均未超过 88000/1024/1024。

LLM 共 40 次中间失败：34 次 JSON/结构/数据校验拒绝（6 次 JSONDecodeError、11 次 ValueError、17 次 ValidationError），6 次 APIConnectionError。均在现有预算内重试恢复，共 40 次额外发送。连接错误约 4 秒返回，剩余请求预算仍约 56 秒；具体网络原因不确定，不能称为 60 秒模型超时。embedding/reranker 没有失败尝试。

LLM 滚动峰值 30、并发峰值 5；embedding/reranker 并发峰值 3/1，均在配置内。三种最终 held、waiting、deadlines 全为 0；剩余 RPM 历史 5/3/6 不占并发名额。MCP JSONRPC 解析错误为零。C1/H04 和 C6/H18 均正常完成。

证据：[完整验收结果](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r4/result.json)、[统计明细](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r4/acceptance_metrics.json)。

### r4 非流式补测：状态读取错误中断，未发送模型请求

批次在 9.234 秒遇到状态 API 500，测试脚本提前退出并停止服务，两个任务未发出实际模型请求，不能作为模型调用失败或非流式通过证据。原始记录：[nonstream-final-r4/result.json](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/nonstream-final-r4/result.json)、[compose.log](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/nonstream-final-r4/compose.log)。

### 最终代码整批验收

最终镜像 lexora:model-control-20261026-r5，代码提交 fb38af85。非流式批次 mc20261026005010406e82，354.969 秒，两份用户指定合同均 18/18，API、日志、报告一致。

| 合同 | task_id | 输入 token | 已知输出 token | 实际请求 | 报告 |
| --- | --- | --- | --- | --- | --- |
| C1 | 20261026-005017-60ff | 457,568 | 46,325 | 91 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-delivery-20261026/nonstream-r5/C1.docx>) |
| C2 | 20261026-005017-10ba | 531,380 | 65,180† | 105 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-delivery-20261026/nonstream-r5/C2.docx>) |

† C2 一次非流式请求超时未返回输出 usage，因此其已知输出和 token 总量为下界；未将未知消耗写成零。共 9 次中间失败（8 次输出校验、1 次模型请求超时），均按现有配置恢复。

真实超时恢复案例：C2/SubAgent_H10，同一 logical_call_id=ce3ea3a851c64ec7b7248d452038093a：

| 实际请求 | 名额等待 | HTTP 耗时 | 输入/输出 token | 结果 |
| --- | --- | --- | --- | --- |
| 第一次 ece2c6960e8f4877918968c5e83e0f7a | 14.089 秒 | 60.002 秒 | 5970 / 未知 | ModelCallTimeout；失败输入保留 |
| 第二次 cfda7ff619db488aac3903d5f7976460 | 0.001 秒 | 33.043 秒 | 5970 / 5029 | 成功；本次预算还剩 26.957 秒 |

同一逻辑操作累计约 107.924 秒。等待预算累计消耗约 14.090 秒，但每次 HTTP 独立拥有 60 秒；不是两次共用一个 60 秒。最终所有活动名额、等待者与截止项为零。LLM 并发/RPM 峰值 5/30，embedding/reranker 并发峰值 3/1。状态 API 未重现 r4 首次查询的 500，MCP 解析错误为零。

证据：[nonstream-final-r5/result.json](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/nonstream-final-r5/result.json)。

### 最终混合六份：r5 流式验收通过

批次 mc202610260056324b8812，使用最终代码 fb38af85 与同一镜像 lexora:model-control-20261026-r5。六份不同合同通过同一提交屏障一次性提交，由三个 worker、每个 concurrency=1 执行；先执行三份，其余等待接手。整批耗时 1317.828 秒（约 21 分 58 秒）。六份均 18/18，API、任务记录、最终日志事件、汇总及报告全部一致，验收 failures=[]、passed=true。未拼接其他批次。

| 合同 | task_id | 完成 | 输入 token | 输出 token | 额外发送 | 报告 |
| --- | --- | --- | --- | --- | --- | --- |
| C1 test_contract | 20261026-005639-b43e | 18/18 | 429,291 | 56,383 | 5 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-delivery-20261026/stream-six-r5/C1.docx>) |
| C2 2025 算力采购 | 20261026-005640-635f | 18/18 | 996,082 | 147,954 | 15 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-delivery-20261026/stream-six-r5/C2.docx>) |
| C3 2026 GPU 算力 | 20261026-005640-5a36 | 18/18 | 529,379 | 51,295 | 9 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-delivery-20261026/stream-six-r5/C3.docx>) |
| C4 办公场地租赁 | 20261026-005639-4f67 | 18/18 | 169,563 | 18,513 | 0 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-delivery-20261026/stream-six-r5/C4.docx>) |
| C5 IPv6 设备采购 | 20261026-005639-ac1f | 18/18 | 669,097 | 78,722 | 21 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-delivery-20261026/stream-six-r5/C5.docx>) |
| C6 医疗合作协议 | 20261026-005639-bcce | 18/18 | 268,790 | 32,332 | 1 | [报告](<C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-delivery-20261026/stream-six-r5/C6.docx>) |

六份本批次统计均 stats_complete=true、unknown_input_attempts=0、unknown_output_attempts=0。输入包括三类模型；输出只统计 LLM 文本，不将 embedding 维度或 reranker 分数记成输出 token。回退估算的来源标记仍保留；统计完整不代表所有数字都来自服务精确 usage。报告已复制到短路径并逐份比对 SHA256，原始报告和日志保持不变。

| 类型 | 实际发送 | 中间失败 | 请求平均秒数 | 请求最长秒数 | 单条本地输入最大值 | 并发峰值 | 滚动分钟峰值 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| llm | 418 | 55 | 6.077 | 52.168 | 32,888 | 5 | 30 |
| embedding | 106 | 0 | 0.523 | 1.167 | 1,024 | 3 | 21 |
| reranker | 25 | 0 | 0.342 | 0.526 | 1,024 | 1 | 5 |

本批 LLM 55 次中间失败均发生在输出校验：37 次 ValidationError、15 次 ValueError、3 次 JSONDecodeError；没有 HTTP、断流或模型请求超时失败。共 51 次逻辑操作内额外发送。失败次数与额外发送次数不是同一指标：失败是一次实际响应被拒绝，额外发送是同一逻辑操作内的重发；用尽该操作预算后，外层还可重新尝试整个要点。

整要点重新尝试单独记录如下，均未超过 criterion_retry_max_attempts=3：

| 合同 | 要点 | 最终总尝试次数 | 最终质量状态 |
| --- | --- | --- | --- |
| C2 | H06 | 2 | passed |
| C2 | H12 | 2 | passed |
| C2 | H15 | 3 | passed |
| C3 | H11 | 2 | passed |
| C5 | H18 | 2 | passed |
| C6 | H11 | 2 | not_required |

C2/H15 的前两次失败分别为引用改变了原文数字宽度，以及复核模型改写金额后审查模型沿用错误引用。对照原 DOCX、document_index、vector_index 及实际请求，原文和锚点一致，校验拒绝正确。第三次在原配置内通过；没有放宽逐字引用规则，没有改 checkpoint，也没有增加 H15 特殊判断。原失败及消耗全部保留。

LLM 最长连续无内容间隔约 4.009 秒，低于 60 秒。单条应用端输入最大值 32888/1024/1024，均满足 88000/1024/1024；LLM 滚动峰值 30、并发峰值 5，embedding/reranker 并发峰值 3/1，均在限额内。三类最终 held、waiting、deadlines 均为 0；RPM 历史保留 7/3/5，属于窗口计数，不占并发名额。

本批请求时长未触及配置的 600 秒 lease，实际续期次数为 0；自动续期由短租约 Redis 专项验证，不能将本批误写成实际触发过续期。流式持续超过 60 秒而不被总时长中断的真实证据见首轮 300 秒以上请求；非流式真实超时与独立预算重试见上一节。MCP JSONRPC 解析错误为零，状态 API 未重现读取竞争。

证据：[整批结果](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r5/result.json)、[逐次与逐合同统计](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-20261025/stream-six-final-r5/acceptance_metrics.json)、[报告副本校验清单](C:/Users/lenovo/Desktop/contract_agent/data/verification/model-control-delivery-20261026/stream-six-r5/manifest.json)。

本方案第 15.4 节的结束条件已满足。本次结果证明所测环境与这批输入通过；模型响应仍有不确定性，不承诺任何部署、任何合同永远不会失败。

## 6. 提交与交付边界

- 修改前基线：de8e517e。
- 模型控制主体实现：6574e575。
- 比较证据、补检边界及截断回复修复：f81b3f90。
- 状态读取竞争修复及最终运行代码：fb38af85。
- 最终验收文档与原始数据分开保存；本记录随最终文档提交交付。

验收使用的独立 Compose 项目与测试 Redis 容器已停止清理；原始输入、调用日志、批次结果和报告保留在宿主机 data/verification。无关运行容器未操作。

实际镜像与源码/配置校验值由各批次 image.json、configuration.json、fixtures.json 保存。既有 deploy.tar.gz/images.tar 不因镜像构建自动更新；本记录不宣称旧归档已包含本次实现。生产部署需要使用新镜像、更新后的挂载配置，并重建 API 与三个 worker。

2026-10-26 按用户要求清理了 data 根目录 36 个不归属单份合同任务的测试控制台日志（862636 字节），直接移除，不另行归档。各合同任务的 logs/logical_calls、详细 calls、输入、报告及整批 result.json/acceptance_metrics.json 保留。另移除 docs/code-learning 的四个旧学习页面文件及 scripts/build_code_learning_page.py；业务日志生成代码、模型配置和服务流程未修改。
