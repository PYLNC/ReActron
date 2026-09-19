"""P2 宿主 Agent 树控制面：仅薄路由与安全投影。"""

import unittest
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import api
from backend.app import http_error_handler, validation_error_handler


ROOT = Path(__file__).resolve().parent.parent


class P2AgentApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Mock()
        self.root = {
            "id": "root-1", "title": "Supervisor", "agent_role": "supervisor",
            "parent_session_id": None, "root_session_id": "root-1", "depth": 0,
            "can_delegate": 1,
            "capabilities_json": '{"grants":["read_only","private-marker"],"secret_ref":"private-marker"}',
            "system_prompt": "private-marker", "created_at": "now", "updated_at": "now",
            "archived_at": None,
        }
        self.worker = {
            **self.root, "id": "worker-1", "title": "Research", "agent_role": "worker",
            "parent_session_id": "root-1", "depth": 1, "can_delegate": 0,
        }
        self.database.get_supervisor.return_value = self.root
        self.database.create_supervisor.return_value = self.root
        self.database.select_supervisor.return_value = self.root
        self.database.list_agent_tree.return_value = [self.root, self.worker]
        self.database.create_worker.return_value = self.worker
        self.database.rename_agent.return_value = self.worker
        self.database.archive_worker.return_value = {**self.worker, "archived_at": "later"}
        self.database.get_agent_status.return_value = {
            "agent_id": "root-1", "state": "idle", "active_run_id": None,
            "queued_task_count": 0,
        }
        self.task = {
            "id": "task-1", "session_id": "worker-1", "creator_session_id": "root-1",
            "parent_task_id": None, "goal": "Investigate", "acceptance_json": "[]",
            "context_json": "{}", "status": "open", "created_at": "now",
            "updated_at": "now", "finished_at": None,
        }
        self.run = {
            "id": "run-1", "task_id": "task-1", "session_id": "worker-1",
            "attempt": 1, "status": "queued", "wait_reason": None,
            "created_at": "now", "started_at": None, "finished_at": None,
        }
        self.message = {
            "id": 7, "session_id": "root-1", "sender_session_id": "worker-1",
            "source_run_id": "run-1", "recipient_run_id": None,
            "related_task_id": "task-1", "kind": "question", "role": "assistant",
            "content": "Need input", "created_at": "now", "needs_reply": True,
            "private": "private-marker",
        }
        self.database.host_delegate_task.return_value = (self.task, self.run)
        self.database.list_tree_tasks.return_value = [self.task]
        self.database.get_task.return_value = self.task
        self.database.get_task_result.return_value = {
            "summary": "done", "acceptance_results": [{"criterion": "checked", "ok": True}],
            "artifacts": [{"artifact_id": "artifact-1", "kind": "text"}],
            "evidence": [{"kind": "test", "command": "pytest", "exit_code": 0}],
            "followups": [], "secret_ref": "private-marker",
        }
        self.database.get_latest_run_id.return_value = "run-1"
        self.database.get_last_event_id.return_value = 0
        self.database.list_agent_messages.return_value = [self.message]
        self.database.host_reply.return_value = (
            {**self.message, "id": 8, "session_id": "worker-1",
             "sender_session_id": None, "recipient_run_id": "run-1",
             "kind": "reply", "role": "user", "content": "Proceed", "needs_reply": False},
            self.run,
        )
        self.runner = SimpleNamespace(wake=Mock())
        runner_patch = patch.object(api, "_runner", return_value=self.runner)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
        from backend import runtime
        actions_patch = patch.object(runtime, "allowed_actions", return_value=[])
        actions_patch.start()
        self.addCleanup(actions_patch.stop)
        application = FastAPI()
        application.include_router(api.router)
        application.dependency_overrides[api._require_local_session] = lambda: SimpleNamespace(
            actor_id="local-host"
        )
        application.add_exception_handler(RequestValidationError, validation_error_handler)
        self.client = TestClient(application)

    def test_explicit_root_and_tree_do_not_expose_prompt_or_secret(self):
        with patch.object(api, "db", self.database):
            found = self.client.get("/api/supervisor")
            created = self.client.post("/api/supervisor", json={"title": "Supervisor"})
            selected = self.client.post("/api/supervisor/select", json={"agent_id": "root-1"})
            tree = self.client.get("/api/agents/tree")
        self.assertEqual((found.status_code, created.status_code, selected.status_code),
                         (200, 201, 200))
        self.assertEqual([item["agent_id"] for item in tree.json()["agents"]],
                         ["root-1", "worker-1"])
        self.assertEqual(tree.json()["agents"][1]["parent_agent_id"], "root-1")
        self.assertEqual(tree.json()["agents"][1]["capability_grants"], ["read_only"])
        self.assertNotIn("private-marker", tree.text)
        self.assertNotIn("system_prompt", tree.text)
        self.assertNotIn("session_id", tree.text)
        self.database.create_supervisor.assert_called_once_with("Supervisor", "", None)
        self.database.list_agent_tree.assert_called_once_with(None, False)

    def test_legacy_session_read_does_not_leak_agent_prompt(self):
        self.database.get_session.return_value = {**self.root, "project_id": "project-1"}
        self.database.get_messages.return_value = [{"role": "user", "content": "history"}]
        with patch.object(api, "db", self.database):
            response = self.client.get("/api/sessions/root-1")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("private-marker", response.text)
        self.assertNotIn("system_prompt", response.text)
        self.assertEqual(response.json()["session"]["id"], "root-1")
        self.assertEqual(response.json()["session"]["title"], "Supervisor")
        self.assertEqual(response.json()["session"]["project_id"], "project-1")
        self.assertEqual(response.json()["messages"][0]["content"], "history")

    def test_worker_create_rename_archive_and_status(self):
        with patch.object(api, "db", self.database):
            created = self.client.post("/api/agents/workers", json={
                "parent_agent_id": "root-1", "title": "Research",
                "capabilities": {"grants": ["read_only"]},
            })
            status = self.client.get("/api/agents/worker-1/status")
            renamed = self.client.patch("/api/agents/worker-1", json={"title": "New name"})
            archived = self.client.delete("/api/agents/worker-1")
        self.assertEqual([response.status_code for response in
                          (created, status, renamed, archived)], [201, 200, 200, 200])
        self.assertEqual(created.json()["can_delegate"], False)
        self.assertEqual(archived.json()["archived_at"], "later")
        self.database.create_worker.assert_called_once_with(
            "root-1", "Research", "", {"grants": ["read_only"]}, False
        )
        self.database.rename_agent.assert_called_once_with("worker-1", "New name")
        self.database.archive_worker.assert_called_once_with("worker-1")

    def test_identity_grants_paths_and_unknown_fields_are_rejected(self):
        with patch.object(api, "db", self.database):
            bad = [
                self.client.post("/api/supervisor", json={"actor_id": "worker-1"}),
                self.client.post("/api/supervisor/select", json={
                    "agent_id": "root-1", "agent_role": "supervisor",
                }),
                self.client.post("/api/agents/workers", json={
                    "parent_agent_id": "root-1", "title": "X", "from_agent_id": "worker-1",
                }),
                self.client.post("/api/agents/workers", json={
                    "parent_agent_id": "root-1", "title": "X",
                    "capabilities": {"grants": ["filesystem.read"]},
                }),
                self.client.post("/api/agents/workers", json={
                    "parent_agent_id": "root-1", "title": "X",
                    "capabilities": {"grants": ["read_only"], "workspace_roots": ["private-marker"]},
                }),
                self.client.post("/api/sessions", json={
                    "title": "X", "project_id": "project-1", "agent_role": "supervisor",
                }),
                self.client.post("/api/chat", json={
                    "session_id": "root-1", "message": "X", "sender_session_id": "worker-1",
                }),
            ]
        self.assertTrue(all(response.status_code == 422 for response in bad))
        self.assertTrue(all(set(response.json()) ==
                            {"code", "message", "details", "trace_id"} for response in bad))
        self.assertNotIn("private-marker", " ".join(response.text for response in bad))
        self.database.create_worker.assert_not_called()
        self.database.create_session.assert_not_called()
        self.database.create_legacy_chat_run.assert_not_called()

    def test_repository_conflict_and_hidden_resource_mapping(self):
        class RepoError(ValueError):
            def __init__(self, code):
                self.code = code

        with patch.object(api, "db", self.database):
            self.database.create_supervisor.side_effect = RepoError("supervisor_exists")
            duplicate = self.client.post("/api/supervisor", json={})
            self.database.create_worker.side_effect = RepoError("invalid_agent_hierarchy")
            hierarchy = self.client.post("/api/agents/workers", json={
                "parent_agent_id": "root-1", "title": "X",
            })
            self.database.archive_worker.side_effect = RepoError("not_found")
            hidden = self.client.delete("/api/agents/hidden")
        self.assertEqual([(item.status_code, item.json()["code"]) for item in
                          (duplicate, hierarchy, hidden)], [
                              (409, "supervisor_exists"),
                              (409, "invalid_agent_hierarchy"),
                              (404, "not_found"),
                          ])
        self.assertTrue(all(set(item.json()) ==
                            {"code", "message", "details", "trace_id"} for item in
                            (duplicate, hierarchy, hidden)))

    def test_host_delegation_injects_root_and_busy_worker_keeps_task(self):
        with patch.object(api, "db", self.database):
            created = self.client.post("/api/delegations", json={
                "assignee_agent_id": "worker-1", "goal": "Investigate",
                "context": {"message_refs": ["message-1"]},
                "limits": {"max_rounds": 3},
            })
            self.database.host_delegate_task.return_value = (self.task, None)
            busy = self.client.post("/api/delegations", json={
                "assignee_agent_id": "worker-1", "goal": "Investigate",
            })
        self.assertEqual((created.status_code, busy.status_code), (201, 201))
        self.assertEqual(created.headers["location"], "/api/delegations/task-1")
        self.assertEqual(created.json()["task"]["creator_agent_id"], "root-1")
        self.assertEqual(created.json()["run"]["run_id"], "run-1")
        self.assertIsNone(busy.json()["run"])
        self.assertEqual(self.database.host_delegate_task.call_args_list[0].args, (
            "root-1", "worker-1", "Investigate", [],
            {"message_refs": ["message-1"], "artifact_refs": []}, None,
            {"max_rounds": 3},
        ))
        self.assertEqual(self.runner.wake.call_count, 2)

    def test_cross_tree_worker_and_missing_root_are_hidden(self):
        with patch.object(api, "db", self.database):
            hidden = self.client.post("/api/delegations", json={
                "assignee_agent_id": "other-tree", "goal": "Goal",
            })
            self.database.get_supervisor.return_value = None
            no_root = self.client.post("/api/delegations", json={
                "assignee_agent_id": "worker-1", "goal": "Goal",
            })
        for response in (hidden, no_root):
            self.assertEqual((response.status_code, response.json()["code"]),
                             (404, "not_found"))
            self.assertEqual(set(response.json()),
                             {"code", "message", "details", "trace_id"})
        self.database.host_delegate_task.assert_not_called()

    def test_public_task_entries_allow_only_legacy_or_active_root_with_safe_context(self):
        inactive_root = {**self.root, "id": "root-old"}
        legacy = {
            "id": "legacy-1", "agent_role": None, "archived_at": None,
        }
        agents = {row["id"]: row for row in
                  (self.root, self.worker, inactive_root, legacy)}
        self.database.get_session.side_effect = agents.get
        self.database.create_task.side_effect = lambda agent_id, goal, acceptance, context: {
            **self.task, "session_id": agent_id, "goal": goal,
            "acceptance_json": "[]", "context_json": "{}",
        }
        with patch.object(api, "db", self.database):
            worker = self.client.post("/api/tasks", json={
                "agent_id": "worker-1", "goal": "Direct bypass",
            })
            inactive = self.client.post("/api/tasks", json={
                "agent_id": "root-old", "goal": "Wrong root",
            })
            injected = self.client.post("/api/tasks", json={
                "agent_id": "root-1", "goal": "Forged context", "context": {
                    "sender_session_id": "worker-1",
                    "workspace_roots": ["private-marker"],
                    "grant": ["filesystem.write"],
                },
            })
            worker_chat = self.client.post("/api/chat", json={
                "session_id": "worker-1", "message": "Direct bypass",
            })
            self.database.create_task.assert_not_called()
            self.database.create_legacy_chat_run.assert_not_called()
            root = self.client.post("/api/tasks", json={
                "agent_id": "root-1", "goal": "Host task",
                "context": {"message_refs": ["message-1"]},
            })
            old = self.client.post("/api/tasks", json={
                "agent_id": "legacy-1", "goal": "Legacy task",
            })
        self.assertEqual([(item.status_code, item.json()["code"])
                          for item in (worker, inactive)], [
                              (404, "not_found"), (404, "not_found"),
                          ])
        self.assertEqual(injected.status_code, 422)
        self.assertNotIn("private-marker", injected.text)
        self.assertEqual((worker_chat.status_code, worker_chat.json()["code"]),
                         (404, "not_found"))
        self.assertEqual((root.status_code, old.status_code), (201, 201))
        self.assertEqual(self.database.create_task.call_args_list[0].args, (
            "root-1", "Host task", [],
            {"message_refs": ["message-1"], "artifact_refs": []},
        ))
        self.assertEqual(self.database.create_task.call_args_list[1].args,
                         ("legacy-1", "Legacy task", [], None))

    def test_tree_tasks_and_result_are_scoped_and_safely_projected(self):
        with patch.object(api, "db", self.database):
            listed = self.client.get("/api/delegations?limit=10")
            detail = self.client.get("/api/delegations/task-1")
            self.database.get_task.return_value = {**self.task, "session_id": "other-tree"}
            hidden = self.client.get("/api/delegations/task-other")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["tasks"][0]["task_id"], "task-1")
        self.database.list_tree_tasks.assert_called_once_with("root-1", 10)
        self.assertEqual(detail.json()["result"]["summary"], "done")
        self.assertNotIn("private-marker", detail.text)
        self.assertEqual((hidden.status_code, hidden.json()["code"]), (404, "not_found"))
        self.assertEqual(self.database.get_task_result.call_count, 1)

    def test_message_list_and_exact_question_reply(self):
        with patch.object(api, "db", self.database):
            listed = self.client.get("/api/agents/root-1/agent-messages?after_id=4&limit=5")
            worker_inbox = self.client.post("/api/agents/worker-1/replies", json={
                "reply_to_message_id": 7, "content": "Forbidden",
            })
            self.database.host_reply.assert_not_called()
            replied = self.client.post("/api/agents/root-1/replies", json={
                "reply_to_message_id": 7, "content": "Proceed",
            })
            self.database.host_reply.side_effect = RepoError("invalid_run_transition")
            duplicate = self.client.post("/api/agents/root-1/replies", json={
                "reply_to_message_id": 7, "content": "Again",
            })
            hidden = self.client.get("/api/agents/other-tree/agent-messages")
            cross_tree_reply = self.client.post("/api/agents/other-tree/replies", json={
                "reply_to_message_id": 7, "content": "No",
            })
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["messages"][0]["kind"], "question")
        self.assertNotIn("private-marker", listed.text)
        self.database.list_agent_messages.assert_called_once_with("root-1", 4, 5)
        self.assertEqual(replied.status_code, 202)
        self.assertEqual(replied.json()["message"]["agent_id"], "worker-1")
        self.assertEqual((worker_inbox.status_code, worker_inbox.json()["code"]),
                         (404, "not_found"))
        self.database.host_reply.assert_any_call("root-1", 7, "Proceed")
        self.assertEqual(self.database.host_reply.call_count, 2)
        self.assertEqual((duplicate.status_code, duplicate.json()["code"]),
                         (409, "invalid_run_transition"))
        self.assertEqual((hidden.status_code, hidden.json()["code"]), (404, "not_found"))
        self.assertEqual((cross_tree_reply.status_code, cross_tree_reply.json()["code"]),
                         (404, "not_found"))

    def test_delegation_and_reply_reject_client_identity_and_paths(self):
        bad_payloads = [
            {"assignee_agent_id": "worker-1", "goal": "Goal", "creator_agent_id": "worker-1"},
            {"assignee_agent_id": "worker-1", "goal": "Goal",
             "context": {"workspace_roots": ["private-marker"]}},
            {"assignee_agent_id": "worker-1", "goal": "Goal",
             "requested_grant": {"grants": ["filesystem.read"]}},
            {"assignee_agent_id": "worker-1", "goal": "Goal",
             "limits": {"max_tool_calls": 33}},
        ]
        with patch.object(api, "db", self.database):
            rejected = [self.client.post("/api/delegations", json=body)
                        for body in bad_payloads]
            bad_reply = self.client.post("/api/agents/root-1/replies", json={
                "reply_to_message_id": 7, "content": "Proceed", "sender_agent_id": "root-1",
            })
        self.assertTrue(all(item.status_code == 422 for item in rejected + [bad_reply]))
        self.assertNotIn("private-marker", " ".join(item.text for item in rejected))
        self.database.host_delegate_task.assert_not_called()
        self.database.host_reply.assert_not_called()

    def test_p2_mutations_retain_host_cookie_and_exact_origin_dependency(self):
        def require_host_session(request):
            if request.cookies.get("host") != "ok":
                raise HTTPException(status_code=401, detail="unauthenticated")
            if request.method == "POST" and request.headers.get("origin") != "http://127.0.0.1:8001":
                raise HTTPException(status_code=403, detail="forbidden")
            return SimpleNamespace(actor_id="local-host")

        application = FastAPI()
        application.include_router(api.router)
        application.add_exception_handler(StarletteHTTPException, http_error_handler)
        with patch.dict(sys.modules, {"backend.auth": SimpleNamespace(
            require_host_session=require_host_session
        )}), patch.object(api, "db", self.database):
            with TestClient(application, base_url="http://127.0.0.1:8001") as client:
                unauthenticated = client.post("/api/delegations", json={
                    "assignee_agent_id": "worker-1", "goal": "Goal",
                })
                client.cookies.set("host", "ok")
                forbidden = client.post("/api/delegations", headers={
                    "Origin": "http://other.invalid",
                }, json={"assignee_agent_id": "worker-1", "goal": "Goal"})
                allowed = client.post("/api/delegations", headers={
                    "Origin": "http://127.0.0.1:8001",
                }, json={"assignee_agent_id": "worker-1", "goal": "Goal"})
        self.assertEqual([(item.status_code, item.json()["code"])
                          for item in (unauthenticated, forbidden)], [
                              (401, "unauthenticated"), (403, "forbidden"),
                          ])
        self.assertEqual(allowed.status_code, 201)


class RepoError(ValueError):
    def __init__(self, code):
        self.code = code


class P2AgentUiTest(unittest.TestCase):
    def test_tree_renders_status_and_capability_ceiling_without_html(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const nodes = new Map();
function node() {
  return { children: [], style: {}, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; }, querySelectorAll() { return []; } };
}
const context = {
  document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    createElement: node, querySelectorAll() { return []; } },
  window: {}, localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  fetch: async () => ({ ok: false, status: 500 }), console: { log() {} },
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
vm.runInContext(`agentTree = [{agent_id:'root', title:'<private-marker>',
  agent_role:'supervisor', depth:0, can_delegate:true, capability_grants:['read_only'],
  status:{state:'idle'}, archived_at:null}]; renderAgentTree();`, context);
const tree = nodes.get('agentTreeList');
assert.strictEqual(tree.children.length, 1);
assert(tree.children[0].children[0].textContent.includes('<private-marker>'));
assert.strictEqual(tree.children[0].children[0].innerHTML, undefined);
assert(tree.children[0].children[1].textContent.includes('read_only'));
assert.strictEqual(nodes.get('createSupervisorBtn').disabled, true);
"""
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "app.js")],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_question_reply_uses_inbox_agent_and_no_sender_body(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const nodes = new Map();
const calls = [];
function node() {
  return { children: [], style: {}, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; }, querySelectorAll() { return []; } };
}
const tree = [{agent_id:'root', title:'Supervisor', agent_role:'supervisor', depth:0,
  can_delegate:true, capability_grants:['read_only'], status:{state:'idle'}},
  {agent_id:'worker', title:'Worker', agent_role:'worker', depth:1,
  can_delegate:false, capability_grants:['read_only'], status:{state:'idle'}}];
const context = {
  document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    createElement: node, querySelectorAll() { return []; } },
  window: { prompt() { return 'Answer'; } },
  localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  fetch: async (url, options) => {
    if (url === '/api/config') return {ok:false,status:500};
    calls.push({url, options});
    let data = {};
    if (url.includes('/agent-messages')) data = {messages:[{message_id:7,
      agent_id:'root', sender_agent_id:'worker', kind:'question',
      content:'Need input', needs_reply:true}]};
    if (url === '/api/agents/tree') data = {agents:tree};
    if (url.startsWith('/api/delegations?')) data = {tasks:[]};
    return {ok:true,json:async()=>data};
  },
  console: {log() {}},
  tree,
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
vm.runInContext("activeSessionId='root'; agentTree=tree", context);
(async () => {
  await vm.runInContext('loadAgentMessages', context)();
  assert.strictEqual(nodes.get('agentMessageList').children.length, 1);
  assert.strictEqual(nodes.get('agentMessageList').children[0].children[1].textContent, '答复');
  await vm.runInContext('replyToAgent', context)('root', 7);
  const reply = calls.find((call) => call.url.endsWith('/replies'));
  assert.strictEqual(reply.url, '/api/agents/root/replies');
  assert.deepStrictEqual(JSON.parse(reply.options.body),
    {reply_to_message_id:7,content:'Answer'});
  vm.runInContext("activeSessionId='worker'; agentTree=tree", context);
  await vm.runInContext('loadAgentMessages', context)();
  assert.strictEqual(nodes.get('agentMessageList').children[0].children.length, 1);
  await vm.runInContext('replyToAgent', context)('worker', 7);
  assert.strictEqual(calls.filter((call) => call.url.endsWith('/replies')).length, 1);
  vm.runInContext('setSendButton()', context);
  assert.strictEqual(nodes.get('input').readOnly, true);
  assert.strictEqual(nodes.get('sendBtn').disabled, true);
  nodes.get('input').value = 'Direct Worker task';
  await vm.runInContext('sendMessage', context)();
  assert.strictEqual(calls.filter((call) => call.url === '/api/tasks').length, 0);
  vm.runInContext("activeSessionId='root'; setSendButton()", context);
  assert.strictEqual(nodes.get('input').readOnly, false);
})().catch((error) => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "app.js")],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
