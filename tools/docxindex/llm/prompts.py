from __future__ import annotations

import json

EXPAND_PROMPT_VERSION = "docx_expand_v2"
SUMMARY_PROMPT_VERSION = "docx_summary_v3"
SUMMARY_BATCH_PROMPT_VERSION = "docx_summary_batch_v1"
ATTACHMENT_HIERARCHY_PROMPT_VERSION = "local_hierarchy_v4"

ROUTE_EXPECTED_JSON = """{
  "steps": [
    {
      "method": "rule",
      "terms": ["合同金额"],
      "regions": [],
      "all_titles": false,
      "anchor_terms": [],
      "coverage": "relevant"
    }
  ],
  "fallback": false
}"""

SCAN_EXPECTED_JSON = """{
  "findings": [
    {
      "anchor_id": "p_0001",
      "reason": "存在明确的错别字或语病"
    }
  ]
}"""


def route_prompt(
    query: str,
    criterion: str,
    titles: list[dict[str, object]],
    methods: dict[str, str],
) -> str:
    methods_text = "\n".join(f"- {name}: {description}" for name, description in methods.items())
    title_view = [
        {
            "title": item["title"],
            "node_type": item["node_type"],
            "depth": item["depth"],
            "region": item["region"],
        }
        for item in titles
    ]
    return f"""你是 DOCX 合同检索路由器，只规划检索方法，不审查合同，不选择 node_id。

用户查询：
{query}

审查要点原文：
{criterion}

当前合同标题目录：
{json.dumps(title_view, ensure_ascii=False)}

可用方法：
{methods_text}

规划规则：
- 可以组合多个步骤，但 scan 最多出现一次。
- 一级标题是否存在、一级标题顺序和目录检查使用 title；需要完整一级标题目录时 all_titles=true。
- 明确固定区域或明确标题下内容使用 region。固定区域只填写一个 regions 值；标题下内容只填写 anchor_terms，两种定位方式不能同时填写。
- region 的 coverage=complete 表示完整读取定位范围；coverage=relevant 表示在范围内执行结构树语义检索，结果不足时才由向量补充。
- 精确关键词、金额、存在或不存在判断使用 rule，并在 terms 中列出需要原文匹配的短词。
- 需要比较两个以上区域、首尾、正文与附件时，为每个区域分别生成一个 region 步骤；每个 region 步骤只填写一个 regions 值。
- 错别字、语病或必须逐段通读全文的问题使用 scan。同一审查标准还包含标题、顺序、精确关键词或金额检查时，scan 可以与 title/rule 组合；不得与 region/hybrid 组合。
- 标题、区域和精确关键词均无法可靠限定范围时使用 hybrid；hybrid 先执行全局结构树语义检索，结果不足时才由向量补充，不要再设置 fallback=true。
- 只有 title/rule 结果还需要全局语义补充时 fallback=true；region 与 scan 不使用全局 fallback，避免越过范围或重复扫描。
- 不得把审查结论写进检索计划。

输出格式要求（JSON）：
{ROUTE_EXPECTED_JSON}

字段约束：
1. 顶层只能包含 steps 和 fallback。
2. steps 必须是数组，包含 1 到 5 个路由步骤。
3. 每个步骤必须包含 method；method 只能是 title、region、rule、scan、hybrid 之一。
4. terms 和 anchor_terms 必须是字符串数组；regions 只能包含 frontmatter、body、tail、attachments。
5. all_titles 和 fallback 必须是布尔值；coverage 只能是 relevant 或 complete。
6. region 必须且只能使用一种定位方式：填写一个 regions 值，或者填写至少一个 anchor_terms；非 region 方法两者都必须为空数组。
7. rule 必须填写至少一个 term；title 必须填写 terms 或设置 all_titles=true。
8. scan 最多出现一次，只能与 title/rule 组合；包含 region、scan、hybrid 时 fallback 必须为 false。
9. 不使用的 terms、regions、anchor_terms 返回空数组；非 region 方法使用 coverage=relevant；不需要完整目录时 all_titles=false。
10. 只返回合法 JSON object，不要返回 Markdown、代码块、解释、纯文本或 schema 外字段。"""


def scan_prompt(query: str, items: list[dict[str, str]]) -> str:
    return f"""你正在逐项检查 DOCX 合同原文，必须检查本批次中的每一个 anchor。
审查问题：{query}

本批次原文：
{json.dumps(items, ensure_ascii=False)}

规则：
- 只报告与审查问题直接相关、原文中真实存在的问题。
- 错别字检查应报告明显的错字、漏字、重复字和不合理用词；语病检查应报告明确的成分残缺、搭配不当、语序或逻辑问题。
- 没有问题的 anchor 不要返回。
- anchor_id 必须原样来自本批次，不得编造；同一 anchor 最多返回一次。
- 只返回合法 JSON object，不要返回 Markdown、解释或 schema 外字段。

输出格式要求：
{SCAN_EXPECTED_JSON}"""


def expand_prompt(node_id: str, title: str, start_anchor: str, end_anchor: str, items_text: str) -> str:
    return f"""你正在把一个过长的 DOCX 合同 node 拆成更小的子 node。
当前 node:
- node_id: {node_id}
- title: {title}
- anchor range: {start_anchor} - {end_anchor}

下面是当前 node 范围内的段落和表格。每一项前面的方括号是 anchor:
{items_text}

任务：找出这些内容中真实出现、可以作为子 node 起点的标题。

判断规则：
- 只能使用原文中实际出现的标题，不得改写、概括或编造。
- 标题可以是“第一条”“一、”“（一）”“1、”“1.”“1.1”“附件：”“其他。”等形式。
- 表格列名、表格行名、正文交叉引用、普通句子不能作为标题。
- 不要返回当前 node 自己的标题。
- 如果没有可拆分的子标题，返回空数组。
- anchor 必须来自上面方括号中的 anchor。
- level_hint 表示相对层级，1 是本 node 下的最高子层级，2/3 表示更低层级；不确定时填 null。

输出规范：
- 必须只返回一个合法 JSON object。
- 不要返回 Markdown。
- 不要返回解释。
- 不要返回纯文本。
- 不要添加 schema 以外的字段。
- JSON 必须严格符合：
{{
  "subsections": [
    {{"anchor": "p_0001", "title": "标题原文", "level_hint": 1}}
  ]
}}"""


def attachment_hierarchy_prompt(
    attachment_title: str,
    pattern_summary: str,
    candidate_text: str,
    max_levels: int,
    *,
    inside_attachment: bool = True,
) -> str:
    scope = "合同附件内部" if inside_attachment else "合同正文大节点内部"
    multiple_documents = (
        "附件中可能连续包含多份独立细则或标准，可以划分多个 segment。"
        if inside_attachment
        else "当前正文节点属于同一局部文档，除非原文明确开始另一份独立文档，否则只建立一个 segment。"
    )
    return f"""你正在识别{scope}的真实标题及父子层级。
当前节点标题：{attachment_title}

候选编号形态统计：
{pattern_summary}

候选段落如下。每条包含 anchor、原文、结构信号及相邻内容：
{candidate_text}

任务：
- 将当前节点划分为一个或多个局部文档 segment，并归纳每个 segment 的“编号族到层级”规则。
- {multiple_documents}
- start_anchor 是 segment 覆盖的第一个候选；下一个 segment 开始时，前一个 segment 结束。
- document_title 只填写无编号的内部文档名称；没有时填 null，且其 level 必须为 1。
- level_rules 只返回该 segment 中真实作为结构标题的编号族及层级。脚本会把规则应用到该编号族的全部连续候选。
- additional_headings 只返回无法通过编号族表达、但确实属于标题的少量无编号候选；每一项必须完整包含 anchor、title、level，任何字段都不能省略。
- level 是相对于当前附件的层级，1 为附件内部最高层，最多 {max_levels} 级。
- 例如“科技部门驻场外包考核细则 -> 第一条 -> （一） -> 1、”应归纳为 document_title level 1、article level 2、cn_paren level 3、arabic_comma level 4。
- chapter、article、section 是不同编号族；同一 segment 内同一编号族只能对应一个层级。
- required=true 表示脚本确认该候选属于连续编号序列；相应 segment 必须提供其 number_family 规则。
- 排除正文句子、表格行列名、普通交叉引用，不要逐条抄写连续编号标题。
- 所有 anchor 和 title 必须来自候选原文，不得改写、截断或编造。
- segment 按 start_anchor 原文顺序返回，同一 anchor 只能出现一次。
- 如果不存在可形成结构的标题，返回空 segments。

输出规范：
- 必须只返回合法 JSON object，不要返回 Markdown 或解释。
- 不要添加 schema 以外字段。
- JSON 必须严格符合：
{{
  "segments": [
    {{
      "start_anchor": "p_0001",
      "document_title": {{"anchor": "p_0001", "title": "内部文档标题", "level": 1}},
      "level_rules": [
        {{"number_family": "article", "level": 2}},
        {{"number_family": "cn_paren", "level": 3}}
      ],
      "additional_headings": [
        {{"anchor": "p_0008", "title": "其他约定", "level": 2}}
      ]
    }}
  ]
}}"""


def summary_prompt(title: str, node_type: str, content: str, max_tokens: int = 200) -> str:
    return f"""你正在为合同结构树中的一个 node 生成检索摘要。
node 标题：{title}
node 类型：{node_type}

内容：
{content}

摘要要求：
- 使用中文。
- 只概括本 node 的主要内容，不添加原文没有的信息。
- 摘要用于后续 LLM 根据结构树选择相关 node，因此必须保留可用于导航的关键信息。
- 优先保留合同审查相关信息，例如主体、金额、期限、付款、发票、验收、违约、附件引用、保密、知识产权、项目负责人等。
- 摘要不得超过 {max_tokens} tokens。

输出规范：
- 必须只返回一个合法 JSON object。
- 不要返回 Markdown。
- 不要返回解释。
- 不要返回纯文本。
- 不要添加 schema 以外的字段。
- JSON 必须严格符合：{{"summary": "摘要内容"}}"""


def summary_batch_prompt(nodes: list[dict[str, str]], max_tokens: int = 200) -> str:
    return f"""你正在批量为合同结构树 node 生成检索摘要。

待摘要 node：
{json.dumps(nodes, ensure_ascii=False)}

摘要要求：
- 每个输入 node_id 必须且只能返回一条摘要，node_id 原样返回。
- 使用中文，只概括对应 node 内容，不添加原文没有的信息。
- 摘要用于后续 LLM 根据结构树选择相关 node，保留主体、金额、期限、付款、发票、验收、违约、附件引用、保密、知识产权、项目负责人等导航信息。
- 每条摘要不得超过 {max_tokens} tokens。

只返回合法 JSON object，不要返回 Markdown、解释或 schema 外字段：
{{"summaries":[{{"node_id":"body/sec_001","summary":"摘要内容"}}]}}"""
