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


def make_builtin_run_context(run_id: str, task_id: str = "", actor_id: str = "local-host") -> RunContext:
    """本地主机 P0 固定只读能力：仅无路径的 builtin 工具可执行。"""
    return RunContext(
        run_id=run_id,
        task_id=task_id,
        actor_id=actor_id,
        tool_specs=snapshot_builtin_tools(),
        grants=frozenset({"read_only"}),
    )


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

    if spec.origin != "builtin":
        if not spec.origin.startswith("mcp:") or tool_name not in run_context.mcp_tools:
            return _outcome(run_context, "error", effect, "mcp_not_approved")
    if not run_context.budget.reserve(mcp=spec.origin != "builtin"):
        return _outcome(run_context, "error", effect, "limit_exceeded")

    action_hash = hashlib.sha256(json.dumps(
        [tool_name, arguments, sorted(spec.effects), resolved_paths, spec.origin],
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    if spec.approval in {"always", "on_risk"}:
        logger.info("tool rejected run=%s call=%s name=%s code=approval_unavailable hash=%s",
                    run_context.run_id, run_context.call_id, tool_name, action_hash)
        return _outcome(run_context, "error", effect, "approval_unavailable")
    if spec.approval != "never":
        return _outcome(run_context, "error", effect, "approval_unavailable")

    try:
        if inspect.iscoroutinefunction(spec.handler):
            result = await asyncio.wait_for(spec.handler(**arguments), timeout=spec.timeout_seconds)
        else:
            result = await asyncio.wait_for(asyncio.to_thread(spec.handler, **arguments), timeout=spec.timeout_seconds)
        raw_result = json.dumps(result, ensure_ascii=False, default=str, allow_nan=False)
        raw_bytes = len(raw_result.encode("utf-8"))
        if raw_bytes > MAX_RAW_BYTES:
            return _outcome(run_context, "error", effect, "limit_exceeded")
        safe_result = _redact(json.loads(raw_result), run_context.secrets)
        model_result = json.dumps(safe_result, ensure_ascii=False, allow_nan=False)
        if len(model_result.encode("utf-8")) > MAX_RAW_BYTES:
            return _outcome(run_context, "error", effect, "limit_exceeded")
        logger.info("tool completed run=%s call=%s name=%s hash=%s bytes=%d ms=%d",
                    run_context.run_id, run_context.call_id, tool_name, action_hash,
                    raw_bytes, int((time.monotonic()-started)*1000))
        return _outcome(run_context, "ok", effect, text=model_result)
    except asyncio.TimeoutError:
        unknown = effect == "effectful"
        return _outcome(run_context, "outcome_unknown" if unknown else "timeout", effect,
                        "timeout_unknown_effect" if unknown else "tool_timeout", quiesced=False)
    except Exception:
        logger.warning("tool failed run=%s call=%s name=%s hash=%s code=tool_failed",
                       run_context.run_id, run_context.call_id, tool_name, action_hash)
        return _outcome(run_context, "error", effect, "tool_failed")
