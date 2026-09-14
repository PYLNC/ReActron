"""P2 宿主 Agent 树控制面：仅薄路由与安全投影。"""

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from backend import api
from backend.app import validation_error_handler


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
            ]
        self.assertTrue(all(response.status_code == 422 for response in bad))
        self.assertTrue(all(set(response.json()) ==
                            {"code", "message", "details", "trace_id"} for response in bad))
        self.assertNotIn("private-marker", " ".join(response.text for response in bad))
        self.database.create_worker.assert_not_called()

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


if __name__ == "__main__":
    unittest.main()
