"""
Base Agent class — handles LLM communication, tool calling, context
management, token tracking, and conversation logging.
"""
from pydantic import BaseModel
from openai import AsyncOpenAI, RateLimitError, APITimeoutError, APIConnectionError, InternalServerError, APIStatusError
from redis.exceptions import RedisError
from loggers.agent_logger import agent_call_log_dir, log_agent_step, log_conversation
from loggers.call_log import new_call
from loggers.trace_helpers import llm_inputs, llm_outputs
from loggers.trace_logger import get_current_trace
from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_ENABLE_THINKING,
    LLM_NAME,
    MAX_TOOL_CALLS,
    MAX_CONTEXT_TOKENS,
    MAX_PROMPT_TOKENS,
    MAX_RESULT_TOKENS,
    MODEL_CALL_TIMEOUT_SECONDS,
    MODEL_CALL_MAX_RETRIES,
    SEED,
    TEMPERATURE,
    TOP_P,
)
from endpoints.runtime.errors import ModelCallError
from endpoints.runtime.llm_semaphore import LLMGlobalSemaphoreTimeout, llm_global_semaphore
import json
import tiktoken


_PROMPT_ENCODING = tiktoken.get_encoding("cl100k_base")
_CHAT_MESSAGE_OVERHEAD_TOKENS = 16
_CHAT_REQUEST_OVERHEAD_TOKENS = 64


def estimate_chat_prompt_tokens(messages: list, tools: list | None = None) -> int:
    """Estimate one complete chat request, including tool schemas."""
    payload = json.dumps(
        {"messages": messages, "tools": tools or []},
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return (
        len(_PROMPT_ENCODING.encode(payload))
        + len(messages) * _CHAT_MESSAGE_OVERHEAD_TOKENS
        + _CHAT_REQUEST_OVERHEAD_TOKENS
    )

MODEL_API_ERRORS = (
    APITimeoutError,
    APIConnectionError,
    RateLimitError,
    InternalServerError,
    APIStatusError,
)


class Settings(BaseModel):
    api_key: str = LLM_API_KEY
    base_url: str = LLM_BASE_URL
    model: str = LLM_NAME
    temperature: float = TEMPERATURE
    top_p: float = TOP_P
    seed: int = SEED
    enable_thinking: bool | None = LLM_ENABLE_THINKING


class Agent:

    def __init__(
        self,
        system_prompt: str = "",
        name: str = "Agent",
        mcp_client=None,
        tools=None,
        settings: Settings | None = None,
        debug: bool = False,
        max_tool_calls: int = MAX_TOOL_CALLS,
        max_context_tokens: int = MAX_CONTEXT_TOKENS,
        max_prompt_tokens: int = MAX_PROMPT_TOKENS,
        max_result_tokens: int = MAX_RESULT_TOKENS,
    ) -> None:
        """
        Initialize the Agent.

        Args:
            system_prompt: System prompt for the agent.
            name: Name of the agent.
            mcp_client: MCP client for tool calling.
            tools: List of tools for the agent.
            settings: Settings for the agent.
            debug: Whether to enable debug mode.
            MAX_TOOL_CALLS: Maximum number of tool calls.
            MAX_CONTEXT_TOKENS: Maximum number of context tokens.
            MAX_RESULT_TOKENS: Maximum number of result tokens.
        """
        self.settings = settings or Settings()
        self.client = AsyncOpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            timeout=MODEL_CALL_TIMEOUT_SECONDS,
            max_retries=MODEL_CALL_MAX_RETRIES,
        )
        self.name = name
        self.mcp_client = mcp_client
        self.tools = tools or (mcp_client.tools if mcp_client else [])
        self.messages: list = []
        self.debug = debug
        self.MAX_TOOL_CALLS = max_tool_calls
        self.MAX_CONTEXT_TOKENS = max_context_tokens
        self.MAX_PROMPT_TOKENS = max_prompt_tokens
        self.MAX_RESULT_TOKENS = max_result_tokens

        # Token tracking
        self.token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.step_count = 0
        self.tool_call_count = 0

        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})



    # ==================== utils (Logging & Esti Tokens) ====================

    def _log(self, message: str):
        if self.debug:
            print(f"[{self.name}] {message}")
    
    def _estimate_tokens(self, chars: int) -> int:
        return int(chars * 0.3)

    # ==================== Context ====================

    def _check_context_limits(self) -> int:
        total_chars = sum(len(str(msg.get("content", ""))) for msg in self.messages)
        estimated = self._estimate_tokens(total_chars)
        prompt_tokens = estimate_chat_prompt_tokens(self.messages, self.tools)
        self._log(
            f"Pre-request: {len(self.messages)} messages, "
            f"~{estimated} content tokens, ~{prompt_tokens} complete prompt tokens"
        )
        if prompt_tokens > self.MAX_PROMPT_TOKENS:
            raise ValueError(
                f"{self.name} prompt exceeds the configured per-call safety limit "
                f"({prompt_tokens} estimated tokens > {self.MAX_PROMPT_TOKENS}); "
                "the model call was not sent"
            )
        if estimated > self.MAX_CONTEXT_TOKENS:
            print(f"[{self.name}] ⚠️ Context tokens ({estimated}) approaching limit!")
        return prompt_tokens

    def _truncate_text(self, text: str, max_tokens: int = None) -> str:
        if max_tokens is None:
            max_tokens = self.MAX_RESULT_TOKENS
        estimated = self._estimate_tokens(len(text))
        if estimated <= max_tokens:
            return text
        max_chars = int(max_tokens / 0.3)
        return text[:max_chars] + "\n\n[... Content truncated ...]"

    # ==================== Token Tracking ====================

    def _update_token_usage(self, usage: dict):
        if usage:
            self.token_usage["prompt_tokens"] += usage.prompt_tokens
            self.token_usage["completion_tokens"] += usage.completion_tokens
            self.token_usage["total_tokens"] += usage.total_tokens

    def get_token_usage(self) -> dict:
        return self.token_usage.copy()

    def get_step_usage(self) -> dict:
        return {
            "steps": self.step_count,
            "tool_calls": self.tool_call_count,
        }

    # ==================== Tool Calls ====================

    ## Parse tool arguments from tool call.
    def _parse_tool_arguments(self, args_str: str, function_name: str) -> dict | None:
        try:
            return json.loads(args_str)
        except json.JSONDecodeError:
            self._log(f"⚠️ JSON parse error for '{function_name}'")
            start = args_str.find("{")
            end = args_str.rfind("}") + 1
            if start != -1 and end != 0:
                try:
                    return json.loads(args_str[start:end])
                except json.JSONDecodeError:
                    pass
            print(f"[{self.name}] ✗ Failed to parse tool arguments for {function_name}")
            return None
    
    ## Execute a tool call and return text message.
    async def _execute_a_tool(self, tool_call: dict) -> str:
        function_name = tool_call.function.name
        self._log(f"→ Calling tool: {function_name}")

        args_dict = self._parse_tool_arguments(tool_call.function.arguments, function_name) # Intercepts tool executions and returns text message.
        if args_dict is None:
            return "Failed to parse tool arguments."

        if self.mcp_client:
            tool_msg = await self.mcp_client.tool_message_from_call(tool_call)
            result = tool_msg["content"]
        else:
            return f"No MCP client for tool {function_name}"

        result_str = str(result)
        estimated = self._estimate_tokens(len(result_str))
        if estimated > self.MAX_RESULT_TOKENS:
            result_str = self._truncate_text(result_str)
            self._log(f"← Tool '{function_name}': ~{estimated} tokens (truncated)")
        else:
            self._log(f"← Tool '{function_name}': ~{estimated} tokens")
        return result_str

    ## Append tool calls onto history
    async def _process_tool_calls(self, tool_calls):
        self.tool_call_count += len(tool_calls)
        self._log(f"Processing {len(tool_calls)} tool calls (total: {self.tool_call_count})")

        if self.tool_call_count > self.MAX_TOOL_CALLS:
            print(f"[{self.name}] ⚠️ Max tool calls ({self.MAX_TOOL_CALLS}) reached. Aborting task and marking review invalid.")
            return "You have used up all your tool calls. Please provide the final answer."

        for tool_call in tool_calls:
            result = await self._execute_a_tool(tool_call)
            self.messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": result
            })
        log_conversation(self.name, self.messages)
        return None

    # ==================== Main Loop ====================

    async def chat(self, message="") -> str:
        if message:
            self.messages.append({"role": "user", "content": message})
            log_conversation(self.name, self.messages)

        result = await self.execute()
        if result is not None:
            self.messages.append({"role": "assistant", "content": result})
            log_conversation(self.name, self.messages)
        else:
            raise ValueError("No final assistant content returned.")
        return result

    async def execute(self) -> str:
        while True:
            prompt_token_estimate = self._check_context_limits()
            self.step_count += 1
            current_step = self.step_count

            try:
                request_kwargs = {
                    "model": self.settings.model,
                    "temperature": self.settings.temperature,
                    "top_p": self.settings.top_p,
                    "seed": self.settings.seed,
                    "messages": self.messages,
                }
                if self.tools:
                    request_kwargs["tools"] = self.tools
                    request_kwargs["tool_choice"] = "auto"
                if self.settings.enable_thinking is not None:
                    request_kwargs["extra_body"] = {
                        "enable_thinking": self.settings.enable_thinking
                    }

                async with get_current_trace().span(
                    f"llm.{self.name}",
                    run_type="llm",
                    inputs=llm_inputs(
                        agent_name=self.name,
                        model=self.settings.model,
                        messages=self.messages,
                        tools=self.tools,
                        step=current_step,
                        prompt_token_estimate=prompt_token_estimate,
                        prompt_token_limit=self.MAX_PROMPT_TOKENS,
                    ),
                    metadata={
                        "temperature": self.settings.temperature,
                        "top_p": self.settings.top_p,
                        "seed": self.settings.seed,
                    },
                ) as span:
                    record = new_call(
                        component="agent", model=self.settings.model,
                        request=request_kwargs, directory=agent_call_log_dir(),
                        criterion_id=self.name.removeprefix("SubAgent_") if self.name.startswith("SubAgent_") else None,
                        stage="agent_model_call",
                        source=self.name,
                    )
                    span.set_metadata({"call_id": record.call_id,
                                       "call_path": str(record.path) if record.path else None})
                    request_started = False
                    try:
                        async with llm_global_semaphore(
                            agent_name=self.name,
                            model=self.settings.model,
                        ) as lease:
                            if lease.enabled:
                                span.set_metadata(
                                    {
                                        "llm_global_semaphore_key": lease.key,
                                        "llm_global_semaphore_wait_seconds": lease.wait_seconds,
                                        "llm_global_semaphore_max_requests": lease.max_concurrent_requests,
                                    }
                                )
                            request_started = True
                            completion = await self.client.chat.completions.create(**request_kwargs)
                    except Exception as exc:
                        record.failed(exc, stage="request" if request_started else "semaphore_wait")
                        conversation_path = log_conversation(self.name, self.messages)
                        span.set_metadata(
                            {"conversation_path": str(conversation_path) if conversation_path else None}
                        )
                        raise
                    record.succeeded(completion.model_dump())
                    span.set_outputs(llm_outputs(completion))
            except MODEL_API_ERRORS as exc:
                error = ModelCallError(
                    "agent",
                    f"{self.name}: {exc}",
                    http_status=getattr(exc, "status_code", None),
                    error_type=type(exc).__name__,
                )
                error.origin = "agent"
                error.failure_stage = "agent_model_call"
                error.call_id = record.call_id
                error.call_path = str(record.path) if record.path else None
                raise error from exc
            except LLMGlobalSemaphoreTimeout as exc:
                error = ModelCallError(
                    "agent",
                    f"{self.name}: {exc}",
                    http_status=None,
                    error_type=type(exc).__name__,
                )
                error.origin = "agent"
                error.failure_stage = "semaphore_wait"
                error.call_id = record.call_id
                error.call_path = str(record.path) if record.path else None
                raise error from exc
            except RedisError as exc:
                error = ModelCallError(
                    "agent", f"{self.name}: {exc}", http_status=None,
                    error_type=type(exc).__name__,
                )
                error.origin = "semaphore"
                error.failure_stage = "semaphore_wait"
                error.call_id = record.call_id
                error.call_path = str(record.path) if record.path else None
                raise error from exc

            response_message = completion.choices[0].message
            finish_reason = completion.choices[0].finish_reason
            self._update_token_usage(completion.usage)
            tool_calls = response_message.tool_calls or []
            log_agent_step(
                self.name,
                {
                    "agent_step": current_step,
                    "finish_reason": finish_reason,
                    "message_count": len(self.messages),
                    "tool_calls_in_step": len(tool_calls),
                    "tool_call_names": [
                        tool_call.function.name for tool_call in tool_calls
                    ],
                    "tool_calls_total_before_processing": self.tool_call_count,
                    "max_tool_calls": self.MAX_TOOL_CALLS,
                    "token_usage_total": self.token_usage.get("total_tokens", 0),
                },
            )

            if tool_calls:
                self.messages.append(response_message.model_dump(exclude_unset=True)) # Append tool calls onto history
                log_conversation(self.name, self.messages)
                forced = await self._process_tool_calls(tool_calls)
                if forced:
                    self.messages.pop()  # Remove unresolved tool_calls message preventing API errors
                    self.messages.append(
                        {"role": "user", "content": "你已用尽所有工具调用次数，请根据已收集的信息直接给出最终结论。"}
                    )
                    log_conversation(self.name, self.messages)
                    continue
            else:
                return response_message.content

