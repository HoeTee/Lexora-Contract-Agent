"""HTTP API client for submitting a contract to the shared Celery workers."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack, ExitStack
import math
import os
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

from endpoints.runtime.filenames import build_report_display_name


DOCX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
CLI_WAIT_TIMEOUT_SECONDS = 9000


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Submit a contract to the Web/API service and download its report.")
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
    parser.add_argument("--base-url", help="API service URL; defaults to REVIEW_API_URL or http://127.0.0.1:8000.")
    parser.add_argument("--api-key", help="Platform API key. The API_KEY environment variable takes precedence.")
    parser.add_argument("--template-code", help="Value submitted as templateCode; required when the API requires it.")
    parser.add_argument("--serial-no", help="Value submitted as serialNo; required when the API requires it.")
    parser.add_argument("--wait-timeout-seconds", type=float, default=CLI_WAIT_TIMEOUT_SECONDS,
                        help="Client wait limit, including upload/download (default: 9000). Does not cancel the server task; increase if needed.")
    return parser


def resolve_existing_docx(path_value: str, label: str) -> Path:
    path = Path(path_value).expanduser().resolve()
    if not path.is_file():
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


async def _download_report(client: httpx.AsyncClient, endpoint: str, headers: dict[str, str], output_path: Path) -> None:
    temporary: Path | None = None
    try:
        async with client.stream("POST", endpoint, headers=headers, json={"output_type": "file"}) as response:
            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if media_type not in {DOCX_MEDIA_TYPE, "application/octet-stream"}:
                raise RuntimeError(f"Expected a DOCX report, received content-type {media_type!r}.")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=output_path.parent, prefix=".review-download-", delete=False) as file:
                temporary = Path(file.name)
                async for chunk in response.aiter_bytes():
                    file.write(chunk)
            temporary.replace(output_path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def run_cli(
    contract: str,
    criteria: str | None = None,
    output: str | None = None,
    *,
    base_url: str | None = None,
    api_key: str | None = None,
    template_code: str | None = None,
    serial_no: str | None = None,
    wait_timeout_seconds: float = CLI_WAIT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = 2,
    client: httpx.AsyncClient | None = None,
) -> dict:
    """Upload local files and return the successful API task plus final_report_docx.

    The client only waits for the service; it does not execute models or cancel
    the server task when its own wait limit expires. An injected client remains open.
    """
    contract_path = resolve_existing_docx(contract, "Contract")
    criteria_path = resolve_existing_docx(criteria, "Review criteria") if criteria else None
    output_path = resolve_output_path(contract_path, output)
    if not math.isfinite(wait_timeout_seconds) or wait_timeout_seconds <= 0:
        raise ValueError("Client wait timeout must be a finite positive number.")
    if not math.isfinite(poll_interval_seconds) or poll_interval_seconds < 0:
        raise ValueError("Polling interval must be a finite nonnegative number.")
    service_url = (base_url or os.getenv("REVIEW_API_URL") or "http://127.0.0.1:8000").strip().rstrip("/")
    try:
        parsed_url = httpx.URL(service_url)
    except httpx.InvalidURL as exc:
        raise ValueError("Invalid API service URL.") from exc
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.host or parsed_url.query or parsed_url.fragment:
        raise ValueError("API service URL must use http:// or https:// with a host and no query or fragment.")
    key = (os.getenv("API_KEY") or api_key or "").strip()
    if not key:
        raise ValueError("Provide the platform API key through API_KEY or --api-key.")
    headers = {"Authorization": key if key.lower().startswith("bearer ") else "Bearer " + key}
    data = {}
    if template_code is not None:
        data["templateCode"] = template_code
    if serial_no is not None:
        data["serialNo"] = serial_no
    jobs_endpoint = service_url + "/api/review/jobs"
    task_id = None
    try:
        async with asyncio.timeout(wait_timeout_seconds), AsyncExitStack() as stack:
            if client is None:
                client = await stack.enter_async_context(httpx.AsyncClient(timeout=60))
            with ExitStack() as files_stack:
                files = {"file": (contract_path.name, files_stack.enter_context(contract_path.open("rb")), DOCX_MEDIA_TYPE)}
                if criteria_path:
                    files["criteria_file"] = (criteria_path.name, files_stack.enter_context(criteria_path.open("rb")), DOCX_MEDIA_TYPE)
                response = await client.post(jobs_endpoint, headers=headers, data=data, files=files)
            response.raise_for_status()
            submitted = response.json()
            if not isinstance(submitted, dict) or not isinstance(submitted.get("task_id"), str) or not submitted["task_id"]:
                raise RuntimeError("Submission response did not contain a task_id.")
            task_id = submitted["task_id"]
            task_endpoint = jobs_endpoint + "/" + quote(task_id, safe="")
            while True:
                response = await client.get(task_endpoint, headers=headers)
                response.raise_for_status()
                result = response.json()
                if not isinstance(result, dict):
                    raise RuntimeError("Task status response must be a JSON object.")
                status = result.get("status")
                if status == "succeeded":
                    await _download_report(client, task_endpoint + "/result", headers, output_path)
                    result["final_report_docx"] = str(output_path)
                    print(f"Final report: {output_path}")
                    return result
                if status in {"failed", "cancelled"}:
                    raise RuntimeError(f"Review task {task_id} {status}: {result.get('error') or result.get('message') or status}")
                if status not in {"pending", "queued", "running"}:
                    raise RuntimeError(f"Task {task_id} returned unknown status {status!r}.")
                await asyncio.sleep(poll_interval_seconds)
    except TimeoutError as exc:
        identity = f" for task {task_id}" if task_id else ""
        raise TimeoutError(f"Client wait limit expired{identity}; any submitted server task continues running.") from exc


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        asyncio.run(run_cli(args.contract, criteria=args.criteria, output=args.output,
                            base_url=args.base_url, api_key=args.api_key, template_code=args.template_code,
                            serial_no=args.serial_no, wait_timeout_seconds=args.wait_timeout_seconds))
    except (OSError, ValueError, RuntimeError, httpx.HTTPError) as exc:
        parser.exit(status=1, message=f"Review failed: {exc}\n")


if __name__ == "__main__":
    main()
