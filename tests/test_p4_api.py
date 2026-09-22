"""P4 workspace write approval stays in its root-only, one-shot host lane."""

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from backend import api
from backend.app import validation_error_handler
from backend.db import Database
from backend.runtime import TaskRunner


ROOT = Path(__file__).resolve().parent.parent


class WorkspaceWriteApprovalApiTest(unittest.TestCase):
    def test_root_only_lane_and_old_approval_route_is_closed(self):
        database = Mock()
        root = {"id": "root-1", "archived_at": None}
        database.get_supervisor.return_value = root
        database.get_session.return_value = root
        database.get_run.return_value = {
            "id": "run-1", "task_id": "task-1", "session_id": "worker-1",
        }
        approval = {
            "id": "approval-1", "run_id": "run-1", "tool_call_id": "call-1",
            "kind": "pre_execution", "status": "pending",
            "expires_at": "9999-01-01T00:00:00.000Z",
            "request_summary_json": json.dumps({
                "tool_name": "workspace_write_text", "attempt": 1,
                "workspace_id": "ws-1", "relative_path": "src/a.py",
                "content": "print('hello')", "content_sha256": "sha-1",
                "content_bytes": 14, "arguments_hash": "arguments-1",
                "boot_nonce": "server-only",
            }),
        }
        database.list_approvals.return_value = [approval]
        database.get_approval.return_value = approval
        database.list_workspace_write_approvals.return_value = [approval]
        database.decide_workspace_write_approval.return_value = {
            "id": "approval-1", "status": "resolved",
        }
        runner = Mock(approval_nonce="nonce-1")
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[api._require_local_session] = lambda: SimpleNamespace(
            actor_id="local-host"
        )
        app.add_exception_handler(RequestValidationError, validation_error_handler)
        with patch.object(api, "db", database), patch.object(
            api, "_workspace_approval_runner", return_value=runner
        ):
            client = TestClient(app)
            legacy_list = client.get("/api/approvals")
            legacy_decision = client.post("/api/approvals/approval-1/decision",
                                          json={"decision": "approve"})
            listed = client.get("/api/approvals/workspace-writes")
            injected = client.post("/api/approvals/approval-1/workspace-decision",
                                   json={"decision": "approve", "actor": "worker-1"})
            invalid = client.post("/api/approvals/approval-1/workspace-decision",
                                  json={"decision": "explicit_retry"})
            decided = client.post("/api/approvals/approval-1/workspace-decision",
                                  json={"decision": "approve"})
            with patch.object(api, "_workspace_approval_runner", return_value=None):
                unavailable = client.post("/api/approvals/approval-1/workspace-decision",
                                          json={"decision": "approve"})

        self.assertEqual((legacy_list.json(), legacy_decision.status_code),
                         ({"approvals": []}, 404))
        self.assertEqual((listed.status_code, decided.status_code), (200, 200))
        self.assertEqual((injected.status_code, invalid.status_code,
                          unavailable.status_code), (422, 422, 503))
        visible = listed.json()["approvals"][0]
        self.assertEqual((visible["source_agent_id"], visible["content"],
                          visible["expires_at"]),
                         ("worker-1", "print('hello')", "9999-01-01T00:00:00.000Z"))
        self.assertNotIn("server-only", listed.text)
        database.list_workspace_write_approvals.assert_called_once_with(
            "root-1", "nonce-1", 50
        )
        database.decide_workspace_write_approval.assert_called_once_with(
            "approval-1", "root-1", "nonce-1", "approve", decided_by="local-host"
        )
        database.decide_approval.assert_not_called()
        runner.wake.assert_called_once_with()

    def test_real_db_runner_and_host_approve_or_reject(self):
        with tempfile.TemporaryDirectory() as temporary:
            root_path = Path(temporary)
            workspace_root = root_path / "workspace"
            workspace_root.mkdir()
            note = workspace_root / "note.txt"
            note.write_text("before", encoding="utf-8")
            database = Database(root_path / "run.db")
            try:
                supervisor = database.create_supervisor()
                workspace = database.create_workspace(
                    "docs", str(workspace_root), "directory", "read_write")

                def create_run(key):
                    task = database.create_task(
                        supervisor["id"], "edit", workspace_id=workspace["id"],
                        workspace_mode="read_write")
                    return database.create_run(
                        task["id"], key,
                        config_snapshot={"model": "fake", "max_rounds": 2})[0]

                first = create_run("first")

                def model_factory(*_):
                    async def step(messages, *, run_id, round_index, **_kwargs):
                        if round_index == 1:
                            content = "after" if run_id == first["id"] else "rejected"
                            return {"content": None, "tool_calls": [{
                                "id": "write", "type": "function", "function": {
                                    "name": "workspace_write_text",
                                    "arguments": json.dumps({"path": "note.txt", "content": content}),
                                },
                            }]}
                        self.assertEqual(json.loads(next(
                            item["content"] for item in messages
                            if item.get("tool_call_id") == "write"))["bytes"], 5)
                        return {"content": "done", "tool_calls": []}
                    return step

                runner = TaskRunner(
                    database, model_factory=model_factory,
                    provider_config=lambda _: SimpleNamespace(api_key="key", base_url="url"))
                asyncio.run(runner.run_once())
                self.assertEqual(database.get_run(first["id"])["status"], "waiting")
                self.assertEqual(note.read_text(encoding="utf-8"), "before")
                self.assertIsNone(database._query_one(
                    "SELECT 1 FROM workspace_writer_leases WHERE task_id=?",
                    (first["task_id"],)))

                app = FastAPI()
                app.include_router(api.router)
                app.dependency_overrides[api._require_local_session] = lambda: SimpleNamespace(
                    actor_id="local-host")
                app.add_exception_handler(RequestValidationError, validation_error_handler)
                with patch.object(api, "db", database), patch.object(
                    api, "_workspace_approval_runner", return_value=runner
                ):
                    client = TestClient(app)
                    self.assertEqual(client.get("/api/approvals").json(), {"approvals": []})
                    approvals = client.get("/api/approvals/workspace-writes").json()["approvals"]
                    self.assertEqual(len(approvals), 1)
                    self.assertEqual(approvals[0]["content"], "after")
                    self.assertNotIn(runner.approval_nonce, json.dumps(approvals))
                    approval_id = approvals[0]["approval_id"]
                    old = client.post(f"/api/approvals/{approval_id}/decision",
                                      json={"decision": "approve"})
                    self.assertEqual(old.status_code, 404)
                    approved = client.post(
                        f"/api/approvals/{approval_id}/workspace-decision",
                        json={"decision": "approve"})
                    self.assertEqual(approved.status_code, 200)
                    duplicate = client.post(
                        f"/api/approvals/{approval_id}/workspace-decision",
                        json={"decision": "approve"})
                    self.assertEqual(duplicate.status_code, 409)
                    asyncio.run(runner.run_once())
                    self.assertEqual(database.get_run(first["id"])["status"], "completed")
                    self.assertEqual(note.read_text(encoding="utf-8"), "after")
                    self.assertEqual(sum(event["type"] == "tool.started"
                                         for event in database.list_events(first["id"])), 1)

                    second = create_run("second")
                    asyncio.run(runner.run_once())
                    approvals = client.get("/api/approvals/workspace-writes").json()["approvals"]
                    self.assertEqual(len(approvals), 1)
                    rejected = client.post(
                        f"/api/approvals/{approvals[0]['approval_id']}/workspace-decision",
                        json={"decision": "reject"})
                    self.assertEqual(rejected.status_code, 200)
                    self.assertEqual(database.get_run(second["id"])["status"], "failed")
                    self.assertEqual(note.read_text(encoding="utf-8"), "after")
                    self.assertFalse(any(event["type"] == "tool.started"
                                         for event in database.list_events(second["id"])))
            finally:
                database.close()


class WorkspaceWriteApprovalUiTest(unittest.TestCase):
    def test_root_inbox_content_expiry_and_one_shot_decision(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const nodes = new Map();
let confirmation = 0, posted = null, writes = 0, decided = false;
function node() {
  return {children: [], value: '', textContent: '', hidden: false, disabled: false,
    style: {}, listeners: {}, classList: {add() {}, remove() {}, toggle() {}, contains() {return false;}},
    addEventListener(name, callback) {this.listeners[name] = callback;},
    appendChild(child) {this.children.push(child); return child;},
    replaceChildren() {this.children = [];},
    querySelectorAll(tag) {return this.children.filter((child) => child.tag === tag);}};
}
const content = '<script>alert(1)</script>';
const request = {approval_id:'approval-1', relative_path:'src/a.py', source_agent_id:'worker-1',
  content_bytes:content.length, content_sha256:'sha-1', content, workspace_id:'ws-1',
  task_id:'task-1', run_id:'run-1', attempt:1, tool_call_id:'call-1', status:'pending',
  expires_at:'9999-01-01T00:00:00.000Z'};
const context = {document: {
  getElementById(id) {if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id);},
  createElement(tag) {const item=node(); item.tag=tag; return item;}, querySelectorAll() {return [];}},
  window: {confirm() {confirmation++; return true;}, prompt() {return null;}},
  localStorage: {getItem() {return null;}, setItem() {}, removeItem() {}},
  fetch: async (url, options) => {
    if (url.startsWith('/api/approvals?')) return {ok:true, json:async()=>({approvals:[]})};
    if (url === '/api/approvals/workspace-writes') {
      writes++;
      return {ok:true, json:async()=>({approvals:decided ? [] :
        [request, {...request, approval_id:'expired', expires_at:'2000-01-01T00:00:00.000Z'}]})};
    }
    if (url.endsWith('/workspace-decision')) {
      posted = {url, body:JSON.parse(options.body)}; decided=true;
      return {ok:true, json:async()=>({status:'resolved'})};
    }
    return {ok:false, status:500, json:async()=>({})};
  }, console: {log() {}}, setTimeout() {}};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
(async () => {
  vm.runInContext("agentTree=[{agent_id:'root-1',agent_role:'supervisor'}," +
    "{agent_id:'worker-1',agent_role:'worker'}];activeSessionId='root-1'", context);
  await vm.runInContext('loadApprovals()', context);
  const list = nodes.get('approvalList');
  assert.strictEqual(list.children.length, 2);
  assert.strictEqual(list.children[0].children[2].children[1].textContent, content);
  assert.strictEqual(list.children[0].children.length, 5);
  assert.strictEqual(list.children[1].children.length, 3);
  assert(list.children[1].children[1].textContent.includes('已过期'));
  await list.children[0].children[3].listeners.click();
  assert.deepStrictEqual(posted, {url:'/api/approvals/approval-1/workspace-decision',
    body:{decision:'approve'}});
  assert.strictEqual(confirmation, 1);
  assert.strictEqual(list.children.length, 0);
  vm.runInContext("activeSessionId='worker-1'", context);
  await vm.runInContext('loadApprovals()', context);
  assert.strictEqual(writes, 2);
})().catch((error) => {process.stderr.write(String(error)); process.exitCode=1;});
"""
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "app.js")],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
