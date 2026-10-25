"""Keep import-time application logs outside the MCP protocol channel."""

import importlib
import sys
from contextlib import redirect_stdout


def main() -> None:
    with redirect_stdout(sys.stderr):
        server = importlib.import_module("mcp_service.server.server")

    server.mcp.run()


if __name__ == "__main__":
    main()
