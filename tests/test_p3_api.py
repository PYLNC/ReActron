"""P3 host Workspace routes, projections, and read-only Worker UI."""

import json
import shutil
import subprocess
import sys
import unittest
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


class CodeError(ValueError):
    def __init__(self, code):
        self.code = code


class P3WorkspaceApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Mock()
        self.service = Mock()
        self.root = {
            "id": "root-1", "title": "Supervisor", "agent_role": "supervisor",
            "parent_session_id": None, "root_session_id": "root-1", "depth": 0,
            "can_delegate": 1, "capabilities_json": json.dumps({"grants": ["read_only"]}),
            "created_at": "now", "updated_at": "now", "archived_at": None,
        }
        self.worker = {
            **self.root, "id": "worker-1", "title": "Worker", "agent_role": "worker",
            "parent_session_id": "root-1", "depth": 1, "can_delegate": 0,
        }
        self.legacy = {**self.root, "id": "legacy-1", "agent_role": None}
        self.workspace = {
            "id": "workspace-1", "name": "Repo", "root_path": "C:\\trusted\\repo",
            "kind": "git", "max_mode": "read_write", "created_at": "now",
            "updated_at": "now", "archived_at": None,
            "storage_relpath": "private-storage-marker",
        }
        self.task = {
            "id": "task-1", "session_id": "root-1", "goal": "Edit",
            "acceptance_json": "[]", "context_json": "{}", "status": "open",
            "workspace_id": "workspace-1", "workspace_mode": "read_write",
            "created_at": "now", "updated_at": "now", "finished_at": None,
        }
        agents = {row["id"]: row for row in (self.root, self.worker, self.legacy)}
        self.database.get_session.side_effect = agents.get
        self.database.get_supervisor.return_value = self.root
        self.database.get_latest_run_id.return_value = None
        self.database.get_agent_status.return_value = {
            "agent_id": "root-1", "state": "idle", "active_run_id": None,
            "queued_task_count": 0,
        }
        self.database.create_task.return_value = self.task
        self.database.get_task.return_value = self.task
        self.database.create_workspace.return_value = self.workspace
        self.database.list_workspaces.return_value = [self.workspace]
        self.database.update_workspace.return_value = {
            **self.workspace, "max_mode": "read_only",
        }
        self.database.list_task_artifacts.return_value = [{
            "id": "artifact-1", "task_id": "task-1", "run_id": "run-1",
            "owner_session_id": "worker-1", "workspace_id": "workspace-1",
            "kind": "patch", "relative_path": "changes.patch", "size_bytes": 12,
            "mime_type": "text/plain", "created_at": "now",
            "sha256": "private-hash-marker", "storage_relpath": "private-storage-marker",
        }]
        self.service.normalize_workspace_root.return_value = Path(self.workspace["root_path"])
        self.service.get_task_diff.return_value = "diff --git a/a b/a\n"
        self.service.cleanup_task_workspace.return_value = {
            "branch": "reactron/task-1", "removed": True,
        }

        db_patch = patch.object(api, "db", self.database)
        service_patch = patch.object(api, "_workspace_service", return_value=self.service)
        db_patch.start()
        service_patch.start()
        self.addCleanup(db_patch.stop)
        self.addCleanup(service_patch.stop)

        application = FastAPI()
        application.include_router(api.router)
        application.dependency_overrides[api._require_local_session] = lambda: SimpleNamespace(
            actor_id="local-host"
        )
        application.add_exception_handler(RequestValidationError, validation_error_handler)
        self.client = TestClient(application)

    def test_workspace_crud_is_strict_and_projects_only_host_metadata(self):
        created = self.client.post("/api/workspaces", json={
            "name": "Repo", "root_path": "C:\\trusted\\repo",
            "kind": "git", "max_mode": "read_write",
        })
        listed = self.client.get("/api/workspaces")
        shrunk = self.client.patch("/api/workspaces/workspace-1", json={
            "max_mode": "read_only",
        })
        expanded = self.client.patch("/api/workspaces/workspace-1", json={
            "max_mode": "read_write",
        })
        extra_create = self.client.post("/api/workspaces", json={
            "name": "Repo", "root_path": "C:\\trusted\\repo",
            "kind": "git", "max_mode": "read_only", "owner": "worker-1",
        })
        mutable_root = self.client.patch("/api/workspaces/workspace-1", json={
            "root_path": "C:\\other",
        })

        self.assertEqual((created.status_code, listed.status_code, shrunk.status_code),
                         (201, 200, 200))
        self.assertEqual(created.json()["workspace_id"], "workspace-1")
        self.assertIn("root_path", created.json())
        self.assertNotIn("private-storage-marker", created.text + listed.text)
        self.service.normalize_workspace_root.assert_called_once_with(
            "C:\\trusted\\repo", "git"
        )
        self.database.create_workspace.assert_called_once_with(
            "Repo", str(self.service.normalize_workspace_root.return_value),
            "git", "read_write",
        )
        self.database.update_workspace.assert_called_once_with(
            "workspace-1", None, "read_only", False
        )
        for response in (expanded, extra_create, mutable_root):
            self.assertEqual(response.status_code, 422)

    def test_workspace_routes_keep_host_cookie_and_exact_origin_gate(self):
        def require_host_session(request):
            if request.cookies.get("host") != "ok":
                raise HTTPException(status_code=401, detail="unauthenticated")
            if (request.method in {"POST", "PUT", "PATCH", "DELETE"}
                    and request.headers.get("origin") != "http://127.0.0.1:8001"):
                raise HTTPException(status_code=403, detail="forbidden")
            return SimpleNamespace(actor_id="local-host")

        application = FastAPI()
        application.include_router(api.router)
        application.add_exception_handler(StarletteHTTPException, http_error_handler)
        with patch.dict(sys.modules, {"backend.auth": SimpleNamespace(
                require_host_session=require_host_session
        )}):
            with TestClient(application, base_url="http://127.0.0.1:8001") as client:
                unauthenticated = client.get("/api/workspaces")
                client.cookies.set("host", "ok")
                forbidden = client.post("/api/workspaces", headers={
                    "Origin": "http://other.invalid",
                }, json={"name": "Repo", "root_path": "C:\\trusted\\repo",
                         "kind": "git", "max_mode": "read_only"})
                allowed = client.post("/api/workspaces", headers={
                    "Origin": "http://127.0.0.1:8001",
                }, json={"name": "Repo", "root_path": "C:\\trusted\\repo",
                         "kind": "git", "max_mode": "read_only"})
        self.assertEqual((unauthenticated.status_code, forbidden.status_code,
                          allowed.status_code), (401, 403, 201))

    def test_task_workspace_pair_is_root_only_and_delegation_cannot_override(self):
        bound = self.client.post("/api/tasks", json={
            "agent_id": "root-1", "goal": "Edit", "workspace_id": "workspace-1",
            "workspace_mode": "read_write",
        })
        self.assertEqual(bound.status_code, 201)
        self.assertEqual(bound.json()["workspace_id"], "workspace-1")
        self.database.create_task.assert_called_once_with(
            "root-1", "Edit", [], None, "workspace-1", "read_write"
        )

        self.database.create_task.reset_mock()
        self.database.create_task.return_value = {**self.task, "session_id": "legacy-1",
                                                   "workspace_id": None,
                                                   "workspace_mode": None}
        legacy = self.client.post("/api/tasks", json={
            "agent_id": "legacy-1", "goal": "Legacy",
        })
        missing_mode = self.client.post("/api/tasks", json={
            "agent_id": "root-1", "goal": "Edit", "workspace_id": "workspace-1",
        })
        legacy_bound = self.client.post("/api/tasks", json={
            "agent_id": "legacy-1", "goal": "Edit", "workspace_id": "workspace-1",
            "workspace_mode": "read_only",
        })
        worker_direct = self.client.post("/api/tasks", json={
            "agent_id": "worker-1", "goal": "Edit",
        })
        delegated_override = self.client.post("/api/delegations", json={
            "assignee_agent_id": "worker-1", "goal": "Edit",
            "workspace_id": "workspace-1", "workspace_mode": "read_only",
        })

        self.assertEqual(legacy.status_code, 201)
        self.database.create_task.assert_called_once_with("legacy-1", "Legacy", [], None)
        self.assertEqual((missing_mode.status_code, legacy_bound.status_code,
                          worker_direct.status_code, delegated_override.status_code),
                         (422, 422, 404, 422))
        self.database.host_delegate_task.assert_not_called()

    def test_filesystem_capability_is_two_strict_bools_and_write_implies_read(self):
        updated = {
            **self.root,
            "capabilities_json": json.dumps({
                "grants": ["read_only", "filesystem.read", "filesystem.write",
                           "private-marker"]
            }),
        }
        self.database.set_agent_filesystem_capability.return_value = updated
        accepted = self.client.put("/api/agents/root-1/filesystem-capability", json={
            "filesystem_read": True, "filesystem_write": True,
        })
        no_read = self.client.put("/api/agents/root-1/filesystem-capability", json={
            "filesystem_read": False, "filesystem_write": True,
        })
        non_bool = self.client.put("/api/agents/root-1/filesystem-capability", json={
            "filesystem_read": 1, "filesystem_write": False,
        })
        extra = self.client.put("/api/agents/root-1/filesystem-capability", json={
            "filesystem_read": True, "filesystem_write": False, "grants": [],
        })

        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["capability_grants"],
                         ["read_only", "filesystem.read", "filesystem.write"])
        self.assertNotIn("private-marker", accepted.text)
        self.database.set_agent_filesystem_capability.assert_called_once_with(
            "root-1", True, True
        )
        for response in (no_read, non_bool, extra):
            self.assertEqual(response.status_code, 422)

    def test_artifact_diff_cleanup_projection_and_workspace_error_codes(self):
        artifacts = self.client.get("/api/tasks/task-1/artifacts")
        diff = self.client.get("/api/tasks/task-1/diff?max_bytes=64")
        cleanup = self.client.post("/api/tasks/task-1/cleanup", json={})
        force = self.client.post("/api/tasks/task-1/cleanup", json={"force": True})

        self.assertEqual((artifacts.status_code, diff.status_code, cleanup.status_code),
                         (200, 200, 200))
        self.assertEqual(set(artifacts.json()["artifacts"][0]), {
            "artifact_id", "kind", "relative_path", "size_bytes", "mime_type", "created_at",
        })
        self.assertNotIn("private-hash-marker", artifacts.text)
        self.assertNotIn("private-storage-marker", artifacts.text)
        self.assertEqual(diff.json()["diff"], "diff --git a/a b/a\n")
        self.assertEqual(cleanup.json(), {"task_id": "task-1", "cleanup_status": "removed"})
        self.assertEqual(force.status_code, 422)
        self.service.get_task_diff.assert_called_once_with(self.database, "task-1", 64)
        self.service.cleanup_task_workspace.assert_called_once_with(self.database, "task-1")

        for code, status in (("workspace_in_use", 409), ("worktree_dirty", 409)):
            self.service.cleanup_task_workspace.side_effect = CodeError(code)
            response = self.client.post("/api/tasks/task-1/cleanup", json={})
            self.assertEqual((response.status_code, response.json()["code"]), (status, code))
        for code, status in (("workspace_scope_invalid", 422),
                             ("task_workspace_not_found", 404),
                             ("limit_exceeded", 413),
                             ("git_unavailable", 503),
                             ("git_timeout", 504),
                             ("git_failed", 500)):
            self.service.get_task_diff.side_effect = CodeError(code)
            response = self.client.get("/api/tasks/task-1/diff")
            self.assertEqual((response.status_code, response.json()["code"]), (status, code))


class P3WorkspaceUiTest(unittest.TestCase):
    def test_worker_is_read_only_and_only_git_write_tasks_expose_checkout_actions(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const nodes = new Map();
function node() {
  return {children: [], style: {}, value: '', textContent: '', hidden: false,
    disabled: false, readOnly: false,
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    addEventListener() {}, appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; }, querySelectorAll() { return []; }};
}
const context = {
  document: {getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
    createElement: node, querySelectorAll() { return []; }},
  window: {confirm() { return true; }, prompt() { return null; }},
  localStorage: {getItem() { return null; }, setItem() {}, removeItem() {}},
  fetch: async (url) => url === '/api/workspaces'
    ? {ok: true, json: async () => ({workspaces: [{workspace_id:'workspace-1',
        name:'<Repo>', kind:'git', max_mode:'read_write', root_path:'C:/trusted/repo'}]})}
    : url.includes('/artifacts')
      ? {ok: true, json: async () => ({artifacts: [{relative_path: 'report.txt',
          kind: 'text', size_bytes: 7}]})}
      : {ok: false, status: 500, json: async () => ({})},
  console: {log() {}}, setTimeout() {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
vm.runInContext(`workspaces = [{workspace_id:'workspace-1', name:'<Repo>', kind:'git',
  max_mode:'read_write', root_path:'C:/trusted/repo'}];
agentTree = [{agent_id:'root', agent_role:'supervisor'},
  {agent_id:'worker', agent_role:'worker'}]; activeSessionId='root';
renderWorkspaces(); workspaceSelect.value='workspace-1'; renderWorkspaceModeOptions();
setSendButton();`, context);
assert.strictEqual(nodes.get('workspaceControls').hidden, false);
assert.strictEqual(nodes.get('workspaceList').children[0].children[0].textContent.includes('<Repo>'), true);
assert.deepStrictEqual(nodes.get('workspaceModeSelect').children.map((item) => item.value),
  ['read_only', 'read_write']);
(async () => {
  const show = vm.runInContext('showTaskWorkspace', context);
  await show({task_id:'read-task', workspace_id:'workspace-1', workspace_mode:'read_only'});
  assert.strictEqual(nodes.get('viewTaskDiffBtn').disabled, true);
  assert.strictEqual(nodes.get('cleanupTaskBtn').disabled, true);
  await show({task_id:'write-task', workspace_id:'workspace-1', workspace_mode:'read_write'});
  assert.strictEqual(nodes.get('viewTaskDiffBtn').disabled, false);
  assert.strictEqual(nodes.get('cleanupTaskBtn').disabled, false);
  assert(nodes.get('taskWorkspaceStatus').textContent.includes('当前失败关闭'));
  assert.strictEqual(nodes.get('taskArtifactList').children[0].textContent.includes('report.txt'), true);
  vm.runInContext('workspaces = []', context);
  await show({task_id:'late-workspace', workspace_id:'workspace-1', workspace_mode:'read_write'});
  assert.strictEqual(nodes.get('viewTaskDiffBtn').disabled, true);
  await vm.runInContext('loadWorkspaces()', context);
  assert.strictEqual(nodes.get('viewTaskDiffBtn').disabled, false);
  vm.runInContext("activeSessionId='worker'; setSendButton()", context);
  assert.strictEqual(nodes.get('workspaceControls').hidden, true);
  assert.strictEqual(nodes.get('input').readOnly, true);
  assert.strictEqual(nodes.get('sendBtn').disabled, true);
})().catch((error) => { process.stderr.write(String(error)); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "app.js")],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
