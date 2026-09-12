"""MCP Manager：连接外部 MCP Server，发现并注册工具。"""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from jsonschema import Draft202012Validator


logger = logging.getLogger("agent.mcp")


def _safe_name(name: str) -> str:
    """把任意名称转换为 OpenAI 工具名允许的字符。"""
    return re.sub(r"[^A-Za-z0-9_]", "_", name or "mcp")


def _result_text(result) -> str:
    """把 MCP CallToolResult 转成纯文本。"""
    parts = []
    total = 0
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        part = text if text is not None else str(item)
        total += len(part.encode("utf-8"))
        if total > 256 * 1024:
            raise RuntimeError("limit_exceeded")
        parts.append(part)
    return "\n".join(parts)


@dataclass
class MCPServerConfig:
    """一个 MCP Server 的配置。"""

    id: str
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list = field(default_factory=list)
    url: str = ""
    enabled: bool = False
    approved_config_hash: str = ""
    approved_tools: dict[str, dict] = field(default_factory=dict)


class MCPNotApproved(PermissionError):
    pass


def config_hash(config: MCPServerConfig) -> str:
    """审批摘要覆盖 stdio 配置与可执行文件内容。"""
    if config.transport != "stdio":
        raise MCPNotApproved("mcp_not_approved")  # URL transport 缺连接点策略 hook。
    executable = Path(config.command)
    if not executable.is_absolute() or not executable.is_file():
        raise MCPNotApproved("mcp_not_approved")
    if not isinstance(config.args, list) or not all(isinstance(arg, str) for arg in config.args):
        raise MCPNotApproved("mcp_not_approved")
    digest = hashlib.sha256()
    with executable.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    for arg in config.args:
        if Path(arg).suffix.lower() in {".py", ".js", ".mjs", ".ps1", ".bat", ".cmd"}:
            script = Path(arg)
            if not script.is_absolute() or not script.is_file():
                raise MCPNotApproved("mcp_not_approved")
            with script.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
    canonical = json.dumps(
        [config.transport, str(executable.resolve()), config.args, digest.hexdigest()],
        ensure_ascii=False, separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def schema_hash(schema: dict) -> str:
    if not isinstance(schema, dict):
        raise MCPNotApproved("mcp_not_approved")
    encoded = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise MCPNotApproved("mcp_not_approved")
    Draft202012Validator.check_schema(schema)
    return hashlib.sha256(encoded).hexdigest()


class MCPManager:
    """负责连接、发现、调用和注册 MCP 工具。"""

    def __init__(self):
        self._configs: dict[str, MCPServerConfig] = {}
        self._discovered: dict[str, list] = {}
        self._active_calls: dict[str, int] = {}

    def load_env_configs(self):
        raw = os.getenv("MCP_SERVERS", "").strip()
        if not raw:
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("MCP_SERVERS 不是合法 JSON：%s", exc)
            return
        for item in data or []:
            config = MCPServerConfig(
                id=item.get("id") or str(uuid.uuid4()),
                name=item.get("name", "mcp"),
                transport=item.get("transport", "stdio"),
                command=item.get("command", ""),
                args=item.get("args", []),
                url=item.get("url", ""),
                enabled=False,
            )
            self._configs[config.id] = config

    def load_db_configs(self):
        from backend.db import db

        rows = db.list_mcp_servers()
        for row in rows:
            try:
                args = json.loads(row["args_json"]) if row["args_json"] else []
            except json.JSONDecodeError:
                args = []
            try:
                approved_tools = json.loads(row.get("approved_tools_json") or "{}")
                if not isinstance(approved_tools, dict):
                    approved_tools = {}
            except (TypeError, json.JSONDecodeError):
                approved_tools = {}
            config = MCPServerConfig(
                id=row["id"],
                name=row["name"],
                transport=row["transport"],
                command=row["command"] or "",
                args=args,
                url=row["url"] or "",
                enabled=bool(row.get("enabled", False)) and bool(row.get("approved_config_hash")),
                approved_config_hash=row.get("approved_config_hash") or "",
                approved_tools=approved_tools,
            )
            self._configs[config.id] = config

    def load_configs(self):
        self._configs = {}
        self._discovered = {}
        self.load_env_configs()
        self.load_db_configs()

    def get_configs(self):
        return list(self._configs.values())

    def get_config(self, server_id):
        return self._configs.get(server_id)

    def _require_approved(self, config: MCPServerConfig, tool_name: str | None = None, schema: dict | None = None):
        if not config.enabled or not config.approved_config_hash:
            raise MCPNotApproved("mcp_not_approved")
        if config_hash(config) != config.approved_config_hash:
            raise MCPNotApproved("mcp_not_approved")
        if tool_name is not None:
            approved = config.approved_tools.get(tool_name)
            if (
                not isinstance(approved, dict)
                or schema is None
                or schema_hash(schema) != approved.get("schema_hash")
                or not isinstance(approved.get("effects"), list)
                or not all(isinstance(effect, str) for effect in approved["effects"])
                or approved.get("approval") not in {"never", "always", "on_risk"}
            ):
                raise MCPNotApproved("mcp_not_approved")

    @asynccontextmanager
    async def _open_session(self, config: MCPServerConfig):
        """打开一个 MCP 会话。"""
        self._require_approved(config)
        if config.transport == "stdio":
            with tempfile.TemporaryDirectory(prefix="reactron-mcp-") as cwd:
                params = StdioServerParameters(
                    command=config.command,
                    args=config.args,
                    cwd=cwd,
                    env={"SystemRoot": os.environ.get("SystemRoot", "")} if os.name == "nt" else {},
                )
                async with stdio_client(params) as (read, write):
                    async with ClientSession(read, write) as session:
                        await asyncio.wait_for(session.initialize(), timeout=10)
                        yield session
        else:
            raise MCPNotApproved("mcp_not_approved")

    async def discover(self, config: MCPServerConfig):
        """连接服务器并发现工具。"""
        self._require_approved(config)
        async with asyncio.timeout(15):
            async with self._open_session(config) as session:
                result = await session.list_tools()
            return list(getattr(result, "tools", []) or [])

    async def call(self, config: MCPServerConfig, tool_name: str, arguments: dict):
        """调用 MCP 工具并返回文本结果。"""
        tools = self._discovered.get(config.id, [])
        matching = next((tool for tool in tools if getattr(tool, "name", None) == tool_name), None)
        self._require_approved(config, tool_name, getattr(matching, "inputSchema", None))
        active = self._active_calls.get(config.id, 0)
        if active >= 4:
            raise RuntimeError("limit_exceeded")
        self._active_calls[config.id] = active + 1
        try:
            async with asyncio.timeout(30):
                async with self._open_session(config) as session:
                    result = await session.call_tool(tool_name, arguments or {})
        finally:
            self._active_calls[config.id] -= 1
        if getattr(result, "isError", False):
            raise RuntimeError(_result_text(result) or f"MCP 工具调用失败: {tool_name}")
        return _result_text(result)

    def build_definition(self, config: MCPServerConfig, tool):
        """把 MCP 工具转换为 OpenAI 工具 JSON Schema。"""
        tool_name = getattr(tool, "name", "unknown")
        description = getattr(tool, "description", "") or f"MCP 工具 {tool_name}"
        input_schema = getattr(tool, "inputSchema", None)
        if not isinstance(input_schema, dict):
            input_schema = {"type": "object", "properties": {}}
        full_name = f"mcp_{_safe_name(config.name)}_{_safe_name(tool_name)}"
        definition = {
            "type": "function",
            "function": {
                "name": full_name,
                "description": f"[MCP:{config.name}] {description}",
                "parameters": input_schema,
            },
        }
        return full_name, definition

    def snapshot_tool_specs(self):
        """仅把已批准且 schema 未变的 MCP Tool 交给可信 Runner。"""
        from backend.tool_gateway import ToolSpec

        specs = {}
        for config in self.get_configs():
            try:
                self._require_approved(config)
            except Exception:
                continue
            for tool in self._discovered.get(config.id, []):
                remote_name = getattr(tool, "name", "")
                schema = getattr(tool, "inputSchema", None)
                try:
                    self._require_approved(config, remote_name, schema)
                except Exception:
                    continue
                full_name, _ = self.build_definition(config, tool)
                approved = config.approved_tools[remote_name]

                async def invoke(_config=config, _name=remote_name, **arguments):
                    return await self.call(_config, _name, arguments)

                specs[full_name] = ToolSpec(
                    name=full_name,
                    schema=schema,
                    handler=invoke,
                    effects=frozenset(approved["effects"]),
                    approval=approved["approval"],
                    idempotent=bool(approved.get("idempotent", False)),
                    timeout_seconds=30.0,
                    origin=f"mcp:{config.id}:{config.approved_config_hash}",
                )
        return MappingProxyType(specs)

    async def test_server(self, config: MCPServerConfig):
        """测试连接并返回发现的工具。"""
        self._require_approved(config)
        tools = await self.discover(config)
        self._discovered[config.id] = tools
        return {
            "server": config.name,
            "tool_count": len(tools),
            "tools": [getattr(tool, "name", "unknown") for tool in tools],
        }

    async def refresh_registry(self) -> dict:
        """重新连接所有服务器并注册工具到 skills。"""
        from backend.skills import register_mcp_tools

        self.load_configs()
        definitions = []
        errors = []

        for config in self.get_configs()[:16]:
            try:
                self._require_approved(config)
            except Exception:
                continue
            try:
                tools = await self.discover(config)
                if len(tools) > 64:
                    raise MCPNotApproved("mcp_not_approved")
                self._discovered[config.id] = tools
                schema_bytes = 0
                for tool in tools:
                    try:
                        self._require_approved(config, getattr(tool, "name", ""), getattr(tool, "inputSchema", None))
                        schema_bytes += len(json.dumps(tool.inputSchema, ensure_ascii=False).encode("utf-8"))
                        if schema_bytes > 512 * 1024:
                            raise MCPNotApproved("mcp_not_approved")
                    except Exception:
                        continue
                    full_name, definition = self.build_definition(config, tool)
                    definitions.append(definition)
                logger.info(
                    "MCP 工具发现完成：server=%s，tools=%d",
                    config.name,
                    len(tools),
                )
            except Exception:
                errors.append({"server": config.name, "error": "mcp_unavailable"})
                logger.warning("MCP 连接失败：server=%s", config.name)

        # 只发布模型 schema；不把可执行函数塞入 legacy 注册表。
        register_mcp_tools(definitions, {})
        connected = sum(
            1
            for config in self.get_configs()
            if config.enabled and self._discovered.get(config.id)
        )
        return {
            "server_count": len(self.get_configs()),
            "enabled_count": sum(1 for config in self.get_configs() if config.enabled),
            "connected_count": connected,
            "tool_count": len(definitions),
            "errors": errors,
        }

    def server_status(self):
        return [
            {
                "id": config.id,
                "name": config.name,
                "transport": config.transport,
                "enabled": config.enabled,
                "status": "approved" if config.enabled and config.approved_config_hash else "pending",
                "tool_count": len(self._discovered.get(config.id, [])),
            }
            for config in self.get_configs()
        ]


_manager = None


def get_manager() -> MCPManager:
    """返回 MCP Manager 单例。"""
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager
