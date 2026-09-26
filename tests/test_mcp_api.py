"""Focused MCP Host API/UI contract checks."""

import json
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend import api


ROOT = Path(__file__).resolve().parent.parent


class MCPApiTest(unittest.TestCase):
    def setUp(self):
        self.row = {
            "id": "server-1", "name": "Local", "transport": "stdio",
            "command": "private-command", "args_json": '["private-arg"]',
            "url": "https://private.invalid", "enabled": 0,
            "approved_config_hash": "private-config-hash",
            "approved_tools_json": json.dumps({
                "read": {"schema_hash": "private-schema-hash", "effects": ["read_only"]},
            }),
            "created_at": "now", "updated_at": "now",
        }
        self.database = Mock()
        self.database.list_mcp_servers.return_value = [self.row]
        self.database.get_mcp_server.return_value = self.row
        self.database.create_mcp_server.return_value = {
            **self.row, "id": "new", "enabled": 0, "approved_config_hash": "",
            "approved_tools_json": "{}",
        }
        self.manager = Mock()
        self.manager.server_status.return_value = [{
            "id": "server-1", "status": "drift", "tool_count": 1,
        }]
        catalog = {
            "server": {
                "id": "server-1", "name": "Local", "transport": "stdio",
                "enabled": False, "status": "pending", "config_hash": "private-config-hash",
            },
            "catalog_hash": "private-catalog-hash",
            "tools": [{
                "name": "read", "model_name": "mcp_Local_read", "description": "Read data",
                "input_schema": {"type": "object"}, "schema_hash": "private-schema-hash",
                "effects": ["read_only"], "approval": "never", "idempotent": True,
                "annotations": {"readOnlyHint": True, "openWorldHint": True},
            }],
        }
        self.manager.discover_server = AsyncMock(return_value=catalog)
        self.manager.approve_server = AsyncMock(return_value=catalog)
        self.manager.enable_server = AsyncMock()
        self.manager.disable_server = AsyncMock()
        self.manager.refresh_registry = AsyncMock(return_value={"tool_count": 1})
        application = FastAPI()
        application.include_router(api.router)
        application.dependency_overrides[api._require_local_session] = (
            lambda: SimpleNamespace(actor_id="local-host")
        )
        self.client = TestClient(application)
        self.patches = [
            patch.object(api, "db", self.database),
            patch.object(api, "get_manager", return_value=self.manager),
        ]
        for active in self.patches:
            active.start()

    def tearDown(self):
        for active in reversed(self.patches):
            active.stop()
        self.client.close()

    def test_server_lifecycle_keeps_hashes_and_configuration_server_side(self):
        created = self.client.post("/api/mcp/servers", json={
            "name": "New", "transport": "stdio", "command": "C:\\tools\\server.exe",
            "args": [],
        })
        listed = self.client.get("/api/mcp/servers")
        discovered = self.client.post("/api/mcp/servers/server-1/discover")
        forged = self.client.post("/api/mcp/servers/server-1/approve", json={
            "tool_names": ["read"], "config_hash": "forged",
        })
        approved = self.client.post("/api/mcp/servers/server-1/approve", json={
            "tool_names": ["read"],
        })
        enabled = self.client.put("/api/mcp/servers/server-1/enabled", json={"enabled": True})

        self.assertEqual(created.status_code, 201)
        self.database.create_mcp_server.assert_called_once_with(
            name="New", transport="stdio", command="C:\\tools\\server.exe",
            args=[], url=None, enabled=False,
        )
        self.assertEqual(listed.json()["servers"][0]["status"], "drift")
        for marker in ("private-command", "private-arg", "private.invalid",
                       "private-config-hash", "private-schema-hash"):
            self.assertNotIn(marker, listed.text)
            self.assertNotIn(marker, discovered.text)
        self.assertEqual(discovered.json()["tools"][0]["annotations"], {
            "read_only": True, "destructive": False, "idempotent": True,
            "open_world": True, "requires_approval": False,
        })
        self.assertEqual(forged.status_code, 422)
        self.assertEqual(approved.status_code, 200)
        self.manager.approve_server.assert_awaited_once_with("server-1", ["read"])
        self.assertEqual(enabled.status_code, 200)
        self.manager.enable_server.assert_awaited_once_with("server-1")
        self.manager.refresh_registry.assert_awaited_once()

    def test_transport_validation_and_narrow_mcp_write_capability(self):
        bad_payloads = [
            {"name": "x", "transport": "stdio", "command": "x", "enabled": True},
            {"name": "x" * 81, "transport": "stdio", "command": "x"},
            {"name": "x", "transport": "streamable_http", "url": "https://u:p@host/mcp"},
            {"name": "x", "transport": "streamable_http", "url": "https://host/mcp?token=x"},
            {"name": "x", "transport": "streamable_http", "url": "https://host/mcp?"},
            {"name": "x", "transport": "streamable_http", "url": "https://host/mcp#"},
            {"name": "x", "transport": "sse", "url": "ftp://host/sse"},
        ]
        self.assertTrue(all(
            self.client.post("/api/mcp/servers", json=payload).status_code == 422
            for payload in bad_payloads
        ))
        accepted = self.client.post("/api/mcp/servers", json={
            "name": "Remote", "transport": "streamable_http", "url": "https://host/mcp",
        })
        self.assertEqual(accepted.status_code, 201)

        agent = {
            "id": "root", "title": "Supervisor", "agent_role": "supervisor",
            "parent_session_id": None, "root_session_id": "root", "depth": 0,
            "can_delegate": 1, "capabilities_json": '{"grants":["read_only","mcp.write"]}',
            "created_at": "now", "updated_at": "now", "archived_at": None,
        }
        self.database.set_agent_mcp_write_capability.return_value = agent
        self.database.get_agent_status.return_value = {
            "agent_id": "root", "state": "idle", "active_run_id": None,
            "queued_task_count": 0,
        }
        response = self.client.put(
            "/api/agents/root/mcp-write-capability", json={"mcp_write": True},
        )
        forged = self.client.put(
            "/api/agents/root/mcp-write-capability", json={"mcp_write": 1},
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn("mcp.write", response.json()["capability_grants"])
        self.assertEqual(forged.status_code, 422)
        self.database.set_agent_mcp_write_capability.assert_called_once_with("root", True)

    def test_manager_error_codes_are_stable_http_errors(self):
        class ManagerFailure(Exception):
            def __init__(self, code):
                super().__init__(code)
                self.code = code

        self.manager.discover_server.side_effect = ManagerFailure("mcp_timeout")
        timed_out = self.client.post("/api/mcp/servers/server-1/discover")
        self.assertEqual(timed_out.status_code, 504)
        self.assertEqual(timed_out.json()["code"], "mcp_timeout")

        self.database.create_mcp_server.side_effect = ManagerFailure("mcp_invalid_config")
        invalid = self.client.post("/api/mcp/servers", json={
            "name": "Bad", "transport": "stdio", "command": "bad.exe",
        })
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(invalid.json()["code"], "mcp_invalid_config")

    def test_mcp_approval_projection_redacts_and_hides_binding_hashes(self):
        summary = {
            "approval_type": "mcp_tool", "tool_name": "mcp_Local_write",
            "name": "legacy", "run_id": "run-1", "attempt": 2,
            "call_id": "call-1", "server_id": "server-1",
            "config_hash": "private-config-hash", "schema_hash": "private-schema-hash",
            "arguments_hash": "private-arguments-hash", "boot_nonce_hash": "private-nonce",
            "arguments": {"path": "safe.txt", "api_token": "private-token"},
        }
        self.database.list_approvals.return_value = [{
            "id": "approval-1", "run_id": "run-1", "tool_call_id": "call-1",
            "kind": "pre_execution", "action_hash": "action-hash",
            "request_summary_json": json.dumps(summary), "status": "pending",
            "expires_at": "2099-01-01T00:00:00+00:00", "decided_at": None,
            "decision_json": None,
        }]
        self.database.get_run.return_value = {"id": "run-1", "session_id": "agent-1"}
        self.database.get_session.return_value = {"id": "agent-1", "archived_at": None}

        response = self.client.get("/api/approvals")
        self.assertEqual(response.status_code, 200)
        safe = response.json()["approvals"][0]["request_summary"]
        self.assertEqual(safe, {
            "approval_type": "mcp_tool", "tool_name": "mcp_Local_write",
            "server_id": "server-1", "attempt": 2, "call_id": "call-1",
            "arguments": {"path": "safe.txt", "api_token": "[redacted]"},
        })
        for marker in ("private-config-hash", "private-schema-hash",
                       "private-arguments-hash", "private-nonce", "private-token"):
            self.assertNotIn(marker, response.text)


class MCPUiSyntaxTest(unittest.TestCase):
    def test_ui_exposes_transport_and_trust_flow_without_inline_html(self):
        result = subprocess.run(
            ["node", "--check", str(ROOT / "static" / "app.js")],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        script = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn('value="streamable_http"', html)
        self.assertIn('value="sse">SSE（兼容）', html)
        self.assertIn("发现 stdio 会立即启动本地程序", html)
        self.assertIn("批准所选工具（仍禁用）", script)
        self.assertIn("argumentsJson.textContent", script)
        self.assertIn('summary.approval_type === "mcp_tool"', script)


if __name__ == "__main__":
    unittest.main()
