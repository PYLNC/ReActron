"""唯一工具执行边界：快照、grant、路径与预算均先于 handler。"""

import asyncio
import hashlib
import inspect
import json
import logging
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from jsonschema import Draft202012Validator

from backend.harness import HarnessError, PathPolicy


logger = logging.getLogger("agent.tool_gateway")
MAX_ARGUMENT_BYTES = 64 * 1024
MAX_RAW_BYTES = 256 * 1024
MAX_MODEL_CHARS = 8_000
_SECRET_FIELDS = frozenset({"authorization", "cookie", "api_key", "token", "secret", "password"})
P2_HOST_GRANT_CEILING = frozenset({
    "read_only", "control.delegate_task", "control.send_agent_message",
    "control.get_task_status", "control.wait_for_tasks", "control.cancel_task",
    "control.request_human_approval",
})
_BUILTIN_POLICY = {
    "get_current_time": ("read_only", (), "never"),
    "list_dir": ("filesystem.read", ("/path",), "never"),
    "excel_list_sheets": ("filesystem.read", ("/file_path",), "never"),
    "excel_read_sheet": ("filesystem.read", ("/file_path",), "never"),
    "excel_write_cell": ("filesystem.write", ("/file_path",), "always"),
    "excel_create_file": ("filesystem.write", ("/file_path",), "always"),
    "ppt_list_slides": ("filesystem.read", ("/file_path",), "never"),
    "ppt_read_text": ("filesystem.read", ("/file_path",), "never"),
    "ppt_create_file": ("filesystem.write", ("/file_path",), "always"),
}


@dataclass(frozen=True)
class ToolSpec:
    name: str
    schema: dict
    handler: Callable
    effects: frozenset[str]
    path_fields: tuple[str, ...] = ()
    idempotent: bool = False
    approval: str = "always"
    timeout_seconds: float = 30.0
    origin: str = "builtin"


@dataclass
class ToolBudget:
    max_tool_calls: int = 32
    max_mcp_calls: int = 32
    tool_calls: int = 0
    mcp_calls: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self, *, mcp: bool) -> bool:
        with self.lock:
            if self.tool_calls >= self.max_tool_calls:
                return False
            if mcp and self.mcp_calls >= self.max_mcp_calls:
                return False
            self.tool_calls += 1
            if mcp:
                self.mcp_calls += 1
            return True


@dataclass(frozen=True)
class RunContext:
    run_id: str
    task_id: str = ""
    actor_id: str = "local-host"
    call_id: str = ""
    tool_specs: Mapping[str, ToolSpec] = field(default_factory=lambda: MappingProxyType({}))
    grants: frozenset[str] = frozenset()
    workspace_roots: tuple[Path, ...] = ()
    mcp_tools: frozenset[str] = frozenset()
    budget: ToolBudget = field(default_factory=ToolBudget)
    secrets: tuple[str, ...] = ()
    agent_id: str = ""
    role: str = ""
    parent_agent_id: str = ""
    root_agent_id: str = ""
    can_delegate: bool = False
    delegate_agent_ids: frozenset[str] = frozenset()
    message_agent_ids: frozenset[str] = frozenset()
    status_task_ids: frozenset[str] = frozenset()
    wait_child_task_ids: frozenset[str] = frozenset()
    cancel_task_ids: frozenset[str] = frozenset()
    remaining_depth: int = 0
    remaining_children: int = 0
    remaining_hops: int = 0


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    status: str
    effect: str
    quiesced: bool
    model_text: str
    result_ref: str | None
    error_code: str | None


def make_run_context(run_id: str, task_id: str = "", actor_id: str = "local-host") -> RunContext:
    """P0 默认无 ToolSpec/grant；只有可信 Runner 能附加不可变快照。"""
    return RunContext(run_id=run_id, task_id=task_id, actor_id=actor_id)


def intersect_effective_grants(parent_run, worker_capability, task_requested,
                               host_ceiling=P2_HOST_GRANT_CEILING) -> frozenset[str]:
    """四个可信上界求交；畸形输入失败关闭，P2 不开放 Workspace/网络/进程。"""
    def grant_set(value):
        if (not isinstance(value, (set, frozenset, list, tuple))
                or any(not isinstance(item, str) for item in value)):
            return frozenset()
        return frozenset(value)

    return (grant_set(parent_run) & grant_set(worker_capability)
            & grant_set(task_requested) & grant_set(host_ceiling)
            & P2_HOST_GRANT_CEILING)


def _strict_schema(schema: dict) -> dict:
    schema = deepcopy(schema)

    def visit(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    Draft202012Validator.check_schema(schema)
    return schema


def snapshot_builtin_tools() -> Mapping[str, ToolSpec]:
    """仅从明确审核过的内建工具名单冻结规格；不授予 Run 权限。"""
    from backend.skills import TOOL_DEFINITIONS, TOOL_FUNCTIONS

    specs = {}
    for definition in TOOL_DEFINITIONS:
        function = definition["function"]
        name = function["name"]
        if name not in _BUILTIN_POLICY or name not in TOOL_FUNCTIONS:
            continue
        effect, paths, approval = _BUILTIN_POLICY[name]
        specs[name] = ToolSpec(
            name=name,
            schema=_strict_schema(function["parameters"]),
            handler=TOOL_FUNCTIONS[name],
            effects=frozenset({effect}),
            path_fields=paths,
            idempotent=approval == "never",
            approval=approval,
            timeout_seconds=30.0,
        )
    return MappingProxyType(specs)


def control_dispatch(_context: RunContext, _name: str, _arguments: dict) -> dict:
    """纯控制预检确认；A 的单事务稍后提交实际动作和工具结果。"""
    return {"accepted": True}


def snapshot_internal_tools() -> Mapping[str, ToolSpec]:
    """固定内部控制面规格；真正副作用仅由 A 的 Run 事务提交。"""
    from backend.skills.control_skill import CONTROL_TOOLS

    return MappingProxyType({name: ToolSpec(
        name=name, schema=_strict_schema(schema), handler=control_dispatch,
        effects=frozenset({f"control.{name}"}), idempotent=name == "get_task_status",
        approval="never", timeout_seconds=30.0, origin="internal",
    ) for name, (_description, schema) in CONTROL_TOOLS.items()})


def make_builtin_run_context(run_id: str, task_id: str = "", actor_id: str = "local-host") -> RunContext:
    """本地主机 P0 固定只读能力：仅无路径的 builtin 工具可执行。"""
    return RunContext(
        run_id=run_id,
        task_id=task_id,
        actor_id=actor_id,
        tool_specs=snapshot_builtin_tools(),
        grants=frozenset({"read_only"}),
    )


def make_frozen_run_context(run_row: dict) -> RunContext:
    """只恢复与当前审核内建规格完全一致的 Run 快照能力。"""
    if not isinstance(run_row, dict):
        return make_run_context("")
    context = make_run_context(run_row.get("id", ""), run_row.get("task_id", ""))
    try:
        if not all(isinstance(run_row.get(field), str) and run_row[field]
                   for field in ("id", "task_id", "session_id")):
            return make_run_context("")
        snapshot = json.loads(run_row["tool_snapshot_json"])
        frozen = snapshot["specs"]
        grants = snapshot["grants"]
        if (type(snapshot.get("v")) is not int or snapshot["v"] != 1
                or not isinstance(frozen, dict)
                or not isinstance(grants, list)
                or not all(isinstance(grant, str) for grant in grants)):
            return context
        current = {**snapshot_builtin_tools(), **snapshot_internal_tools()}
        allowed = {name: spec for name, spec in current.items() if frozen.get(name) == {
            "schema": spec.schema,
            "effects": sorted(spec.effects),
            "path_fields": list(spec.path_fields),
            "idempotent": spec.idempotent,
            "approval": spec.approval,
            "timeout_seconds": spec.timeout_seconds,
            "origin": spec.origin,
        }}
        return RunContext(
            run_id=context.run_id,
            task_id=context.task_id,
            tool_specs=MappingProxyType(allowed),
            grants=frozenset(grants) & P2_HOST_GRANT_CEILING,
        )
    except (KeyError, TypeError, ValueError):
        return context


def _path_values(arguments: dict, pointer: str) -> list[str]:
    values = [arguments]
    for part in pointer.strip("/").split("/"):
        next_values = []
        for value in values:
            if part == "*" and isinstance(value, list):
                next_values.extend(value)
            elif isinstance(value, dict) and part in value:
                next_values.append(value[part])
        values = next_values
    return values


def _redact(value, secrets: tuple[str, ...]):
    if isinstance(value, dict):
        return {
            key: "[redacted]" if any(word in str(key).lower() for word in _SECRET_FIELDS) else _redact(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
    return value


def _control_rejection(context: RunContext, name: str, arguments: dict) -> str | None:
    if any(field in arguments for field in (
        "from_agent_id", "actor_id", "agent_id", "grant", "requested_grant",
        "root_path", "workspace_root", "parent_agent_id", "root_agent_id",
    )):
        return "invalid_arguments"
    if (not context.agent_id or not context.run_id or not context.task_id
            or context.role not in {"supervisor", "worker"}):
        return "control_unavailable"
    if name == "delegate_task":
        if context.role != "supervisor" and not context.can_delegate:
            return "tool_not_granted"
        if not all(type(value) is int and value > 0 for value in (
            context.remaining_depth, context.remaining_children, context.remaining_hops,
        )):
            return "limit_exceeded"
        if arguments["assignee_agent_id"] not in context.delegate_agent_ids:
            return "not_found"
    elif name == "send_agent_message":
        if type(context.remaining_hops) is not int or context.remaining_hops <= 0:
            return "limit_exceeded"
        if arguments["to_agent_id"] not in context.message_agent_ids:
            return "not_found"
    elif name == "get_task_status":
        if arguments["task_id"] not in context.status_task_ids:
            return "not_found"
    elif name == "wait_for_tasks":
        if not set(arguments["task_ids"]) <= context.wait_child_task_ids:
            return "not_found"
    elif name == "cancel_task":
        if arguments["task_id"] not in context.cancel_task_ids:
            return "not_found"
    return None


def _outcome(context: RunContext, status: str, effect: str, code: str | None = None,
             *, quiesced: bool = True, text: str = "") -> ToolOutcome:
    return ToolOutcome(
        call_id=context.call_id,
        status=status,
        effect=effect,
        quiesced=quiesced,
        model_text=(text if status == "ok" else f"Tool unavailable: {code}")[:MAX_MODEL_CHARS],
        result_ref=None,
        error_code=code,
    )


async def execute_tool(run_context: RunContext, tool_name: str, arguments: str | dict) -> ToolOutcome:
    """只返回安全摘要；拒绝时不调用 handler，异常正文不外送。"""
    started = time.monotonic()
    effect = "read_only"
    try:
        if isinstance(arguments, str):
            raw = arguments.encode("utf-8")
            if len(raw) > MAX_ARGUMENT_BYTES:
                return _outcome(run_context, "error", effect, "limit_exceeded")
            arguments = json.loads(arguments)
        elif isinstance(arguments, dict):
            raw = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
            if len(raw) > MAX_ARGUMENT_BYTES:
                return _outcome(run_context, "error", effect, "limit_exceeded")
        else:
            return _outcome(run_context, "error", effect, "invalid_arguments")
        if not isinstance(arguments, dict):
            return _outcome(run_context, "error", effect, "invalid_arguments")
    except (UnicodeError, ValueError, TypeError):
        return _outcome(run_context, "error", effect, "invalid_arguments")

    spec = run_context.tool_specs.get(tool_name)
    if not spec or spec.name != tool_name:
        return _outcome(run_context, "error", effect, "tool_not_granted")
    if spec.origin == "internal" and spec != snapshot_internal_tools().get(tool_name):
        return _outcome(run_context, "error", effect, "tool_not_granted")
    effect = "read_only" if spec.effects <= {"read_only", "filesystem.read"} else (
        "idempotent" if spec.idempotent else "effectful"
    )
    try:
        if not isinstance(spec.schema, dict):
            raise ValueError("schema")
        Draft202012Validator(_strict_schema(spec.schema)).validate(arguments)
    except Exception:
        return _outcome(run_context, "error", effect, "invalid_arguments")
    if not spec.effects <= run_context.grants:
        return _outcome(run_context, "error", effect, "tool_not_granted")
    if spec.origin == "internal":
        rejection = _control_rejection(run_context, tool_name, arguments)
        if rejection:
            return _outcome(run_context, "error", effect, rejection)

    resolved_paths = []
    if spec.path_fields:
        if not run_context.workspace_roots:
            return _outcome(run_context, "error", effect, "path_not_granted")
        policy = PathPolicy(allowed_roots=tuple(Path(root).resolve() for root in run_context.workspace_roots))
        try:
            for pointer in spec.path_fields:
                values = _path_values(arguments, pointer)
                if not values and pointer != "/path":
                    raise HarnessError("missing path")
                for value in values:
                    if not isinstance(value, str) or not value:
                        raise HarnessError("invalid path")
                    resolved_paths.append(str(policy.resolve(value, operation=tool_name)))
            if tool_name == "list_dir" and not resolved_paths:
                resolved_paths.append(str(policy.resolve(".", operation=tool_name)))
        except (HarnessError, OSError, ValueError):
            return _outcome(run_context, "error", effect, "path_not_granted")

    if spec.origin not in {"builtin", "internal"}:
        if not spec.origin.startswith("mcp:") or tool_name not in run_context.mcp_tools:
            return _outcome(run_context, "error", effect, "mcp_not_approved")
    if not run_context.budget.reserve(mcp=spec.origin.startswith("mcp:")):
        return _outcome(run_context, "error", effect, "limit_exceeded")

    action_hash = hashlib.sha256(json.dumps(
        [tool_name, arguments, sorted(spec.effects), resolved_paths, spec.origin],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    call_hash = hashlib.sha256(str(run_context.call_id).encode("utf-8")).hexdigest()[:16]
    if spec.approval in {"always", "on_risk"}:
        logger.info("tool rejected run=%s call_hash=%s name=%s code=approval_unavailable hash=%s",
                    run_context.run_id, call_hash, tool_name, action_hash)
        return _outcome(run_context, "error", effect, "approval_unavailable")
    if spec.approval != "never":
        return _outcome(run_context, "error", effect, "approval_unavailable")

    async_task = None
    try:
        if spec.origin == "internal":
            result = spec.handler(run_context, tool_name, arguments)
        elif inspect.iscoroutinefunction(spec.handler):
            async_task = asyncio.create_task(spec.handler(**arguments))
            result = await asyncio.wait_for(async_task, timeout=spec.timeout_seconds)
        else:
            result = await asyncio.wait_for(asyncio.to_thread(spec.handler, **arguments),
                                            timeout=spec.timeout_seconds)
        if spec.origin == "internal" and result != {"accepted": True}:
            return _outcome(run_context, "error", effect, "control_unavailable")
        raw_result = result if isinstance(result, str) else json.dumps(
            result, ensure_ascii=False, default=str, allow_nan=False
        )
        raw_bytes = len(raw_result.encode("utf-8"))
        if raw_bytes > MAX_RAW_BYTES:
            return _outcome(run_context, "error", effect, "limit_exceeded")
        safe_result = _redact(result if isinstance(result, str) else json.loads(raw_result), run_context.secrets)
        model_result = json.dumps(safe_result, ensure_ascii=False, allow_nan=False)
        logger.info("%s run=%s call_hash=%s name=%s hash=%s bytes=%d ms=%d",
                    "control preflight accepted" if spec.origin == "internal" else "tool completed",
                    run_context.run_id, call_hash, tool_name, action_hash,
                    raw_bytes, int((time.monotonic()-started)*1000))
        return _outcome(run_context, "ok", effect, text=model_result)
    except asyncio.TimeoutError:
        # Coroutine cancellation cannot prove an external side effect did not occur.
        unknown = effect == "effectful"
        return _outcome(run_context, "outcome_unknown" if unknown else "timeout", effect,
                        "timeout_unknown_effect" if unknown else "tool_timeout",
                        quiesced=bool(async_task and async_task.done()) and not unknown)
    except Exception:
        logger.warning("tool failed run=%s call_hash=%s name=%s hash=%s code=tool_failed",
                       run_context.run_id, call_hash, tool_name, action_hash)
        return _outcome(run_context, "error", effect, "tool_failed")
