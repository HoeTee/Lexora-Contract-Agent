from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from json import JSONDecodeError
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from config import (
    API_REQUIRE_REQUEST_MODEL_CONFIG,
    API_KEEP_OUTPUT,
    API_RESULT_OUTPUT_DEFAULT,
    API_RESULT_UPLOAD_ENABLED,
    API_URL_DOWNLOAD_MAX_BYTES,
    API_URL_DOWNLOAD_TIMEOUT_SECONDS,
    QUEUE_ENABLED,
    infer_rerank_provider,
)
from endpoints.api.client_mapping import ApiClient, resolve_api_client
from endpoints.review.job_worker import run_async_review_job
from endpoints.review.meta import (
    MetaFieldsValidationError,
    extract_configured_meta_fields,
)
from endpoints.review.response import (
    present_review_task,
    present_submit_response,
)
from endpoints.review.result_upload import ResultUploadError, upload_result_file
from endpoints.review.task_store import (
    create_task,
    ensure_task_dirs,
    mark_failed,
    new_task_id,
    output_dir,
    read_task,
    request_cancel,
    save_task_model_config,
    save_task_input_url,
    save_task_input_upload,
    write_task_log_event,
)
from endpoints.runtime.document_validation import validate_criteria_content, validate_uploaded_docx
from endpoints.runtime.filenames import build_report_display_name, safe_upload_filename
from endpoints.runtime.json_response import pretty_json_response


api_jobs_router = APIRouter()
MODEL_CONFIG_FIELDS = (
    "llm_api_key",
    "llm_model_name",
    "embedding_api_key",
    "embedding_model_name",
    "reranker_api_key",
    "reranker_model_name",
)


@dataclass(frozen=True)
class PreparedReviewSubmission:
    task_id: str
    filename: str
    contract_path: Path
    criteria_source: str
    criteria_filename: str | None
    criteria_path: Path | None
    request_model_config: dict[str, str] | None
    meta_fields: dict[str, str]


def _is_upload_file(value: object) -> bool:
    return hasattr(value, "filename") and hasattr(value, "file")


def _filename_from_url(file_url: str, fallback: str) -> str:
    raw_name = Path(urlsplit(file_url).path).name
    if not raw_name.lower().endswith(".docx"):
        return fallback
    return safe_upload_filename(raw_name)


def _url_for_log(file_url: str) -> str:
    parsed = urlsplit(file_url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _download_http_status(exc: Exception) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return None


def _extract_request_model_config(
    data: dict | object,
) -> tuple[dict[str, str] | None, list[str], str | None]:
    if not API_REQUIRE_REQUEST_MODEL_CONFIG:
        return None, [], None

    model_config: dict[str, str] = {}
    missing: list[str] = []
    for field in MODEL_CONFIG_FIELDS:
        value = data.get(field) if hasattr(data, "get") else None
        if not isinstance(value, str) or not value.strip():
            missing.append(field)
        else:
            model_config[field] = value.strip()
    if missing:
        return None, missing, None

    try:
        model_config["reranker_provider"] = infer_rerank_provider(model_config["reranker_model_name"])
    except RuntimeError as exc:
        return None, [], str(exc)
    return model_config, [], None


def _model_config_meta(model_config: dict[str, str] | None) -> dict[str, object]:
    if not model_config:
        return {
            "source": "env",
            "llm": False,
            "embedding": False,
            "reranker": False,
        }
    return {
        "source": "request",
        "llm": bool(model_config.get("llm_api_key")),
        "embedding": bool(model_config.get("embedding_api_key")),
        "reranker": bool(model_config.get("reranker_api_key")),
        "llm_model_name": model_config.get("llm_model_name"),
        "embedding_model_name": model_config.get("embedding_model_name"),
        "reranker_model_name": model_config.get("reranker_model_name"),
        "reranker_provider": model_config.get("reranker_provider"),
    }


def _submit_error_response(
    *,
    task_id: str,
    status_code: int,
    code: str,
    message: str,
    http_status: int | None = None,
    include_http_status: bool = False,
):
    error = {
        "code": code,
        "message": message,
    }
    if include_http_status or http_status is not None:
        error["http_status"] = http_status
    return pretty_json_response(
        {
            "task_id": task_id,
            "status": "failed",
            "message": message,
            "error": error,
        },
        status_code=status_code,
    )


def _pre_task_error_response(
    *,
    status_code: int,
    code: str,
    message: str,
):
    return pretty_json_response(
        {
            "message": message,
            "error": {
                "code": code,
                "message": message,
            },
        },
        status_code=status_code,
    )


def _validation_error_response(*, task_id: str, exc: HTTPException):
    return _submit_error_response(
        task_id=task_id,
        status_code=exc.status_code,
        code="INPUT_VALIDATION_FAILED",
        message=str(exc.detail),
    )


async def _result_output_type(request: Request) -> str:
    payload = await _json_body(request, allow_empty=True, context="Result request")
    if payload is None:
        return API_RESULT_OUTPUT_DEFAULT
    output_type = payload.get("output_type", API_RESULT_OUTPUT_DEFAULT)
    return _validate_result_output_type(output_type)


def _validate_result_output_type(output_type: object) -> str:
    if not isinstance(output_type, str):
        raise HTTPException(status_code=400, detail="output_type must be 'file' or 'url'.")
    output_type = output_type.strip().lower()
    if output_type not in {"file", "url"}:
        raise HTTPException(status_code=400, detail="output_type must be 'file' or 'url'.")
    return output_type


async def _json_body(
    request: Request,
    *,
    allow_empty: bool,
    context: str,
) -> dict | None:
    body = await request.body()
    if not body:
        if allow_empty:
            return None
        raise HTTPException(status_code=400, detail=f"{context} JSON body is required.")

    content_type = request.headers.get("content-type", "").lower()
    if content_type and not content_type.startswith("application/json"):
        raise HTTPException(status_code=415, detail=f"{context} body must be application/json.")

    try:
        payload = json.loads(body)
    except JSONDecodeError:
        raise HTTPException(status_code=400, detail=f"{context} JSON body must be valid.") from None
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail=f"{context} JSON body must be an object.")
    return payload


def _task_id_from_payload(payload: dict) -> str:
    task_id = payload.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        raise HTTPException(status_code=400, detail="task_id is required.")
    return task_id.strip()


def _read_api_task_or_404(client_dir: str, task_id: str) -> dict:
    try:
        task = read_task(client_dir, task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found.") from None
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")
    return task


async def _export_review_job_result(
    *,
    client_dir: str,
    task: dict,
    output_type: str,
):
    status = task["status"]
    if status != "succeeded":
        raise HTTPException(status_code=409, detail=f"Task is not finished. Current status: {status}")

    task_id = task["task_id"]
    result_path = Path(task["output"]["result_path"])
    if not result_path.exists():
        raise HTTPException(status_code=500, detail="Result file does not exist.")

    if output_type == "url":
        if not API_RESULT_UPLOAD_ENABLED:
            raise HTTPException(status_code=400, detail="Result URL output is disabled.")
        try:
            result_url = await upload_result_file(result_path, result_path.name)
        except HTTPException as exc:
            write_task_log_event(
                client_dir,
                task_id,
                "result_url_upload_failed",
                file_path=str(result_path),
                size_bytes=result_path.stat().st_size,
                http_status=None,
                error=str(exc.detail),
            )
            raise
        except ResultUploadError as exc:
            write_task_log_event(
                client_dir,
                task_id,
                "result_url_upload_failed",
                file_path=str(result_path),
                size_bytes=result_path.stat().st_size,
                http_status=exc.http_status,
                error=str(exc),
            )
            raise HTTPException(status_code=502, detail="Result file URL upload failed.") from exc

        write_task_log_event(
            client_dir,
            task_id,
            "result_url_uploaded",
            file_path=str(result_path),
            size_bytes=result_path.stat().st_size,
            http_status=200,
            url=result_url,
        )
        if not API_KEEP_OUTPUT:
            result_path.unlink(missing_ok=True)
        return pretty_json_response(
            {
                "task_id": task["task_id"],
                "status": task["status"],
                "output_type": "url",
                "filename": result_path.name,
                "url": result_url,
            }
        )

    return FileResponse(
        path=result_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=result_path.name,
    )


@api_jobs_router.post("/api/review/jobs", status_code=202)
async def submit_review_job(request: Request):
    client = await resolve_api_client(request)
    return await _prepare_and_dispatch_submission(request, client)


async def _prepare_and_dispatch_submission(request: Request, client: ApiClient):
    content_type = request.headers.get("content-type", "").lower()

    criteria_source = "mandatory"
    criteria_filename = None
    selected_criteria_path: Path | None = None
    request_model_config = None
    meta_fields: dict[str, str] = {}

    if content_type.startswith("multipart/form-data"):
        form = await request.form()
        request_model_config, missing_model_config, invalid_model_config = _extract_request_model_config(form)
        if missing_model_config:
            return _pre_task_error_response(
                status_code=400,
                code="MODEL_CONFIG_REQUIRED",
                message=f"Missing request model config fields: {', '.join(missing_model_config)}.",
            )
        if invalid_model_config:
            return _pre_task_error_response(
                status_code=400,
                code="MODEL_CONFIG_INVALID",
                message=invalid_model_config,
            )
        if "file_url" in form or "criteria_file_url" in form:
            return _pre_task_error_response(
                status_code=400,
                code="INVALID_CONTENT_TYPE_FIELDS",
                message="URL fields require application/json.",
            )
        file = form.get("file")
        if not _is_upload_file(file) or not getattr(file, "filename", ""):
            return _pre_task_error_response(
                status_code=400,
                code="FILE_REQUIRED",
                message="file is required.",
            )

        filename = safe_upload_filename(file.filename)
        if not filename.lower().endswith(".docx"):
            return _pre_task_error_response(
                status_code=400,
                code="CONTRACT_FILE_NOT_DOCX",
                message="Contract file must be DOCX.",
            )

        criteria_file = form.get("criteria_file")
        if _is_upload_file(criteria_file) and getattr(criteria_file, "filename", ""):
            criteria_filename = safe_upload_filename(criteria_file.filename)
            if not criteria_filename.lower().endswith(".docx"):
                return _pre_task_error_response(
                    status_code=400,
                    code="CRITERIA_FILE_NOT_DOCX",
                    message="Review criteria file must be DOCX.",
                )

        try:
            meta_fields = extract_configured_meta_fields(form)
        except MetaFieldsValidationError as exc:
            return _pre_task_error_response(
                status_code=400,
                code=exc.code,
                message=exc.message,
            )

        task_id = new_task_id()
        ensure_task_dirs(client.client_dir, task_id)

        contract_path = save_task_input_upload(
            client.client_dir,
            task_id=task_id,
            upload_file=file,
            filename=filename,
        )
        write_task_log_event(
            client.client_dir,
            task_id,
            "contract_saved",
            file_path=str(contract_path),
            size_bytes=contract_path.stat().st_size,
        )
        try:
            validate_uploaded_docx(contract_path)
        except HTTPException as exc:
            return _validation_error_response(task_id=task_id, exc=exc)

        if _is_upload_file(criteria_file) and getattr(criteria_file, "filename", ""):
            selected_criteria_path = save_task_input_upload(
                client.client_dir,
                task_id=task_id,
                upload_file=criteria_file,
                filename=criteria_filename,
            )
            write_task_log_event(
                client.client_dir,
                task_id,
                "criteria_uploaded",
                file_path=str(selected_criteria_path),
                original_filename=criteria_filename,
                size_bytes=selected_criteria_path.stat().st_size,
            )
            try:
                validate_uploaded_docx(selected_criteria_path)
                validate_criteria_content(selected_criteria_path)
            except HTTPException as exc:
                return _validation_error_response(task_id=task_id, exc=exc)
            criteria_source = "uploaded"
    elif content_type.startswith("application/json"):
        try:
            payload = await request.json()
        except JSONDecodeError:
            return _pre_task_error_response(
                status_code=400,
                code="INVALID_JSON_BODY",
                message="JSON body must be valid.",
            )
        if not isinstance(payload, dict):
            return _pre_task_error_response(
                status_code=400,
                code="INVALID_JSON_BODY",
                message="JSON body must be an object.",
            )

        request_model_config, missing_model_config, invalid_model_config = _extract_request_model_config(payload)
        if missing_model_config:
            return _pre_task_error_response(
                status_code=400,
                code="MODEL_CONFIG_REQUIRED",
                message=f"Missing request model config fields: {', '.join(missing_model_config)}.",
            )
        if invalid_model_config:
            return _pre_task_error_response(
                status_code=400,
                code="MODEL_CONFIG_INVALID",
                message=invalid_model_config,
            )
        try:
            meta_fields = extract_configured_meta_fields(payload)
        except MetaFieldsValidationError as exc:
            return _pre_task_error_response(
                status_code=400,
                code=exc.code,
                message=exc.message,
            )

        file_url = payload.get("file_url")
        if not isinstance(file_url, str) or not file_url.strip():
            return _pre_task_error_response(
                status_code=400,
                code="FILE_URL_REQUIRED",
                message="file_url is required.",
            )
        file_url = file_url.strip()

        criteria_file_url = payload.get("criteria_file_url")
        if criteria_file_url is not None:
            if not isinstance(criteria_file_url, str) or not criteria_file_url.strip():
                return _pre_task_error_response(
                    status_code=400,
                    code="CRITERIA_FILE_URL_INVALID",
                    message="criteria_file_url must be a non-empty string.",
                )
            criteria_file_url = criteria_file_url.strip()

        task_id = new_task_id()
        ensure_task_dirs(client.client_dir, task_id)

        filename = _filename_from_url(file_url, "contract_from_url.docx")
        try:
            contract_path = save_task_input_url(
                client.client_dir,
                task_id,
                file_url,
                filename,
                timeout_seconds=API_URL_DOWNLOAD_TIMEOUT_SECONDS,
                max_bytes=API_URL_DOWNLOAD_MAX_BYTES,
            )
        except ValueError as exc:
            write_task_log_event(
                client.client_dir,
                task_id,
                "contract_url_download_failed",
                source_url=_url_for_log(file_url),
                http_status=None,
                error=str(exc),
            )
            return _submit_error_response(
                task_id=task_id,
                status_code=400,
                code="CONTRACT_URL_DOWNLOAD_INVALID",
                message=str(exc),
            )
        except httpx.HTTPError as exc:
            http_status = _download_http_status(exc)
            write_task_log_event(
                client.client_dir,
                task_id,
                "contract_url_download_failed",
                source_url=_url_for_log(file_url),
                http_status=http_status,
                error=str(exc),
            )
            return _submit_error_response(
                task_id=task_id,
                status_code=502,
                code="CONTRACT_URL_DOWNLOAD_FAILED",
                message="Contract file URL download failed.",
                http_status=http_status,
                include_http_status=True,
            )
        filename = contract_path.name
        write_task_log_event(
            client.client_dir,
            task_id,
            "contract_url_downloaded",
            source_url=_url_for_log(file_url),
            file_path=str(contract_path),
            size_bytes=contract_path.stat().st_size,
        )
        try:
            validate_uploaded_docx(contract_path)
        except HTTPException as exc:
            return _validation_error_response(task_id=task_id, exc=exc)

        if criteria_file_url is not None:
            criteria_filename = _filename_from_url(criteria_file_url, "criteria_from_url.docx")
            try:
                selected_criteria_path = save_task_input_url(
                    client.client_dir,
                    task_id,
                    criteria_file_url,
                    criteria_filename,
                    timeout_seconds=API_URL_DOWNLOAD_TIMEOUT_SECONDS,
                    max_bytes=API_URL_DOWNLOAD_MAX_BYTES,
                )
            except ValueError as exc:
                write_task_log_event(
                    client.client_dir,
                    task_id,
                    "criteria_url_download_failed",
                    source_url=_url_for_log(criteria_file_url),
                    http_status=None,
                    error=str(exc),
                )
                return _submit_error_response(
                    task_id=task_id,
                    status_code=400,
                    code="CRITERIA_URL_DOWNLOAD_INVALID",
                    message=str(exc),
                )
            except httpx.HTTPError as exc:
                http_status = _download_http_status(exc)
                write_task_log_event(
                    client.client_dir,
                    task_id,
                    "criteria_url_download_failed",
                    source_url=_url_for_log(criteria_file_url),
                    http_status=http_status,
                    error=str(exc),
                )
                return _submit_error_response(
                    task_id=task_id,
                    status_code=502,
                    code="CRITERIA_URL_DOWNLOAD_FAILED",
                    message="Review criteria file URL download failed.",
                    http_status=http_status,
                    include_http_status=True,
                )
            criteria_filename = selected_criteria_path.name
            write_task_log_event(
                client.client_dir,
                task_id,
                "criteria_url_downloaded",
                source_url=_url_for_log(criteria_file_url),
                file_path=str(selected_criteria_path),
                size_bytes=selected_criteria_path.stat().st_size,
            )
            try:
                validate_uploaded_docx(selected_criteria_path)
                validate_criteria_content(selected_criteria_path)
            except HTTPException as exc:
                return _validation_error_response(task_id=task_id, exc=exc)
            criteria_source = "uploaded"
    else:
        return _pre_task_error_response(
            status_code=415,
            code="UNSUPPORTED_CONTENT_TYPE",
            message="Unsupported Content-Type.",
        )

    submission = PreparedReviewSubmission(
        task_id=task_id,
        filename=filename,
        contract_path=contract_path,
        criteria_source=criteria_source,
        criteria_filename=criteria_filename,
        criteria_path=selected_criteria_path,
        request_model_config=request_model_config,
        meta_fields=meta_fields,
    )
    task = _create_submission_task(client, submission)
    dispatch_error = _dispatch_review_job(client.client_dir, task_id)
    if dispatch_error is not None:
        return dispatch_error
    return pretty_json_response(present_submit_response(task), status_code=202)


def _create_submission_task(client: ApiClient, submission: PreparedReviewSubmission) -> dict:
    result_filename = build_report_display_name(submission.filename)
    result_path = output_dir(client.client_dir, submission.task_id) / result_filename
    task = create_task(
        client_id=client.client_id,
        client_dir=client.client_dir,
        task_id=submission.task_id,
        contract_filename=submission.filename,
        contract_path=submission.contract_path,
        criteria_source=submission.criteria_source,
        criteria_filename=submission.criteria_filename,
        criteria_path=submission.criteria_path,
        result_filename=result_filename,
        result_path=result_path,
        model_config_meta=_model_config_meta(submission.request_model_config),
        meta_fields=submission.meta_fields,
    )
    if submission.request_model_config:
        config = submission.request_model_config
        save_task_model_config(client.client_dir, submission.task_id, config)
        write_task_log_event(
            client.client_dir,
            submission.task_id,
            "request_model_config_received",
            llm=True,
            embedding=True,
            reranker=True,
            llm_model_name=config["llm_model_name"],
            embedding_model_name=config["embedding_model_name"],
            reranker_model_name=config["reranker_model_name"],
            reranker_provider=config["reranker_provider"],
        )
    write_task_log_event(
        client.client_dir,
        submission.task_id,
        "api_review_job_received",
        filename=submission.filename,
        client_id=client.client_id,
        source_ip=client.source_ip,
    )
    return task


def _dispatch_review_job(client_dir: str, task_id: str):
    if not QUEUE_ENABLED:
        asyncio.create_task(run_async_review_job(client_dir, task_id))
        return None
    try:
        from task_queue.review_queue import enqueue_review_job

        queue_task_id = enqueue_review_job(client_dir, task_id)
    except Exception as exc:
        write_task_log_event(client_dir, task_id, "review_queue_submit_failed", error=repr(exc))
        mark_failed(
            client_dir,
            task_id,
            code="QUEUE_SUBMIT_FAILED",
            message="Review job queue submission failed.",
            error={
                "code": "QUEUE_SUBMIT_FAILED",
                "message": "Review job queue submission failed.",
                "detail": repr(exc),
            },
        )
        return _submit_error_response(
            task_id=task_id,
            status_code=503,
            code="QUEUE_SUBMIT_FAILED",
            message="Review job queue submission failed.",
        )
    write_task_log_event(client_dir, task_id, "review_queued", queue_task_id=queue_task_id)
    return None


@api_jobs_router.get("/api/review/jobs/{task_id}")
async def get_review_job(request: Request, task_id: str):
    client = await resolve_api_client(request)
    task = _read_api_task_or_404(client.client_dir, task_id)
    return pretty_json_response(present_review_task(task, include_meta_fields=True))


@api_jobs_router.post("/api/review/jobs/status")
async def get_review_job_by_body(request: Request):
    client = await resolve_api_client(request)
    payload = await _json_body(request, allow_empty=False, context="Status request")
    task_id = _task_id_from_payload(payload)
    task = _read_api_task_or_404(client.client_dir, task_id)
    return pretty_json_response(present_review_task(task, include_meta_fields=True))


@api_jobs_router.post("/api/review/jobs/{task_id}/result")
async def export_review_job_result(
    request: Request,
    task_id: str,
):
    client = await resolve_api_client(request)
    task = _read_api_task_or_404(client.client_dir, task_id)
    output_type = await _result_output_type(request)
    return await _export_review_job_result(
        client_dir=client.client_dir,
        task=task,
        output_type=output_type,
    )


@api_jobs_router.post("/api/review/jobs/result")
async def export_review_job_result_by_body(request: Request):
    client = await resolve_api_client(request)
    payload = await _json_body(request, allow_empty=False, context="Result request")
    task_id = _task_id_from_payload(payload)
    task = _read_api_task_or_404(client.client_dir, task_id)
    output_type = _validate_result_output_type(payload.get("output_type", API_RESULT_OUTPUT_DEFAULT))
    return await _export_review_job_result(
        client_dir=client.client_dir,
        task=task,
        output_type=output_type,
    )


@api_jobs_router.post("/api/review/jobs/{task_id}/cancel")
async def cancel_review_job(request: Request, task_id: str):
    client = await resolve_api_client(request)
    try:
        task = read_task(client.client_dir, task_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="Task not found.") from None
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found.")

    if task["status"] == "running":
        return pretty_json_response(
            {
                "task_id": task["task_id"],
                "status": task["status"],
                "message": "Review job has already started and cannot be cancelled.",
            },
            status_code=409,
        )

    if task["status"] in {"succeeded", "failed", "cancelled"}:
        raise HTTPException(status_code=409, detail="Task is already finished and cannot be cancelled.")

    updated = request_cancel(client.client_dir, task_id)
    return pretty_json_response(
        {
            "task_id": updated["task_id"],
            "status": updated["status"],
            "message": updated["message"],
        }
    )
