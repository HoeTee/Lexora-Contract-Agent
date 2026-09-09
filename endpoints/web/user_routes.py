from __future__ import annotations

import asyncio
import secrets
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import DATA_DIR, PROJECT_ROOT
from loggers.resolve_review_task_paths import resolve_review_task_paths
from loggers.review_history import (
    HISTORY_SCHEMA_VERSION,
    format_file_size,
    format_timestamp,
    load_history_records,
)
from endpoints.review.job_worker import run_async_review_job
from endpoints.review.task_store import (
    create_task,
    ensure_task_dirs,
    read_task,
    save_task_input_upload,
    update_task,
    web_client_dir,
    write_task_log_event,
    now_iso,
)
from endpoints.runtime.auth import find_user, load_users, normalize_role, save_users, verify_login, verify_password
from endpoints.runtime.tenancy import TenantError, require_tenant, tenant_profiles_file
from endpoints.runtime.document_validation import (
    DOCX_MEDIA_TYPE,
    validate_criteria_content,
    validate_uploaded_docx,
)
from endpoints.runtime.filenames import build_report_display_name, safe_upload_filename, strip_task_file_prefix


templates = Jinja2Templates(directory=str(Path(PROJECT_ROOT) / "frontend" / "templates"))
user_router = APIRouter()


def task_owner_key(tenant_id: str, username: str) -> str:
    return f"{tenant_id}:{username}"


def get_latest_web_task(username: str, tenant_id: str) -> dict | None:
    if not tenant_id:
        return None
    root = Path(DATA_DIR) / "web" / tenant_id
    if not root.exists():
        return None
    client_dir = web_client_dir(tenant_id)
    tasks: list[dict] = []
    for task_path in root.glob("*/task.json"):
        task = read_task(client_dir, task_path.parent.name)
        if task and task.get("client_id") == username:
            tasks.append(task)
    if not tasks:
        return None
    tasks.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return tasks[0]


def get_running_task(username: str, tenant_id: str = "", login_created_at: str = "") -> dict | None:
    task = get_latest_web_task(username, tenant_id)
    if (
        task
        and task.get("status") in {"pending", "queued", "running"}
        and task_started_in_current_login(task, login_created_at)
    ):
        task["filename"] = task.get("input", {}).get("contract_filename")
        return task
    return None


def task_started_in_current_login(task: dict, login_created_at: str) -> bool:
    if not login_created_at:
        return True
    task_created_at = str(task.get("created_at") or "")
    if not task_created_at:
        return True
    return task_created_at >= login_created_at


def issue_login_token(request: Request) -> str:
    token = secrets.token_urlsafe(32)
    tokens = request.session.get("login_tokens")
    if not isinstance(tokens, list):
        tokens = []
    tokens.append(token)
    request.session["login_tokens"] = tokens[-5:]
    return token


def consume_login_token(request: Request, token: str) -> bool:
    tokens = request.session.get("login_tokens")
    if not isinstance(tokens, list):
        return False
    matched = any(secrets.compare_digest(token, item) for item in tokens)
    if matched:
        request.session["login_tokens"] = [
            item for item in tokens if not secrets.compare_digest(token, item)
        ]
    return matched


def clear_transient_page_state(request: Request) -> None:
    for key in (
        "flash_error",
        "flash_success",
        "admin_flash_error",
        "admin_flash_success",
    ):
        request.session.pop(key, None)


def reset_login_page_state(request: Request) -> None:
    clear_transient_page_state(request)
    request.session.pop("auth_contexts", None)


def create_auth_context(request: Request, user: dict, tenant: dict) -> str:
    ctx = secrets.token_urlsafe(16)
    contexts = request.session.get("auth_contexts")
    if not isinstance(contexts, dict):
        contexts = {}
    contexts[ctx] = {
        "tenant_id": tenant["tenant_id"],
        "tenant_name": tenant.get("name") or tenant["tenant_id"],
        "username": user["username"],
        "display_name": user.get("display_name") or user["username"],
        "role": normalize_role(user.get("role")),
        "login_created_at": now_iso(),
    }
    request.session["auth_contexts"] = contexts
    return ctx


def remove_auth_contexts_for_username(request: Request, username: str, tenant_id: str | None = None) -> None:
    contexts = request.session.get("auth_contexts")
    if not isinstance(contexts, dict):
        return
    removed = False
    for ctx, context in list(contexts.items()):
        if isinstance(context, dict) and context.get("username") == username and (
            tenant_id is None or context.get("tenant_id") == tenant_id
        ):
            contexts.pop(ctx, None)
            removed = True
    if removed:
        request.session["auth_contexts"] = contexts


def get_request_ctx(request: Request) -> str | None:
    ctx = request.query_params.get("ctx")
    if ctx:
        return ctx
    return None


def sync_context_user(request: Request) -> dict | None:
    ctx = get_request_ctx(request)
    if not ctx:
        return None
    contexts = request.session.get("auth_contexts")
    if not isinstance(contexts, dict):
        return None
    context = contexts.get(ctx)
    if not isinstance(context, dict):
        return None

    tenant_id = context.get("tenant_id", "")
    try:
        tenant = require_tenant(tenant_id)
    except TenantError:
        contexts.pop(ctx, None)
        request.session["auth_contexts"] = contexts
        return None

    user = find_user(tenant_profiles_file(tenant_id), context.get("username", ""))
    if not user or not user.get("enabled", True):
        contexts.pop(ctx, None)
        request.session["auth_contexts"] = contexts
        return None

    context["tenant_name"] = tenant.get("name") or tenant_id
    context["display_name"] = user.get("display_name") or user["username"]
    context["role"] = normalize_role(user.get("role"))
    contexts[ctx] = context
    request.session["auth_contexts"] = contexts
    return {
        "ctx": ctx,
        "tenant_id": tenant_id,
        "tenant_name": context["tenant_name"],
        "username": user["username"],
        "display_name": context["display_name"],
        "role": context["role"],
        "login_created_at": context.get("login_created_at") or "",
    }


def ctx_path(path: str, ctx: str | None) -> str:
    if not ctx:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}ctx={quote(ctx)}"


def localize_tenant_error(exc: TenantError) -> str:
    message = str(exc)
    if message.startswith("tenant_id must contain"):
        return "租户 ID 只能包含字母、数字、下划线或短横线。"
    if message.startswith("tenant_profiles.json must contain"):
        return "租户配置文件格式错误，请联系管理员。"
    if message.startswith("Tenant not found:"):
        tenant_id = message.split(":", 1)[1].strip()
        return f"租户不存在：{tenant_id}"
    if message.startswith("Tenant is disabled:"):
        tenant_id = message.split(":", 1)[1].strip()
        return f"租户已被禁用：{tenant_id}"
    return "租户校验失败，请联系管理员。"


def remove_auth_context(request: Request, ctx: str | None) -> None:
    if not ctx:
        return
    contexts = request.session.get("auth_contexts")
    if not isinstance(contexts, dict):
        return
    contexts.pop(ctx, None)
    request.session["auth_contexts"] = contexts


def build_history_display_names(record: dict) -> tuple[str, str]:
    contract_name = (
        record.get("contract_original_name")
        or strip_task_file_prefix(record.get("contract_stored_name"))
        or "-"
    )
    report_name = record.get("report_display_name")
    if not report_name:
        report_name = (
            build_report_display_name(contract_name)
            if contract_name != "-"
            else strip_task_file_prefix(record.get("report_stored_name"))
        )
    return strip_task_file_prefix(contract_name), strip_task_file_prefix(report_name)


def build_history_record(paths, output_path: Path, task: dict) -> dict:
    contract_stat = paths.stored_contract_path.stat()
    report_stat = output_path.stat() if output_path.exists() else None
    task_input = task.get("input") if isinstance(task.get("input"), dict) else {}
    criteria_source = task.get("criteria_source") or task_input.get("criteria_source", "mandatory")
    return {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "tenant_id": paths.tenant_id,
        "tenant_name": task.get("tenant_name"),
        "username": paths.username,
        "display_name": task.get("display_name"),
        "task_id": paths.task_id,
        "status": task.get("status", "completed"),
        "contract_original_name": paths.original_filename,
        "contract_stored_name": paths.stored_contract_path.name,
        "contract_size_bytes": contract_stat.st_size,
        "contract_uploaded_at": task.get("contract_uploaded_at") or task.get("created_at") or format_timestamp(contract_stat.st_mtime),
        "criteria_source": criteria_source,
        "criteria_original_name": (
            task.get("criteria_original_name") or task_input.get("criteria_filename")
            if criteria_source == "uploaded"
            else None
        ),
        "report_display_name": build_report_display_name(paths.original_filename),
        "report_stored_name": output_path.name,
        "report_size_bytes": report_stat.st_size if report_stat else None,
        "report_created_at": format_timestamp(report_stat.st_mtime) if report_stat else None,
    }



async def run_web_review_task(username: str, tenant_id: str, paths) -> None:
    client_dir = web_client_dir(tenant_id)
    await run_async_review_job(client_dir, paths.task_id)
    task = read_task(client_dir, paths.task_id)
    if task is None:
        return

    output_path = Path(task["output"]["result_path"])
    history_record = build_history_record(paths, output_path, task)
    if task.get("status") == "succeeded":
        task_updates = history_record.copy()
        task_updates.pop("task_id", None)
        update_task(client_dir, paths.task_id, **task_updates)


def get_current_username(request: Request) -> str | None:
    user = sync_context_user(request)
    if not user:
        return None
    return user["username"]


def require_current_username(request: Request) -> str:
    user = sync_context_user(request)
    username = user["username"] if user else None
    if not username:
        raise HTTPException(
            status_code=401, 
            detail="Not logged in.",
        )
    return username


@user_router.get("/web/session/status")
async def session_status(request: Request):
    user = sync_context_user(request)
    if not user and not get_request_ctx(request):
        contexts = request.session.get("auth_contexts")
        if isinstance(contexts, dict):
            for ctx in list(contexts):
                context = contexts.get(ctx)
                if not isinstance(context, dict):
                    continue
                tenant_id = context.get("tenant_id", "")
                stored_user = find_user(tenant_profiles_file(tenant_id), context.get("username", "")) if tenant_id else None
                if stored_user and stored_user.get("enabled", True):
                    return {
                        "active": True,
                        "role": normalize_role(stored_user.get("role")),
                    }
    if not user:
        return JSONResponse({"active": False}, status_code=401)
    return {"active": True, "role": user["role"]}


@user_router.get("/web/review/status")
async def review_status(request: Request):
    user = sync_context_user(request)
    if not user:
        return JSONResponse({"active": False}, status_code=401)
    if user["role"] == "admin":
        return JSONResponse({"active": False}, status_code=403)

    task = get_latest_web_task(user["username"], user["tenant_id"])
    if task is None:
        return {"active": True, "has_task": False}

    output = task.get("output") if isinstance(task.get("output"), dict) else {}
    result_path_value = output.get("result_path")
    result_path = Path(result_path_value) if result_path_value else None
    status = task.get("status")
    result_ready = status == "succeeded" and result_path is not None and result_path.exists()
    return {
        "active": True,
        "has_task": True,
        "task_id": task.get("task_id"),
        "status": status,
        "message": task.get("message"),
        "result_ready": result_ready,
        "result_name": result_path.name if result_ready and result_path else None,
    }


def update_display_name(users_file: str | Path, username: str, display_name: str) -> str:
    cleaned = display_name.strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="Display name cannot be empty.")
    if len(cleaned) > 40:
        raise HTTPException(status_code=400, detail="Display name cannot exceed 40 characters.")

    users = load_users(users_file)
    for user in users:
        if user.get("username") == username:
            user["display_name"] = cleaned
            save_users(users_file, users)
            return cleaned

    raise HTTPException(status_code=404, detail="Current user was not found.")


def list_history(username: str, tenant_id: str) -> list[dict]:
    data_dir = Path(DATA_DIR)
    rows = []
    for record in load_history_records(data_dir, username, tenant_id=tenant_id):
        report_stored_name = record.get("report_stored_name")
        if not report_stored_name:
            continue
        report_path = data_dir / "web" / tenant_id / str(record.get("task_id") or "") / "output" / safe_upload_filename(report_stored_name)
        if not report_path.exists():
            continue

        criteria_source = record.get("criteria_source")
        criteria_label = "Default criteria"
        if criteria_source == "uploaded":
            criteria_label = f"Uploaded: {record.get('criteria_original_name') or '-'}"

        contract_display_name, report_display_name = build_history_display_names(record)
        rows.append({
            **record,
            "contract_display_name": contract_display_name,
            "report_display_name": report_display_name,
            "contract_size": format_file_size(record.get("contract_size_bytes")),
            "report_size": format_file_size(record.get("report_size_bytes")),
            "criteria_label": criteria_label,
            "download_name": report_path.name,
        })
    rows.sort(key=lambda item: item.get("report_created_at") or "", reverse=True)
    return rows[:20]


@user_router.get("/web/login", response_class=HTMLResponse)
async def login_page(request: Request):
    reset_login_page_state(request)
    login_token = issue_login_token(request)
    return templates.TemplateResponse(
        request,
        "login.html",
            {
                "error": None,
                "login_token": login_token,
                "tenant_id": "",
                "username": "",
            },
    )


@user_router.post("/web/login", response_class=HTMLResponse)
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    tenant_id: str = Form(...),
    login_token: str = Form(...),
):
    if not consume_login_token(request, login_token):
        return RedirectResponse("/web/login", status_code=303)

    next_login_token = issue_login_token(request)
    try:
        tenant = require_tenant(tenant_id)
        users_file = tenant_profiles_file(tenant["tenant_id"])
    except TenantError as exc:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": localize_tenant_error(exc),
                "login_token": next_login_token,
                "tenant_id": tenant_id,
                "username": username,
            },
            status_code=401,
        )

    existing_user = find_user(users_file, username)
    if (
        existing_user
        and verify_password(password, existing_user.get("password_hash", ""))
        and not existing_user.get("enabled", True)
    ):
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "账号已被禁用，请联系管理员。",
                "login_token": next_login_token,
                "tenant_id": tenant_id,
                "username": username,
            },
            status_code=403,
        )

    user = verify_login(users_file, username, password)
    if not user:
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "账号或密码错误。",
                "login_token": next_login_token,
                "tenant_id": tenant_id,
                "username": username,
            },
            status_code=401,
        )

    remove_auth_contexts_for_username(request, user["username"], tenant["tenant_id"])
    clear_transient_page_state(request)
    ctx = create_auth_context(request, user, tenant)
    if normalize_role(user.get("role")) == "admin":
        return RedirectResponse(ctx_path("/web/admin", ctx), status_code=303)
    return RedirectResponse(ctx_path("/web/work", ctx), status_code=303)


@user_router.post("/web/logout")
async def logout(request: Request):
    remove_auth_context(request, get_request_ctx(request))
    clear_transient_page_state(request)
    return RedirectResponse("/web/login", status_code=303)


@user_router.post("/web/profile/display-name")
async def update_profile_display_name(
    request: Request,
    display_name: str = Form(...),
):
    user = sync_context_user(request)
    username = user["username"] if user else None
    ctx = get_request_ctx(request)
    if not username:
        return RedirectResponse("/web/login", status_code=303)

    try:
        cleaned = update_display_name(tenant_profiles_file(user["tenant_id"]), username, display_name)
        request.session["display_name"] = cleaned
        request.session["flash_success"] = "Display name updated."
    except HTTPException as exc:
        request.session["flash_error"] = exc.detail
    except OSError:
        request.session["flash_error"] = "Display name update failed. Check whether the user profile file is writable."
    return RedirectResponse(ctx_path("/web/settings", ctx), status_code=303)


@user_router.get("/web", response_class=HTMLResponse)
async def entry_page(request: Request):
    reset_login_page_state(request)
    login_token = issue_login_token(request)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": None,
            "login_token": login_token,
            "tenant_id": "",
            "username": "",
        },
    )


@user_router.get("/web/work", response_class=HTMLResponse)
async def index(request: Request):
    user = sync_context_user(request)
    if not user:
        return RedirectResponse("/web/login", status_code=303) # happens when user tries to access /work without logging in, redirect them to login page
    if user["role"] == "admin":
        return RedirectResponse(ctx_path("/web/admin", user["ctx"]), status_code=303)
    username = user["username"]
    tenant_id = user["tenant_id"]
    task = get_latest_web_task(username, tenant_id)
    error = request.session.pop("flash_error", None)
    success = request.session.pop("flash_success", None)
    result_name = None
    if task:
        if task.get("status") == "failed" and task_started_in_current_login(
            task,
            user.get("login_created_at", ""),
        ):
            task_error = task.get("error") or {}
            error = task_error.get("message") or task.get("message") or error
        elif task.get("status") == "succeeded" and task_started_in_current_login(
            task,
            user.get("login_created_at", ""),
        ):
            result_name = Path(task["output"]["result_path"]).name
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "username": username,
            "display_name": user["display_name"],
            "tenant_id": tenant_id,
            "tenant_name": user["tenant_name"],
            "ctx": user["ctx"],
            "history": list_history(username, tenant_id),
            "error": error,
            "success": success,
            "result_name": result_name,
            "active_task": get_running_task(username, tenant_id, user.get("login_created_at", "")),
        },
    )


@user_router.get("/web/settings", response_class=HTMLResponse)
async def settings(request: Request):
    user = sync_context_user(request)
    if not user:
        return RedirectResponse("/web/login", status_code=303)
    if user["role"] == "admin":
        return RedirectResponse(ctx_path("/web/admin", user["ctx"]), status_code=303)

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "username": user["username"],
            "display_name": user["display_name"],
            "tenant_id": user["tenant_id"],
            "tenant_name": user["tenant_name"],
            "ctx": user["ctx"],
            "error": request.session.pop("flash_error", None),
            "success": request.session.pop("flash_success", None),
        },
    )


@user_router.post("/web/review", response_class=HTMLResponse)
async def review_page(
    request: Request,
    file: UploadFile = File(...),
    criteria_file: UploadFile | None = File(None),
):
    user = sync_context_user(request)
    username = user["username"] if user else None
    ctx = get_request_ctx(request)
    if not username:
        return RedirectResponse("/web/login", status_code=303)

    tenant_id = user["tenant_id"]
    if get_running_task(username, tenant_id):
        request.session["flash_error"] = "当前已有审核任务在运行，请等待完成后再提交。"
        await file.close()
        if criteria_file:
            await criteria_file.close()
        return RedirectResponse(ctx_path("/web/work", ctx), status_code=303)

    filename = safe_upload_filename(file.filename)
    paths = resolve_review_task_paths(
        username=username,
        tenant_id=tenant_id,
        original_filename=filename,
        data_dir=Path(DATA_DIR),
    )
    client_dir = web_client_dir(tenant_id)

    try:
        paths.ensure_user_dirs()
        ensure_task_dirs(client_dir, paths.task_id)
        # api event I - file received for review, with metadata of username and filename (after sanitization)
        write_task_log_event(
            client_dir,
            paths.task_id,
            "upload_received",
            username=username,
            tenant_id=tenant_id,
            filename=filename
        )

        if not filename.lower().endswith(".docx"):
            raise HTTPException(
                status_code=400, 
                detail="仅支持 DOCX 合同文件。",
            )
        selected_criteria_path: Path | None = None
        criteria_source = "mandatory"
        criteria_filename = None
        has_uploaded_criteria = bool(criteria_file and criteria_file.filename)
        if has_uploaded_criteria:
            criteria_filename = safe_upload_filename(criteria_file.filename)
            if not criteria_filename.lower().endswith(".docx"):
                raise HTTPException(
                    status_code=400,
                    detail="审查要点文件必须是 DOCX。",
                )
            selected_criteria_path = save_task_input_upload(
                client_dir,
                paths.task_id,
                criteria_file,
                criteria_filename,
            )
            validate_uploaded_docx(selected_criteria_path)
            validate_criteria_content(selected_criteria_path)
            criteria_source = "uploaded"

            write_task_log_event(
                client_dir,
                paths.task_id,
                "criteria_uploaded",
                file_path=str(selected_criteria_path),
                original_filename=criteria_filename,
                size_bytes=selected_criteria_path.stat().st_size,
            )
        contract_path = save_task_input_upload(
            client_dir,
            paths.task_id,
            file,
            filename,
        )
        
        # api event II - file saved and ready for validation and review
        write_task_log_event(
            client_dir,
            paths.task_id,
            "contract_saved",
            file_path=str(contract_path),
            size_bytes=contract_path.stat().st_size,
        )

        validate_uploaded_docx(contract_path) # Check if the uploaded file is a valid .docx file, otherwise raise HTTPException with 400 status code and error message.

        # api event III - file passed validation and review is about to start
        write_task_log_event(
            client_dir,
            paths.task_id,
            "docx_validation_passed"
        )

        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        task = create_task(
            client_id=username,
            client_dir=client_dir,
            task_id=paths.task_id,
            contract_filename=filename,
            contract_path=contract_path,
            criteria_source=criteria_source,
            criteria_filename=criteria_filename,
            criteria_path=selected_criteria_path,
            result_filename=paths.final_report_path.name,
            result_path=paths.final_report_path,
        )
        task["tenant_id"] = tenant_id
        task["tenant_name"] = user["tenant_name"]
        task["username"] = username
        task["display_name"] = user["display_name"]
        task["contract_uploaded_at"] = created_at
        update_task(
            client_dir,
            paths.task_id,
            tenant_id=tenant_id,
            tenant_name=user["tenant_name"],
            username=username,
            display_name=user["display_name"],
            contract_uploaded_at=created_at,
        )
        asyncio.create_task(run_web_review_task(username, tenant_id, paths))
        return RedirectResponse(ctx_path("/web/work", ctx), status_code=303)


    except HTTPException as exc:
        write_task_log_event(client_dir, paths.task_id, "review_failed", status_code=exc.status_code, detail=exc.detail)
        request.session["flash_error"] = exc.detail
        return RedirectResponse(ctx_path("/web/work", ctx), status_code=303)
    except Exception as exc:
        write_task_log_event(client_dir, paths.task_id, "review_failed", error=repr(exc))
        request.session["flash_error"] = "审核失败，请检查任务日志。"
        return RedirectResponse(ctx_path("/web/work", ctx), status_code=303)
    finally:
        await file.close()
        if criteria_file:
            await criteria_file.close()


@user_router.get("/web/download/{filename}")
async def download_result(request: Request, filename: str):
    user = sync_context_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not logged in.")
    safe_name = safe_upload_filename(filename)
    path = None
    for record in load_history_records(Path(DATA_DIR), user["username"], tenant_id=user["tenant_id"]):
        if safe_upload_filename(record.get("report_stored_name") or "") != safe_name:
            continue
        candidate = Path(DATA_DIR) / "web" / user["tenant_id"] / str(record.get("task_id") or "") / "output" / safe_name
        if candidate.exists():
            path = candidate
            break
    if path is None:
        path = Path(DATA_DIR) / "web" / user["tenant_id"] / safe_name
    if not path.exists():
        raise HTTPException(status_code=404, detail="Review result file was not found.")
    return FileResponse(
        path,
        media_type=DOCX_MEDIA_TYPE,
        filename=safe_name,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(safe_name)}"},
    )


@user_router.get("/web/history", response_class=HTMLResponse)
async def history(request: Request):
    user = sync_context_user(request)
    if not user:
        return RedirectResponse("/web/login", status_code=303)
    if user["role"] == "admin":
        return RedirectResponse(ctx_path("/web/admin", user["ctx"]), status_code=303)
    return templates.TemplateResponse(
        request,
        "history.html",
        {
            "username": user["username"],
            "display_name": user["display_name"],
            "tenant_id": user["tenant_id"],
            "tenant_name": user["tenant_name"],
            "ctx": user["ctx"],
            "history": list_history(user["username"], user["tenant_id"]),
        },
    )
