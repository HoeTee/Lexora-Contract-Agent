# 字段及代码路径核查

下表记录消融时的字段调查。后续用户仅批准清理无效配置及提示词/载荷不一致：已删除 `VectorRetrievalConfig.auto_build` 及配置示例；旧 YAML 应删除该键，否则严格配置校验报错。结构查询提示词改为只解释实际发送的精简字段，缓存版本升为 v4；完整树的 `key_items` 和范围字段继续保留。并发、线程、进程控制及其余消融候选未在本次应用。

下面区分实验时的执行作用、提示词输入、构建期使用和仅输出的诊断字段；行内“候选”是原调查结论，不代表仍待执行上述已批准清理。

| 字段 / 结构 | 定义与写入位置 | 当前消费者 / 目的 | 本次结论 |
| --- | --- | --- | --- |
| `VectorRetrievalConfig.auto_build` | `retrieval/config.py:20` | 生产代码未读取；建向量由 `retriever._build_index_sync` 按 `vector.enabled` 判断 | 配置空开关；优先清理候选，需要处理旧配置兼容 |
| `ConcurrencyConfig.reranker` | `retrieval/config.py:41` | 传入 `rerank_matches` 后，仅用于每次调用新建的局部 semaphore | E08 指定局部并发 1，实际共享客户端仍可并发 2；该字段没有跨调用限流效果。不要与顶层 `model_semaphore.reranker` 混淆 |
| `DocumentNode.node_type` | `schema/nodes.py:13` | `content.py:71` 等判断表格、表格块；同时进入结构/重排提示词 | 有执行作用，不能从 class 全局删除；F01 只测重排提示词 |
| `DocumentNode.token_estimate` | `schema/nodes.py:24` | 节点元信息及多个模型输入；实际内容预算还会重新估算文本 | F02 只测重排提示词，不等于所有预算字段冗余 |
| 候选 `sources` | `retriever.py:481`、合并向量时更新 | `content.py:152` 决定父子节点过滤；进入重排提示词和 route 计数 | 不是纯日志字段，不能全局删除 |
| `vector_score` | `retriever.py:496` / `502` | `rerank.py:79` 写入模型候选视图 | 无新增节点时仍可改变提示词；E02 不能只比较 node_id |
| `DocumentNode.key_items` | `schema/nodes.py:32`；`build_pipeline.py:396` 填子标题 | 存入结构树；但 `_compact_structure` 没有发送它 | 124 次真实结构模型调用，124 次提示词要求参考它，0 次 JSON 载荷含该字段；提示词与载荷不一致 |
| `start_index/end_index` | `schema/nodes.py:30` / `31` | 最终锚点定位、区域匹配、分块等 | 必须保留；结构提示词提及范围字段，但精简树本身没传它们，需要同步说明 |
| `page_source/page_confidence_score` | `schema/nodes.py:22` / `23`；`node_factory.py:54` / `55` | 构建期记录页码来源和可靠度；当前检索不读取 | 当前不是检索信号；删除前明确是否放弃该诊断信息，不能称已完成全链路消融 |
| `confidence_evidence` | `schema/nodes.py:26` | `splitter.py:49` 继承分块证据；附件层级处理补充成功/回退信息 | 构建期有读写，不是“只定义无使用” |
| `mapping_ref/row_start/row_end` | `schema/nodes.py:34`–`36` | `indexing/tables.py` 写入，`content.py` 继续传递 | 当前最终原文渲染不消费它们；属于表格映射元数据，不据此全局删表格能力 |
| 候选 `structure_reason` | `retriever.py:482` | 当前生产消费者检索未发现读取；最终工具文本不输出 | 中间结果装饰字段候选；不等于结构查询模型的 `reason` 可以直接删除 |
| 候选 `rerank_score/rerank_reason` | `rerank.py:50`–`60` | 当前生产代码未继续读取；排序使用模型返回数组顺序 | E01 可省略这些输出且原文相同；不据此承诺删除模型 score/reason 不影响生成行为 |
| `content_context[].text` | `content.py:128` | 分页预算使用该文本；最终 `retriever.py:509` 重新读取锚点 `raw_text` | 两种文本目的不同；563 个单节点边界探针出现文本差异，不直接等于 563 次真实预算错误 |
| `pagination` | `content.py:55` | `retriever.py:276`–`282` 读取 scan 的全部分页 | 真实执行控制，不能删 |
| `content_view()` | `content.py:15`，由 `retrieval/__init__.py` 导出 | 当前项目内未见调用 | 旧导出接口候选；内部无调用不证明外部无消费者 |
| `RetrievalMatch/RetrievalResult` | `schema/retrieval.py` | `retrieval/keyword.py` 构造返回值 | 仍有代码消费者；不能单凭主调用不用就说 class 没意义 |

## 历史记录边界

本次核查了 `tools/docxindex` 最近变更，相关记录包括：

- `eeadc1980`：向量改为结构检索的补充。
- `fbadde6a9`：混合 scan 路由及模型错误信息保留。
- `77b71b0f2`：批注路由、去重和正文边界。

历史提交只能说明设计演变，不能代替本次运行证据。以上“未读取”指当前仓库生产 Python 代码范围，
不承诺未知外部调用者或将来的接口兼容性。

## 可执行结论

先处理没有生效的配置和提示词/载荷不一致；再评估单候选重排及无复用缓存。
不要把 `node_type`、`sources`、范围锚点、分页等实际执行字段作为同一批“冗余字段”删除。
