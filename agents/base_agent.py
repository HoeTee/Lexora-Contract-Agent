"""
Base Agent class — handles LLM communication, tool calling, context
management, token tracking, and conversation logging.
"""
from pydantic import BaseModel
from openai import AsyncOpenAI, RateLimitError, APITimeoutError, APIConnectionError, InternalServerError, APIStatusError
from redis.exceptions import RedisError
from loggers.agent_logger import agent_call_log_dir, log_agent_step, log_conversation
from loggers.call_log import LogicalCall, current_events_path, logical_call
from endpoints.runtime.llm_client import request_llm
from loggers.trace_helpers import llm_inputs, llm_outputs
from loggers.trace_logger import get_current_trace
from config import (
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_ENABLE_THINKING,
    LLM_NAME,
    MAX_TOOL_CALLS,
    LLM_MAX_INPUT_TOKENS,
    MODEL_CALL_TIMEOUT_SECONDS,
    SEED,
    TEMPERATURE,
    TOP_P,
)
from endpoints.runtime.errors import ModelCallError
from endpoints.runtime.model_budget import ModelCallTimeout
from endpoints.runtime.model_calls import execute_model
from endpoints.runtime.model_tokens import model_tokenizer, prepare_chat_messages
from agents.prompts import get_prompt
import json
from contextlib import contextmanager


def estimate_chat_prompt_tokens(messages: list, tools: list | None = None, *, model: str = LLM_NAME) -> int:
    """Count complete chat input with the configured offline model tokenizer."""
    return model_tokenizer(model).count_chat(messages, tools, enable_thinking=LLM_ENABLE_THINKING)

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
        max_tool_calls: int | None = MAX_TOOL_CALLS,
        max_input_tokens: int | None = LLM_MAX_INPUT_TOKENS,
        criterion_id: str | None = None,
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
            max_input_tokens: Complete request limit; null leaves input unrestricted.
        """
        self.settings = settings or Settings()
        self.client = AsyncOpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            timeout=MODEL_CALL_TIMEOUT_SECONDS,
            max_retries=0,
        )
        self.name = name
        self.criterion_id = criterion_id
        self.call_stage = "model"
        self.call_reason: str | None = None
        self._validation_group_enabled = False
        self._validation_call: LogicalCall | None = None
        self.mcp_client = mcp_client
        self.tools = tools or (mcp_client.tools if mcp_client else [])
        self.messages: list = []
        self.debug = debug
        self.MAX_TOOL_CALLS = max_tool_calls
        self.MAX_INPUT_TOKENS = max_input_tokens
        self.last_input_preparation: dict = {}

        # Token tracking
        self.token_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._token_attempts: dict[str, dict] = {}
        self.step_count = 0
        self.tool_call_count = 0

        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})



    # ==================== utils (Logging & Esti Tokens) ====================

    def _log(self, message: str):
        if self.debug:
            print(f"[{self.name}] {message}")
    
    # ==================== Context ====================

    def _prepare_request_messages(self) -> list[dict]:
        messages, metadata = prepare_chat_messages(
            self.messages, model=self.settings.model, tools=self.tools,
            max_input_tokens=self.MAX_INPUT_TOKENS, strict=False,
            enable_thinking=self.settings.enable_thinking,
        )
        self.last_input_preparation = metadata
        log_agent_step(self.name, {"input_preparation": metadata})
        return messages

    # ==================== Token Tracking ====================

    def _update_token_usage(self, records):
        """Include every sent attempt once; keep unknown output explicitly unknown."""
        if not hasattr(self, "_token_attempts"):
            self._token_attempts = {}
        for record in records:
            payload = record.payload
            if payload.get("sent"):
                self._token_attempts[record.call_id] = payload
        attempts = list(self._token_attempts.values())
        inputs = [item.get("input_tokens") for item in attempts]
        outputs = [item.get("output_tokens") for item in attempts]
        known_input = sum(value for value in inputs if value is not None)
        known_output = sum(value for value in outputs if value is not None)
        input_complete = all(value is not None for value in inputs)
        output_complete = all(value is not None for value in outputs) and all(
            item.get("output_completeness") == "complete" for item in attempts
        )
        self.token_usage = {
            "prompt_tokens": known_input if input_complete else None,
            "completion_tokens": known_output if all(value is not None for value in outputs) else None,
            # Legacy workflow arithmetic consumes total_tokens. It is explicitly
            # a known lower bound when any response was partial or unavailable.
            "total_tokens": known_input + known_output,
            "known_prompt_tokens": known_input, "known_completion_tokens": known_output,
            "input_complete": input_complete, "output_complete": output_complete,
            "total_is_lower_bound": not (input_complete and output_complete),
            "input_token_sources": sorted({item.get("input_token_source") for item in attempts
                                            if item.get("input_token_source")}),
            "output_token_sources": sorted({item.get("output_token_source") for item in attempts
                                             if item.get("output_token_source")}),
            "sent_attempt_count": len(attempts),
        }

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

        if function_name in {"contract_search", "docxindex_search"}:
            criterion_id = getattr(self, "criterion_id", None)
            if criterion_id:
                args_dict["criterion_id"] = criterion_id
            events_path = current_events_path()
            if events_path:
                args_dict["api_events_path"] = events_path
            tool_call.function.arguments = json.dumps(args_dict, ensure_ascii=False)

        if self.mcp_client:
            tool_msg = await self.mcp_client.tool_message_from_call(tool_call)
            result = tool_msg["content"]
        else:
            return f"No MCP client for tool {function_name}"

        result_str = str(result)
        tokens = model_tokenizer(self.settings.model).count_text(result_str)
        self._log(f"← Tool '{function_name}': {tokens} tokens")
        return result_str

    ## Append tool calls onto history
    async def _process_tool_calls(self, tool_calls):
        self.tool_call_count += len(tool_calls)
        self._log(f"Processing {len(tool_calls)} tool calls (total: {self.tool_call_count})")

        if self.MAX_TOOL_CALLS is not None and self.tool_call_count > self.MAX_TOOL_CALLS:
            print(f"[{self.name}] ⚠️ Max tool calls ({self.MAX_TOOL_CALLS}) reached. Aborting task and marking review invalid.")
            return get_prompt("AGENT_TOOL_LIMIT_PROMPT")

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

    @contextmanager
    def validation_requests(self):
        """Keep schema repairs together, closing each tool request before tool execution."""
        self._validation_group_enabled = True
        self._validation_call = None
        try:
            yield
        except BaseException as exc:
            if self._validation_call is not None:
                self._validation_call.finish(exc)
                record = self._validation_call.last_record
                if record is not None and not getattr(exc, "call_id", None):
                    exc.call_id = record.reference_id
                    exc.call_path = str(record.reference_path) if record.reference_path else None
            raise
        else:
            self._finish_validation_call()
        finally:
            self._validation_group_enabled = False
            self._validation_call = None

    def _finish_validation_call(self):
        record = getattr(self, "_validation_call", None)
        if record is not None:
            try:
                record.budget.task_remaining()
            except ModelCallTimeout as exc:
                if record.last_record is not None:
                    exc.call_id = record.last_record.reference_id
                    exc.call_path = str(record.last_record.reference_path) if record.last_record.reference_path else None
                record.finish(exc)
                self._validation_call = None
                raise
            record.finish()
            self._validation_call = None

    def reject_schema_response(self, exc):
        record = getattr(self, "_validation_call", None)
        if record is not None:
            record.reject_response(exc)

    async def _request_completion(self, request_kwargs, span, *, stage=None, reason=None):
        grouped = getattr(self, "_validation_group_enabled", False)
        with logical_call("agent", self.settings.model,
                          directory=agent_call_log_dir(), source=self.name,
                          stage=stage or getattr(self, "call_stage", "model"),
                          reason=reason if reason is not None else getattr(self, "call_reason", None),
                          record=getattr(self, "_validation_call", None) if grouped else None,
                          finalize=not grouped) as logical:
            if grouped:
                self._validation_call = logical
            async def send(remaining):
                return await request_llm(self.client.chat.completions,
                                         **{**{key: value for key, value in request_kwargs.items()
                                               if key != "_input_preparation"}, "timeout": remaining})
            try:
                completion = await execute_model(model_type="llm", model=self.settings.model,
                    caller="agent", request=request_kwargs, send=send,
                    budget=logical.budget, input_limit=self.MAX_INPUT_TOKENS,
                    serialize=lambda response: response.model_dump(),
                    record_options={"directory": agent_call_log_dir(), "source": self.name,
                                    "criterion_id": getattr(self, "criterion_id", None),
                                    "stage": "agent_model_call"})
            finally:
                self._update_token_usage(logical.attempt_records)
                if logical.last_record is not None:
                    record = logical.last_record
                    span.set_metadata({"call_id": record.reference_id,
                                       "call_path": str(record.reference_path) if record.reference_path else None})
            return completion

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
        tool_continuations = 0
        while True:
            request_messages = self._prepare_request_messages()
            prompt_token_estimate = self.last_input_preparation["input_tokens_after"]
            self.step_count += 1
            current_step = self.step_count

            try:
                request_kwargs = {
                    "model": self.settings.model,
                    "temperature": self.settings.temperature,
                    "top_p": self.settings.top_p,
                    "seed": self.settings.seed,
                    "messages": request_messages,
                    "_input_preparation": self.last_input_preparation,
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
                        messages=request_messages,
                        tools=self.tools,
                        step=current_step,
                        prompt_token_estimate=prompt_token_estimate,
                        prompt_token_limit=self.MAX_INPUT_TOKENS,
                    ),
                    metadata={
                        "temperature": self.settings.temperature,
                        "top_p": self.settings.top_p,
                        "seed": self.settings.seed,
                        "input_preparation": self.last_input_preparation,
                    },
                ) as span:
                    stage = getattr(self, "call_stage", "model")
                    completion = await self._request_completion(
                        request_kwargs, span,
                        stage=f"{stage}-tool-result{tool_continuations}" if tool_continuations else stage,
                        reason="根据工具返回结果继续生成审查结论" if tool_continuations else None,
                    )
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
                error.call_id = getattr(exc, "call_id", None)
                error.call_path = getattr(exc, "call_path", None)
                raise error from exc
            except ModelCallTimeout as exc:
                error = ModelCallError(
                    "agent",
                    f"{self.name}: {exc}",
                    http_status=None,
                    error_type=type(exc).__name__,
                )
                error.origin = "agent"
                error.failure_stage = getattr(exc, "failure_stage", "model_budget")
                error.call_id = getattr(exc, "call_id", None)
                error.call_path = getattr(exc, "call_path", None)
                raise error from exc
            except RedisError as exc:
                error = ModelCallError(
                    "agent", f"{self.name}: {exc}", http_status=None,
                    error_type=type(exc).__name__,
                )
                error.origin = "semaphore"
                error.failure_stage = getattr(exc, "failure_stage", "model_budget")
                error.call_id = getattr(exc, "call_id", None)
                error.call_path = getattr(exc, "call_path", None)
                raise error from exc

            response_message = completion.choices[0].message
            finish_reason = completion.choices[0].finish_reason
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
                self._finish_validation_call()
                tool_continuations += 1
                self.messages.append(response_message.model_dump(exclude_unset=True)) # Append tool calls onto history
                log_conversation(self.name, self.messages)
                forced = await self._process_tool_calls(tool_calls)
                if forced:
                    self.messages.pop()  # Remove unresolved tool_calls message preventing API errors
                    self.messages.append(
                        {"role": "user", "content": get_prompt("AGENT_TOOL_LIMIT_PROMPT")}
                    )
                    log_conversation(self.name, self.messages)
                    continue
            else:
                return response_message.content

