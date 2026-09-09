from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

from config import DATA_DIR, PROJECT_ROOT
from endpoints.web.admin_services import (
    admin_summary,
    get_admin_user,
    list_admin_users,
    list_user_log_tasks,
    list_user_review_history,
)
from endpoints.runtime.filenames import safe_upload_filename
from endpoints.web.user_routes import ctx_path, get_request_ctx, sync_context_user
from endpoints.web.user_management import (
    UserManagementError,
    create_user_account,
    delete_user_account,
    reset_user_password,
    set_user_enabled,
    set_user_role,
)


templates = Jinja2Templates(directory=str(Path(PROJECT_ROOT) / "frontend" / "templates"))
admin_router = APIRouter(prefix="/web/admin")


def is_current_admin(request: Request) -> bool:
    user = sync_context_user(request)
    return bool(user and user["role"] == "admin")


def require_admin(request: Request) -> None:
    user = sync_context_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="未登录")
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")


def admin_template_context(request: Request, **extra) -> dict:
    user = sync_context_user(request) or {}
    return {
        "username": user.get("username"),
        "display_name": user.get("display_name") or user.get("username"),
        "tenant_id": user.get("tenant_id"),
        "tenant_name": user.get("tenant_name"),
        "ctx": user.get("ctx"),
        **extra,
    }


def current_tenant_id(request: Request) -> str:
    user = sync_context_user(request) or {}
    return str(user.get("tenant_id") or "")


def admin_user_redirect(request: Request, username: str) -> RedirectResponse:
    return RedirectResponse(ctx_path(f"/web/admin/users/{quote(username)}", get_request_ctx(request)), status_code=303)


def set_admin_flash(request: Request, *, success: str | None = None, error: str | None = None) -> None:
    if success:
        request.session["admin_flash_success"] = success
    if error:
        request.session["admin_flash_error"] = error


@admin_router.get("", response_class=HTMLResponse)
async def admin_index(request: Request):
    try:
        require_admin(request)
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/web/login", status_code=303)
        raise

    return templates.TemplateResponse(
        request,
        "admin_dashboard.html",
        admin_template_context(request, summary=admin_summary(current_tenant_id(request))),
    )


@admin_router.get("/users", response_class=HTMLResponse)
async def admin_users(request: Request):
    require_admin(request)
    return templates.TemplateResponse(
        request,
        "admin_users.html",
        admin_template_context(
            request,
            users=list_admin_users(current_tenant_id(request)),
            error=request.session.pop("admin_flash_error", None),
            success=request.session.pop("admin_flash_success", None),
        ),
    )


@admin_router.post("/users/create")
async def admin_create_user(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    role: str = Form("user"),
):
    require_admin(request)
    try:
        created = create_user_account(
            tenant_id=current_tenant_id(request),
            username=username,
            password=password,
            display_name=display_name,
            role=role,
        )
        set_admin_flash(request, success=f"已创建用户：{created}")
    except UserManagementError as exc:
        set_admin_flash(request, error=str(exc))
    return RedirectResponse(ctx_path("/web/admin/users", get_request_ctx(request)), status_code=303)


@admin_router.get("/users/{target_username}", response_class=HTMLResponse)
async def admin_user_detail(request: Request, target_username: str):
    require_admin(request)
    tenant_id = current_tenant_id(request)
    user = get_admin_user(tenant_id, target_username)
    if not user:
        raise HTTPException(status_code=404, detail="未找到用户。")

    error = request.session.pop("admin_flash_error", None)
    success = request.session.pop("admin_flash_success", None)
    return templates.TemplateResponse(
        request,
        "admin_user_detail.html",
        admin_template_context(
            request,
            target_user=user,
            history=list_user_review_history(tenant_id, target_username),
            logs=list_user_log_tasks(tenant_id, target_username),
            error=error,
            success=success,
        ),
    )


@admin_router.post("/users/{target_username}/role")
async def admin_set_user_role(
    request: Request,
    target_username: str,
    role: str = Form(...),
):
    require_admin(request)
    if target_username == (sync_context_user(request) or {}).get("username") and role != "admin":
        set_admin_flash(request, error="不能移除当前管理员自己的管理员角色。")
        return admin_user_redirect(request, target_username)
    try:
        updated_role = set_user_role(tenant_id=current_tenant_id(request), username=target_username, role=role)
        set_admin_flash(request, success=f"已更新用户角色：{updated_role}")
    except UserManagementError as exc:
        set_admin_flash(request, error=str(exc))
    return admin_user_redirect(request, target_username)


@admin_router.post("/users/{target_username}/password")
async def admin_reset_user_password(
    request: Request,
    target_username: str,
    password: str = Form(...),
):
    require_admin(request)
    try:
        reset_user_password(tenant_id=current_tenant_id(request), username=target_username, password=password)
        set_admin_flash(request, success="已重置用户密码。")
    except UserManagementError as exc:
        set_admin_flash(request, error=str(exc))
    return admin_user_redirect(request, target_username)


@admin_router.post("/users/{target_username}/enable")
async def admin_enable_user(request: Request, target_username: str):
    require_admin(request)
    try:
        set_user_enabled(tenant_id=current_tenant_id(request), username=target_username, enabled=True)
        set_admin_flash(request, success="已启用用户。")
    except UserManagementError as exc:
        set_admin_flash(request, error=str(exc))
    return admin_user_redirect(request, target_username)


@admin_router.post("/users/{target_username}/disable")
async def admin_disable_user(request: Request, target_username: str):
    require_admin(request)
    if target_username == (sync_context_user(request) or {}).get("username"):
        set_admin_flash(request, error="不能禁用当前登录的管理员账号。")
        return admin_user_redirect(request, target_username)
    try:
        set_user_enabled(tenant_id=current_tenant_id(request), username=target_username, enabled=False)
        set_admin_flash(request, success="已禁用用户。")
    except UserManagementError as exc:
        set_admin_flash(request, error=str(exc))
    return admin_user_redirect(request, target_username)


@admin_router.post("/users/{target_username}/delete")
async def admin_delete_user(
    request: Request,
    target_username: str,
    keep_data: bool = Form(False),
):
    require_admin(request)
    if target_username == (sync_context_user(request) or {}).get("username"):
        set_admin_flash(request, error="不能删除当前登录的管理员账号。")
        return admin_user_redirect(request, target_username)
    try:
        delete_user_account(tenant_id=current_tenant_id(request), username=target_username, keep_data=keep_data)
        set_admin_flash(request, success=f"已删除用户：{target_username}")
        return RedirectResponse(ctx_path("/web/admin/users", get_request_ctx(request)), status_code=303)
    except UserManagementError as exc:
        set_admin_flash(request, error=str(exc))
        return admin_user_redirect(request, target_username)


@admin_router.get("/users/{target_username}/logs/{date}/{task_name}/api-events")
async def admin_view_api_events(request: Request, target_username: str, date: str, task_name: str):
    require_admin(request)
    tenant_id = current_tenant_id(request)
    if not get_admin_user(tenant_id, target_username):
        raise HTTPException(status_code=404, detail="未找到用户。")

    logs_root = (Path(DATA_DIR) / "web" / tenant_id).resolve()
    path = (logs_root / safe_upload_filename(task_name) / "logs" / "events.log").resolve()
    if logs_root not in path.parents:
        raise HTTPException(status_code=400, detail="日志路径不合法。")
    if not path.exists():
        raise HTTPException(status_code=404, detail="未找到 API 事件日志。")
    return PlainTextResponse(path.read_text(encoding="utf-8", errors="replace"))
