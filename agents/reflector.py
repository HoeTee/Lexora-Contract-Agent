"""
ReflectorAgent validates sub-agent review quality.
"""

from agents.base_agent import Agent
from agents.json_utils import chat_until_valid_json
from agents.prompts.cn_prompts import REFLECTOR_SYSTEM_PROMPT
from agents.schemas import ReflectorOutput


REFLECTOR_EXPECTED_JSON = """
{
  "status": "PASS",
  "feedback": "",
  "finding_decisions": [
    {
      "finding_id": "F001",
      "decision": "confirm",
      "reason": "原文与审查标准能够确认该问题"
    }
  ]
}
"""


class ReflectorAgent(Agent):
    """Reviews sub-agent output and returns PASS/REJECT with feedback."""

    def __init__(self, **kwargs):
        super().__init__(
            system_prompt=REFLECTOR_SYSTEM_PROMPT,
            name="Reflector",
            **kwargs,
        )

    async def review(self, agent_output: str, missing_text_review_notes: str = "") -> dict:
        """
        Review a sub-agent's output.
        Returns: {"status": "PASS"/"REJECT", "feedback": "..."}
        """
        prompt = (
            "字段职责：subagent_output 是唯一完整审查结果（含 edit 和 evidence）；current_findings 只是修改位置索引，不代表完整证据。\n"
            "finding_decisions 只能由 Reflector 返回，不要要求 SubAgent 添加该字段。finding_id 必须逐字复制输入的 F001 等编号，不是 issue_id。\n"
            "必须逐条判断所有 current_findings 和 missing_previous_findings：事实成立用 confirm，需要改写用 revise，事实不成立用 remove，并写明理由。\n"
            "compliant + issues=[] 是合法结构，不得要求其提供 schema 未定义的顶层 reasoning 或为了引用证据而新增 issue。\n"
            "请审核以下审查结果的质量：\n\n"
            f"SubAgent 输出：\n{agent_output}\n\n"
            "以下是系统针对 anchors 为空的缺失类 issue 做的补充检索结果，"
            "仅用于判断缺失判断是否需要退回重审，不是 Reflector 输出字段：\n"
            f"{missing_text_review_notes}"
        )
        parsed, _ = await chat_until_valid_json(
            self,
            prompt,
            ReflectorOutput,
            REFLECTOR_EXPECTED_JSON,
        )
        return parsed
