from __future__ import annotations

import argparse
import asyncio
import base64
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_service.client.client import MinimalMCPClient  # noqa: E402


async def run(
    docx_path: str,
    query: str | None,
    route_json: str | None,
    route_base64: str | None,
) -> None:
    client = MinimalMCPClient("module:mcp_service.server.server")
    await client.connect()
    try:
        result = await client.call_tool("docxindex_build_index", {"docx_path": docx_path})
        print(result)
        if isinstance(result, str):
            try:
                payload = json.loads(result)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and payload.get("error"):
                raise RuntimeError(f"DocxIndex build failed: {payload['error']}")
        if query:
            encoded_route = base64.b64decode(route_base64).decode("utf-8") if route_base64 else route_json
            route_override = json.loads(encoded_route) if encoded_route else None
            search_result = await client.call_tool(
                "contract_search",
                {
                    "query": query,
                    "route_override": route_override,
                    "criterion_id": "smoke",
                    "return_metadata": True,
                },
            )
            print(search_result)
            if isinstance(search_result, str) and search_result.startswith("Error"):
                raise RuntimeError(search_result)
    finally:
        await client.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="通过真实 stdio MCP 构建 DocxIndex。")
    parser.add_argument("docx_path")
    parser.add_argument("--query")
    parser.add_argument("--route-json")
    parser.add_argument("--route-base64")
    args = parser.parse_args()
    asyncio.run(run(args.docx_path, args.query, args.route_json, args.route_base64))


if __name__ == "__main__":
    main()
