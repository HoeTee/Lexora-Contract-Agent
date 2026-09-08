"""Prompts for the dynamic route planner used by uploaded criteria."""

import json


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
- 标准明确点名字段时可用 rule 定位这些字段，terms 只能使用明确字段名；标准要求检查全合同所有未填写字段或逐项检查未知形态的未填写字段时使用 scan，不得把占位符简化为正则匹配。
- 需要比较两个以上区域、首尾、正文与附件时，为每个区域分别生成一个 region 步骤；每个 region 步骤只填写一个 regions 值。
- 错别字、语病或必须逐段通读全文的问题使用 scan。scan 可以与 title/rule 组合，不得与 region/hybrid 组合。
- 标题、区域和精确关键词均无法可靠限定范围时使用 hybrid；hybrid 不再设置 fallback=true。
- 只有 title/rule 结果还需要全局语义补充时 fallback=true；region 与 scan 不使用全局 fallback。
- 不得把审查结论写进检索计划。

输出格式要求（JSON）：
{ROUTE_EXPECTED_JSON}

字段约束：
1. 顶层只能包含 steps 和 fallback。
2. steps 必须是数组，包含 1 到 5 个路由步骤。
3. method 只能是 title、region、rule、scan、hybrid 之一。
4. region 必须且只能填写一个 regions 值或至少一个 anchor_terms；非 region 方法两者都必须为空数组。
5. rule 必须填写至少一个 term；title 必须填写 terms 或设置 all_titles=true。
6. 包含 region、scan、hybrid 时 fallback 必须为 false。
7. 只返回合法 JSON object，不要返回 Markdown、解释或 schema 外字段。"""
