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
  "removals": []
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
