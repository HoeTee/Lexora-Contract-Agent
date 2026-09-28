from __future__ import annotations

import json

from routing.prompts import ROUTE_EXPECTED_JSON, route_prompt


__all__ = ["ROUTE_EXPECTED_JSON", "route_prompt"]

EXPAND_PROMPT_VERSION = "docx_expand_v2"
SUMMARY_PROMPT_VERSION = "docx_summary_v3"
SUMMARY_BATCH_PROMPT_VERSION = "docx_summary_batch_v1"
ATTACHMENT_HIERARCHY_PROMPT_VERSION = "local_hierarchy_v4"

SCAN_EXPECTED_JSON = """{
  "findings": [
    {
      "anchor_id": "p_0001",
      "reason": "存在明确的错别字或语病"
    }
  ]
}"""


def scan_prompt(
    query: str,
    items: list[dict[str, str]],
    candidate_instruction: str = "",
) -> str:
    candidate_rule = (
        "\n候选提取任务：\n"
        f"- {candidate_instruction}\n"
        "- 当前步骤只负责完整提取候选原文，不要求单个 anchor 独立证明最终问题。"
        "只要 anchor 符合候选提取任务就必须返回，最终比较和审查结论由下游审查 Agent 完成。\n"
        if candidate_instruction.strip()
        else ""
    )
    return f"""你正在逐项检查 DOCX 合同原文，必须检查本批次中的每一个 anchor。
审查问题：{query}
{candidate_rule}

本批次原文：
{json.dumps(items, ensure_ascii=False)}

规则：
- 只报告与审查问题直接相关、原文中真实存在的问题或候选提取任务明确要求返回的原文。
- 严格遵守审查问题及检查要点给出的范围；明确排除的缺陷类型不得作为候选返回，不得用通用语言质量规则扩大范围。
- 字段完整性检查必须结合字段名称、相邻文字、表格关系和合同语境判断，不能按固定字符形态机械判断。可能的未填写示例包括“[合同中心-合同编码]”“#乙方合同号#【     】”“乙方支持联络电话：XX，XX”、字段冒号后为空、待填写的空白单元格或仍保留的模板说明。
- 审查问题若使用“信息是否齐全”“包括……等”或“包括但不限于”，列举项不是封闭清单；与合同身份、联系、签订或履行有关且有明确字段语义的未填写项都属于候选，例如合同编号、主体名称、地址、电话、联系人、银行信息和服务联络信息。
- 反例：“违约金【5%】”“期限【45】日”等括号内已有实际值；“如授权代理人签署，请附授权书”等条件性说明不能证明条件已经发生。这些不能仅凭外形报告为未填写。
- 字段完整性检查应遍历本批次全部输入并返回每一个相关候选 anchor，不设 Top-K，不得在找到若干候选后停止，也不得把多个字段合并成一个 finding；同类字段出现在不同位置时必须分别返回，最终是否构成审查问题由审查 Agent 决定。
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
