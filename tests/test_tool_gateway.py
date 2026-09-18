"""Gateway、MCP 与 host auth 的拒绝计数测试。"""

import asyncio
import io
import json
import logging
import os
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zipfile import ZIP_DEFLATED, ZipFile

from fastapi import HTTPException, Response
from starlette.requests import Request

from backend import auth, file_parser, harness, tool_gateway
from backend.mcp.manager import MCPManager, MCPNotApproved, MCPServerConfig, config_hash, schema_hash
from backend.tool_gateway import (
    MAX_RAW_BYTES,
    RunContext,
    ToolBudget,
    ToolSpec,
    execute_tool,
    intersect_effective_grants,
    make_builtin_run_context,
    make_frozen_run_context,
    make_run_context,
    snapshot_builtin_tools,
    snapshot_internal_tools,
)


def _spec(handler, *, effects=frozenset({"read_only"}), paths=(), approval="never", schema=None):
    return ToolSpec(
        name="demo",
        schema=schema or {"type": "object", "properties": {"value": {"type": "string"}}},
        handler=handler,
        effects=effects,
        path_fields=paths,
        idempotent=True,
        approval=approval,
    )


class GatewayTest(unittest.IsolatedAsyncioTestCase):
    async def test_p2_grant_intersection_never_expands_worker(self):
        parent = {"read_only", "control.delegate_task", "filesystem.read", "network.external"}
        worker = {"read_only", "control.delegate_task", "filesystem.write", "process.spawn"}
        task = {"read_only", "control.delegate_task", "filesystem.write", "network.external"}
        self.assertEqual(intersect_effective_grants(parent, worker, task),
                         frozenset({"read_only", "control.delegate_task"}))
        self.assertEqual(intersect_effective_grants(parent, worker - {"control.delegate_task"}, task),
                         frozenset({"read_only"}))
        self.assertEqual(intersect_effective_grants(parent, worker, task, {"filesystem.write"}),
                         frozenset())
        self.assertEqual(intersect_effective_grants("read_only", worker, task), frozenset())
        self.assertEqual(intersect_effective_grants(parent, {"read_only", 1}, task), frozenset())

    async def test_internal_controls_are_fixed_and_dispatch_only_after_scope_checks(self):
        calls = []

        def dispatch(context, name, arguments):
            calls.append((context.agent_id, context.run_id, name, arguments))
            return {"accepted": True}

        patcher = patch.object(tool_gateway, "control_dispatch", dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)

        arguments = {
            "delegate_task": {"assignee_agent_id": "child", "goal": "Review", "acceptance": []},
            "send_agent_message": {"to_agent_id": "child", "kind": "progress", "content": "working"},
            "get_task_status": {"task_id": "task"},
            "wait_for_tasks": {"task_ids": ["child-task"]},
            "cancel_task": {"task_id": "task"},
            "request_human_approval": {"reason": "Need host decision"},
        }
        specs = snapshot_internal_tools()
        self.assertEqual(set(specs), set(arguments))
        from backend.skills import TOOL_DEFINITIONS
        self.assertTrue(set(arguments) <= {item["function"]["name"] for item in TOOL_DEFINITIONS})
        for name, value in arguments.items():
            with self.subTest(name=name):
                context = RunContext(
                    "run", task_id="task", call_id="call", tool_specs=specs,
                    grants=frozenset({f"control.{name}"}), agent_id="supervisor",
                    role="supervisor",
                    delegate_agent_ids=frozenset({"child"}),
                    message_agent_ids=frozenset({"child"}),
                    status_task_ids=frozenset({"task"}),
                    wait_child_task_ids=frozenset({"child-task"}),
                    cancel_task_ids=frozenset({"task"}),
                    remaining_depth=1, remaining_children=1, remaining_hops=1,
                )
                outcome = await execute_tool(context, name, value)
                self.assertEqual(outcome.status, "ok")
                self.assertEqual(calls[-1], ("supervisor", "run", name, value))
        self.assertEqual(len(calls), 6)

    async def test_internal_forged_identity_and_cross_tree_targets_never_dispatch(self):
        calls = []

        def dispatch(*arguments):
            calls.append(arguments)
            return {"accepted": True}

        patcher = patch.object(tool_gateway, "control_dispatch", dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)

        specs = snapshot_internal_tools()
        context = RunContext(
            "run", task_id="task", tool_specs=specs,
            grants=frozenset(f"control.{name}" for name in specs),
            agent_id="worker", role="worker",
            message_agent_ids=frozenset({"parent"}),
            status_task_ids=frozenset({"own-task"}),
            wait_child_task_ids=frozenset({"own-child"}),
            cancel_task_ids=frozenset({"own-task"}), remaining_hops=1,
        )
        for forged in ("from_agent_id", "actor_id", "requested_grant", "root_path"):
            with self.subTest(forged=forged):
                outcome = await execute_tool(context, "send_agent_message", {
                    "to_agent_id": "parent", "kind": "progress", "content": "ok", forged: "forged"})
                self.assertEqual(outcome.error_code, "invalid_arguments")
        for name, value in (
            ("send_agent_message", {"to_agent_id": "sibling", "kind": "progress", "content": "x"}),
            ("get_task_status", {"task_id": "other-tree"}),
            ("wait_for_tasks", {"task_ids": ["other-child"]}),
            ("cancel_task", {"task_id": "other-tree"}),
        ):
            with self.subTest(name=name):
                outcome = await execute_tool(context, name, value)
                self.assertEqual(outcome.error_code, "not_found")
        self.assertEqual((await execute_tool(context, "wait_for_tasks",
                                              {"task_ids": ["own-child", "own-child"]})).error_code,
                         "invalid_arguments")
        self.assertEqual(calls, [])

    async def test_worker_delegation_and_budget_fail_closed(self):
        calls = []

        def dispatch(*arguments):
            calls.append(arguments)
            return {"accepted": True}

        patcher = patch.object(tool_gateway, "control_dispatch", dispatch)
        patcher.start()
        self.addCleanup(patcher.stop)

        context = RunContext(
            "run", task_id="task", tool_specs=snapshot_internal_tools(),
            grants=frozenset({"control.delegate_task"}), agent_id="worker", role="worker",
            delegate_agent_ids=frozenset({"child"}),
            remaining_depth=1, remaining_children=1, remaining_hops=1,
        )
        args = {"assignee_agent_id": "child", "goal": "Review", "acceptance": []}
        for forged in ({"requested_grant": {"tools": ["process.spawn"]}},
                       {"context": {"root_path": "C:/secret"}}):
            self.assertEqual((await execute_tool(context, "delegate_task", {**args, **forged})).error_code,
                             "invalid_arguments")
        self.assertEqual((await execute_tool(context, "delegate_task", args)).error_code,
                         "tool_not_granted")
        for field in ("remaining_depth", "remaining_children", "remaining_hops"):
            with self.subTest(field=field):
                limited = replace(context, can_delegate=True, **{field: 0})
                self.assertEqual((await execute_tool(limited, "delegate_task", args)).error_code,
                                 "limit_exceeded")
        self.assertEqual((await execute_tool(replace(context, can_delegate=True),
                                             "delegate_task", {**args, "assignee_agent_id": "other"})).error_code,
                         "not_found")
        self.assertEqual(calls, [])

    async def test_frozen_internal_tool_requires_server_scope_and_fixed_handler(self):
        spec = snapshot_internal_tools()["delegate_task"]
        frozen = {"schema": spec.schema, "effects": sorted(spec.effects),
                  "path_fields": list(spec.path_fields), "idempotent": spec.idempotent,
                  "approval": spec.approval, "timeout_seconds": spec.timeout_seconds,
                  "origin": spec.origin}
        row = {"id": "run", "task_id": "task", "session_id": "supervisor",
               "tool_snapshot_json": json.dumps({"v": 1,
                   "specs": {"delegate_task": frozen},
                   "grants": ["control.delegate_task", "filesystem.write"]})}
        context = make_frozen_run_context(row)
        self.assertEqual(context.grants, frozenset({"control.delegate_task"}))
        self.assertEqual(context.agent_id, "")
        args = {"assignee_agent_id": "child", "goal": "Review", "acceptance": []}
        self.assertEqual((await execute_tool(context, "delegate_task", args)).error_code,
                         "control_unavailable")
        invoked = []
        fake = replace(spec, handler=lambda **_args: invoked.append("forged"))
        context = replace(context, agent_id="supervisor", role="supervisor",
                          delegate_agent_ids=frozenset({"child"}), remaining_depth=1,
                          remaining_children=1, remaining_hops=1,
                          tool_specs={"delegate_task": fake})
        self.assertEqual((await execute_tool(context, "delegate_task", args)).error_code,
                         "tool_not_granted")
        self.assertEqual((await execute_tool(replace(context, grants=frozenset(),
                                                    tool_specs={"delegate_task": spec}),
                                              "delegate_task", args)).error_code,
                         "tool_not_granted")
        self.assertEqual(invoked, [])

    async def test_internal_control_argument_marker_never_enters_info(self):
        marker = "CONTROL-ARGUMENT-SECRET-MARKER"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("agent.tool_gateway")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            context = RunContext(
                "run", task_id="task", call_id=marker,
                tool_specs=snapshot_internal_tools(),
                grants=frozenset({"control.request_human_approval"}),
                agent_id="supervisor", role="supervisor",
            )
            outcome = await execute_tool(context, "request_human_approval", {"reason": marker})
        finally:
            logger.removeHandler(handler)
        self.assertEqual(outcome.status, "ok")
        self.assertNotIn(marker, outcome.model_text)
        self.assertNotIn(marker, stream.getvalue())

    async def test_frozen_context_intersects_current_specs_and_grants(self):
        spec = snapshot_builtin_tools()["get_current_time"]
        frozen = {"schema": spec.schema, "effects": sorted(spec.effects),
                  "path_fields": list(spec.path_fields), "idempotent": spec.idempotent,
                  "approval": spec.approval, "timeout_seconds": spec.timeout_seconds,
                  "origin": spec.origin}
        row = {"id": "run", "task_id": "task", "session_id": "session",
               "tool_snapshot_json": json.dumps({"v": 1, "specs": {
                   "get_current_time": frozen, "unreviewed_tool": frozen},
                   "grants": ["read_only", "filesystem.write"]})}
        context = make_frozen_run_context(row)
        self.assertEqual(context.grants, frozenset({"read_only"}))
        self.assertEqual(set(context.tool_specs), {"get_current_time"})
        self.assertEqual((await execute_tool(context, "get_current_time", "{}")).status, "ok")
        self.assertEqual((await execute_tool(context, "unreviewed_tool", "{}")).error_code,
                         "tool_not_granted")
        changed = {**frozen, "approval": "always"}
        row["tool_snapshot_json"] = json.dumps({"v": 1,
            "specs": {"get_current_time": changed}, "grants": ["read_only"]})
        self.assertEqual(make_frozen_run_context(row).tool_specs, {})
        row["tool_snapshot_json"] = "not-json"
        self.assertEqual(make_frozen_run_context(row).grants, frozenset())

    async def test_effectful_thread_timeout_is_unknown_and_never_retried(self):
        release = threading.Event()
        started = threading.Event()
        invoked = 0

        def handler():
            nonlocal invoked
            invoked += 1
            started.set()
            release.wait(timeout=1)
            return {"secret": "UNKNOWN-RESULT-MARKER"}

        spec = replace(_spec(handler, effects=frozenset({"filesystem.write"})),
                       idempotent=False, timeout_seconds=0.01)
        context = RunContext("run", call_id="call", tool_specs={"demo": spec},
                             grants=frozenset({"filesystem.write"}))
        rejected = await execute_tool(replace(context, grants=frozenset()), "demo", "{}")
        self.assertEqual(rejected.error_code, "tool_not_granted")
        self.assertEqual(invoked, 0)
        try:
            pending = asyncio.create_task(execute_tool(context, "demo", "{}"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            outcome = await pending
            self.assertEqual((outcome.status, outcome.effect, outcome.quiesced),
                             ("outcome_unknown", "effectful", False))
            self.assertEqual(outcome.error_code, "timeout_unknown_effect")
            self.assertEqual(invoked, 1)
            self.assertIsNone(outcome.result_ref)
            self.assertNotIn("UNKNOWN-RESULT-MARKER", outcome.model_text)
        finally:
            release.set()
        await asyncio.sleep(0.02)
        self.assertEqual(invoked, 1)

    async def test_read_only_async_timeout_is_confirmed_quiesced(self):
        cancelled = asyncio.Event()

        async def handler():
            try:
                await asyncio.sleep(1)
            finally:
                cancelled.set()

        spec = replace(_spec(handler), timeout_seconds=0.01)
        context = RunContext("run", tool_specs={"demo": spec}, grants=frozenset({"read_only"}))
        outcome = await execute_tool(context, "demo", "{}")
        self.assertEqual((outcome.status, outcome.effect, outcome.quiesced),
                         ("timeout", "read_only", True))
        self.assertTrue(cancelled.is_set())

    async def test_effectful_async_timeout_remains_unknown(self):
        invoked = 0

        async def handler():
            nonlocal invoked
            invoked += 1
            await asyncio.sleep(1)

        spec = replace(_spec(handler, effects=frozenset({"filesystem.write"})),
                       idempotent=False, timeout_seconds=0.01)
        context = RunContext("run", tool_specs={"demo": spec}, grants=frozenset({"filesystem.write"}))
        outcome = await execute_tool(context, "demo", "{}")
        self.assertEqual((outcome.status, outcome.effect, outcome.quiesced),
                         ("outcome_unknown", "effectful", False))
        self.assertEqual(invoked, 1)

    async def test_idempotent_async_timeout_is_confirmed_quiesced(self):
        async def handler():
            await asyncio.sleep(1)

        spec = replace(_spec(handler, effects=frozenset({"filesystem.write"})),
                       timeout_seconds=0.01)
        context = RunContext("run", tool_specs={"demo": spec}, grants=frozenset({"filesystem.write"}))
        outcome = await execute_tool(context, "demo", "{}")
        self.assertEqual((outcome.status, outcome.effect, outcome.quiesced),
                         ("timeout", "idempotent", True))

    async def test_idempotent_thread_timeout_is_not_claimed_quiesced(self):
        release = threading.Event()
        started = threading.Event()

        def handler():
            started.set()
            release.wait(timeout=1)

        spec = replace(_spec(handler, effects=frozenset({"filesystem.write"})), timeout_seconds=0.01)
        context = RunContext("run", tool_specs={"demo": spec}, grants=frozenset({"filesystem.write"}))
        try:
            pending = asyncio.create_task(execute_tool(context, "demo", "{}"))
            self.assertTrue(await asyncio.to_thread(started.wait, 1))
            outcome = await pending
            self.assertEqual((outcome.status, outcome.effect, outcome.quiesced),
                             ("timeout", "idempotent", False))
        finally:
            release.set()

    async def test_result_ref_is_unavailable_for_any_owner(self):
        spec = _spec(lambda: "a" * 9000)
        for actor, run in (("owner-a", "run-a"), ("owner-b", "run-b")):
            with self.subTest(actor=actor):
                context = RunContext(run, actor_id=actor, tool_specs={"demo": spec},
                                     grants=frozenset({"read_only"}))
                outcome = await execute_tool(context, "demo", "{}")
                self.assertEqual(outcome.status, "ok")
                self.assertLessEqual(len(outcome.model_text), 8000)
                self.assertIsNone(outcome.result_ref)

    async def test_plain_text_raw_utf8_boundary(self):
        exact_multibyte = "界" * ((MAX_RAW_BYTES - 1) // 3) + "x"
        self.assertEqual(len(exact_multibyte.encode("utf-8")), MAX_RAW_BYTES)
        for text, expected in (
            ("x" * MAX_RAW_BYTES, "ok"),
            ("x" * (MAX_RAW_BYTES + 1), "limit_exceeded"),
            (exact_multibyte, "ok"),
            (exact_multibyte + "y", "limit_exceeded"),
        ):
            with self.subTest(raw_bytes=len(text.encode("utf-8"))):
                context = RunContext(
                    "run", tool_specs={"demo": _spec(lambda: text)},
                    grants=frozenset({"read_only"}),
                )
                outcome = await execute_tool(context, "demo", "{}")
                self.assertEqual(outcome.status if expected == "ok" else outcome.error_code, expected)

    async def test_structured_raw_json_boundary(self):
        overhead = len(json.dumps({"text": ""}, ensure_ascii=False).encode("utf-8"))
        for length, expected in ((MAX_RAW_BYTES - overhead, "ok"),
                                 (MAX_RAW_BYTES - overhead + 1, "limit_exceeded")):
            with self.subTest(raw_bytes=overhead + length):
                context = RunContext(
                    "run", tool_specs={"demo": _spec(lambda: {"text": "x" * length})},
                    grants=frozenset({"read_only"}),
                )
                outcome = await execute_tool(context, "demo", "{}")
                self.assertEqual(outcome.status if expected == "ok" else outcome.error_code, expected)

    async def test_new_definition_is_not_implicitly_authorized(self):
        from backend import skills

        fake = {"type": "function", "function": {
            "name": "new_no_path_tool",
            "parameters": {"type": "object", "properties": {}},
        }}
        with (
            patch.object(skills, "TOOL_DEFINITIONS", [*skills.TOOL_DEFINITIONS, fake]),
            patch.dict(skills.TOOL_FUNCTIONS, {"new_no_path_tool": lambda: self.fail("handler reached")}),
        ):
            self.assertNotIn("new_no_path_tool", snapshot_builtin_tools())
            outcome = await execute_tool(make_builtin_run_context("run"), "new_no_path_tool", "{}")
        self.assertEqual(outcome.error_code, "tool_not_granted")

    async def test_raw_limit_is_checked_before_secret_redaction(self):
        marker = "SECRET-MARKER-987"
        context = RunContext(
            "run",
            tool_specs={"demo": _spec(lambda: {"secret": marker * 20_000})},
            grants=frozenset({"read_only"}),
            secrets=(marker,),
        )
        outcome = await execute_tool(context, "demo", "{}")
        self.assertEqual(outcome.error_code, "limit_exceeded")
        self.assertNotIn(marker, outcome.model_text)

    async def test_legacy_skill_executor_cannot_reach_handler(self):
        from backend import skills, tools

        with patch.dict(skills.TOOL_FUNCTIONS, {"demo": lambda: self.fail("handler reached")}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "tool_gateway_required"):
                skills.execute_tool("demo", {})
        self.assertFalse(hasattr(tools, "execute_tool"))

    async def test_trusted_builtin_factory_allows_time_but_not_files(self):
        context = make_builtin_run_context("run", actor_id="local-host")
        time_result = await execute_tool(context, "get_current_time", "{}")
        self.assertEqual(time_result.status, "ok")
        self.assertIn("datetime", time_result.model_text)
        file_result = await execute_tool(context, "list_dir", '{"path":"."}')
        self.assertEqual(file_result.error_code, "tool_not_granted")

    async def test_default_context_and_unknown_field_do_not_invoke_handler(self):
        called = 0

        def handler(**_arguments):
            nonlocal called
            called += 1
            return {"ok": True}

        empty = await execute_tool(make_run_context("run"), "demo", "{}")
        self.assertEqual(empty.error_code, "tool_not_granted")
        context = RunContext("run", tool_specs={"demo": _spec(handler)}, grants=frozenset({"read_only"}))
        unknown = await execute_tool(context, "demo", '{"unexpected":"marker"}')
        self.assertEqual(unknown.error_code, "invalid_arguments")
        self.assertEqual(called, 0)

    async def test_argument_shape_and_size_rejected_before_handler(self):
        called = 0

        def handler(**_arguments):
            nonlocal called
            called += 1

        context = RunContext("run", tool_specs={"demo": _spec(handler)}, grants=frozenset({"read_only"}))
        for arguments in ('[]', '"text"', '{"value":"' + "x" * 65536 + '"}'):
            outcome = await execute_tool(context, "demo", arguments)
            self.assertNotEqual(outcome.status, "ok")
        self.assertEqual(called, 0)

    async def test_nested_path_and_grant_rejected_before_handler(self):
        called = 0

        def handler(**_arguments):
            nonlocal called
            called += 1

        schema = {
            "type": "object",
            "properties": {
                "sources": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                }
            },
            "required": ["sources"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _spec(handler, effects=frozenset({"filesystem.read"}), paths=("/sources/*/path",), schema=schema)
            context = RunContext("run", tool_specs={"demo": spec}, workspace_roots=(root,))
            no_grant = await execute_tool(context, "demo", {"sources": [{"path": str(root / "okay.txt")}]})
            self.assertEqual(no_grant.error_code, "tool_not_granted")
            context = replace(context, grants=frozenset({"filesystem.read"}))
            outside = await execute_tool(context, "demo", {
                "sources": [{"path": str(root / "okay.txt")}, {"path": str(root.parent / "outside.txt")}]
            })
            self.assertEqual(outside.error_code, "path_not_granted")
            self.assertEqual(called, 0)

    async def test_approval_unavailable_and_budget_reject_before_handler(self):
        called = 0

        def handler(**_arguments):
            nonlocal called
            called += 1

        context = RunContext(
            "run", tool_specs={"demo": _spec(handler, approval="always")},
            grants=frozenset({"read_only"}),
        )
        outcome = await execute_tool(context, "demo", "{}")
        self.assertEqual(outcome.error_code, "approval_unavailable")
        self.assertEqual(called, 0)
        context = replace(context, budget=ToolBudget(max_tool_calls=0))
        outcome = await execute_tool(context, "demo", "{}")
        self.assertEqual(outcome.error_code, "limit_exceeded")
        self.assertEqual(called, 0)

    async def test_secret_marker_not_in_result_or_logs(self):
        marker = "SECRET-MARKER-987"
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("agent.tool_gateway")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            context = RunContext(
                "run", call_id=marker,
                tool_specs={"demo": _spec(lambda: {"api_key": marker, "value": marker})},
                grants=frozenset({"read_only"}), secrets=(marker,),
            )
            outcome = await execute_tool(context, "demo", "{}")
        finally:
            logger.removeHandler(handler)
        self.assertEqual(outcome.status, "ok")
        self.assertNotIn(marker, outcome.model_text)
        self.assertNotIn(marker, stream.getvalue())

    async def test_mcp_without_approval_rejected_before_handler(self):
        called = 0

        async def handler():
            nonlocal called
            called += 1

        spec = replace(_spec(handler), origin="mcp:server:hash")
        context = RunContext("run", tool_specs={"demo": spec}, grants=frozenset({"read_only"}))
        outcome = await execute_tool(context, "demo", "{}")
        self.assertEqual(outcome.error_code, "mcp_not_approved")
        self.assertEqual(called, 0)


class MCPTest(unittest.IsolatedAsyncioTestCase):
    async def test_pending_config_never_spawns_or_discovers(self):
        manager = MCPManager()
        config = MCPServerConfig(id="one", name="test", command="relative.exe", enabled=True)
        with patch.object(manager, "_open_session", new_callable=AsyncMock) as connect:
            with self.assertRaises(MCPNotApproved):
                await manager.discover(config)
            with self.assertRaises(MCPNotApproved):
                await manager.test_server(config)
            connect.assert_not_called()

    async def test_env_enabled_flag_is_ignored(self):
        manager = MCPManager()
        with patch.dict(os.environ, {"MCP_SERVERS": json.dumps([{
            "id": "one", "name": "test", "transport": "stdio", "command": "relative.exe", "enabled": True,
        }])}):
            manager.load_env_configs()
        self.assertFalse(manager.get_config("one").enabled)

    async def test_config_change_and_unallowlisted_tool_never_connect(self):
        manager = MCPManager()
        with tempfile.TemporaryDirectory() as tmp:
            executable = Path(tmp) / "server.exe"
            executable.write_bytes(b"original")
            config = MCPServerConfig(id="one", name="test", command=str(executable), enabled=True)
            config.approved_config_hash = config_hash(config)
            schema = {"type": "object", "properties": {}}
            tool = type("Tool", (), {"name": "remote", "inputSchema": schema})()
            manager._discovered[config.id] = [tool]
            with patch.object(manager, "_open_session", new_callable=AsyncMock) as connect:
                with self.assertRaises(MCPNotApproved):
                    await manager.call(config, "remote", {})
                connect.assert_not_called()
                config.approved_tools["remote"] = {
                    "schema_hash": schema_hash(schema), "effects": ["read_only"], "approval": "never"
                }
                executable.write_bytes(b"changed")
                with self.assertRaises(MCPNotApproved):
                    await manager.call(config, "remote", {})
                connect.assert_not_called()

    async def test_url_transport_disabled_without_connection_policy_hook(self):
        manager = MCPManager()
        config = MCPServerConfig(id="url", name="test", transport="sse", url="https://example.com", enabled=True,
                                 approved_config_hash="claimed")
        with patch.object(manager, "_open_session", new_callable=AsyncMock) as connect:
            with self.assertRaises(MCPNotApproved):
                await manager.discover(config)
            connect.assert_not_called()


class HarnessAuditTest(unittest.TestCase):
    def test_audit_projection_never_stores_argument_or_result_marker(self):
        marker = "SECRET-MARKER-987"
        gate = harness.Harness(mode="enforce", record=True)
        with patch.object(harness.db, "add_audit_log") as write:
            gate.record_result("demo", {"nested": {"token": marker}}, {"text": marker}, True)
        written = write.call_args.kwargs
        self.assertNotIn(marker, json.dumps(written))
        self.assertEqual(written["arguments"], {"argument_count": 1})


def _request(method="POST", origin="http://127.0.0.1:8000", cookie=""):
    headers = [(b"host", b"127.0.0.1:8000"), (b"origin", origin.encode())]
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    return Request({"type": "http", "method": method, "scheme": "http", "path": "/api/auth/bootstrap", "headers": headers})


class AuthTest(unittest.TestCase):
    def test_concurrent_bootstrap_consumes_secret_once(self):
        secret = auth.issue_bootstrap_secret()
        barrier = threading.Barrier(8)

        def attempt(_index):
            response = Response()
            barrier.wait(timeout=5)
            try:
                auth.bootstrap(_request(), response, secret)
            except HTTPException as exc:
                return exc.status_code
            return 200

        with ThreadPoolExecutor(max_workers=8) as executor:
            statuses = list(executor.map(attempt, range(8)))
        self.assertEqual(statuses.count(200), 1)
        self.assertEqual(statuses.count(401), 7)

    def test_bootstrap_consumes_secret_and_sets_host_only_cookie(self):
        secret = auth.issue_bootstrap_secret()
        response = Response()
        auth.bootstrap(_request(), response, secret)
        cookie = response.headers["set-cookie"]
        self.assertIn("httponly", cookie.lower())
        self.assertIn("samesite=strict", cookie.lower())
        self.assertNotIn("domain=", cookie.lower())
        token = cookie.split(";", 1)[0]
        self.assertEqual(auth.require_host_session(_request(cookie=token)).actor_id, "local-host")
        with self.assertRaises(HTTPException):
            auth.bootstrap(_request(), Response(), secret)

    def test_wrong_origin_and_cookie_fail_closed(self):
        secret = auth.issue_bootstrap_secret()
        with self.assertRaises(HTTPException) as wrong_origin:
            auth.bootstrap(_request(origin="http://evil.example"), Response(), secret)
        self.assertEqual(wrong_origin.exception.status_code, 403)
        with self.assertRaises(HTTPException) as missing_cookie:
            auth.require_host_session(_request())
        self.assertEqual(missing_cookie.exception.status_code, 401)

    def test_non_loopback_host_rejected_before_secret_consumption(self):
        secret = auth.issue_bootstrap_secret()
        request = Request({
            "type": "http", "method": "POST", "scheme": "http", "path": "/api/auth/bootstrap",
            "headers": [(b"host", b"evil.example:8000"), (b"origin", b"http://evil.example:8000")],
        })
        with self.assertRaises(HTTPException) as rejected:
            auth.bootstrap(request, Response(), secret)
        self.assertEqual(rejected.exception.status_code, 403)
        auth.bootstrap(_request(), Response(), secret)


class UploadZipTest(unittest.TestCase):
    def test_zip_entry_limit_rejects_before_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.xlsx"
            with ZipFile(path, "w") as archive:
                for index in range(2001):
                    archive.writestr(f"{index}.txt", "x")
            with patch.object(file_parser, "_parse_excel") as parse:
                with self.assertRaises(ValueError):
                    file_parser.parse_uploaded_file(path)
                parse.assert_not_called()

    def test_compression_ratio_rejects_before_parser(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bomb.pptx"
            with ZipFile(path, "w", ZIP_DEFLATED) as archive:
                archive.writestr("slide.xml", "a" * 200_000)
            with patch.object(file_parser, "_parse_pptx") as parse:
                with self.assertRaises(ValueError):
                    file_parser.parse_uploaded_file(path)
                parse.assert_not_called()


if __name__ == "__main__":
    unittest.main()
