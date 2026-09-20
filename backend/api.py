"""API 路由：设置、会话、健康检查、工具列表和 SSE 聊天。"""

import asyncio
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Header, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from backend.db import DATA_DIR, db
from backend.file_parser import parse_uploaded_file
from backend.harness import get_harness, run_self_tests
from backend.mcp.manager import get_manager
from backend.schemas import (
    AgentFilesystemCapabilityRequest,
    AgentListResponse,
    AgentRenameRequest,
    ApprovalDecisionRequest,
    ChatRequest,
    BootstrapRequest,
    ConfigResponse,
    HealthResponse,
    HostDelegateRequest,
    HumanReplyRequest,
    MCPServerCreateRequest,
    ProjectCreateRequest,
    ProjectRenameRequest,
    RunCreateRequest,
    RunControlRequest,
    RunResponse,
    SessionCreateRequest,
    SessionListResponse,
    SessionProjectRequest,
    SessionRenameRequest,
    SessionSummary,
    SettingsRequest,
    SettingsResponse,
    SupervisorCreateRequest,
    SupervisorSelectRequest,
    TaskCreateRequest,
    TaskCleanupRequest,
    TaskListResponse,
    TaskResponse,
    ToolsResponse,
    WorkerCreateRequest,
    WorkspaceCreateRequest,
    WorkspaceUpdateRequest,
)
from backend.services import (
    InvalidBaseUrl,
    ProviderKeyReentryRequired,
    get_public_settings,
    save_public_settings,
)
from backend.skills import (
    SKILLS,
    TOOL_DEFINITIONS,
    get_skills_info,
)


logger = logging.getLogger("agent.api")


def _require_local_session(request: Request):
    """将 API 认证委托给唯一的宿主认证模块。"""
    from backend.auth import require_host_session

    return require_host_session(request)


def _runner():
    from backend.runtime import get_runner

    return get_runner()


def _workspace_service():
    """延迟导入，保持 API 可独立测试且不把 Git/路径逻辑放入路由。"""
    from backend import workspace

    return workspace


router = APIRouter(prefix="/api", dependencies=[Depends(_require_local_session)])
public_router = APIRouter(prefix="/api")

UPLOAD_DIR = DATA_DIR / "uploads"
ALLOWED_UPLOAD_EXTENSIONS = {".xlsx", ".pptx", ".csv", ".txt", ".md", ".json", ".log"}
MAX_UPLOAD_SIZE = 20 * 1024 * 1024
SAFE_AGENT_GRANTS = frozenset({
    "read_only", "control.delegate_task", "control.send_agent_message",
    "control.get_task_status", "control.wait_for_tasks", "control.cancel_task",
    "control.request_human_approval", "filesystem.read", "filesystem.write",
})
MAX_DIFF_BYTES = 256 * 1024

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
    if code == "supervisor_exists":
        return _error_response(409, code, "已有活动 Supervisor")
    if code == "invalid_agent_hierarchy":
        return _error_response(409, code, "Agent 树结构不允许此操作")
    if code == "event_limit_exceeded":
        return _error_response(409, code, "Run 事件容量已达上限")
    if code in {"workspace_in_use", "worktree_dirty", "worktree_mismatch",
                "worktree_exists", "branch_exists"}:
        return _error_response(409, code, "Workspace 当前不能清理")
    if code in {"task_workspace_not_found", "worktree_not_found"}:
        return _error_response(404, code, "Task Workspace 不存在")
    if code in {"workspace_scope_invalid", "workspace_kind_invalid",
                "git_root_invalid", "invalid_task_id"}:
        return _error_response(422, code, "Workspace 授权无效")
    if code == "limit_exceeded":
        return _error_response(413, code, "Workspace 输出超过限制")
    if code == "git_unavailable":
        return _error_response(503, code, "Git 当前不可用")
    if code == "git_timeout":
        return _error_response(504, code, "Git 操作超时")
    if code in {"git_failed", "workspace_io_failed", "invalid_utf8",
                "active_run_state_required"}:
        return _error_response(500, code, "Workspace 操作失败")
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
        "workspace_id": row.get("workspace_id"),
        "workspace_mode": row.get("workspace_mode"),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


async def _host_task_agent(agent_id: str) -> dict | None:
    """宿主可直接对话 legacy 会话或当前根；Worker 只能接受委派。"""
    agent = await asyncio.to_thread(db.get_session, agent_id)
    if not agent or agent.get("archived_at"):
        return None
    if not agent.get("agent_role"):
        return agent
    root = await asyncio.to_thread(db.get_supervisor)
    return agent if agent.get("agent_role") == "supervisor" and root and root["id"] == agent["id"] else None


def _run_response(row: dict) -> dict:
    from backend.runtime import allowed_actions

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
        "allowed_actions": allowed_actions(row),
    }


def _agent_tree_response(row: dict, status: dict | None = None) -> dict:
    """仅投影宿主可见的树字段；不回显 prompt、路径或内部凭据。"""
    capabilities = json.loads(row.get("capabilities_json") or "{}")
    grants = capabilities.get("grants", [])
    return {
        "agent_id": row["id"],
        "title": row["title"],
        "agent_role": row["agent_role"],
        "parent_agent_id": row.get("parent_session_id"),
        "root_agent_id": row.get("root_session_id"),
        "depth": row["depth"],
        "can_delegate": bool(row["can_delegate"]),
        "capability_grants": [grant for grant in grants if grant in SAFE_AGENT_GRANTS],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "archived_at": row.get("archived_at"),
        "status": _agent_status_response(status) if status else None,
    }


def _agent_status_response(row: dict) -> dict:
    return {key: row[key] for key in
            ("agent_id", "state", "active_run_id", "queued_task_count")}


def _delegation_task_response(row: dict) -> dict:
    return {
        **_task_response(row),
        "creator_agent_id": row.get("creator_session_id"),
        "parent_task_id": row.get("parent_task_id"),
    }


def _task_result_response(result: dict | None) -> dict | None:
    if result is None:
        return None
    return {
        "summary": result["summary"],
        "acceptance_results": [
            {"criterion": item["criterion"], "ok": item["ok"]}
            for item in result["acceptance_results"]
        ],
        "artifacts": [
            {"artifact_id": item["artifact_id"], "kind": item["kind"]}
            for item in result["artifacts"]
        ],
        "evidence": [
            {"kind": item["kind"], "command": item["command"],
             "exit_code": item["exit_code"]}
            for item in result["evidence"]
        ],
        "followups": result["followups"],
    }


def _agent_message_response(row: dict) -> dict:
    return {
        "message_id": row["id"], "agent_id": row["session_id"],
        "sender_agent_id": row["sender_session_id"],
        "source_run_id": row["source_run_id"],
        "recipient_run_id": row["recipient_run_id"],
        "related_task_id": row["related_task_id"],
        "kind": row["kind"], "role": row["role"],
        "content": row["content"], "created_at": row["created_at"],
        "needs_reply": bool(row.get("needs_reply", False)),
    }


def _workspace_response(row: dict) -> dict:
    """Workspace 注册根只对宿主投影；永不回显 checkout/storage 字段。"""
    return {key: row.get(key) for key in (
        "name", "root_path", "kind", "max_mode", "created_at", "updated_at", "archived_at"
    )} | {"workspace_id": row["id"]}


def _artifact_response(row: dict) -> dict:
    """Artifact 仅回显可展示元数据，不回显 owner、hash 或存储位置。"""
    return {
        "artifact_id": row.get("artifact_id", row.get("id")),
        "kind": row.get("kind"),
        "relative_path": row.get("relative_path"),
        "size_bytes": row.get("size_bytes"),
        "mime_type": row.get("mime_type"),
        "created_at": row.get("created_at"),
    }


def _diff_response(task_id: str, row: dict | str, max_bytes: int) -> dict:
    raw = row if isinstance(row, str) else row.get("diff", "")
    encoded = raw.encode("utf-8")
    clipped = len(encoded) > max_bytes
    if clipped:
        raw = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return {
        "task_id": task_id,
        "diff": raw,
        "truncated": clipped or (isinstance(row, dict) and bool(row.get("truncated"))),
        "bytes_returned": len(raw.encode("utf-8")),
    }


def _cursor(raw: str, last_event_id: int) -> int:
    if not raw or not raw.isascii() or not raw.isdigit():
        raise ValueError("invalid cursor")
    value = int(raw)
    if value > last_event_id:
        raise ValueError("future cursor")
    return value


def _event_envelope(row: dict, run: dict) -> dict:
    return {
        "v": 1,
        "event_id": row["id"],
        "task_id": row["task_id"],
        "run_id": row["run_id"],
        "agent_id": run["session_id"],
        "type": row["type"],
        "ts": row["created_at"],
        "data": json.loads(row["payload_json"]),
    }


def _sse(event: dict, data: dict | None = None) -> str:
    payload = event if data is None else data
    return (
        f"id: {event['event_id']}\n"
        f"event: {event['type']}\n"
        f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
    )


async def _legacy_event(event: dict) -> dict | None:
    kind, data = event["type"], event["data"]
    if kind == "model.started":
        return {"type": "round", "round": data.get("round", 0)}
    if kind == "message.completed":
        message = await asyncio.to_thread(db.get_message, data.get("message_id"))
        if message and message["role"] == "assistant" and message["content"]:
            return {"type": "text", "content": message["content"]}
    if kind == "tool.requested":
        return {"type": "tool_call", "name": data.get("name", "")}
    if kind in {"tool.completed", "tool.failed"}:
        return {"type": "tool_result", "name": data.get("name", ""),
                "ok": kind == "tool.completed", "message": data.get("result_summary", "")}
    if kind == "run.completed":
        return {"type": "done", "rounds": data.get("rounds", 0),
                "total_ms": data.get("duration_ms", 0)}
    if kind in {"run.failed", "run.cancelled"}:
        return {"type": "error", "message": "运行失败" if kind == "run.failed" else "运行已取消"}
    return None


async def _follow_events(run: dict, cursor: int, request: Request, *, legacy=False):
    run_id = run["id"]
    while True:
        rows = await asyncio.to_thread(db.list_events, run_id, cursor, 100)
        for row in rows:
            cursor = row["id"]
            event = _event_envelope(row, run)
            if legacy:
                translated = await _legacy_event(event)
                if translated is not None:
                    yield _sse(event, translated)
            else:
                yield _sse(event)
        current = await asyncio.to_thread(db.get_run, run_id)
        if current is None:
            break
        last_id = await asyncio.to_thread(db.get_last_event_id, run_id)
        if current["status"] in {"completed", "failed", "cancelled"} and cursor >= last_id:
            break
        if await request.is_disconnected():
            break
        if await _runner().wait_for_change(run_id, cursor, timeout=15) <= cursor:
            yield ": ping\n\n"


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
        agent = await _host_task_agent(payload.agent_id)
        if not agent:
            return _error_response(404, "not_found", "资源不存在")
        if bool(payload.workspace_id) != bool(payload.workspace_mode):
            return _error_response(422, "invalid_request", "workspace_id 与 workspace_mode 必须同时提供")
        args = [payload.agent_id, payload.goal, payload.acceptance,
                payload.context.model_dump() if payload.context else None]
        if payload.workspace_id:
            if agent.get("agent_role") != "supervisor":
                return _error_response(422, "invalid_request", "只有活动根 Task 可选择 Workspace")
            args.extend((payload.workspace_id, payload.workspace_mode))
        row = await asyncio.to_thread(db.create_task, *args)
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


@router.get("/tasks/{task_id}/artifacts")
async def list_task_artifacts(task_id: str):
    if not await asyncio.to_thread(db.get_task, task_id):
        return _error_response(404, "not_found", "资源不存在")
    try:
        rows = await asyncio.to_thread(db.list_task_artifacts, task_id)
    except Exception as exc:
        return _repository_error(exc)
    return {"task_id": task_id, "artifacts": [_artifact_response(row) for row in rows]}


@router.get("/tasks/{task_id}/diff")
async def get_task_diff(task_id: str,
                        max_bytes: int = Query(MAX_DIFF_BYTES, ge=1, le=MAX_DIFF_BYTES)):
    if not await asyncio.to_thread(db.get_task, task_id):
        return _error_response(404, "not_found", "资源不存在")
    try:
        row = await asyncio.to_thread(_workspace_service().get_task_diff, db, task_id, max_bytes)
    except Exception as exc:
        return _repository_error(exc)
    return _diff_response(task_id, row, max_bytes)


@router.post("/tasks/{task_id}/cleanup")
async def cleanup_task_workspace(
    task_id: str,
    payload: TaskCleanupRequest = Body(default=TaskCleanupRequest()),
):
    if not await asyncio.to_thread(db.get_task, task_id):
        return _error_response(404, "not_found", "资源不存在")
    try:
        result = await asyncio.to_thread(_workspace_service().cleanup_task_workspace, db, task_id)
    except Exception as exc:
        return _repository_error(exc)
    status = result.get("status") if isinstance(result, dict) else None
    return {"task_id": task_id,
            "cleanup_status": status if status in {"removed", "not_applicable"} else "removed"}


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
    _runner().wake()
    snapshot = await asyncio.to_thread(_run_response, row)
    response.headers["Location"] = f"/api/runs/{row['id']}"
    return snapshot


@router.get("/runs/{run_id}", response_model=RunResponse)
async def get_run(run_id: str):
    row = await asyncio.to_thread(db.get_run, run_id)
    if not row:
        return _error_response(404, "not_found", "资源不存在")
    return await asyncio.to_thread(_run_response, row)


@router.post("/runs/{run_id}/control", status_code=202, response_model=RunResponse)
async def control_run(run_id: str, payload: RunControlRequest):
    if not await asyncio.to_thread(db.get_run, run_id):
        return _error_response(404, "not_found", "资源不存在")
    try:
        row = await _runner().control(run_id, payload.action)
    except Exception as exc:
        return _repository_error(exc)
    return await asyncio.to_thread(_run_response, row)


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, request: Request, after_id: str | None = None):
    run = await asyncio.to_thread(db.get_run, run_id)
    if not run:
        return _error_response(404, "not_found", "资源不存在")
    raw = request.headers.get("last-event-id")
    try:
        cursor = _cursor(raw if raw is not None else (after_id if after_id is not None else "0"),
                         await asyncio.to_thread(db.get_last_event_id, run_id))
    except ValueError:
        return _error_response(400, "invalid_cursor", "Event 游标无效")
    return StreamingResponse(
        _follow_events(run, cursor, request), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/messages/{message_id}")
async def get_message(message_id: int):
    row = await asyncio.to_thread(db.get_message, message_id)
    if not row or not await asyncio.to_thread(db.get_session, row["session_id"]):
        return _error_response(404, "not_found", "资源不存在")
    return {
        "message_id": row["id"], "agent_id": row["session_id"],
        "task_id": row["task_id"], "run_id": row["run_id"],
        "role": row["role"], "content": row["content"],
        "created_at": row["created_at"],
    }


def _approval_response(row: dict, run: dict) -> dict:
    return {
        "approval_id": row["id"], "run_id": row["run_id"],
        "agent_id": run["session_id"], "tool_call_id": row["tool_call_id"],
        "kind": row["kind"], "action_hash": row["action_hash"],
        "request_summary": json.loads(row["request_summary_json"]),
        "status": row["status"], "expires_at": row["expires_at"],
        "decided_at": row["decided_at"],
        "decision": json.loads(row["decision_json"]) if row["decision_json"] else None,
    }


async def _visible_approval(row: dict):
    run = await asyncio.to_thread(db.get_run, row["run_id"])
    session = (await asyncio.to_thread(db.get_session, run["session_id"])) if run else None
    return run if session and not session.get("archived_at") else None


@router.get("/approvals")
async def list_approvals(agent_id: str | None = None, status: str = Query("pending", pattern="^pending$"),
                         limit: int = Query(50, ge=1, le=100)):
    rows = await asyncio.to_thread(db.list_approvals, None, status, limit)
    approvals = []
    for row in rows:
        run = await _visible_approval(row)
        if run and (agent_id is None or agent_id == run["session_id"]):
            approvals.append(_approval_response(row, run))
    return {"approvals": approvals}


@router.post("/approvals/{approval_id}/decision")
async def decide_approval(approval_id: str, payload: ApprovalDecisionRequest,
                          principal=Depends(_require_local_session)):
    row = await asyncio.to_thread(db.get_approval, approval_id)
    run = await _visible_approval(row) if row else None
    if not run:
        return _error_response(404, "not_found", "资源不存在")
    try:
        decided = await asyncio.to_thread(db.decide_approval, approval_id, payload.decision,
                                          principal.actor_id, payload.note)
    except Exception as exc:
        return _repository_error(exc)
    await _runner().approval_decided(approval_id)
    return _approval_response(decided, run)


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


async def _tree_agent(row: dict) -> dict:
    status = await asyncio.to_thread(db.get_agent_status, row["id"])
    return _agent_tree_response(row, status)


@router.post("/workspaces", status_code=201)
async def create_workspace(payload: WorkspaceCreateRequest):
    try:
        root_path = await asyncio.to_thread(
            _workspace_service().normalize_workspace_root, payload.root_path, payload.kind
        )
        row = await asyncio.to_thread(
            db.create_workspace, payload.name, str(root_path), payload.kind, payload.max_mode
        )
    except Exception as exc:
        return _repository_error(exc)
    return _workspace_response(row)


@router.get("/workspaces")
async def list_workspaces(include_archived: bool = False):
    try:
        rows = await asyncio.to_thread(db.list_workspaces, include_archived)
    except Exception as exc:
        return _repository_error(exc)
    return {"workspaces": [_workspace_response(row) for row in rows]}


@router.patch("/workspaces/{workspace_id}")
async def update_workspace(workspace_id: str, payload: WorkspaceUpdateRequest):
    changes = payload.model_dump(exclude_unset=True)
    if (not changes or changes.get("archived") is False
            or changes.get("max_mode") == "read_write"):
        return _error_response(422, "invalid_request", "只允许重命名、收缩 max_mode 或归档")
    try:
        row = await asyncio.to_thread(
            db.update_workspace, workspace_id, changes.get("name"),
            changes.get("max_mode"), bool(changes.get("archived", False)),
        )
    except Exception as exc:
        return _repository_error(exc)
    return _workspace_response(row)


@router.get("/supervisor")
async def get_supervisor():
    row = await asyncio.to_thread(db.get_supervisor)
    return {"supervisor": await _tree_agent(row) if row else None}


@router.post("/supervisor", status_code=201)
async def create_supervisor(payload: SupervisorCreateRequest):
    try:
        row = await asyncio.to_thread(
            db.create_supervisor, payload.title, payload.system_prompt,
            payload.capabilities.model_dump() if payload.capabilities else None,
        )
    except Exception as exc:
        return _repository_error(exc)
    return await _tree_agent(row)


@router.post("/supervisor/select")
async def select_supervisor(payload: SupervisorSelectRequest):
    try:
        row = await asyncio.to_thread(db.select_supervisor, payload.agent_id)
    except Exception as exc:
        return _repository_error(exc)
    return await _tree_agent(row)


@router.get("/agents/tree")
async def get_agent_tree(include_archived: bool = False):
    rows = await asyncio.to_thread(db.list_agent_tree, None, include_archived)
    return {"agents": [await _tree_agent(row) for row in rows]}


@router.post("/agents/workers", status_code=201)
async def create_worker(payload: WorkerCreateRequest):
    try:
        row = await asyncio.to_thread(
            db.create_worker, payload.parent_agent_id, payload.title,
            payload.system_prompt,
            payload.capabilities.model_dump() if payload.capabilities else None,
            payload.can_delegate,
        )
    except Exception as exc:
        return _repository_error(exc)
    return await _tree_agent(row)


@router.get("/agents/{agent_id}/status")
async def get_agent_status(agent_id: str):
    try:
        row = await asyncio.to_thread(db.get_agent_status, agent_id)
    except Exception as exc:
        return _repository_error(exc)
    return _agent_status_response(row)


@router.patch("/agents/{agent_id}")
async def rename_agent(agent_id: str, payload: AgentRenameRequest):
    try:
        row = await asyncio.to_thread(db.rename_agent, agent_id, payload.title)
    except Exception as exc:
        return _repository_error(exc)
    return await _tree_agent(row)


@router.put("/agents/{agent_id}/filesystem-capability")
async def update_agent_filesystem_capability(
    agent_id: str, payload: AgentFilesystemCapabilityRequest,
):
    if payload.filesystem_write and not payload.filesystem_read:
        return _error_response(422, "invalid_request", "filesystem.write 必须同时允许 read")
    try:
        row = await asyncio.to_thread(
            db.set_agent_filesystem_capability, agent_id,
            payload.filesystem_read, payload.filesystem_write,
        )
    except Exception as exc:
        return _repository_error(exc)
    return await _tree_agent(row)


@router.delete("/agents/{agent_id}")
async def archive_worker(agent_id: str):
    try:
        row = await asyncio.to_thread(db.archive_worker, agent_id)
    except Exception as exc:
        return _repository_error(exc)
    return await _tree_agent(row)


async def _host_tree_ids(include_archived: bool = False) -> tuple[dict | None, set[str]]:
    root = await asyncio.to_thread(db.get_supervisor)
    if not root:
        return None, set()
    rows = await asyncio.to_thread(db.list_agent_tree, root["id"], include_archived)
    return root, {row["id"] for row in rows}


@router.post("/delegations", status_code=201)
async def host_delegate_task(payload: HostDelegateRequest, response: Response):
    try:
        root, visible = await _host_tree_ids()
        if not root or payload.assignee_agent_id not in visible:
            return _error_response(404, "not_found", "资源不存在")
        task, run = await asyncio.to_thread(
            db.host_delegate_task, root["id"], payload.assignee_agent_id,
            payload.goal, payload.acceptance,
            payload.context.model_dump() if payload.context else None,
            payload.requested_grant.model_dump() if payload.requested_grant else None,
            payload.limits.model_dump(exclude_none=True) if payload.limits else None,
        )
    except Exception as exc:
        return _repository_error(exc)
    _runner().wake()
    response.headers["Location"] = f"/api/delegations/{task['id']}"
    return {
        "task": await asyncio.to_thread(_delegation_task_response, task),
        "run": await asyncio.to_thread(_run_response, run) if run else None,
    }


@router.get("/delegations")
async def list_delegations(limit: int = Query(50, ge=1, le=100)):
    try:
        root, _visible = await _host_tree_ids()
        if not root:
            return {"tasks": []}
        rows = await asyncio.to_thread(db.list_tree_tasks, root["id"], limit)
    except Exception as exc:
        return _repository_error(exc)
    return {"tasks": await asyncio.to_thread(lambda: [_delegation_task_response(row) for row in rows])}


@router.get("/delegations/{task_id}")
async def get_delegation(task_id: str):
    try:
        _root, visible = await _host_tree_ids(True)
        task = await asyncio.to_thread(db.get_task, task_id)
        if not task or task["session_id"] not in visible:
            return _error_response(404, "not_found", "资源不存在")
        result = await asyncio.to_thread(db.get_task_result, task_id)
    except Exception as exc:
        return _repository_error(exc)
    return {
        "task": await asyncio.to_thread(_delegation_task_response, task),
        "result": _task_result_response(result),
    }


@router.get("/agents/{agent_id}/agent-messages")
async def list_agent_messages(agent_id: str, after_id: int = Query(0, ge=0),
                              limit: int = Query(100, ge=1, le=100)):
    try:
        _root, visible = await _host_tree_ids(True)
        if agent_id not in visible:
            return _error_response(404, "not_found", "资源不存在")
        rows = await asyncio.to_thread(db.list_agent_messages, agent_id, after_id, limit)
    except Exception as exc:
        return _repository_error(exc)
    return {"messages": [_agent_message_response(row) for row in rows]}


@router.post("/agents/{agent_id}/replies", status_code=202)
async def host_reply(agent_id: str, payload: HumanReplyRequest):
    try:
        root = await asyncio.to_thread(db.get_supervisor)
        if not root or agent_id != root["id"]:
            return _error_response(404, "not_found", "资源不存在")
        message, run = await asyncio.to_thread(
            db.host_reply, agent_id, payload.reply_to_message_id, payload.content
        )
    except Exception as exc:
        return _repository_error(exc)
    _runner().wake()
    return {
        "message": _agent_message_response(message),
        "run": await asyncio.to_thread(_run_response, run),
    }


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
    return {
        "session": {key: session.get(key) for key in
                    ("id", "title", "project_id", "created_at", "updated_at", "archived_at")},
        "messages": messages,
    }


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
    try:
        db.delete_session(session_id)
    except Exception as exc:
        return _repository_error(exc)
    logger.info("删除会话输出：session_id=%s", session_id)
    return {"ok": True}


@router.post("/chat")
async def chat(payload: ChatRequest, request: Request,
               idempotency_key: str | None = Header(None, alias="Idempotency-Key")):
    """旧入口仅创建持久 Run 并订阅；浏览器断流不影响 Runner。"""
    message = payload.message.strip()
    if not message:
        return _error_response(400, "invalid_request", "message 不能为空")

    session_id = (payload.session_id or "").strip() or None
    if session_id:
        session = await _host_task_agent(session_id)
        if not session:
            return _error_response(404, "not_found", "资源不存在")
    else:
        if not payload.project_id:
            return _error_response(400, "invalid_request", "请先创建并选择项目")
        if not db.get_project(payload.project_id):
            raise HTTPException(status_code=404, detail="项目不存在")
        session = db.create_session(_make_session_title(message), payload.project_id)
        session_id = session["id"]
        logger.info("自动创建会话：%s", session_id)

    try:
        _task, run = await asyncio.to_thread(
            db.create_legacy_chat_run, session_id, message, idempotency_key or uuid.uuid4().hex
        )
    except Exception as exc:
        return _repository_error(exc)
    _runner().wake()
    return StreamingResponse(
        _follow_events(run, 0, request, legacy=True),
        media_type="text/event-stream",
        headers={"X-ReActron-Run-ID": run["id"], "Cache-Control": "no-cache"},
    )
