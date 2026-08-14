"""MCP Manager：连接外部 MCP Server，发现并注册工具。"""

import asyncio
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client


logger = logging.getLogger("agent.mcp")


def _safe_name(name: str) -> str:
    """把任意名称转换为 OpenAI 工具名允许的字符。"""
    return re.sub(r"[^A-Za-z0-9_]", "_", name or "mcp")


def _result_text(result) -> str:
    """把 MCP CallToolResult 转成纯文本。"""
    parts = []
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        parts.append(text if text is not None else str(item))
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
    enabled: bool = True


class MCPManager:
    """负责连接、发现、调用和注册 MCP 工具。"""

    def __init__(self):
        self._configs: dict[str, MCPServerConfig] = {}
        self._discovered: dict[str, list] = {}

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
                enabled=bool(item.get("enabled", True)),
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
            config = MCPServerConfig(
                id=row["id"],
                name=row["name"],
                transport=row["transport"],
                command=row["command"] or "",
                args=args,
                url=row["url"] or "",
                enabled=bool(row["enabled"]),
            )
            self._configs[config.id] = config

    def load_configs(self):
        self._configs = {}
        self.load_env_configs()
        self.load_db_configs()

    def get_configs(self):
        return list(self._configs.values())

    def get_config(self, server_id):
        return self._configs.get(server_id)

    @asynccontextmanager
    async def _open_session(self, config: MCPServerConfig):
        """打开一个 MCP 会话。"""
        if config.transport == "stdio":
            params = StdioServerParameters(
                command=config.command,
                args=config.args,
            )
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        elif config.transport == "sse":
            async with sse_client(config.url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
        else:
            raise ValueError(f"不支持的 MCP 传输方式: {config.transport}")

    async def discover(self, config: MCPServerConfig):
        """连接服务器并发现工具。"""
        async with self._open_session(config) as session:
            result = await session.list_tools()
            return list(getattr(result, "tools", []) or [])

    async def call(self, config: MCPServerConfig, tool_name: str, arguments: dict):
        """调用 MCP 工具并返回文本结果。"""
        async with self._open_session(config) as session:
            result = await session.call_tool(tool_name, arguments or {})
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

    def make_function(self, config: MCPServerConfig, tool):
        """为 MCP 工具生成可被 Agent 调用的同步函数。"""
        tool_name = getattr(tool, "name", "unknown")

        async def _call(arguments: dict):
            return await self.call(config, tool_name, arguments or {})

        def _sync_call(**arguments):
            return asyncio.run(_call(arguments))

        return _sync_call

    async def test_server(self, config: MCPServerConfig):
        """测试连接并返回发现的工具。"""
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
        functions = {}
        errors = []

        for config in self.get_configs():
            if not config.enabled:
                continue
            try:
                tools = await self.discover(config)
                self._discovered[config.id] = tools
                for tool in tools:
                    full_name, definition = self.build_definition(config, tool)
                    definitions.append(definition)
                    functions[full_name] = self.make_function(config, tool)
                logger.info(
                    "MCP 工具发现完成：server=%s，tools=%d",
                    config.name,
                    len(tools),
                )
            except Exception as exc:
                errors.append({"server": config.name, "error": str(exc)})
                logger.warning("MCP 连接失败：server=%s，error=%s", config.name, exc)

        register_mcp_tools(definitions, functions)
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
