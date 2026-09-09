"""
Local CLI entry point for the contract review workflow.
"""
from __future__ import annotations

import argparse
import asyncio
import shutil
import tempfile
from pathlib import Path

from config import MCP_SERVER_TARGET
from endpoints.runtime.document_validation import validate_criteria_content, validate_uploaded_docx
from endpoints.runtime.filenames import build_report_display_name
from loggers.agent_logger import reset_conversation_log_dir, set_conversation_log_dir
from workflow.workflow import ContractReviewWorkflow


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local contract review.")
    parser.add_argument(
        "--contract",
        required=True,
        help="Contract DOCX path.",
    )
    parser.add_argument(
        "--criteria",
        help="Optional user review criteria DOCX path. Mandatory criteria always come from criteria-routes.yaml.",
    )
    parser.add_argument(
        "--output",
        help="Optional output DOCX path. Defaults to the current working directory.",
    )
    return parser


def resolve_existing_docx(path_value: str, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"{label} file was not found: {path}")
    if path.suffix.lower() != ".docx":
        raise ValueError(f"{label} file must be DOCX: {path}")
    return path


def resolve_output_path(contract_path: Path, output: str | None) -> Path:
    if output:
        path = Path(output).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        if path.suffix.lower() != ".docx":
            raise ValueError(f"Output path must end with .docx: {path}")
        return path.resolve()
    return (Path.cwd() / build_report_display_name(contract_path.name)).resolve()


async def run_cli(contract: str, criteria: str | None = None, output: str | None = None) -> dict:
    contract_path = resolve_existing_docx(contract, "Contract")
    criteria_path = resolve_existing_docx(criteria, "Review criteria") if criteria else None
    output_path = resolve_output_path(contract_path, output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    validate_uploaded_docx(contract_path)
    if criteria_path:
        validate_uploaded_docx(criteria_path)
        validate_criteria_content(criteria_path)

    with tempfile.TemporaryDirectory(prefix="contract-review-cli-") as temp_dir:
        temp_root = Path(temp_dir)
        logs_dir = temp_root / "logs"
        conversation_log_dir = temp_root / "logs" / "conversations"
        trace_path = temp_root / "logs" / "trace.json"
        review_outputs_path = temp_root / "logs" / "review_outputs.json"
        for path in (logs_dir, conversation_log_dir):
            path.mkdir(parents=True, exist_ok=True)

        temp_output_path = temp_root / output_path.name
        token = set_conversation_log_dir(conversation_log_dir)
        try:
            workflow = ContractReviewWorkflow(
                server_target=MCP_SERVER_TARGET,
                conversation_log_dir=str(conversation_log_dir),
                mcp_log_file=str(logs_dir / "mcp.log"),
                trace_path=str(trace_path),
                review_outputs_path=str(review_outputs_path),
            )
            result = await workflow.run(
                contract_path=str(contract_path),
                criteria_path=str(criteria_path) if criteria_path else None,
                output_path=str(temp_output_path),
            )
        finally:
            reset_conversation_log_dir(token)

        generated_path = Path(result["report_docx"])
        if not generated_path.exists():
            raise RuntimeError("Output DOCX file was not found.")
        shutil.copy2(generated_path, output_path)

    result["final_report_docx"] = str(output_path)
    print(f"Final report: {output_path}")
    return result


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(run_cli(args.contract, criteria=args.criteria, output=args.output))


if __name__ == "__main__":
    main()
