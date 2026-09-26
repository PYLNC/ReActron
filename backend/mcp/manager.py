"""MCP discovery, host approval, frozen specs, and live execution."""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit, urlunsplit

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client


logger = logging.getLogger("agent.mcp")
MAX_TOOLS = 64
MAX_SNAPSHOT_BYTES = 64 * 1024
MAX_RESULT_BYTES = 256 * 1024
_SCRIPT_SUFFIXES = frozenset({".py", ".js", ".mjs", ".ps1", ".bat", ".cmd"})
_ANNOTATIONS = (
    ("readOnlyHint", "read_only_hint"),
    ("destructiveHint", "destructive_hint"),
    ("idempotentHint", "idempotent_hint"),
    ("openWorldHint", "open_world_hint"),
)
_ERROR_CODES = frozenset({
    "mcp_invalid_config", "mcp_not_approved", "mcp_schema_changed",
    "mcp_name_conflict", "mcp_unavailable", "mcp_timeout", "mcp_tool_error",
    "limit_exceeded", "mcp_invalid_arguments",
})
_DRIFT_CODES = frozenset({
    "mcp_invalid_config", "mcp_not_approved", "mcp_schema_changed", "mcp_name_conflict",
})


class MCPError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class MCPNotApproved(PermissionError):
    code = "mcp_not_approved"

    def __init__(self, code: str = "mcp_not_approved"):
        self.code = code
        super().__init__(code)


def _canonical(value) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False,
    ).encode("utf-8")


def _safe_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", name or "tool")


def _model_name(server_id: str, remote_name: str) -> str:
    semantic = (_safe_name(remote_name)[:31] or "tool")
    identity = hashlib.sha256(f"{server_id}\0{remote_name}".encode("utf-8")).hexdigest()[:24]
    return f"mcp_{semantic}_{identity}"


def _normalize_url(raw: str) -> str:
    if not isinstance(raw, str) or not raw or any(ord(char) < 32 for char in raw):
        raise MCPError("mcp_invalid_config")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise MCPError("mcp_invalid_config") from exc
    if (parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.fragment or parsed.query):
        raise MCPError("mcp_invalid_config")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    netloc = f"{host}:{port}" if port is not None else host
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", "", ""))


def _file_digest(path: Path, digest) -> None:
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)


@dataclass
class MCPServerConfig:
    id: str
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    url: str = ""
    enabled: bool = False
    approved_config_hash: str = ""
    approved_tools: dict[str, dict] = field(default_factory=dict)


def normalize_config(config: MCPServerConfig) -> MCPServerConfig:
    if (not isinstance(config.id, str) or not config.id
            or not isinstance(config.name, str) or not 1 <= len(config.name.strip()) <= 80
            or config.transport not in {"stdio", "streamable_http", "sse"}
            or not isinstance(config.args, list)
            or any(not isinstance(arg, str) or "\0" in arg for arg in config.args)):
        raise MCPError("mcp_invalid_config")
    normalized = MCPServerConfig(
        id=config.id, name=config.name.strip(), transport=config.transport,
        enabled=bool(config.enabled), approved_config_hash=config.approved_config_hash or "",
        approved_tools=config.approved_tools if isinstance(config.approved_tools, dict) else {},
    )
    if config.transport == "stdio":
        executable = Path(config.command)
        if not executable.is_absolute() or not executable.is_file() or config.url:
            raise MCPError("mcp_invalid_config")
        normalized.command = str(executable.resolve())
        normalized.args = list(config.args)
        for arg in normalized.args:
            if Path(arg).suffix.lower() in _SCRIPT_SUFFIXES:
                script = Path(arg)
                if not script.is_absolute() or not script.is_file():
                    raise MCPError("mcp_invalid_config")
    else:
        if config.command or config.args:
            raise MCPError("mcp_invalid_config")
        normalized.url = _normalize_url(config.url)
    return normalized


def config_hash(config: MCPServerConfig) -> str:
    try:
        config = normalize_config(config)
        content_digest = hashlib.sha256()
        if config.transport == "stdio":
            _file_digest(Path(config.command), content_digest)
            for arg in config.args:
                if Path(arg).suffix.lower() in _SCRIPT_SUFFIXES:
                    _file_digest(Path(arg), content_digest)
            identity = [config.transport, config.command, config.args, content_digest.hexdigest()]
        else:
            identity = [config.transport, config.url]
        return hashlib.sha256(_canonical(identity)).hexdigest()
    except MCPError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise MCPError("mcp_invalid_config") from exc


def schema_hash(schema: dict) -> str:
    if not isinstance(schema, dict):
        raise MCPError("mcp_schema_changed")
    try:
        encoded = _canonical(schema)
    except (TypeError, ValueError) as exc:
        raise MCPError("mcp_schema_changed") from exc
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        raise MCPError("limit_exceeded")
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise MCPError("mcp_schema_changed") from exc
    return hashlib.sha256(encoded).hexdigest()


def _tool_schema(tool) -> dict:
    schema = getattr(tool, "input_schema", None)
    if schema is None:
        schema = getattr(tool, "inputSchema", None)
    if not isinstance(schema, dict):
        raise MCPError("mcp_schema_changed")
    return schema


def _tool_annotations(tool) -> dict:
    annotations = getattr(tool, "annotations", None)
    normalized = {}
    for public, python_name in _ANNOTATIONS:
        if isinstance(annotations, dict):
            value = annotations.get(public, annotations.get(python_name))
        else:
            value = getattr(annotations, python_name, None) if annotations is not None else None
        if value is not None and type(value) is not bool:
            raise MCPError("mcp_schema_changed")
        normalized[public] = value
    return normalized


def _catalog_entry(config: MCPServerConfig, tool) -> dict:
    remote_name = getattr(tool, "name", None)
    if not isinstance(remote_name, str) or not 1 <= len(remote_name) <= 200:
        raise MCPError("mcp_schema_changed")
    schema = _tool_schema(tool)
    if schema.get("type") != "object":
        raise MCPError("mcp_schema_changed")
    annotations = _tool_annotations(tool)
    read_only = annotations["readOnlyHint"] is True
    policy = {
        "name": remote_name,
        "model_name": _model_name(config.id, remote_name),
        "schema_hash": schema_hash(schema),
        "annotations": annotations,
        "effects": ["read_only"] if read_only else ["mcp.write"],
        "approval": "never" if read_only else "always",
        "idempotent": annotations["idempotentHint"] is True,
    }
    policy["fingerprint"] = hashlib.sha256(_canonical({
        key: policy[key]
        for key in ("name", "schema_hash", "annotations", "effects", "approval", "idempotent")
    })).hexdigest()
    description = getattr(tool, "description", None)
    return {
        **policy,
        "description": description[:4000] if isinstance(description, str) else "",
        "input_schema": schema,
    }


def _approved_entry(entry: dict) -> dict:
    return {
        key: entry[key]
        for key in ("model_name", "schema_hash", "annotations", "effects", "approval",
                    "idempotent", "fingerprint")
    }


def _catalog_hash(entries: list[dict]) -> str:
    summary = [
        {key: entry[key] for key in ("name", "schema_hash", "annotations")}
        for entry in sorted(entries, key=lambda item: item["name"])
    ]
    return hashlib.sha256(_canonical(summary)).hexdigest()


def _result_text(result) -> str:
    parts = []
    total = 0
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text is not None:
            part = text
        elif hasattr(item, "model_dump"):
            part = json.dumps(
                item.model_dump(mode="json", by_alias=True), ensure_ascii=False,
                separators=(",", ":"), allow_nan=False,
            )
        else:
            part = str(item)
        total += len(part.encode("utf-8"))
        if total > MAX_RESULT_BYTES:
            raise MCPError("limit_exceeded")
        parts.append(part)
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        try:
            part = json.dumps(
                structured, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise MCPError("mcp_tool_error") from exc
        total += len(part.encode("utf-8"))
        if total > MAX_RESULT_BYTES:
            raise MCPError("limit_exceeded")
        parts.append(part)
    return "\n".join(parts)


class MCPManager:
    def __init__(self, database=None):
        self.database = database
        self._configs: dict[str, MCPServerConfig] = {}
        self._discovered: dict[str, tuple[str, tuple]] = {}
        self._refresh_errors: dict[str, str] = {}
        self._published_specs = MappingProxyType({})
        self._active_calls: dict[str, int] = {}
        self._state_lock = threading.RLock()

    def _database(self):
        if self.database is not None:
            return self.database
        from backend.db import db
        return db

    @staticmethod
    def _config_from_row(row: dict) -> MCPServerConfig:
        try:
            args = json.loads(row.get("args_json") or "[]")
            approved = json.loads(row.get("approved_tools_json") or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise MCPError("mcp_invalid_config") from exc
        if (not isinstance(args, list) or any(not isinstance(arg, str) for arg in args)
                or not isinstance(approved, dict)):
            raise MCPError("mcp_invalid_config")
        # Keep pending/drifted rows visible in server_status; live operations normalize.
        return MCPServerConfig(
            id=row["id"], name=row["name"], transport=row["transport"],
            command=row.get("command") or "", args=args, url=row.get("url") or "",
            enabled=bool(row.get("enabled")),
            approved_config_hash=row.get("approved_config_hash") or "",
            approved_tools=approved,
        )

    def _load_server(self, server_id: str) -> MCPServerConfig:
        row = self._database().get_mcp_server(server_id)
        if not row:
            raise MCPError("not_found")
        return self._config_from_row(row)

    @staticmethod
    def _env_configs() -> dict[str, MCPServerConfig]:
        raw = os.getenv("MCP_SERVERS", "").strip()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("MCP_SERVERS is invalid JSON")
            return {}
        configs = {}
        for item in data if isinstance(data, list) else []:
            try:
                config = MCPServerConfig(
                    id=item.get("id") or str(uuid.uuid4()), name=item.get("name", "mcp"),
                    transport=item.get("transport", "stdio"), command=item.get("command", ""),
                    args=item.get("args", []), url=item.get("url", ""), enabled=False,
                )
                if (not isinstance(config.id, str) or not config.id
                        or not isinstance(config.args, list)
                        or any(not isinstance(arg, str) for arg in config.args)):
                    raise ValueError
            except (AttributeError, TypeError, ValueError):
                continue
            configs[config.id] = config
        return configs

    def load_env_configs(self):
        with self._state_lock:
            configs = dict(self._configs)
            configs.update(self._env_configs())
            self._configs = configs

    def load_db_configs(self):
        configs = {}
        for row in self._database().list_mcp_servers():
            try:
                config = self._config_from_row(row)
            except MCPError:
                logger.warning("Invalid MCP config ignored: server_id=%s", row.get("id"))
                continue
            configs[config.id] = config
        with self._state_lock:
            merged = dict(self._configs)
            merged.update(configs)
            self._configs = merged

    def load_configs(self):
        configs = self._env_configs()
        for row in self._database().list_mcp_servers():
            try:
                config = self._config_from_row(row)
            except MCPError:
                logger.warning("Invalid MCP config ignored: server_id=%s", row.get("id"))
                continue
            configs[config.id] = config
        with self._state_lock:
            discovered = {
                server_id: value for server_id, value in self._discovered.items()
                if server_id in configs and configs[server_id].enabled
                and value[0] == configs[server_id].approved_config_hash
            }
        published = self._build_specs(configs, discovered)
        with self._state_lock:
            self._configs = configs
            self._discovered = discovered
            self._published_specs = published
            self._refresh_errors = {
                server_id: code for server_id, code in self._refresh_errors.items()
                if server_id in configs
            }

    def get_configs(self):
        with self._state_lock:
            return list(self._configs.values())

    def get_config(self, server_id):
        with self._state_lock:
            return self._configs.get(server_id)

    @staticmethod
    def _error_code(value) -> str:
        code = value if isinstance(value, str) else getattr(value, "code", None)
        return code if code in _ERROR_CODES else "mcp_unavailable"

    def _unpublish_server(self, server_id: str, error) -> None:
        """Atomically revoke one server without DB or network access."""
        code = self._error_code(error)
        prefix = f"mcp:{server_id}:"
        with self._state_lock:
            discovered = dict(self._discovered)
            discovered.pop(server_id, None)
            errors = dict(self._refresh_errors)
            errors[server_id] = code
            specs = {
                name: spec for name, spec in self._published_specs.items()
                if not spec.origin.startswith(prefix)
            }
            self._discovered = discovered
            self._refresh_errors = errors
            self._published_specs = MappingProxyType(specs)

    @staticmethod
    def _require_config_approved(config: MCPServerConfig, *, enabled=True):
        if ((enabled and not config.enabled) or not config.approved_config_hash
                or not config.approved_tools
                or config_hash(config) != config.approved_config_hash):
            raise MCPNotApproved()

    @staticmethod
    def _require_tool_approved(config: MCPServerConfig, entry: dict):
        approved = config.approved_tools.get(entry["name"])
        if not isinstance(approved, dict) or approved != _approved_entry(entry):
            raise MCPNotApproved("mcp_schema_changed")

    @asynccontextmanager
    async def _open_session(self, config: MCPServerConfig):
        if config.transport == "stdio":
            with tempfile.TemporaryDirectory(prefix="reactron-mcp-") as cwd:
                params = StdioServerParameters(
                    command=config.command, args=config.args, cwd=cwd,
                    env={"SystemRoot": os.environ.get("SystemRoot", "")} if os.name == "nt" else {},
                )
                async with stdio_client(params) as streams:
                    async with ClientSession(*streams[:2]) as session:
                        await asyncio.wait_for(session.initialize(), timeout=10)
                        yield session
            return
        transport = streamable_http_client if config.transport == "streamable_http" else sse_client
        async with transport(config.url) as streams:
            async with ClientSession(*streams[:2]) as session:
                await asyncio.wait_for(session.initialize(), timeout=10)
                yield session

    @staticmethod
    async def _list_tools(session) -> list:
        tools = []
        cursor = None
        seen_cursors = set()
        while True:
            params = types.PaginatedRequestParams(cursor=cursor) if cursor else None
            result = await session.list_tools(params=params)
            tools.extend(list(getattr(result, "tools", []) or []))
            if len(tools) > MAX_TOOLS:
                raise MCPError("limit_exceeded")
            cursor = getattr(result, "next_cursor", None)
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in seen_cursors:
                raise MCPError("mcp_unavailable")
            seen_cursors.add(cursor)

    async def _discover(self, config: MCPServerConfig) -> tuple[list, list[dict]]:
        try:
            config = normalize_config(config)
            async with asyncio.timeout(15):
                async with self._open_session(config) as session:
                    tools = await self._list_tools(session)
            entries = [_catalog_entry(config, tool) for tool in tools]
            if (len({entry["name"] for entry in entries}) != len(entries)
                    or len({entry["model_name"] for entry in entries}) != len(entries)):
                raise MCPError("mcp_name_conflict")
            return tools, entries
        except (MCPError, MCPNotApproved):
            raise
        except Exception as exc:
            raise MCPError("mcp_unavailable") from exc

    def _assert_global_names(self, server_id: str, entries: list[dict]):
        names = {entry["model_name"] for entry in entries}
        if len(names) != len(entries):
            raise MCPError("mcp_name_conflict")
        for row in self._database().list_mcp_servers():
            if row["id"] == server_id:
                continue
            try:
                approved = json.loads(row.get("approved_tools_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            other = {
                item.get("model_name") for item in approved.values()
                if isinstance(item, dict) and isinstance(item.get("model_name"), str)
            }
            if names & other:
                raise MCPError("mcp_name_conflict")

    @staticmethod
    def _server_view(config: MCPServerConfig, *, config_digest: str | None = None) -> dict:
        status = "enabled" if config.enabled else (
            "approved_disabled" if config.approved_config_hash else "pending"
        )
        return {
            "id": config.id, "name": config.name, "transport": config.transport,
            "enabled": config.enabled, "status": status,
            "config_hash": config_digest or config_hash(config),
        }

    async def discover_server(self, server_id: str) -> dict:
        config = self._load_server(server_id)
        try:
            _tools, entries = await self._discover(config)
            digest = config_hash(config)
        except (MCPError, MCPNotApproved) as exc:
            self._unpublish_server(server_id, exc)
            raise
        with self._state_lock:
            configs = dict(self._configs)
            configs[server_id] = config
            errors = dict(self._refresh_errors)
            errors.pop(server_id, None)
            self._configs, self._refresh_errors = configs, errors
        return {
            "server": self._server_view(config, config_digest=digest),
            "catalog_hash": _catalog_hash(entries), "tools": entries,
        }

    async def approve_server(self, server_id: str, tool_names: list[str]) -> dict:
        if (not isinstance(tool_names, list) or not tool_names
                or any(not isinstance(name, str) or not name for name in tool_names)
                or len(set(tool_names)) != len(tool_names)):
            raise MCPError("mcp_invalid_config")
        config = self._load_server(server_id)
        try:
            _tools, entries = await self._discover(config)
            by_name = {entry["name"]: entry for entry in entries}
            if not set(tool_names) <= set(by_name):
                raise MCPError("mcp_schema_changed")
            selected = [by_name[name] for name in tool_names]
            self._assert_global_names(server_id, selected)
            approved = {entry["name"]: _approved_entry(entry) for entry in selected}
            digest = config_hash(config)
            self._database().approve_mcp_server(server_id, digest, approved)
        except (MCPError, MCPNotApproved) as exc:
            self._unpublish_server(server_id, exc)
            raise
        config = self._load_server(server_id)
        with self._state_lock:
            configs = dict(self._configs)
            configs[server_id] = config
            discovered = dict(self._discovered)
            discovered.pop(server_id, None)
            errors = dict(self._refresh_errors)
            errors.pop(server_id, None)
        published = self._build_specs(configs, discovered)
        with self._state_lock:
            self._configs, self._discovered = configs, discovered
            self._refresh_errors = errors
            self._published_specs = published
        return {
            "server": self._server_view(config, config_digest=digest),
            "catalog_hash": _catalog_hash(selected), "tools": selected,
        }

    def _select_approved(self, config: MCPServerConfig, entries: list[dict]) -> list[dict]:
        self._require_config_approved(config, enabled=False)
        by_name = {entry["name"]: entry for entry in entries}
        selected = []
        for remote_name in config.approved_tools:
            entry = by_name.get(remote_name)
            if not entry:
                raise MCPNotApproved("mcp_schema_changed")
            self._require_tool_approved(config, entry)
            selected.append(entry)
        return selected

    def _validate_approved_catalog(self, config: MCPServerConfig, entries: list[dict]) -> list[dict]:
        selected = self._select_approved(config, entries)
        self._assert_global_names(config.id, selected)
        return selected

    async def enable_server(self, server_id: str) -> dict:
        try:
            config = self._load_server(server_id)
            tools, entries = await self._discover(config)
            selected = self._validate_approved_catalog(config, entries)
            row = self._database().set_mcp_server_enabled(server_id, True)
            config = self._config_from_row(row)
            with self._state_lock:
                configs = dict(self._configs)
                configs[server_id] = config
                discovered = dict(self._discovered)
                discovered[server_id] = (config.approved_config_hash, tuple(tools))
                errors = dict(self._refresh_errors)
                errors.pop(server_id, None)
            published = self._build_specs(configs, discovered)
        except (MCPError, MCPNotApproved) as exc:
            row = self._database().get_mcp_server(server_id)
            if row and row["enabled"]:
                self._database().set_mcp_server_enabled(server_id, False)
            self._unpublish_server(server_id, exc)
            raise
        with self._state_lock:
            self._configs, self._discovered = configs, discovered
            self._refresh_errors = errors
            self._published_specs = published
        return {
            "server": self._server_view(config),
            "catalog_hash": _catalog_hash(selected), "tools": selected,
        }

    async def disable_server(self, server_id: str) -> dict:
        row = self._database().set_mcp_server_enabled(server_id, False)
        config = self._config_from_row(row)
        with self._state_lock:
            configs = dict(self._configs)
            configs[server_id] = config
            discovered = dict(self._discovered)
            discovered.pop(server_id, None)
            errors = dict(self._refresh_errors)
            errors.pop(server_id, None)
        published = self._build_specs(configs, discovered)
        with self._state_lock:
            self._configs, self._discovered = configs, discovered
            self._refresh_errors = errors
            self._published_specs = published
        return row

    async def call(self, server_id: str, tool_name: str, arguments: dict):
        if not isinstance(arguments, dict):
            raise MCPError("mcp_invalid_arguments")
        try:
            config = self._load_server(server_id)
            self._require_config_approved(config)
        except (MCPError, MCPNotApproved) as exc:
            self._unpublish_server(server_id, exc)
            raise
        with self._state_lock:
            active = self._active_calls.get(server_id, 0)
            if active >= 4:
                raise MCPError("limit_exceeded")
            active_calls = dict(self._active_calls)
            active_calls[server_id] = active + 1
            self._active_calls = active_calls
        try:
            async with asyncio.timeout(30):
                async with self._open_session(config) as session:
                    tools = await asyncio.wait_for(self._list_tools(session), timeout=15)
                    matching = next((tool for tool in tools if getattr(tool, "name", None) == tool_name), None)
                    if matching is None:
                        raise MCPNotApproved("mcp_schema_changed")
                    self._require_tool_approved(config, _catalog_entry(config, matching))
                    result = await session.call_tool(tool_name, arguments)
        except (MCPError, MCPNotApproved) as exc:
            if self._error_code(exc) in _DRIFT_CODES | {"mcp_unavailable", "mcp_timeout"}:
                self._unpublish_server(server_id, exc)
            raise
        except TimeoutError as exc:
            raise MCPError("mcp_timeout") from exc
        except Exception as exc:
            raise MCPError("mcp_unavailable") from exc
        finally:
            with self._state_lock:
                active_calls = dict(self._active_calls)
                active_calls[server_id] = max(0, active_calls.get(server_id, 1) - 1)
                self._active_calls = active_calls
        if getattr(result, "is_error", False):
            raise MCPError("mcp_tool_error")
        return _result_text(result)

    @staticmethod
    def _handler(manager, server_id: str, remote_name: str):
        async def invoke(**arguments):
            return await manager.call(server_id, remote_name, arguments)
        return invoke

    @staticmethod
    def _definition(config: MCPServerConfig, entry: dict) -> dict:
        return {"type": "function", "function": {
            "name": entry["model_name"],
            "description": f"[MCP:{config.name}] {entry['description']}",
            "parameters": entry["input_schema"],
        }}

    def _build_specs(self, configs: dict[str, MCPServerConfig],
                     discovered: dict[str, tuple[str, tuple]]):
        from backend.tool_gateway import ToolSpec

        specs = {}
        frozen = {}
        for config in configs.values():
            try:
                self._require_config_approved(config)
                cached = discovered.get(config.id)
                if not cached or cached[0] != config.approved_config_hash:
                    raise MCPNotApproved()
                entries = [_catalog_entry(config, tool) for tool in cached[1]]
                selected = self._select_approved(config, entries)
            except (MCPError, MCPNotApproved):
                continue
            for entry in selected:
                name = entry["model_name"]
                if name in specs:
                    raise MCPError("mcp_name_conflict")
                spec = ToolSpec(
                    name=name, schema=entry["input_schema"],
                    handler=self._handler(self, config.id, entry["name"]),
                    effects=frozenset(entry["effects"]), idempotent=entry["idempotent"],
                    approval=entry["approval"], timeout_seconds=30.0,
                    origin=f"mcp:{config.id}:{config.approved_config_hash}",
                )
                specs[name] = spec
                frozen[name] = {
                    "schema": spec.schema, "effects": sorted(spec.effects),
                    "path_fields": list(spec.path_fields), "idempotent": spec.idempotent,
                    "approval": spec.approval, "timeout_seconds": spec.timeout_seconds,
                    "origin": spec.origin,
                }
                if len(_canonical({"v": 1, "specs": frozen})) > MAX_SNAPSHOT_BYTES:
                    raise MCPError("limit_exceeded")
        return MappingProxyType(specs)

    def snapshot_tool_specs(self):
        """Return the immutable published snapshot; never touches DB or network."""
        with self._state_lock:
            return self._published_specs

    def clear_registry(self, error_code="mcp_unavailable"):
        """Fail closed without connecting after startup/refresh failure."""
        from backend.skills import register_mcp_tools

        error_code = self._error_code(error_code)
        with self._state_lock:
            self._discovered = {}
            self._refresh_errors = {
                config.id: error_code for config in self._configs.values() if config.enabled
            }
            self._published_specs = MappingProxyType({})
        register_mcp_tools([], {})

    async def test_server(self, config: MCPServerConfig):
        return await self.discover_server(config.id)

    async def refresh_registry(self) -> dict:
        from backend.skills import register_mcp_tools

        self.load_configs()
        configs = self.get_configs()
        definitions = []
        errors = []
        discovered = {}
        refresh_errors = {}
        for config in configs[:16]:
            if not config.enabled:
                continue
            try:
                self._require_config_approved(config)
                tools, entries = await self._discover(config)
                selected = self._validate_approved_catalog(config, entries)
                discovered[config.id] = (config.approved_config_hash, tuple(tools))
                definitions.extend(self._definition(config, entry) for entry in selected)
            except (MCPError, MCPNotApproved) as exc:
                code = getattr(exc, "code", "mcp_unavailable")
                refresh_errors[config.id] = code
                errors.append({"server": config.name, "error": code})
                logger.warning("MCP refresh failed: server_id=%s code=%s", config.id,
                               code)
        config_map = {config.id: config for config in configs}
        try:
            published = self._build_specs(config_map, discovered)
        except Exception:
            self.clear_registry()
            raise
        with self._state_lock:
            self._discovered = discovered
            self._refresh_errors = refresh_errors
            self._published_specs = published
        register_mcp_tools(definitions, {})
        return {
            "server_count": len(configs),
            "enabled_count": sum(1 for config in configs if config.enabled),
            "connected_count": len(discovered),
            "tool_count": len(definitions), "errors": errors,
        }

    def server_status(self):
        with self._state_lock:
            configs = tuple(self._configs.values())
            discovered = dict(self._discovered)
            errors = dict(self._refresh_errors)
        statuses = []
        for config in configs:
            last_error = errors.get(config.id)
            if last_error:
                last_error = self._error_code(last_error)
                status = "drift" if last_error in _DRIFT_CODES else "error"
            elif config.enabled and config.id in discovered:
                status = "enabled"
                last_error = None
            elif config.enabled:
                last_error = "mcp_unavailable"
                status = "error"
            else:
                status = "approved_disabled" if config.approved_config_hash else "pending"
            statuses.append({
                "id": config.id, "name": config.name, "transport": config.transport,
                "enabled": config.enabled, "status": status, "last_error": last_error,
                "tool_count": len(discovered.get(config.id, ("", ()))[1]),
            })
        return statuses


_manager = None


def get_manager() -> MCPManager:
    global _manager
    if _manager is None:
        _manager = MCPManager()
    return _manager
