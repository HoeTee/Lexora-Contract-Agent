from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from mcp_service.client.client import MinimalMCPClient  # noqa: E402


async def run(docx_path: str) -> None:
    client = MinimalMCPClient("module:mcp_service.server.server")
    await client.connect()
    try:
        result = await client.call_tool("docxindex_build_index", {"docx_path": docx_path})
        print(result)
    finally:
        await client.cleanup()


def main() -> None:
    parser = argparse.ArgumentParser(description="通过真实 stdio MCP 构建 DocxIndex。")
    parser.add_argument("docx_path")
    args = parser.parse_args()
    asyncio.run(run(args.docx_path))


if __name__ == "__main__":
    main()
