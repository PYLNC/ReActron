"""P2 pre-integration smoke test with a real temporary SQLite and host cookie."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend import api, auth
from backend.app import http_error_handler, validation_error_handler
from backend.db import Database


class P2IntegrationTest(unittest.TestCase):
    def test_authenticated_host_tree_and_legacy_api_share_real_database(self):
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            database = Database(Path(directory) / "p2.db")
            stack.callback(database.close)
            legacy = database.create_session("legacy")

            application = FastAPI()
            application.include_router(api.public_router)
            application.include_router(api.router)
            application.add_exception_handler(StarletteHTTPException, http_error_handler)
            application.add_exception_handler(RequestValidationError, validation_error_handler)
            secret = auth.issue_bootstrap_secret()
            origin = "http://127.0.0.1:8001"

            with patch.object(api, "db", database), TestClient(application, base_url=origin) as client:
                self.assertEqual(client.get("/api/supervisor").status_code, 401)
                self.assertEqual(client.post("/api/auth/bootstrap", headers={"Origin": origin},
                                             json={"secret": secret}).status_code, 200)
                self.assertIsNone(client.get("/api/supervisor").json()["supervisor"])
                self.assertIsNone(database.get_session(legacy["id"])["agent_role"])
                self.assertEqual(client.post("/api/supervisor", json={}).status_code, 403)

                headers = {"Origin": origin}
                root_response = client.post("/api/supervisor", headers=headers, json={
                    "title": "Root", "system_prompt": "private-marker",
                })
                self.assertEqual(root_response.status_code, 201, root_response.text)
                root_id = root_response.json()["agent_id"]
                self.assertEqual(client.post("/api/supervisor", headers=headers,
                                             json={}).status_code, 409)
                worker_response = client.post("/api/agents/workers", headers=headers, json={
                    "parent_agent_id": root_id, "title": "Worker",
                    "system_prompt": "private-marker",
                    "capabilities": {"grants": ["read_only", "control.send_agent_message"]},
                })
                self.assertEqual(worker_response.status_code, 201, worker_response.text)
                worker_id = worker_response.json()["agent_id"]
                tree = client.get("/api/agents/tree")
                self.assertEqual(tree.status_code, 200)
                self.assertEqual([row["agent_id"] for row in tree.json()["agents"]],
                                 [root_id, worker_id])
                self.assertEqual(worker_response.json()["parent_agent_id"], root_id)
                self.assertEqual(worker_response.json()["capability_grants"],
                                 ["control.send_agent_message", "read_only"])
                self.assertNotIn("private-marker", tree.text + worker_response.text)
                self.assertNotIn("system_prompt", tree.text + worker_response.text)
                self.assertNotIn("capabilities_json", tree.text + worker_response.text)
                self.assertEqual(client.post("/api/agents/workers", headers=headers, json={
                    "parent_agent_id": root_id, "title": "Forged", "from_agent_id": worker_id,
                }).status_code, 422)
                self.assertEqual(client.get(f"/api/agents/{worker_id}/status").json()["state"],
                                 "idle")

                self.assertEqual(client.get(f"/api/sessions/{legacy['id']}").status_code, 200)
                task = client.post("/api/tasks", headers=headers, json={
                    "agent_id": legacy["id"], "goal": "legacy task",
                })
                self.assertEqual(task.status_code, 201, task.text)
                self.assertEqual(task.json()["agent_id"], legacy["id"])
                self.assertIsNone(database.get_session(legacy["id"])["agent_role"])
                self.assertEqual(database._conn.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
