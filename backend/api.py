"""API 路由：设置、会话、健康检查、工具列表和 SSE 聊天。"""

import asyncio
import json
import logging
import threading
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from backend.agent import Agent
from backend.db import DATA_DIR, db
from backend.file_parser import parse_uploaded_file
from backend.harness import get_harness, run_self_tests
from backend.mcp.manager import get_manager
from backend.schemas import (
    AgentListResponse,
    ChatRequest,
    BootstrapRequest,
    ConfigResponse,
    HealthResponse,
    MCPServerCreateRequest,
    ProjectCreateRequest,
    ProjectRenameRequest,
    RunCreateRequest,
    RunResponse,
    SessionCreateRequest,
    SessionListResponse,
    SessionProjectRequest,
    SessionRenameRequest,
    SessionSummary,
    SettingsRequest,
    SettingsResponse,
    TaskCreateRequest,
    TaskListResponse,
    TaskResponse,
    ToolsResponse,
)
from backend.services import (
    InvalidBaseUrl,
    ProviderKeyReentryRequired,
    get_public_settings,
    get_runtime_config,
    save_public_settings,
)
from backend.skills import (
    SKILLS,
    TOOL_DEFINITIONS,
    build_system_prompt,
    get_skills_info,
)


logger = logging.getLogger("agent.api")


def _require_local_session(request: Request):
    """将 API 认证委托给唯一的宿主认证模块。"""
    from backend.auth import require_host_session

    return require_host_session(request)


def _make_chat_context(session_id: str):
    """仅由服务端冻结内建工具与最小安全 grant。"""
    from backend.tool_gateway import make_builtin_run_context

    return make_builtin_run_context(run_id=session_id)


router = APIRouter(prefix="/api", dependencies=[Depends(_require_local_session)])
public_router = APIRouter(prefix="/api")

UPLOAD_DIR = DATA_DIR / "uploads"
ALLOWED_UPLOAD_EXTENSIONS = {".xlsx", ".pptx", ".csv", ".txt", ".md", ".json", ".log"}
MAX_UPLOAD_SIZE = 20 * 1024 * 1024

# ponytail: single-process gate; delete when P1's persisted active-Run index lands.
_active_session_ids = set()
_active_session_lock = threading.Lock()


def _reserve_session(session_id: str) -> bool:
    with _active_session_lock:
        if session_id in _active_session_ids:
            return False
        _active_session_ids.add(session_id)
        return True


def _release_session(session_id: str):
    with _active_session_lock:
        _active_session_ids.discard(session_id)


def _error_response(status_code: int, code: str, message: str, details=None):
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "details": details or {},
            "trace_id": uuid.uuid4().hex,
        },
    )


def _repository_error(exc: Exception):
    """仅映射仓库冻结的公开错误码；未知错误交给全局 500。"""
    code = getattr(exc, "code", None)
    if code == "not_found":
        return _error_response(404, "not_found", "资源不存在")
    if code == "idempotency_key_reused":
        return _error_response(409, code, "幂等键已用于不同请求")
    if code == "run_already_active":
        run_id = getattr(exc, "existing_run_id", None)
        return _error_response(409, code, "该 Agent 已有活动 Run", {"run_id": run_id} if run_id else {})
    if code == "invalid_run_transition":
        return _error_response(409, code, "当前 Run 状态不允许此操作")
    if isinstance(exc, ValueError):
        return _error_response(422, "invalid_request", "请求参数无效")
    raise exc


def _task_response(row: dict) -> dict:
    return {
        "task_id": row["id"],
        "agent_id": row["session_id"],
        "goal": row["goal"],
        "acceptance": json.loads(row["acceptance_json"]),
        "context": json.loads(row["context_json"]),
        "status": row["status"],
        "latest_run_id": db.get_latest_run_id(row["id"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


def _run_response(row: dict) -> dict:
    return {
        "run_id": row["id"],
        "task_id": row["task_id"],
        "agent_id": row["session_id"],
        "attempt": row["attempt"],
        "status": row["status"],
        "wait_reason": row["wait_reason"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "last_event_id": db.get_last_event_id(row["id"]),
        "allowed_actions": [],  # Wave 4 才开放 control/Runner。
    }


def _sse(event: dict) -> str:
    """把事件包装成 SSE data 行。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


def _make_session_title(message: str) -> str:
    """根据首条消息生成会话标题。"""
    title = message.strip().replace("\n", " ")
    return title[:20] + ("..." if len(title) > 20 else "") or "新会话"


def _log_sse_event(event: dict):
    """只记录受控事件类型，不记录模型或工具产生的内容。"""
    event_type = event.get("type")
    allowed = {"round", "text", "tool_call", "tool_result", "done", "error"}
    logger.info("[SSE] event=%s", event_type if event_type in allowed else "other")


@public_router.get("/health", response_model=HealthResponse)
async def health():
    """健康检查。"""
    result = HealthResponse(
        status="ok",
        service="countboter",
        time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )
    logger.info("健康检查输出：%s", result.model_dump())
    return result


@public_router.post("/auth/bootstrap")
async def bootstrap_auth(payload: BootstrapRequest, request: Request, response: Response):
    """一次性 secret 换取同源宿主 cookie，不要求已有 cookie。"""
    from backend.auth import bootstrap

    bootstrap(request, response, payload.secret)
    return {"ok": True}


@router.get("/tools", response_model=ToolsResponse)
async def tools():
    """返回当前注册给模型的工具列表。"""
    result = ToolsResponse(count=len(TOOL_DEFINITIONS), tools=TOOL_DEFINITIONS)
    logger.info("工具列表输出：count=%d", result.count)
    return result


@router.get("/skills")
async def skills():
    """返回已注册的技能列表。"""
    info = get_skills_info()
    logger.info("技能列表输出：count=%d", len(SKILLS))
    return {"count": len(SKILLS), "skills": info}


@router.post("/tasks", status_code=201, response_model=TaskResponse)
async def create_task(payload: TaskCreateRequest):
    try:
        row = await asyncio.to_thread(
            db.create_task, payload.agent_id, payload.goal, payload.acceptance, payload.context
        )
    except Exception as exc:
        return _repository_error(exc)
    return await asyncio.to_thread(_task_response, row)


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    request: Request,
    agent_id: str | None = None,
    status: str | None = Query(None, pattern="^(open|completed|failed|cancelled)$"),
    limit: int = Query(50, ge=1, le=100),
):
    if "session_id" in request.query_params:
        return _error_response(422, "invalid_request", "请使用 agent_id")
    try:
        rows = await asyncio.to_thread(db.list_tasks, agent_id, status, limit)
    except Exception as exc:
        return _repository_error(exc)
    return {"tasks": await asyncio.to_thread(lambda: [_task_response(row) for row in rows])}


@router.get("/tasks/{task_id}", response_model=TaskResponse)
async def get_task(task_id: str):
    row = await asyncio.to_thread(db.get_task, task_id)
    if not row:
        return _error_response(404, "not_found", "资源不存在")
    return await asyncio.to_thread(_task_response, row)


@router.post("/tasks/{task_id}/runs", status_code=202, response_model=RunResponse)
async def create_run(
    task_id: str,
    response: Response,
    payload: RunCreateRequest = Body(default=RunCreateRequest()),
    idempotency_key: str | None = Header(None, alias="Idempotency-Key"),
):
    key = (idempotency_key or "").strip()
    if not key:
        return _error_response(400, "idempotency_key_required", "缺少 Idempotency-Key")
    try:
        row, _created = await asyncio.to_thread(db.create_run, task_id, key)
    except Exception as exc:
        return _repository_error(exc)
    snapshot = await asyncio.to_thread(_run_response, row)
    response.headers["Location"] = f"/api/runs/{row['id']}"
    return snapshot


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str):
    row = await asyncio.to_thread(db.get_run, run_id)
    if not row:
        return _error_response(404, "not_found", "资源不存在")
    return await asyncio.to_thread(_run_response, row)


@router.get("/agents", response_model=AgentListResponse)
async def list_agents():
    sessions = await asyncio.to_thread(db.list_sessions)
    return {"agents": [
        {
            "agent_id": row["id"],
            "title": row["title"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "project_id": row["project_id"],
        }
        for row in sessions
    ]}


@router.post("/upload")
async def upload_file(file: UploadFile = File(...)):
    """上传并解析文件，返回 file_id 与解析摘要。"""
    filename = Path(file.filename or "upload").name
    extension = Path(filename).suffix.lower()
    if extension not in ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=415, detail="文件类型不受支持")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    file_id = str(uuid.uuid4())
    stored_path = UPLOAD_DIR / f"{file_id}_{filename}"
    size = 0
    try:
        with stored_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_SIZE:
                    raise HTTPException(status_code=413, detail="文件超过大小限制")
                handle.write(chunk)
        try:
            parsed = await asyncio.to_thread(parse_uploaded_file, stored_path)
        except ValueError as exc:
            if extension in {".xlsx", ".pptx"}:
                if str(exc) in {"ZIP entry limit exceeded", "ZIP expansion limit exceeded"}:
                    raise HTTPException(status_code=413, detail="压缩文件超过限制") from exc
                if str(exc) == "Invalid ZIP container":
                    raise HTTPException(status_code=415, detail="文件内容与类型不符") from exc
            raise
        db.register_upload(file_id, filename, stored_path, size, file.content_type, parsed)
    except Exception:
        stored_path.unlink(missing_ok=True)
        raise

    logger.info("上传文件完成：file_id=%s，size=%d", file_id, size)
    return {
        "file_id": file_id,
        "filename": filename,
        "size": size,
        "parsed": parsed,
    }


@router.get("/harness")
async def harness_status():
    """返回安全 Harness 当前状态。"""
    return get_harness().status()


@router.post("/harness/self-test")
async def harness_self_test():
    """运行 Harness 内置自检。"""
    results = run_self_tests()
    return {
        "passed": all(item["ok"] for item in results),
        "results": results,
    }


@router.get("/harness/audit")
async def harness_audit():
    """返回最近的工具调用审计记录。"""
    return {
        "count": db.count_audit_logs(),
        "logs": db.get_audit_logs(50),
    }


@router.get("/mcp/servers")
async def list_mcp_servers():
    """返回 MCP Server 配置和连接状态。"""
    manager = get_manager()
    manager.load_configs()
    return {
        "servers": db.list_mcp_servers(),
        "status": manager.server_status(),
    }


@router.post("/mcp/servers")
async def create_mcp_server(payload: MCPServerCreateRequest):
    """新增 MCP Server 配置。"""
    row = db.create_mcp_server(
        name=payload.name,
        transport=payload.transport,
        command=payload.command,
        args=payload.args or [],
        url=payload.url,
        enabled=False,
    )
    logger.info("创建 MCP Server：%s", row["id"])
    return row


@router.delete("/mcp/servers/{server_id}")
async def delete_mcp_server(server_id: str):
    """删除 MCP Server 配置。"""
    if not db.get_mcp_server(server_id):
        raise HTTPException(status_code=404, detail="MCP Server 不存在")
    db.delete_mcp_server(server_id)
    return {"ok": True}


@router.post("/mcp/refresh")
async def refresh_mcp():
    """重新连接所有 MCP Server 并注册工具。"""
    result = await get_manager().refresh_registry()
    logger.info("MCP 刷新完成")
    return result


@router.post("/mcp/servers/{server_id}/test")
async def test_mcp_server(server_id: str):
    """测试指定 MCP Server 连接并列出工具。"""
    manager = get_manager()
    manager.load_configs()
    config = manager.get_config(server_id)
    if not config:
        raise HTTPException(status_code=404, detail="MCP Server 不存在")
    return await manager.test_server(config)


@router.get("/config", response_model=ConfigResponse)
async def config():
    """返回非敏感运行配置。"""
    settings = get_public_settings()
    logger.info("配置输出完成")
    return ConfigResponse(**settings)


@router.get("/settings", response_model=SettingsResponse)
async def get_settings():
    """返回前端设置面板信息。"""
    settings = get_public_settings()
    logger.info("设置输出完成")
    return settings


@router.put("/settings", response_model=SettingsResponse)
async def update_settings(payload: SettingsRequest):
    """保存前端设置，API Key 加密后写入 SQLite。"""
    try:
        settings = save_public_settings(
            api_key=payload.api_key,
            base_url=payload.base_url,
            model=payload.model,
            max_rounds=payload.max_rounds,
            tool_timeout=payload.tool_timeout,
        )
    except ProviderKeyReentryRequired:
        return _error_response(
            409,
            "provider_key_reentry_required",
            "当前 Provider 凭据需要重新输入 API Key",
        )
    except InvalidBaseUrl as exc:
        return _error_response(422, "invalid_base_url", str(exc))
    logger.info("设置保存完成：api_key_configured=%s", settings["api_key_configured"])
    return settings


@router.get("/sessions", response_model=SessionListResponse)
async def list_sessions():
    """返回会话列表。"""
    sessions = db.list_sessions()
    logger.info("会话列表输出：count=%d", len(sessions))
    return SessionListResponse(sessions=sessions)


@router.post("/sessions", response_model=SessionSummary)
async def create_session(payload: SessionCreateRequest):
    """创建新会话。"""
    if not payload.project_id:
        raise HTTPException(status_code=400, detail="请先创建并选择项目")
    session = db.create_session(payload.title or "新会话", payload.project_id)
    logger.info("创建会话：session_id=%s", session["id"])
    return session


@router.get("/projects")
async def list_projects():
    """返回项目文件夹列表。"""
    projects = db.list_projects()
    logger.info("项目列表输出：count=%d", len(projects))
    return {"projects": projects}


@router.post("/projects")
async def create_project(payload: ProjectCreateRequest):
    """创建项目文件夹。"""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="项目名称不能为空")
    project = db.create_project(name)
    logger.info("创建项目：project_id=%s", project["id"])
    return project


@router.patch("/projects/{project_id}")
async def rename_project(project_id: str, payload: ProjectRenameRequest):
    """重命名项目。"""
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="项目名称不能为空")
    project = db.rename_project(project_id, name)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.delete("/projects/{project_id}")
async def delete_project(project_id: str):
    """删除项目，会话会移回未分组。"""
    project = db.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    try:
        db.delete_project(project_id)
    except ValueError:
        return _error_response(
            409,
            "default_project_protected",
            "默认项目不能删除",
        )
    return {"ok": True}


@router.put("/sessions/{session_id}/project", response_model=SessionSummary)
async def assign_session_to_project(
    session_id: str,
    payload: SessionProjectRequest,
):
    """把会话归入指定项目；project_id 为空时移回未分组。"""
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    if payload.project_id and not db.get_project(payload.project_id):
        raise HTTPException(status_code=404, detail="项目不存在")
    db.assign_session_to_project(session_id, payload.project_id)
    return db.get_session(session_id)


@router.get("/sessions/{session_id}")
async def get_session_messages(session_id: str):
    """返回指定会话的信息与全部消息。"""
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = db.get_messages(session_id)
    logger.info("读取会话输出：session_id=%s，messages=%d", session_id, len(messages))
    return {"session": session, "messages": messages}


@router.patch("/sessions/{session_id}", response_model=SessionSummary)
async def rename_session(session_id: str, payload: SessionRenameRequest):
    """重命名会话。"""
    title = payload.title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    session = db.rename_session(session_id, title)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    logger.info("重命名会话：session_id=%s", session_id)
    return session


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    """删除会话及其全部消息。"""
    session = db.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")
    db.delete_session(session_id)
    logger.info("删除会话输出：session_id=%s", session_id)
    return {"ok": True}


@router.post("/chat")
async def chat(payload: ChatRequest):
    """接收消息，按会话持久化并 SSE 流式返回 Agent 执行过程。"""
    message = payload.message.strip()
    if not message:
        return _error_response(400, "invalid_request", "message 不能为空")

    session_id = (payload.session_id or "").strip() or None
    if session_id:
        if not db.get_session(session_id):
            raise HTTPException(status_code=404, detail="会话不存在")
    else:
        if not payload.project_id:
            return _error_response(400, "invalid_request", "请先创建并选择项目")
        if not db.get_project(payload.project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        session = db.create_session(_make_session_title(message), payload.project_id)
        session_id = session["id"]
        logger.info("自动创建会话：%s", session_id)

    if not _reserve_session(session_id):
        return _error_response(
            409,
            "run_already_active",
            "该会话已有活动执行",
            {"session_id": session_id},
        )

    try:
        # reserve 必须先于消息写入，拒绝请求不能留下孤立 user message。
        run_context = _make_chat_context(session_id)
        db.add_message(session_id, "user", message)
        session = db.get_session(session_id)
        if session and session["title"] in ("新会话", ""):
            db.rename_session(session_id, _make_session_title(message))
        history = db.get_messages(session_id)
        cfg = get_runtime_config()
    except Exception:
        _release_session(session_id)
        raise
    logger.info("聊天请求：session_id=%s，历史消息数=%d", session_id, len(history))
    logger.info(
        "聊天运行配置：max_rounds=%d，tool_timeout=%.1f，"
        "api_key_configured=%s",
        cfg.max_rounds,
        cfg.tool_timeout,
        bool(cfg.api_key),
    )

    async def persist_message(message_dict: dict):
        """把 Agent 产生的助手消息和工具消息写入当前会话。"""
        await asyncio.to_thread(
            db.add_message,
            session_id,
            message_dict.get("role"),
            message_dict.get("content"),
            message_dict.get("tool_calls"),
            message_dict.get("tool_call_id"),
        )

    async def event_stream():
        try:
            if not cfg.api_key:
                raise ValueError("缺少 API Key，请先在前端设置中配置")
            agent = Agent(
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                model=cfg.model,
                max_rounds=cfg.max_rounds,
                tool_timeout=cfg.tool_timeout,
                on_message=persist_message,
                system_prompt=build_system_prompt(),
                run_context=run_context,
            )
            async for event in agent.run(history):
                _log_sse_event(event)
                yield _sse(event)
            logger.info("SSE 响应完成，session_id=%s", session_id)
        except Exception:
            logger.error("处理 /api/chat 请求失败")
            yield _sse({"type": "error", "message": "请求失败"})
        finally:
            _release_session(session_id)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream; charset=utf-8",
    )
