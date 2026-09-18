"""P2 pre-integration smoke tests with a real temporary SQLite and host cookie."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, patch

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

    def test_host_reply_only_targets_root_inbox_and_resumes_once(self):
        with ExitStack() as stack:
            directory = stack.enter_context(tempfile.TemporaryDirectory())
            database = Database(Path(directory) / "replies.db")
            stack.callback(database.close)
            root = database.create_supervisor()
            worker = database.create_worker(root["id"], "worker")
            _task, run = database.host_delegate_task(root["id"], worker["id"], "ask")
            arguments = {"to_agent_id": root["id"], "kind": "question", "content": "Need input"}
            call = {"id": "question-1", "type": "function", "function": {
                "name": "send_agent_message", "arguments": json.dumps(arguments),
            }}
            active = database.claim_next_run("integration-test", 120)
            active, _ = database.transition_run(
                active["id"], active["version"], "running", "model.completed",
                {"round": 1, "tool_count": 1},
                checkpoint={"v": 1, "phase": "tool_pending", "round": 1,
                            "assistant_message_id": None, "next_tool_index": 0},
                message={"role": "assistant", "content": None, "tool_calls": [call]},
                lease_owner="integration-test", lease_epoch=active["lease_epoch"],
            )
            active = database.start_tool_call(
                active["id"], active["version"], "integration-test", active["lease_epoch"],
                "question-1", "send_agent_message", "effectful", "a" * 64, 8192,
            )
            waiting, _ = database.apply_control_tool(
                active["id"], active["version"], "integration-test", active["lease_epoch"],
                "question-1", "send_agent_message", arguments,
            )
            self.assertEqual((waiting["status"], waiting["wait_reason"]), ("waiting", "input"))
            question_id = database.list_agent_messages(root["id"])[0]["id"]

            application = FastAPI()
            application.include_router(api.public_router)
            application.include_router(api.router)
            application.add_exception_handler(StarletteHTTPException, http_error_handler)
            application.add_exception_handler(RequestValidationError, validation_error_handler)
            origin = "http://127.0.0.1:8001"
            secret = auth.issue_bootstrap_secret()
            with (patch.object(api, "db", database),
                  patch.object(api, "_runner", return_value=Mock()),
                  patch.object(database, "host_reply", wraps=database.host_reply) as handler,
                  TestClient(application, base_url=origin) as client):
                self.assertEqual(client.post("/api/auth/bootstrap", headers={"Origin": origin},
                                             json={"secret": secret}).status_code, 200)
                payload = {"reply_to_message_id": question_id, "content": "Proceed"}
                headers = {"Origin": origin}
                for agent_id in (worker["id"], "other-tree"):
                    denied = client.post(f"/api/agents/{agent_id}/replies",
                                         headers=headers, json=payload)
                    self.assertEqual((denied.status_code, denied.json()["code"]),
                                     (404, "not_found"))
                handler.assert_not_called()
                replied = client.post(f"/api/agents/{root['id']}/replies",
                                      headers=headers, json=payload)
                self.assertEqual(replied.status_code, 202, replied.text)
                self.assertEqual(replied.json()["run"]["run_id"], run["id"])
                self.assertEqual(handler.call_count, 1)
                duplicate = client.post(f"/api/agents/{root['id']}/replies",
                                        headers=headers, json=payload)
                self.assertEqual((duplicate.status_code, duplicate.json()["code"]),
                                 (409, "invalid_run_transition"))
                self.assertEqual(handler.call_count, 2)

            self.assertEqual(database.get_run(run["id"])["status"], "queued")
            self.assertFalse(database.list_agent_messages(root["id"])[0]["needs_reply"])
            self.assertEqual(database._query_one(
                "SELECT COUNT(*) AS n FROM events WHERE run_id=? AND type='run.resumed'",
                (run["id"],))["n"], 1)


if __name__ == "__main__":
    unittest.main()
