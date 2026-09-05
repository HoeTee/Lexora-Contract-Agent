# DOCX 结构化索引与检索算法设计

本文定义合同审查场景下的 DOCX 结构化索引和检索机制。

核心结论：

```text
持久化 DOCX 结构树索引是主索引。
规则审查只是少数审查要点的执行方式，不单独设计“规则索引”。
向量检索不产生新 node；只有结构结果少于 10 个时，它才在 region、hybrid 或显式 fallback 中补齐缺口。
最终审查必须展开结构树中的原文 node。
```

当前审查要点来源：

```text
resources/criteria/criteria-formal.docx
```

## 1. 审查要点分类

`criteria-formal.docx` 中的审查要点按执行方式分为四类。

| 类别 | 审查要点 | 执行方式 |
|---|---|---|
| 关键词规则审查 | 4、5、8、9、16 | 使用关键词或正则扫描全文，返回命中的 node 和 anchor |
| 标题规则审查 | 2 | 检查结构树 title 是否存在、是否符合要求 |
| 全文遍历审查 | 12、15 | 遍历结构树 node，逐个展开 node 输入模型检查 |
| 普通 LLM 审查 | 其他审查要点 | 先用结构树检索候选 node，再展开原文交给模型审查 |

其中 12、15 可以归入规则审查的大类，但执行方式不是关键词命中，而是“遍历每个 node 后逐个判断”。

标题顺序混乱也归入规则审查：直接检查结构树中标题编号是否连续、层级是否跳变、同级标题是否乱序。

## 2. 总体架构

整体流程：

```text
DOCX
  -> 解析 word/document.xml、word/styles.xml
  -> 生成 body child anchor
  -> 识别合同首部、正文、合同末尾、附件
  -> 构建结构树 node
  -> 计算 node token 并切分超长 node
  -> 生成 summary tree
  -> 持久化 DocumentIndex
  -> 审查要点驱动检索和原文展开
  -> SubAgent 输出 issue
  -> 批注工具通过 anchor 回写 DOCX
```

主索引只有一个：

```text
DocumentIndex
```

规则检索和向量检索都只返回 `node_id` / `anchor`，然后回到 DocumentIndex 展开原文。

## 3. DocumentIndex 持久化索引

在线 API 为每个任务生成临时内存索引，不把索引写入 `/data`。离线 CLI 需要持久化时写入：

```text
scripts/docxindex/outputs/index/{合同目录名}/document_index.json
```

索引主结构：

```json
{
  "schema_version": "docx-index-v4",
  "source_file": "contract.docx",
  "top_regions": {
    "frontmatter": "frontmatter",
    "body": "body",
    "tail": "tail",
    "attachments": "attachments"
  },
  "settings": {},
  "nodes": [],
  "structure_tree": [],
  "anchor_map": {},
  "table_map": {}
}
```

只存一份完整 node 数据。所谓 `structure_view`、`content_view` 不是额外存储两份索引，而是从同一份完整 node 中按场景裁剪字段。

完整 node 示例：

```json
{
  "node_id": "body/sec_002/l2_003",
  "parent_id": "body/sec_002",
  "node_type": "section",
  "level": 2,
  "title": "三、本合同款项的分批支付方式及时限如下",
  "text": "三、本合同款项的分批支付方式及时限如下：...",
  "summary": "约定分批付款节点、比例、发票类型和付款期限。",
  "start_anchor": "p_0042",
  "end_anchor": "p_0051",
  "page_start": null,
  "page_end": null,
  "page_source": "unavailable",
  "page_confidence_score": 0,
  "token_estimate": 720,
  "confidence_score": 9,
  "confidence_evidence": ["number_pattern", "sequence", "style_outline"],
  "nodes": []
}
```

`nodes` 在平铺存储中是子节点 ID 数组；v4 不再同时保存内容相同的 `children`，也不再保存可由 `structure_tree` 或 `parent_id` 推导的 `root_nodes`。

## 4. 数据来源

| 数据 | 来源 | 用途 |
|---|---|---|
| 阅读顺序 | `word/document.xml` 的 `w:body` 直接子节点 | 保证结构树顺序 |
| 段落 | `w:body > w:p` | 生成 paragraph anchor 和文本 |
| 表格 | `w:body > w:tbl` | 生成 table anchor 和 TableNode |
| 段落文本 | `w:t`、`w:delText` | 标题识别、node text、关键词检索 |
| direct outline | `w:pPr/w:outlineLvl` | 标题强证据 |
| style outline | `w:pPr/w:pStyle -> word/styles.xml` | 标题强证据 |
| 样式继承 | `styles.xml` 中 `w:basedOn` 链 | 计算 effective outline |
| 对齐 | `w:pPr/w:jc` | 视觉标题判断 |
| 字号 | `w:rPr/w:sz` | 视觉标题判断 |
| 加粗 | `w:rPr/w:b` | 视觉标题判断 |
| 页码 | `w:lastRenderedPageBreak` | 可选展示字段 |
| 批注关系 | `word/_rels/document.xml.rels` 指向 `word/comments.xml` | 读取和写入批注正文 |

`w:lastRenderedPageBreak` 并非所有 DOCX 都存在。它只能作为可选页码来源，不影响检索、审查和批注。

## 5. Body Child Anchor

`word/document.xml` 中 `w:body` 的直接子节点是结构索引的基础。

遍历规则：

```text
遇到 w:p   -> 生成 p_0001、p_0002、...
遇到 w:tbl -> 生成 tbl_0001、tbl_0002、...
保持它们在 w:body 中的原始顺序。
```

`anchor_map` 示例：

```json
{
  "p_0042": {
    "body_child_index": 42,
    "type": "p",
    "text": "三、本合同款项的分批支付方式及时限如下：",
    "node_id": "body/sec_002/l2_003"
  },
  "tbl_0003": {
    "body_child_index": 58,
    "type": "tbl",
    "text": "表格提取后的文本",
    "node_id": "body/sec_008"
  }
}
```

`p_...` / `tbl_...` 不是业务 ID，而是检索结果和批注工具之间的桥梁。

## 6. 顶层区域

结构树必须保留四个顶层区域：

```text
frontmatter  合同首部
body         正文
tail         合同末尾
attachments  附件
```

### 6.1 合同首部 FrontMatter

合同首部包括正式正文开始前的内容，例如合同标题页、合同编号、甲乙方信息、签署提示等。

识别规则：

```text
从 document.xml 开始扫描；
遇到第一个正式正文标题后，之前内容归入 frontmatter；
正式正文标题通常是 第X条 / 第X章。
```

### 6.2 正文 Body

正文从第一个正式正文标题开始，到合同末尾或附件父章节前结束。

正文标题识别不包含：

```text
附件内部的 第X条 / 第X章
表格单元格内的标题文本
附件清单里的 附件N
```

表格内容仍然属于正文，只是不参与正文章节标题识别。

### 6.3 合同末尾 Tail

合同末尾用于保存正文结束后的签署页、无正文提示、盖章栏和附件前的尾部内容。

TailNode 不只依赖“以下无正文”“以下为合同签署栏”。

建议使用多信号识别：

```text
最后一个正式正文 Level1 section 之后的连续内容进入 tail 候选；
出现签署、盖章、法定代表人、授权代表、日期等信号时提高得分；
如果后续出现正式附件 section，则附件开始前的签署/空白/说明内容归 tail；
没有明确 tail marker 时，正文标题连续性结束后的内容可形成低置信度 tail。
```

### 6.4 附件 Attachments

附件区从正文中的附件父章节或正文末尾的正式附件 section 开始。

附件父章节示例：

```text
第二十条 附件
第十四条 附件
```

正式附件 section 示例：

```text
附件1
附件 1
附件一
附件1：
附件1、
```

正式附件 section 必须位于附件父章节之后，或位于正文末尾附件区。正文中的“详见附件3”不应直接升格为附件 section。

## 7. 正文章节 Node 构建

正文默认提取到三级：

```text
Level 1: 第X条 / 第X章
Level 2: 一、二、三、
Level 3: （一）（二）（三）
```

标题判断信号：

```text
1. 编号形态
2. 编号连续性
3. effective_outlineLvl
4. 样式、字号、加粗、居中等辅助信号
```

`effective_outlineLvl` 计算顺序：

```text
1. 段落 direct outlineLvl
2. 当前 pStyle 自身 outlineLvl
3. pStyle basedOn 链上的 outlineLvl
```

`outlineLvl` 是强证据，但不是唯一条件。对于没有 outline 的合同，仍然要依赖编号形态和连续性识别章节。

索引还输出 `region_boundaries`，用于把结构识别结果传递给 DOCX 报告写入阶段：

```json
{
  "body_start": {
    "xml_anchor_type": "paragraph",
    "xml_anchor_id": "path:body/p12",
    "quoted_text": "第一条 合同标的"
  },
  "body_content_end": {
    "xml_anchor_type": "paragraph",
    "xml_anchor_id": "path:body/p96",
    "quoted_text": "正文最后一个有效段落"
  }
}
```

`body_content_end` 在附件区域之前，并排除已识别的合同末尾区域。报告写入器处理“正文结尾”类缺失问题时只使用该锚点；边界缺失时记录未匹配事件，不得把附件的最后一段当成正文结尾。

标题置信度使用数字化字段：

```json
{
  "confidence_score": 9,
  "confidence_evidence": [
    "number_pattern",
    "sequence",
    "style_outline"
  ]
}
```

示例计分：

```text
编号形态命中 +2
同级编号连续 +3
direct outlineLvl +4
style outlineLvl +3
视觉标题得分 +0 到 +4
附件上下文命中 +2
排除规则命中 -5
```

## 8. 附件内部 Node 构建

附件内部不能依赖统一 `outlineLvl`。先识别正式附件 section，再在附件 section 内部做局部切分。

附件内部标题候选包括：

```text
视觉标题：科技部门驻场外包考核细则
编号标题：第一条、第一章、一、（一）、1、
局部标签：目的：、范围：、包装：、考核：、罚则：
表格：独立 TableNode
```

视觉标题参数：

```json
{
  "visual_title_max_chars": 55,
  "visual_title_min_score": 2,
  "large_font_size_threshold": 28,
  "bold_fraction_threshold": 0.5,
  "attachment_start_window": 8
}
```

视觉标题打分：

```text
+1 居中
+1 加粗比例 >= 0.5
+1 最大字号 >= 28
+1 后 5 个 body child 内出现正文、编号标题或表格
```

PlainLabel 参数：

```json
{
  "plain_label_keywords": [
    "目的",
    "对象",
    "分工",
    "内容",
    "方式",
    "结果",
    "要求",
    "标准",
    "范围",
    "期限",
    "责任",
    "义务",
    "说明",
    "证明",
    "来源",
    "包装",
    "请假",
    "考核",
    "罚则",
    "承诺",
    "服务",
    "电话"
  ],
  "standalone_plain_label_max_chars": 16
}
```

PlainLabel 只作为局部内容边界，不提升为正式正文章节。

## 9. Node Token 阈值与 ChunkNode

不照搬 PageIndex 的 20000 token。合同审查需要精确计算、定位和批注，node 应更小。

建议参数：

```yaml
node_target_tokens: 700
node_soft_limit_tokens: 1000
node_hard_limit_tokens: 1800
chunk_overlap_tokens: 80
```

含义：

| 参数 | 含义 |
|---|---|
| `node_target_tokens` | 理想子 node 大小 |
| `node_soft_limit_tokens` | 超过后优先按子标题或编号拆 |
| `node_hard_limit_tokens` | 超过后必须拆，即使只能按段落聚合 |
| `chunk_overlap_tokens` | 相邻 chunk 的上下文重叠 |

ChunkNode 生成逻辑：

```text
1. 一个结构 node 超过 soft limit 时，先找更细编号或视觉子标题。
2. 如果找到可靠子标题，按子标题生成子 node。
3. 如果没有可靠子标题，按 w:body child 顺序聚合段落和表格。
4. 聚合目标是 node_target_tokens。
5. 单个 chunk 不应超过 node_hard_limit_tokens。
6. 表格不拆散；单个表格过大时独立为 TableNode，并标记 oversized=true。
```

ChunkNode 示例：

```json
{
  "node_id": "body/sec_008/l3_002/chunk_001",
  "node_type": "chunk",
  "title": "（二）服务内容 / chunk 1",
  "start_anchor": "p_0212",
  "end_anchor": "p_0238",
  "token_estimate": 700,
  "chunk_index": 1,
  "chunk_count": 3,
  "overlap_tokens": 80
}
```

## 10. Summary Tree

summary tree 是给 LLM 的低 token 导航，不是最终事实判断依据。

summary 生成规则：

```text
叶子 node：基于自身原文生成 summary。
非叶子 node：基于 children 的 title 和 summary 汇总生成 summary。
短 node 可不生成 summary，直接使用 title 和 token_estimate。
```

建议参数：

```yaml
summary_trigger_min_tokens: 300
summary_max_tokens_per_node: 120
summary_tree_inline_budget_tokens: 6000
```

输出给 LLM 的结构视图只包含轻量字段：

```json
{
  "node_id": "body/sec_002",
  "title": "第二条 合同金额及支付方式",
  "summary": "约定合同金额、付款方式、发票和账户。",
  "token_estimate": 1200,
  "children": []
}
```

## 11. 结构树加载方式

借鉴 PageIndex 的思想：先返回轻量结构树，再按命中的位置展开原文；结构树和原文都必须受预算控制。

PageIndex 使用字符预算：

```text
TOOL_RESPONSE_CHAR_LIMIT = 100000
CHAR_BUDGET = 95000
```

DOCX 检索不照搬字符数，统一使用 token 预算：

```yaml
retrieval:
  docxindex:
    input_tokens: 6000
```

含义：

```text
每一次输入模型的内容最多 6000 tokens。
结构树输入、向量候选输入、rerank 候选输入、原文上下文输入都使用该预算。
```

结构树加载：

```text
structure_tree <= input_tokens
  -> 一次输入完整轻量结构树

structure_tree > input_tokens
  -> 按顶层区域或 Level1 section 分批输入

单个结构 node > input_tokens 且存在 children
  -> 保留父 node 外壳，递归拆 children

单个结构叶子 node > input_tokens
  -> 仍返回该 node 的轻量字段，不在结构阶段展开 text
```

注意：这里分页的是结构树轻量视图，不是合同原文。

## 12. 检索工具接口

结构树工具应提供少量稳定接口。

### 12.1 get_document_structure

返回结构树 summary，不返回全文。

输入：

```json
{
  "part": 1,
  "filter": null
}
```

输出：

```json
{
  "document_structure_mode": "compact_structure",
  "structure_tokens": 4200,
  "part": 1,
  "total_parts": 1,
  "structure_index": []
}
```

### 12.2 get_node_content

按 `node_id` 展开原文。

输入：

```json
{
  "node_id": "body/sec_002"
}
```

输出：

```json
{
  "node_id": "body/sec_002",
  "title": "第二条 合同金额及支付方式",
  "text": "...完整原文...",
  "start_anchor": "p_0038",
  "end_anchor": "p_0058",
  "token_estimate": 1200
}
```

### 12.3 search_by_keyword

关键词规则检索。只做命中定位，不做字段抽取缓存。

输入：

```json
{
  "keywords": ["支付方式", "付款", "合同金额"]
}
```

输出：

```json
{
  "matches": [
    {
      "keyword": "支付方式",
      "match_text": "第二条 合同金额及支付方式",
      "match_type": "title",
      "node_id": "body/sec_002",
      "anchor": "p_0038"
    }
  ]
}
```

### 12.4 validate_title_sequence

检查结构树标题顺序。

输出：

```json
{
  "issues": [
    {
      "node_id": "body/sec_006",
      "title": "第八条 违约责任",
      "message": "同级标题从第六条跳到第八条，缺少第七条。"
    }
  ]
}
```

### 12.5 vector_search_nodes

语义补召回。只返回已有结构树 node，不创建新 node。

输入：

```json
{
  "query": "维护响应机制"
}
```

输出：

```json
{
  "matches": [
    {
      "node_id": "body/sec_008/l3_002",
      "score": 0.82,
      "title": "售后服务要求"
    }
  ]
}
```

## 13. 规则审查执行方式

规则审查不设计独立“规则索引”。规则审查直接使用 DocumentIndex 的 text、title、anchor 和 node 顺序。

关键词规则审查要点：

```text
4、5、8、9、16
```

执行流程：

```text
1. 根据审查要点配置关键词或正则。
2. 在 DocumentIndex 的 title/text 中扫描。
3. 返回命中的 node_id、anchor、match_text。
4. 根据审查要点直接生成问题，或展开命中 node 后交给 LLM 复核。
```

标题规则审查要点：

```text
2
```

执行流程：

```text
1. 读取结构树 title。
2. 检查标题是否存在、是否符合审查要求。
3. 如果发现缺失或异常，返回对应 node_id 或父级 node_id。
```

标题顺序规则：

```text
遍历同级 title；
解析 第X条、一、（一） 等序号；
检查是否连续、是否重复、是否跳号、是否层级反转。
```

## 14. 全文遍历型审查

全文遍历型审查要点：

```text
12、15
```

执行方式：

```text
1. 遍历 DocumentIndex 中需要审查的 leaf node 或 chunk node。
2. 每次输入一个 node 的原文给模型。
3. 模型判断该 node 是否存在对应问题。
4. 输出 issue 时必须带 node_id、quoted_text、comment_text。
5. 汇总所有 node 的结果。
```

这种方式可以算作规则审查的一部分，因为它的遍历范围是确定的；但单个 node 内部的问题判断仍然由模型完成。

## 15. 普通 LLM 审查与两阶段检索

除规则审查、标题审查、全文遍历审查以外的审查要点，使用两阶段检索。

默认流程：

```text
1. 输入审查要点 query。
2. 将 structure_tree 的轻量视图输入 LLM，返回 structure_matches。
3. 向量检索返回 vector_matches。
4. 合并 structure_matches 和 vector_matches，得到 candidate_nodes。
5. 对 candidate_nodes 做 rerank。
6. 根据 rerank 后的 node_id 从 DocumentIndex 展开原文 content_context。
7. 审查 LLM 只读取 query + content_context 中的原文和 anchor。
8. 输出 issue。
```

两次输入模型的内容不同：

```text
第一次输入：
  query + structure_view
  目标是定位 node_id。

第二次输入：
  query + content_context.text + anchor
  目标是完成审查判断。
```

`pagination`、`budget`、`score`、`reason` 是工具控制字段，只进入日志和调试输出，不进入最终审查正文 prompt。

原文展开规则：

```text
多个 node 合计超过 input_tokens：
  按 node 分批返回，不切短 node。

父 node 和子 node 同时命中：
  优先展开更具体的子 node。

单个叶子 node 本身超过 input_tokens：
  按 anchor 顺序切 chunk，并显式标记 truncated=true 和 pagination.has_more=true。
```

## 16. 向量补召回与 rerank

向量检索不用于构建结构树，也不是与结构树并列执行的独立检索。当前有三个受控调用位置：

- `region`：先用 `allowed_node_ids` 限定区域并执行结构语义；结构结果少于 10 个时，向量仅在同一区域补齐缺口；
- `hybrid`：先对全局结构树执行结构语义；结构结果少于 10 个时，向量补齐缺口；
- `fallback`：仅当 `RoutePlan.fallback=true` 且路线只有可安全扩大的 `title/rule` 时执行同一套全局语义检索。

`region`、`scan`、`hybrid` 不进入全局 fallback。跨区域比较使用多个独立 `region` 步骤，结果按步骤交错合并，避免后一个区域在 `semantic.max_selected_nodes` 截断前完全丢失。

`title(all_titles=true)` 是完整目录查询：所有顶层正文标题、tail 和附件标题都必须以紧凑标题证据返回，
不受普通 `semantic.max_selected_nodes` 候选上限截断。`rule` 的精确关键词全部未命中时，执行器必须同时返回
“确定性全文精确检索未出现这些词”的结果，供审查模型判断缺失型问题。

向量索引对象来自 DocumentIndex 中已有 node 或 chunk：

```text
embedding(node.text)
metadata.node_id = node.node_id
metadata.start_anchor = node.start_anchor
metadata.end_anchor = node.end_anchor
```

向量 metadata 必须与结构树统一：

```json
{
  "node_id": "body/sec_002/l2_003",
  "start_anchor": "p_0042",
  "end_anchor": "p_0051",
  "node_type": "section",
  "token_estimate": 720
}
```

默认配置：

```yaml
retrieval:
  docxindex:
    input_tokens: 20000
    vector:
      enabled: true
      score_threshold: 0.45
      probe_limit: 20
    semantic:
      structure_candidate_limit: 12
      max_selected_nodes: 10
    rerank:
      enabled: true
```

语义召回同时使用固定数量边界和 token 输入预算。固定数量决定检索行为：

```text
1. 结构树 LLM 最多选择 semantic.structure_candidate_limit=12 个候选。
2. 按 node_id 去重；如果结构候选已经达到 semantic.max_selected_nodes=10，不调用向量检索。
3. 如果结构候选少于 10，才对 query 做 embedding，并读取最多 vector.probe_limit=20 个向量候选。
4. 过滤 score < vector.score_threshold=0.45 的候选，再过滤与结构候选重复的 node_id。
5. 向量候选只填补到总数 10，不能挤掉已经由结构树选中的候选。
6. 向量补充、去重合并完成后，候选超过 1 个且重排启用时才统一 rerank；仅剩 1 个时跳过重排，但不跳过此前的向量补充。最终仍截断为最多 10 个 node。
```

合并不是合并原文，也不是合并索引。合并只做：

```text
structure_matches + vector_supplements
-> 按 node_id 去重
-> 保留 sources、vector_score、structure_reason
-> 只填补到 semantic.max_selected_nodes
-> 得到 candidate_nodes
```

rerank 对合并后的 `candidate_nodes` 统一重排序，不只重排向量结果。

rerank 输入：

```json
{
  "query": "审查要点",
  "candidates": [
    {
      "node_id": "body/sec_002/l2_003",
      "sources": ["structure", "vector"],
      "title": "三、本合同款项的分批支付方式及时限如下",
      "summary": "约定分批付款节点、比例、发票类型和付款期限。"
    }
  ]
}
```

rerank 输出：

```json
{
  "nodes": [
    {
      "node_id": "body/sec_002/l2_003",
      "score": 0.95,
      "reason": "直接包含付款计划、付款比例和发票类型"
    }
  ]
}
```

程序必须校验：

```text
node_id 必须来自 candidate_nodes；
score 必须在 0 到 1 之间；
不得接受候选集之外的 node_id。
```

向量召回和 rerank 都只用于找到已有 `node_id`。最终仍然回到 DocumentIndex 展开原文，再进行审查。

### LlamaIndex reranker provider 配置

以下配置只用于 LlamaIndex 后端的独立 HTTP reranker；docxindex 的候选重排复用其结构化 LLM client。`rerank.provider_model_names` 是模型名到请求体格式的唯一映射来源，模型名不能同时属于多个 provider：

```yaml
rerank:
  base_url: "<完整请求 URL>"
  name: "qwen3-rerank"
  instruct: "<可选指令>"
  provider_model_names:
    higress_qwen: ["Qwen3-Reranker-8B"]
    bge: ["bge-reranker-v2-m3"]
    dashscope_qwen: ["qwen3-rerank", "gte-rerank-v2"]
```

- `higress_qwen`：发送 `model/query/documents/top_n` 和可选 `instruct`。
- `bge`：发送 `query/documents/top_n`，不发送 `model`、`instruct`。
- `dashscope_qwen`：使用 DashScope `input`、`parameters` 结构。

provider 推断与校验在 `config.py`，请求体和响应解析在 `tools/retrieval/llamaindex/qwen_reranker.py`。HTTP 408、409、429、5xx 和网络错误按 workflow 模型重试配置处理；HTTP 400 不重试。

## 17. 批注定位链路

审查结果必须能回到 DOCX XML。

链路：

```text
task_id
  -> DocumentIndex
  -> node_id
  -> start_anchor/end_anchor
  -> document.xml 中的 w:p / w:tbl
  -> quoted_text 精确匹配字符范围
  -> 插入 commentRangeStart/commentRangeEnd/commentReference
  -> comments.xml 写入批注正文
```

SubAgent 输出 issue 时至少包含：

```json
{
  "node_id": "body/sec_002/l2_003",
  "quoted_text": "支付合同总金额的40%",
  "comment_text": "建议核验付款比例加总是否等于合同总金额。",
  "severity": "medium"
}
```

如果能计算字符位置，则补充：

```json
{
  "char_start": 36,
  "char_end": 48
}
```
