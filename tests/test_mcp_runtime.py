"""Minimal MCP Run freeze and one-shot side-effect approval checks."""

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from backend.db import AgentHierarchyError, Database, InvalidRunTransition
from backend.runtime import TaskRunner
from backend.tool_gateway import ToolSpec, make_frozen_run_context


NAME = "mcp_demo_remote"
ORIGIN = f"mcp:server-one:{'a' * 64}"
SCHEMA = {
    "type": "object",
    "properties": {"message": {"type": "string"}, "api_key": {"type": "string"}},
    "required": ["message", "api_key"],
}


class Manager:
    def __init__(self, spec):
        self.spec = spec

    def snapshot_tool_specs(self):
        return MappingProxyType({NAME: self.spec})


class MCPRuntimeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.db = Database(Path(self.temporary.name) / "mcp.db")
        self.addCleanup(self.db.close)
        self.agent = self.db.create_supervisor()

    @staticmethod
    def spec(handler, *, schema=SCHEMA):
        return ToolSpec(
            name=NAME, schema=schema, handler=handler,
            effects=frozenset({"mcp.write"}), approval="always",
            timeout_seconds=30.0, origin=ORIGIN,
        )

    async def _waiting_mcp_write(self, handler):
        spec = self.spec(handler)
        manager = Manager(spec)
        task = self.db.create_task(self.agent["id"], "approved MCP")
        with patch("backend.mcp.manager.get_manager", return_value=manager):
            run, _ = self.db.create_run(task["id"], "execute")

        def model_factory(*_):
            async def step(_messages, *, round_index, tool_names, **_kwargs):
                self.assertIn(NAME, tool_names)
                if round_index == 1:
                    return {"content": None, "tool_calls": [{
                        "id": "mcp-call-1", "type": "function", "function": {
                            "name": NAME,
                            "arguments": json.dumps({"message": "hello", "api_key": "secret"}),
                        },
                    }]}
                return {"content": "complete", "tool_calls": []}
            return step

        runner = TaskRunner(
            self.db, model_factory=model_factory,
            provider_config=lambda _: SimpleNamespace(api_key="provider", base_url="url"),
        )
        with patch("backend.mcp.manager.get_manager", return_value=manager):
            await runner.run_once()
        self.assertEqual(self.db.get_run(run["id"])["status"], "waiting")
        return manager, runner, run, self.db.list_approvals(run["id"])[0]

    async def test_snapshot_thaws_exact_manager_spec_and_rejects_drift(self):
        async def handler(**_):
            return "ok"

        spec = self.spec(handler)
        manager = Manager(spec)
        task = self.db.create_task(self.agent["id"], "freeze MCP")
        with patch("backend.mcp.manager.get_manager", return_value=manager):
            run, _ = self.db.create_run(task["id"], "freeze")
            context = make_frozen_run_context(run)
        self.assertEqual(context.mcp_tools, frozenset({NAME}))
        self.assertIs(context.tool_specs[NAME], spec)

        manager.spec = replace(spec, schema={"type": "object", "properties": {}})
        with patch("backend.mcp.manager.get_manager", return_value=manager):
            drifted = make_frozen_run_context(run)
        self.assertNotIn(NAME, drifted.tool_specs)
        self.assertFalse(drifted.mcp_tools)

    async def test_mcp_write_capability_is_parent_bounded_and_future_only(self):
        worker = self.db.create_worker(
            self.agent["id"], "worker", capabilities={"grants": ["read_only"]})
        first = self.db.create_task(worker["id"], "before")
        first_run, _ = self.db.create_run(first["id"], "before")
        self.db.set_agent_mcp_write_capability(worker["id"], True)
        self.assertNotIn("mcp.write", json.loads(
            first_run["effective_grant_json"])["grants"])
        self.db.request_control(first_run["id"], "cancel")
        second = self.db.create_task(worker["id"], "after")
        second_run, _ = self.db.create_run(second["id"], "after")
        self.assertIn("mcp.write", json.loads(
            second_run["effective_grant_json"])["grants"])

        self.db.set_agent_mcp_write_capability(self.agent["id"], False)
        other = self.db.create_worker(
            self.agent["id"], "other", capabilities={"grants": ["read_only"]})
        with self.assertRaises(AgentHierarchyError):
            self.db.set_agent_mcp_write_capability(other["id"], True)

    async def test_effectful_mcp_requires_and_consumes_one_approval(self):
        calls = []

        async def handler(**arguments):
            calls.append(arguments)
            return "done"

        manager, runner, run, approval = await self._waiting_mcp_write(handler)
        waiting = self.db.get_run(run["id"])
        self.assertEqual((waiting["status"], waiting["wait_reason"]),
                         ("waiting", "approval"))
        summary = json.loads(approval["request_summary_json"])
        self.assertEqual(summary["arguments"], {"message": "hello", "api_key": "[redacted]"})
        self.assertEqual(summary["arguments_hash"], hashlib.sha256(
            json.dumps({"api_key": "secret", "message": "hello"},
                       sort_keys=True, separators=(",", ":")).encode()).hexdigest())

        self.db.decide_approval(approval["id"], "approve")
        with patch("backend.mcp.manager.get_manager", return_value=manager):
            await runner.run_once()
        self.assertEqual(self.db.get_run(run["id"])["status"], "completed")
        self.assertEqual(calls, [{"message": "hello", "api_key": "secret"}])
        consumed = json.loads(self.db.get_approval(approval["id"])["decision_json"])
        self.assertIn("consumed_at", consumed)
        self.assertIsNone(self.db.get_mcp_tool_approval(
            run["id"], "mcp-call-1", runner.approval_nonce))
        starts = [event for event in self.db.list_events(run["id"])
                  if event["type"] == "tool.started"]
        self.assertEqual(len(starts), 1)
        with self.assertRaises(InvalidRunTransition):
            self.db.start_tool_call(
                run["id"], self.db.get_run(run["id"])["version"],
                runner._owner, self.db.get_run(run["id"])["lease_epoch"],
                "mcp-call-1", NAME, "effectful", approval["action_hash"],
                boot_nonce=runner.approval_nonce,
            )

    async def test_mcp_deny_and_restart_staleness_end_waiting_run(self):
        calls = []

        async def handler(**arguments):
            calls.append(arguments)
            return "done"

        _manager, runner, run, approval = await self._waiting_mcp_write(handler)
        self.db.decide_approval(approval["id"], "deny")
        denied = self.db.get_run(run["id"])
        self.assertEqual((denied["status"], denied["wait_reason"]), ("failed", None))
        self.assertEqual(calls, [])
        self.assertIsNone(self.db.get_mcp_tool_approval(
            run["id"], "mcp-call-1", runner.approval_nonce))

        self.db = Database(Path(self.temporary.name) / "restart.db")
        self.addCleanup(self.db.close)
        self.agent = self.db.create_supervisor()
        _manager, _runner, run, approval = await self._waiting_mcp_write(handler)
        self.assertEqual(
            self.db.reconcile_mcp_tool_approvals("replacement-boot"), [approval["id"]])
        stale = self.db.get_run(run["id"])
        self.assertEqual((stale["status"], stale["wait_reason"]), ("failed", None))
        self.assertEqual(self.db.get_approval(approval["id"])["status"], "expired")

    async def test_tool_snapshot_limit_counts_utf8_bytes(self):
        async def handler(**_):
            return "ok"

        manager = Manager(self.spec(handler, schema={
            "type": "object", "description": "界" * 22_000, "properties": {},
        }))
        task = self.db.create_task(self.agent["id"], "large snapshot")
        with (patch("backend.mcp.manager.get_manager", return_value=manager),
              self.assertRaisesRegex(ValueError, "tool_snapshot_too_large")):
            self.db.create_run(task["id"], "large")


if __name__ == "__main__":
    unittest.main()
