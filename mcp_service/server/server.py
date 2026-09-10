from mcp.server.fastmcp import FastMCP
from contextlib import redirect_stdout
import json
import logging
import os
import sys
from pathlib import Path
import uuid

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# Stdio is the MCP protocol channel. Keep HTTP client diagnostics off the
# child-process streams during repeated model requests.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from tools.document.file_parser import FileParser
from tools.document.report_generator import ReportGenerator
from tools.retrieval.index_retriever import IndexRetriever
from tools.docxindex.retriever import DocxIndexRetriever
from loggers.model_event_context import reset_model_event_path, set_model_event_path
from config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    CONFIG_PATH,
    EMBED_API_KEY,
    EMBED_BASE_URL,
    EMBED_NAME,
    LLM_API_KEY,
    LLM_BASE_URL,
    LLM_ENABLE_THINKING,
    LLM_NAME,
    RERANK_API_KEY,
    RERANK_BASE_URL,
    RERANK_NAME,
    RERANK_PROVIDER,
    RERANK_TOP_N,
    RETRIEVAL_BACKEND,
    SIMILARITY_TOP_K,
    DATA_DIR,
)


mcp = FastMCP("docs-server")
index_retriever = IndexRetriever(
    project_root=PROJECT_ROOT,
    llm_api_key=LLM_API_KEY,
    llm_base_url=LLM_BASE_URL,
    llm_name=LLM_NAME,
    llm_enable_thinking=LLM_ENABLE_THINKING,
    embed_api_key=EMBED_API_KEY,
    embed_base_url=EMBED_BASE_URL,
    embed_name=EMBED_NAME,
    rerank_api_key=RERANK_API_KEY,
    rerank_base_url=RERANK_BASE_URL,
    rerank_name=RERANK_NAME,
    rerank_provider=RERANK_PROVIDER,
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    similarity_top_k=SIMILARITY_TOP_K,
    rerank_top_n=RERANK_TOP_N,
)
docxindex_retriever = None


def _get_docxindex_retriever() -> DocxIndexRetriever:
    global docxindex_retriever
    if docxindex_retriever is None:
        docxindex_retriever = DocxIndexRetriever(
            CONFIG_PATH,
            llm_model=LLM_NAME,
            llm_base_url=LLM_BASE_URL,
            llm_api_key=LLM_API_KEY,
            embedding_model=EMBED_NAME,
            embedding_base_url=EMBED_BASE_URL,
            embedding_api_key=EMBED_API_KEY,
        )
    return docxindex_retriever


# ============ Document Tools ============

@mcp.tool(structured_output=False)
async def ingest_file(file_path: str) -> str:
    """
    Parse a DOCX file, return full markdown content.
    """
    if os.path.splitext(file_path)[1].lower() != ".docx":
        return "Error parsing file: only .docx files are supported"
    try:
        content = FileParser.parse_file(file_path)
    except Exception as e:
        return f"Error parsing file: {str(e)}"

    filename = os.path.basename(file_path)
    return f"File '{filename}' ingested. Content:\n\n{content}"


@mcp.tool(structured_output=False)
async def generate_markdown_report(
    content_json: str,
    contract_name: str,
    elapsed_seconds: float = None,
) -> str:
    """Generate only a markdown report."""
    try:
        with redirect_stdout(sys.stderr):
            path = ReportGenerator.generate_markdown_report(
                content_json,
                contract_name,
                elapsed_seconds=elapsed_seconds,
            )
        return json.dumps({"md": path}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(structured_output=False)
async def generate_docx_report(
    contract_name: str,
    contract_path: str,
    results_json: str | list,
    summary_sections_json: str | dict | None = None,
    region_boundaries_json: str | dict | None = None,
    output_dir: str | None = None,
    output_path: str | None = None,
    annotation_events_path: str | None = None,
) -> str:
    """Generate only the annotated original-contract DOCX."""
    try:
        if not output_dir and not output_path:
            return json.dumps({"error": "output_dir or output_path is required"}, ensure_ascii=False)

        results = json.loads(results_json) if isinstance(results_json, str) else results_json
        summary_sections = (
            json.loads(summary_sections_json)
            if isinstance(summary_sections_json, str) and summary_sections_json
            else summary_sections_json
        )
        region_boundaries = (
            json.loads(region_boundaries_json)
            if isinstance(region_boundaries_json, str) and region_boundaries_json
            else region_boundaries_json
        )
        with redirect_stdout(sys.stderr):
            path = ReportGenerator.generate_docx_report(
                contract_name=contract_name,
                contract_path=contract_path,
                results=results,
                summary_sections=summary_sections,
                region_boundaries=region_boundaries,
                output_dir=output_dir,
                output_path=output_path,
                annotation_events_path=annotation_events_path,
            )
        if not path:
            return json.dumps({"error": "DOCX report generation failed."}, ensure_ascii=False)
        response = {"docx": path}
        if annotation_events_path and os.path.exists(annotation_events_path):
            with open(annotation_events_path, encoding="utf-8") as report_file:
                annotation_report = json.load(report_file)
            response["annotation_events_path"] = annotation_events_path
            response["annotation_stats"] = annotation_report.get("stats", {})
            response["annotation_skipped_count"] = annotation_report.get("skipped_count", 0)
        return json.dumps(response, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(structured_output=False)
async def generate_pdf_report(
    content_json: str,
    contract_name: str,
    elapsed_seconds: float = None,
) -> str:
    """Generate only a PDF report."""
    try:
        with redirect_stdout(sys.stderr):
            path = ReportGenerator.generate_pdf_report(
                content_json,
                contract_name,
                elapsed_seconds=elapsed_seconds,
            )
        return json.dumps({"pdf": path}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ============ Retrieval Tools ============

async def llamaindex_build_index(docx_path: str, api_events_path: str | None = None) -> str:
    """
    Build a temporary in-memory LlamaIndex index for the current contract.
    This does not persist contract vectors to RAG_persist.
    """
    token = set_model_event_path(api_events_path)
    try:
        with redirect_stdout(sys.stderr):
            return await index_retriever.build_contract_llamaindex_index(docx_path)
    finally:
        reset_model_event_path(token)


async def llamaindex_search(query: str, api_events_path: str | None = None) -> str:
    """
    Search the temporary in-memory LlamaIndex index for the current contract.
    Must call llamaindex_build_index first in the same workflow run.
    """
    token = set_model_event_path(api_events_path)
    try:
        with redirect_stdout(sys.stderr):
            return await index_retriever.search_contract_llamaindex(query)
    finally:
        reset_model_event_path(token)


def _write_docxindex_artifacts(artifact_dir: str, artifacts: dict[str, object]) -> None:
    target = Path(artifact_dir).resolve()
    data_root = Path(DATA_DIR).resolve()
    if target != data_root and data_root not in target.parents:
        raise ValueError("docxindex artifact directory must be inside DATA_DIR")
    if target.name != "docxindex" or target.parent.name != "state":
        raise ValueError("docxindex artifacts must be stored under a task state/docxindex directory")
    target.mkdir(parents=True, exist_ok=True)
    for filename, payload in artifacts.items():
        destination = target / filename
        temporary = destination.with_name(f"{destination.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(destination)


async def docxindex_build_index(
    docx_path: str,
    api_events_path: str | None = None,
    artifact_dir: str | None = None,
) -> str:
    """Build the temporary in-memory docxindex index for the current contract."""
    token = set_model_event_path(api_events_path)
    try:
        with redirect_stdout(sys.stderr):
            retriever = _get_docxindex_retriever()
            result = await retriever.build_index(docx_path)
            if artifact_dir:
                _write_docxindex_artifacts(artifact_dir, retriever.artifact_snapshot())
            return result
    finally:
        reset_model_event_path(token)


async def docxindex_search(
    query: str,
    api_events_path: str | None = None,
    route_override: dict | None = None,
    criterion_id: str | None = None,
    return_metadata: bool = False,
) -> str:
    """Search the temporary in-memory docxindex index for the current contract."""
    token = set_model_event_path(api_events_path)
    try:
        with redirect_stdout(sys.stderr):
            return await _get_docxindex_retriever().search(
                query,
                route_override=route_override,
                criterion_id=criterion_id,
                return_metadata=return_metadata,
            )
    finally:
        reset_model_event_path(token)


async def contract_build_index(
    docx_path: str,
    api_events_path: str | None = None,
    artifact_dir: str | None = None,
) -> str:
    """Build the configured temporary contract retrieval index."""
    if RETRIEVAL_BACKEND == "docxindex":
        return await docxindex_build_index(docx_path, api_events_path, artifact_dir)
    return await llamaindex_build_index(docx_path, api_events_path)


async def contract_search(
    query: str,
    api_events_path: str | None = None,
    route_override: dict | None = None,
    criterion_id: str | None = None,
    return_metadata: bool = False,
) -> str:
    """Search the configured temporary contract retrieval index."""
    if RETRIEVAL_BACKEND == "docxindex":
        return await docxindex_search(
            query,
            api_events_path,
            route_override=route_override,
            criterion_id=criterion_id,
            return_metadata=return_metadata,
        )
    return await llamaindex_search(query, api_events_path)


mcp.tool(structured_output=False)(llamaindex_build_index)
mcp.tool(structured_output=False)(llamaindex_search)
mcp.tool(structured_output=False)(docxindex_build_index)
mcp.tool(structured_output=False)(docxindex_search)
mcp.tool(structured_output=False)(contract_build_index)
mcp.tool(structured_output=False)(contract_search)


if __name__ == "__main__":
    mcp.run()
