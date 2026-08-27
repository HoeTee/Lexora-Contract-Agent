from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from config import (
    API_CALLBACK_ENABLED,
    API_CALLBACK_FILE_FIELD,
    API_CALLBACK_URL,
    DEFAULT_CRITERIA_PATH,
)
from endpoints.review.callbacks import post_api_review_callback
from endpoints.review.job_worker import run_async_review_job
from endpoints.review.meta import build_meta_response_headers, extract_api_meta_fields
from endpoints.review.task_store import (
    create_task,
    ensure_task_dirs,
    output_dir,
    read_task,
    new_task_id,
    save_task_input_copy,
    save_task_input_upload,
    task_api_events_path,
    write_task_log_event,
)
from endpoints.runtime.document_validation import (
    DOCX_MEDIA_TYPE,
    validate_criteria_content,
    validate_uploaded_docx,
)
from endpoints.runtime.filenames import build_report_display_name, safe_upload_filename
from endpoints.runtime.json_response import pretty_json_response


OA_CLIENT_ID = "oa"
OA_CLIENT_DIR = "oa"
oa_router = APIRouter(prefix="/oa")


@oa_router.post("/review")
async def review_for_oa(
    request: Request,
    file: UploadFile = File(...),
    criteria_file: UploadFile | None = File(None),
):
    """Review an OA contract synchronously and return the resulting DOCX.

    Input is multipart form data: ``file``, optional ``criteria_file``, and the
    fields configured through ``api.meta_fields``. The configured metadata is
    forwarded unchanged in the callback request.
    """
    task_id = new_task_id()
    log_path = task_api_events_path(OA_CLIENT_DIR, task_id)
    task_created = False

    try:
        if API_CALLBACK_ENABLED and not API_CALLBACK_URL:
            raise HTTPException(
                status_code=500,
                detail="api.callback_enabled is true but api.callback_url is empty.",
            )

        filename = safe_upload_filename(file.filename)
        if not filename.lower().endswith(".docx"):
            raise HTTPException(status_code=400, detail="Contract file must be DOCX.")

        meta_fields = await extract_api_meta_fields(request)
        ensure_task_dirs(OA_CLIENT_DIR, task_id)
        write_task_log_event(
            OA_CLIENT_DIR,
            task_id,
            "oa_review_received",
            filename=filename,
            meta_fields=meta_fields,
        )

        selected_criteria_path = Path(DEFAULT_CRITERIA_PATH)
        criteria_source = "default"
        criteria_filename = None
        if criteria_file and criteria_file.filename:
            criteria_filename = safe_upload_filename(criteria_file.filename)
            if not criteria_filename.lower().endswith(".docx"):
                raise HTTPException(status_code=400, detail="Review criteria file must be DOCX.")
            selected_criteria_path = save_task_input_upload(
                OA_CLIENT_DIR, task_id, criteria_file, criteria_filename
            )
            validate_uploaded_docx(selected_criteria_path)
            validate_criteria_content(selected_criteria_path)
            criteria_source = "uploaded"
        elif not selected_criteria_path.exists():
            raise HTTPException(status_code=404, detail="Default review criteria file was not found.")
        else:
            selected_criteria_path = save_task_input_copy(
                OA_CLIENT_DIR, task_id, selected_criteria_path, selected_criteria_path.name
            )

        contract_path = save_task_input_upload(OA_CLIENT_DIR, task_id, file, filename)
        validate_uploaded_docx(contract_path)
        response_filename = build_report_display_name(filename)
        result_path = output_dir(OA_CLIENT_DIR, task_id) / response_filename
        create_task(
            client_id=OA_CLIENT_ID,
            client_dir=OA_CLIENT_DIR,
            task_id=task_id,
            contract_filename=filename,
            contract_path=contract_path,
            criteria_source=criteria_source,
            criteria_filename=criteria_filename,
            criteria_path=selected_criteria_path,
            result_filename=response_filename,
            result_path=result_path,
            meta_fields=meta_fields,
        )
        task_created = True

        await run_async_review_job(OA_CLIENT_DIR, task_id)
        task = read_task(OA_CLIENT_DIR, task_id)
        if task is None or task["status"] != "succeeded":
            error = (task or {}).get("error") or {}
            message = error.get("message", "Review failed. Check task logs.")
            raise HTTPException(status_code=500, detail=message)

        output_path = Path(task["output"]["result_path"])
        await post_api_review_callback(
            output_path=output_path,
            response_filename=response_filename,
            meta_fields=meta_fields,
        )
        write_task_log_event(
            OA_CLIENT_DIR,
            task_id,
            "oa_callback_completed",
            enabled=API_CALLBACK_ENABLED,
            url=API_CALLBACK_URL if API_CALLBACK_ENABLED else "",
            file_field=API_CALLBACK_FILE_FIELD,
            meta_fields=meta_fields,
        )
        headers = {
            "x-review-task-id": task_id,
            "x-review-criteria-source": criteria_source,
            **build_meta_response_headers(meta_fields),
        }
        if log_path:
            headers["x-review-log-path"] = str(log_path)
        return FileResponse(
            path=output_path,
            media_type=DOCX_MEDIA_TYPE,
            filename=response_filename,
            headers=headers,
        )
    except HTTPException as exc:
        if task_created:
            write_task_log_event(OA_CLIENT_DIR, task_id, "oa_review_failed", detail=exc.detail)
        return pretty_json_response(
            {
                "task_id": task_id,
                "status": "failed",
                "message": exc.detail,
                "api_events_path": str(log_path) if log_path else None,
            },
            status_code=exc.status_code,
        )
    finally:
        await file.close()
        if criteria_file:
            await criteria_file.close()
