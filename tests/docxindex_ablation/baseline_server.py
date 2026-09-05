"""Explicit baseline entrypoint: does not rely on MCP environment inheritance."""

from tests.docxindex_ablation.service import serve_mcp


if __name__ == "__main__":
    serve_mcp("baseline")
