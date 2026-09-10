from __future__ import annotations # no quotes are needed for type hints

import json
import os
import sys
import traceback # help show where the error happended
from contextlib import AsyncExitStack
from typing import Any, Dict, List, Optional

from loggers.mcp_logger import create_mcp_logger
from loggers.trace_helpers import mcp_inputs, mcp_outputs
from loggers.trace_logger import get_current_trace
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


def resolve_stdio_target(server_target: str, executable: str | None = None) -> tuple[str, list[str]]:
    """Resolve a local MCP target into its executable and argument list."""
    if server_target.startswith("module:"):
        module_name = server_target.removeprefix("module:").strip()
        if not module_name:
            raise ValueError("MCP module target must include a module name")
        return executable or sys.executable, ["-m", module_name]
    if server_target.endswith(".py"):
        return executable or sys.executable, [server_target]
    if server_target.endswith(".js"):
        return "node", [server_target]
    raise ValueError("Local MCP target must be module:<name>, a .py file, or a .js file")


def _stdio_environment(overrides: dict[str, str] | None) -> dict[str, str]:
    """Preserve the container environment when launching the trusted local MCP server."""
    return {**os.environ, **(overrides or {})}


class MinimalMCPClient:
    """
    Minimal MCP client that:
    - connects to a server over stdio
    - exposes MCP tools in OpenAI Chat Completions format
    - executes tool calls and returns Chat Completions tool messages
    """

    def __init__(
        self,
        server_target: str = "",
        log_file: str | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.server_target = server_target
        self.env = env or {}
        self.session: Optional[ClientSession] = None
        self.exit_stack = AsyncExitStack()
        self.tools: List[Dict[str, Any]] = []
        self.logger = create_mcp_logger(log_file)

    async def connect(
        self,
        server_target: str | None = None,
    ) -> None:
        server_target = server_target or self.server_target
        if not server_target:
            raise ValueError("No server_target provided")
        try: 
            is_http = server_target.startswith("http://")
            is_https = server_target.startswith("https://")
            is_module = server_target.startswith("module:")
            is_python = server_target.endswith(".py")
            is_js = server_target.endswith(".js")

            if not (is_http or is_https or is_module or is_python or is_js):
                raise ValueError("Server target must be an http(s) URL, module name, or a .py/.js file")
            
            elif is_http or is_https:
                http_transport = await self.exit_stack.enter_async_context(
                    streamable_http_client(server_target)
                )
                read_stream, write_stream, _ = http_transport
                self.session = await self.exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await self.session.initialize()

                self.tools = await self.list_tools_openai()
                self.logger.info(
                    f"Connected to remote server. Tools: {[t['function']['name'] for t in self.tools]}"
                )

            elif is_module or is_python or is_js:
                command, args = resolve_stdio_target(server_target)
                server_params = StdioServerParameters(
                    command=command,
                    args=args,
                    env=_stdio_environment(self.env),
                )
                stdio_transport = await self.exit_stack.enter_async_context(
                    stdio_client(server_params)
                )
                read_stream, write_stream = stdio_transport
                self.session = await self.exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream)
                )
                await self.session.initialize()

                self.tools = await self.list_tools_openai()
                self.logger.info(
                    f"Connected to remote server. Tools: {[t['function']['name'] for t in self.tools]}"
                )
        
        except Exception as e:
            self.logger.error(f"Error connecting to MCP server: {e}")
            traceback.print_exc() 
            raise


    async def list_tools_openai(
        self
    ) -> List[Dict[str, Any]]:
        if not self.session:
            raise RuntimeError("Client not connected")
        response = await self.session.list_tools()
        tool_schema = [
            {
                "type": "function",
                "function": {
                    "name": tool.name, # function name
                    "description": tool.description, # doc string
                    "parameters": tool.inputSchema, # MyModel.model_json_schema()
                },
            }
            for tool in response.tools
        ]
        return tool_schema


    async def call_tool(
        self,
        name: str,
        args: Dict[str, Any]
    ) -> str:
        """Call tool and return text result as string."""
        if not self.session:
            raise RuntimeError("Client not connected")
        # Functions in an MCP server that are exposed as tools cannot use variable arguments like *args or **kwargs. 
        # This restriction is because the protocol requires a complete and predictable parameter schema to be generated 
        # for the Language Model (LLM) client, which is not possible with variable argument lists.
        async with get_current_trace().span(
            f"mcp.{name}",
            run_type="mcp_tool",
            inputs=mcp_inputs(name, args),
        ) as span:
            result = await self.session.call_tool(name, args)
            # Extract .text from TextContent objects
            if hasattr(result, 'content') and isinstance(result.content, list):
                texts = []
                for item in result.content:
                    if hasattr(item, 'text'):
                        texts.append(item.text)
                    elif isinstance(item, dict) and 'text' in item:
                        texts.append(item['text'])
                    else:
                        texts.append(str(item))
                text_result = "\n".join(texts)
            else:
                text_result = str(result)
            span.set_outputs(mcp_outputs(text_result))
            return text_result

    async def get_available_tools(self) -> List[Dict[str, Any]]:
        """Return cached tools list in OpenAI format."""
        return self.tools


    def serialize_content(
        self, 
        content: Any
    ) -> Any:
        """
        serialize_content performs purely in-memory data transformation, 
        which does not involve any I/O operations like network requests, 
        file reading, or database queries that would require waiting (await). 
        """
        if content is None or isinstance(content, str):
            return content
        if isinstance(content, list):
            return [
                c.model_dump() if hasattr(c, "model_dump") else
                c.text if hasattr(c, "text") else
                str(c)
                for c in content
            ]
        if hasattr(content, "model_dump"):
            return content.model_dump()
        return str(content)


    async def tool_message_from_call(
        self, 
        tool_call: Any
    ) -> Dict[str, Any]:
        """
        Accepts a Chat Completions tool_call object and returns a
        {"role":"tool", ...} message with serialized content.
        """
        name = tool_call.function.name
        args = json.loads(tool_call.function.arguments or "{}")
        content = await self.call_tool(name, args)
        return {
            "role": "tool",
            "tool_call_id": tool_call.id,
            "content": content,
        }

    async def cleanup(
        self
    ) -> None:
        try:
            await self.exit_stack.aclose()
            self.logger.info("Disconnected from MCP server")
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")
            traceback.print_exc()

