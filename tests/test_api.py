"""P0 API lifecycle, compatibility gate, and launcher contracts."""

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException


ROOT = Path(__file__).resolve().parent.parent


class _FakeRunner:
    def __init__(self):
        self.wakes = 0
        self.started = False
        self.closed = False
        self.control_result = None
        self.control_error = None
        self.approval_calls = []

    def wake(self):
        self.wakes += 1

    async def start(self):
        self.started = True

    async def close(self):
        self.closed = True

    async def control(self, run_id, action):
        if self.control_error:
            raise self.control_error
        return self.control_result

    async def approval_decided(self, approval_id):
        self.approval_calls.append(approval_id)
        return None

    async def wait_for_change(self, run_id, after_id, timeout=15):
        return after_id


class AppImportTest(unittest.TestCase):
    def test_import_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_backend = temp_root / "backend"
            temp_backend.mkdir()
            (temp_root / "static").mkdir()
            for filename in ("__init__.py", "app.py", "config.py"):
                shutil.copy2(ROOT / "backend" / filename, temp_backend / filename)

            env = os.environ.copy()
            env["PYTHONPATH"] = str(temp_root)
            result = subprocess.run(
                [sys.executable, "-B", "-c", "import backend.app"],
                cwd=temp_root,
                env=env,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((temp_root / "data").exists())


class AppLifespanTest(unittest.TestCase):
    def test_health_validation_shape_and_shutdown(self):
        from backend import app as app_module
        from backend import db as db_module
        from backend import runtime as runtime_module

        startup_order = []

        class FakeDatabase:
            initialized = False
            closed = False

            def initialize(self):
                self.initialized = True
                startup_order.append("database")

            def close(self):
                self.closed = True

        class FakeManager:
            refreshed = False

            async def refresh_registry(self):
                self.assert_database_ready()
                assert not fake_runner.started
                startup_order.append("mcp")
                self.refreshed = True
                return {"server_count": 0, "tool_count": 0, "errors": []}

            def clear_registry(self):
                raise AssertionError("successful refresh must not clear registry")

            def assert_database_ready(self):
                assert fake_db.initialized

        class OrderedRunner(_FakeRunner):
            async def start(self):
                startup_order.append("runner")
                await super().start()

        fake_db = FakeDatabase()
        fake_runner = OrderedRunner()
        fake_manager = FakeManager()
        with (
            patch.object(db_module, "db", fake_db),
            patch.object(runtime_module, "get_runner", return_value=fake_runner, create=True),
            patch("backend.security.validate_secret_key") as validate_secret_key,
            patch("backend.mcp.manager.get_manager", return_value=fake_manager),
            patch.dict(sys.modules, {"backend.auth": SimpleNamespace(
                issue_bootstrap_secret=lambda: "test-secret",
                require_host_session=lambda request: None,
            )}),
            TestClient(app_module.app) as client,
        ):
            health = client.get("/api/health")
            static = client.get("/static/app.js")
            missing_route = client.get("/api/does-not-exist")
            marker = "validation-secret-marker"
            with self.assertLogs("agent.app", level="WARNING") as logs:
                invalid = client.post("/api/chat", json={"message": {"value": marker}})

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
        self.assertEqual(static.status_code, 200)
        self.assertEqual(missing_route.status_code, 404)
        self.assertEqual(missing_route.json()["code"], "not_found")
        self.assertEqual(
            set(missing_route.json()), {"code", "message", "details", "trace_id"}
        )
        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(
            set(invalid.json()), {"code", "message", "details", "trace_id"}
        )
        self.assertEqual(invalid.json()["code"], "request_validation_error")
        self.assertTrue(invalid.json()["trace_id"])
        self.assertNotIn(marker, invalid.text)
        self.assertNotIn(marker, "\n".join(logs.output))
        validate_secret_key.assert_called_once_with()
        self.assertTrue(fake_manager.refreshed)
        self.assertEqual(startup_order, ["database", "mcp", "runner"])
        self.assertTrue(fake_db.initialized)
        self.assertTrue(fake_runner.started)
        self.assertTrue(fake_runner.closed)
        self.assertTrue(fake_db.closed)

    def test_failed_mcp_refresh_clears_before_runner_starts(self):
        from backend import app as app_module
        from backend import db as db_module
        from backend import runtime as runtime_module

        order = []

        class FakeDatabase:
            def initialize(self):
                order.append("database")

            def close(self):
                order.append("database.close")

        class FakeManager:
            async def refresh_registry(self):
                order.append("mcp.refresh")
                raise RuntimeError("offline")

            def clear_registry(self):
                order.append("mcp.clear")

        class FakeRunner(_FakeRunner):
            async def start(self):
                order.append("runner")
                await super().start()

            async def close(self):
                order.append("runner.close")
                await super().close()

        fake_db, fake_runner = FakeDatabase(), FakeRunner()
        with (
            patch.object(db_module, "db", fake_db),
            patch.object(runtime_module, "get_runner", return_value=fake_runner, create=True),
            patch("backend.security.validate_secret_key"),
            patch("backend.mcp.manager.get_manager", return_value=FakeManager()),
            patch.dict(sys.modules, {"backend.auth": SimpleNamespace(
                issue_bootstrap_secret=lambda: "test-secret",
                require_host_session=lambda request: None,
            )}),
            self.assertLogs("agent.app", level="WARNING"),
            TestClient(app_module.app),
        ):
            pass

        self.assertEqual(order, ["database", "mcp.refresh", "mcp.clear", "runner",
                                 "runner.close", "database.close"])

    def test_invalid_secret_fails_before_database_or_mcp_import(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            temp_backend = temp_root / "backend"
            temp_backend.mkdir()
            (temp_root / "static").mkdir()
            for filename in ("__init__.py", "app.py", "config.py", "security.py"):
                shutil.copy2(ROOT / "backend" / filename, temp_backend / filename)

            environment = os.environ.copy()
            environment.update(
                {
                    "APP_SECRET_KEY": "not-a-fernet-key",
                    "PYTHONPATH": str(temp_root),
                    "WEB_CONCURRENCY": "1",
                }
            )
            script = """
import sys
from fastapi.testclient import TestClient
import backend.app as app_module

try:
    with TestClient(app_module.app):
        pass
except RuntimeError as exc:
    assert "APP_SECRET_KEY" in str(exc), exc
    assert "backend.api" not in sys.modules
    assert "backend.db" not in sys.modules
    assert "backend.mcp.manager" not in sys.modules
else:
    raise AssertionError("invalid secret unexpectedly started the application")
"""
            result = subprocess.run(
                [sys.executable, "-B", "-c", script],
                cwd=temp_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((temp_root / "data").exists())

    def test_multiple_workers_are_rejected_before_startup(self):
        from backend import app as app_module

        with patch.dict(os.environ, {"WEB_CONCURRENCY": "2"}):
            with self.assertRaisesRegex(ValueError, "WEB_CONCURRENCY"):
                with TestClient(app_module.app):
                    pass


class ErrorContractTest(unittest.TestCase):
    def test_http_auth_and_hidden_resource_errors(self):
        from backend.app import http_error_handler

        request = SimpleNamespace(url=SimpleNamespace(path="/api/private"))
        for status, code, message in (
            (401, "unauthenticated", "请先完成本地认证"),
            (403, "forbidden", "无权执行此操作"),
            (404, "not_found", "资源不存在"),
        ):
            response = asyncio.run(
                http_error_handler(request, StarletteHTTPException(status, "secret-marker"))
            )
            body = json.loads(response.body)
            self.assertEqual(response.status_code, status)
            self.assertEqual(body["code"], code)
            self.assertEqual(body["message"], message)
            self.assertEqual(set(body), {"code", "message", "details", "trace_id"})
            self.assertNotIn("secret-marker", response.body.decode())

    def test_unexpected_error_does_not_expose_exception(self):
        from backend.app import internal_error_handler

        request = SimpleNamespace(url=SimpleNamespace(path="/api/private"))
        with self.assertLogs("agent.app", level="ERROR") as logs:
            response = asyncio.run(
                internal_error_handler(request, RuntimeError("private-marker"))
            )
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(body["code"], "internal_error")
        self.assertEqual(set(body), {"code", "message", "details", "trace_id"})
        self.assertNotIn("private-marker", response.body.decode())
        self.assertNotIn("private-marker", "\n".join(logs.output))

    def test_sse_info_log_omits_model_and_tool_payloads(self):
        from backend.api import _log_sse_event

        marker = "private-tool-marker"
        with self.assertLogs("agent.api", level="INFO") as logs:
            for event_type in ("text", "tool_call", "tool_result", marker):
                _log_sse_event({
                    "type": event_type,
                    "content": marker,
                    "name": marker,
                    "arguments": marker,
                    "message": marker,
                })
        self.assertNotIn(marker, "\n".join(logs.output))


class AuthWiringTest(unittest.TestCase):
    def test_bootstrap_is_public_and_other_routes_delegate_auth(self):
        from fastapi import FastAPI

        from backend import api
        from backend.app import http_error_handler

        seen = []

        def require_host_session(request):
            seen.append((request.method, request.url.path))
            if request.cookies.get("test_host_session") != "valid":
                raise HTTPException(status_code=401, detail="unauthenticated")
            if request.method == "POST" and request.headers.get("origin") != "http://127.0.0.1:8001":
                raise HTTPException(status_code=403, detail="forbidden")

        def bootstrap(request, response, secret):
            seen.append(("bootstrap", request.headers.get("host"), request.headers.get("origin")))
            if secret != "one-time-secret":
                raise HTTPException(status_code=401, detail="unauthenticated")
            response.set_cookie("test_host_session", "valid")

        application = FastAPI()
        application.include_router(api.public_router)
        application.include_router(api.router)
        application.add_exception_handler(StarletteHTTPException, http_error_handler)
        fake_auth = SimpleNamespace(
            bootstrap=bootstrap,
            require_host_session=require_host_session,
        )
        with patch.dict(sys.modules, {"backend.auth": fake_auth}), patch.object(
            api, "db", SimpleNamespace(get_session=lambda session_id: None)
        ):
            with TestClient(application, base_url="http://127.0.0.1:8001") as client:
                self.assertEqual(client.get("/api/health").status_code, 200)
                denied = client.get("/api/tools")
                authenticated = client.post(
                    "/api/auth/bootstrap",
                    headers={"Origin": "http://127.0.0.1:8001"},
                    json={"secret": "one-time-secret"},
                )
                allowed = client.get("/api/tools")
                missing = client.get("/api/sessions/no-such-session")
                forbidden = client.post(
                    "/api/harness/self-test",
                    headers={"Origin": "http://other.invalid"},
                )

        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["code"], "unauthenticated")
        self.assertEqual(authenticated.status_code, 200)
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["code"], "not_found")
        self.assertEqual(forbidden.status_code, 403)
        self.assertEqual(forbidden.json()["code"], "forbidden")
        self.assertIn(("bootstrap", "127.0.0.1:8001", "http://127.0.0.1:8001"), seen)
        self.assertNotIn(("GET", "/api/health"), seen)
        self.assertNotIn(("POST", "/api/auth/bootstrap"), seen)


class Wave3SchemaTest(unittest.TestCase):
    def test_task_accepts_only_agent_id_and_nonempty_goal(self):
        from pydantic import ValidationError

        from backend.schemas import TaskCreateRequest

        payload = TaskCreateRequest(agent_id=" agent-1 ", goal=" goal ")
        self.assertEqual((payload.agent_id, payload.goal), ("agent-1", "goal"))
        self.assertEqual(payload.acceptance, [])
        self.assertIsNone(payload.context)
        with self.assertRaises(ValidationError):
            TaskCreateRequest(agent_id="agent-1", session_id="agent-1", goal="goal")
        with self.assertRaises(ValidationError):
            TaskCreateRequest(agent_id="agent-1", goal="  ")

    def test_run_body_rejects_client_options_and_identity(self):
        from pydantic import ValidationError

        from backend.schemas import RunCreateRequest

        self.assertEqual(RunCreateRequest().model_dump(), {})
        with self.assertRaises(ValidationError):
            RunCreateRequest(agent_id="agent-1")


class Wave3RouteTest(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI

        from backend import api, runtime
        from backend.app import http_error_handler, validation_error_handler

        self.api = api
        self.runner = _FakeRunner()
        runner_patch = patch.object(api, "_runner", return_value=self.runner)
        runner_patch.start()
        self.addCleanup(runner_patch.stop)
        actions_patch = patch.object(runtime, "allowed_actions", side_effect=lambda row:
                                     ["cancel"] if row["status"] == "queued" else [])
        actions_patch.start()
        self.addCleanup(actions_patch.stop)
        self.database = Mock()
        self.task = {
            "id": "task-1", "session_id": "agent-1", "goal": "goal",
            "acceptance_json": "[]", "context_json": "{}", "status": "open",
            "created_at": "2026-09-13T00:00:00Z", "updated_at": "2026-09-13T00:00:00Z",
            "finished_at": None,
        }
        self.run = {
            "id": "run-1", "task_id": "task-1", "session_id": "agent-1",
            "attempt": 1, "status": "queued", "wait_reason": None,
            "created_at": "2026-09-13T00:01:00Z", "started_at": None,
            "finished_at": None, "idempotency_key": "private-key", "lease_owner": "private",
        }
        self.database.create_task.return_value = self.task
        self.database.get_session.return_value = {
            "id": "agent-1", "agent_role": None, "archived_at": None,
        }
        self.database.get_task.return_value = self.task
        self.database.list_tasks.return_value = [self.task]
        self.database.get_latest_run_id.return_value = None
        self.database.create_run.return_value = (self.run, True)
        self.database.get_run.return_value = self.run
        self.database.get_last_event_id.return_value = 1
        self.database.list_sessions.return_value = [{
            "id": "agent-1", "title": "Agent", "project_id": "project-1",
            "created_at": "2026-09-13T00:00:00Z", "updated_at": "2026-09-13T00:00:00Z",
        }]
        application = FastAPI()
        application.include_router(api.router)
        application.dependency_overrides[api._require_local_session] = lambda: None
        application.add_exception_handler(StarletteHTTPException, http_error_handler)
        application.add_exception_handler(RequestValidationError, validation_error_handler)
        self.client = TestClient(application)

    def test_task_create_get_list_and_agent_alias(self):
        with patch.object(self.api, "db", self.database):
            created = self.client.post("/api/tasks", json={"agent_id": "agent-1", "goal": "goal"})
            fetched = self.client.get("/api/tasks/task-1")
            listed = self.client.get("/api/tasks?agent_id=agent-1&status=open&limit=10")
            agents = self.client.get("/api/agents")
        self.assertEqual(created.status_code, 201)
        self.assertEqual(created.json()["task_id"], "task-1")
        self.assertEqual(created.json()["agent_id"], "agent-1")
        self.assertNotIn("session_id", created.text)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(listed.json()["tasks"][0]["task_id"], "task-1")
        self.database.list_tasks.assert_called_once_with("agent-1", "open", 10)
        self.assertEqual(agents.json()["agents"][0]["agent_id"], "agent-1")
        self.assertNotIn("session_id", agents.text)

    def test_run_created_and_replayed_with_location(self):
        with patch.object(self.api, "db", self.database):
            first = self.client.post(
                "/api/tasks/task-1/runs", headers={"Idempotency-Key": "key-1"}
            )
            self.database.create_run.return_value = (self.run, False)
            replay = self.client.post(
                "/api/tasks/task-1/runs", headers={"Idempotency-Key": "key-1"}, json={}
            )
            fetched = self.client.get("/api/runs/run-1")
        for response in (first, replay):
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.headers["location"], "/api/runs/run-1")
            self.assertEqual(response.json()["run_id"], "run-1")
            self.assertEqual(response.json()["last_event_id"], 1)
            self.assertEqual(response.json()["allowed_actions"], ["cancel"])
            self.assertEqual(response.json()["status"], "queued")
            self.assertNotIn("idempotency_key", response.text)
            self.assertNotIn("lease_owner", response.text)
        self.assertEqual(fetched.status_code, 200)
        self.assertEqual(self.database.create_run.call_count, 2)
        self.database.create_run.assert_called_with("task-1", "key-1")

    def test_missing_key_extra_identity_invalid_limit_and_missing_resources(self):
        with patch.object(self.api, "db", self.database):
            missing_key = self.client.post("/api/tasks/task-1/runs", json={})
            extra_run = self.client.post(
                "/api/tasks/task-1/runs", headers={"Idempotency-Key": "key-1"},
                json={"session_id": "agent-1"},
            )
            extra = self.client.post("/api/tasks", json={
                "agent_id": "agent-1", "session_id": "agent-1", "goal": "goal",
            })
            invalid_limit = self.client.get("/api/tasks?limit=101")
            dual_query = self.client.get("/api/tasks?agent_id=agent-1&session_id=agent-1")
            self.database.get_task.return_value = None
            self.database.get_run.return_value = None
            missing_task = self.client.get("/api/tasks/unknown")
            missing_run = self.client.get("/api/runs/unknown")
        self.assertEqual((missing_key.status_code, missing_key.json()["code"]),
                         (400, "idempotency_key_required"))
        for response in (extra, extra_run, invalid_limit, dual_query):
            self.assertEqual(response.status_code, 422)
            self.assertEqual(set(response.json()), {"code", "message", "details", "trace_id"})
        for response in (missing_task, missing_run):
            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["code"], "not_found")

    def test_repository_conflicts_map_to_stable_409(self):
        class RepoError(ValueError):
            def __init__(self, code):
                self.code = code
                self.existing_run_id = "run-1"

        with patch.object(self.api, "db", self.database):
            outcomes = []
            for code in ("idempotency_key_reused", "run_already_active"):
                self.database.create_run.side_effect = RepoError(code)
                outcomes.append(self.client.post(
                    "/api/tasks/task-1/runs", headers={"Idempotency-Key": "key-2"}, json={}
                ))
        self.assertEqual([response.status_code for response in outcomes], [409, 409])
        self.assertEqual([response.json()["code"] for response in outcomes],
                         ["idempotency_key_reused", "run_already_active"])
        self.assertEqual(outcomes[1].json()["details"], {"run_id": "run-1"})
        self.assertTrue(all(set(response.json()) == {"code", "message", "details", "trace_id"}
                            for response in outcomes))

    def test_repository_hidden_resource_maps_to_404(self):
        class MissingResource(ValueError):
            code = "not_found"

        self.database.create_task.side_effect = MissingResource()
        with patch.object(self.api, "db", self.database):
            response = self.client.post(
                "/api/tasks", json={"agent_id": "other-owner", "goal": "goal"}
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["code"], "not_found")
        self.assertEqual(set(response.json()), {"code", "message", "details", "trace_id"})

    def test_delete_session_with_active_run_is_409_without_data_loss(self):
        class ActiveRun(ValueError):
            code = "run_already_active"
            existing_run_id = "run-1"

        session = dict(self.database.list_sessions.return_value[0])
        run = dict(self.run)
        self.database.get_session.side_effect = lambda _id: session or None

        def delete(_id):
            if run["status"] == "queued":
                raise ActiveRun()
            session.clear()

        self.database.delete_session.side_effect = delete
        with patch.object(self.api, "db", self.database):
            conflict = self.client.delete("/api/sessions/agent-1")
            self.assertEqual(session["id"], "agent-1")
            self.assertEqual(run["status"], "queued")
            run["status"] = "completed"
            deleted = self.client.delete("/api/sessions/agent-1")

        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["code"], "run_already_active")
        self.assertEqual(conflict.json()["details"], {"run_id": "run-1"})
        self.assertEqual(set(conflict.json()), {"code", "message", "details", "trace_id"})
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"ok": True})
        self.assertFalse(session)


class UploadCleanupTest(unittest.TestCase):
    def _rejected(self, filename, content, expected_status, expected_code, parser=None):
        from backend import api
        from backend.app import http_error_handler

        marker = "private-upload-marker"
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_dir = Path(temp_dir) / "uploads"
            database = Mock()
            file = UploadFile(file=io.BytesIO(content), filename=filename)
            with patch.object(api, "UPLOAD_DIR", upload_dir), patch.object(api, "db", database):
                with patch.object(api, "parse_uploaded_file", side_effect=parser) as parse:
                    with self.assertRaises(HTTPException) as raised:
                        asyncio.run(api.upload_file(file))
            response = asyncio.run(http_error_handler(
                SimpleNamespace(url=SimpleNamespace(path="/api/upload")), raised.exception
            ))
            body = json.loads(response.body)
            self.assertEqual(response.status_code, expected_status)
            self.assertEqual(body["code"], expected_code)
            self.assertEqual(set(body), {"code", "message", "details", "trace_id"})
            self.assertNotIn(marker, response.body.decode())
            self.assertEqual(list(upload_dir.iterdir()) if upload_dir.exists() else [], [])
            database.register_upload.assert_not_called()
            return parse.call_count

    def test_raw_limit_and_exact_boundary(self):
        from backend import api

        limit = api.MAX_UPLOAD_SIZE
        self.assertEqual(self._rejected(
            "private-upload-marker.txt", b"x" * (limit + 1),
            413, "upload_limit_exceeded",
        ), 0)
        with tempfile.TemporaryDirectory() as temp_dir:
            upload_dir = Path(temp_dir) / "uploads"
            database = Mock()
            with (
                patch.object(api, "UPLOAD_DIR", upload_dir),
                patch.object(api, "db", database),
                patch.object(api, "parse_uploaded_file", return_value={"type": "text"}),
            ):
                result = asyncio.run(api.upload_file(UploadFile(
                    file=io.BytesIO(b"x" * limit), filename="exact.txt"
                )))
            self.assertEqual(result["size"], limit)
            database.register_upload.assert_called_once()

    def test_invalid_extension_and_non_zip_signature(self):
        from backend import file_parser

        parser = (file_parser.parse_uploaded_file if hasattr(file_parser, "_check_zip_limits")
                  else ValueError("Invalid ZIP container"))
        self.assertEqual(self._rejected(
            "private-upload-marker.exe", b"bad", 415, "upload_type_mismatch",
        ), 0)
        with patch.object(file_parser, "_parse_excel") as heavy_parser:
            self.assertEqual(self._rejected(
                "private-upload-marker.xlsx", b"not a ZIP",
                415, "upload_type_mismatch",
                parser=parser,
            ), 1)
            heavy_parser.assert_not_called()

    def test_zip_entry_bomb_maps_to_413(self):
        from backend import file_parser

        parser = (file_parser.parse_uploaded_file if hasattr(file_parser, "_check_zip_limits")
                  else ValueError("ZIP entry limit exceeded"))
        archive_bytes = io.BytesIO()
        with ZipFile(archive_bytes, "w") as archive:
            for index in range(2_001):
                archive.writestr(f"entry-{index}", b"")
        with patch.object(file_parser, "_parse_excel") as heavy_parser:
            self.assertEqual(self._rejected(
                "private-upload-marker.xlsx", archive_bytes.getvalue(),
                413, "upload_limit_exceeded",
                parser=parser,
            ), 1)
            heavy_parser.assert_not_called()

    def test_parser_failure_removes_uploaded_file(self):
        from backend import api

        with tempfile.TemporaryDirectory() as temp_dir:
            upload_dir = Path(temp_dir) / "uploads"
            file = UploadFile(file=io.BytesIO(b"input"), filename="note.txt")
            database = Mock()
            with (
                patch.object(api, "UPLOAD_DIR", upload_dir),
                patch.object(api, "parse_uploaded_file", side_effect=ValueError("bad")),
                patch.object(api, "db", database),
            ):
                with self.assertRaises(ValueError):
                    asyncio.run(api.upload_file(file))
            self.assertEqual(list(upload_dir.iterdir()), [])
            database.register_upload.assert_not_called()

    def test_database_failure_removes_uploaded_file(self):
        from backend import api

        with tempfile.TemporaryDirectory() as temp_dir:
            upload_dir = Path(temp_dir) / "uploads"
            file = UploadFile(file=io.BytesIO(b"input"), filename="note.txt")
            database = Mock()
            database.register_upload.side_effect = RuntimeError("db unavailable")
            with (
                patch.object(api, "UPLOAD_DIR", upload_dir),
                patch.object(api, "parse_uploaded_file", return_value={"text": "input"}),
                patch.object(api, "db", database),
            ):
                with self.assertRaises(RuntimeError):
                    asyncio.run(api.upload_file(file))
            self.assertEqual(list(upload_dir.iterdir()), [])
            database.register_upload.assert_called_once()


class MCPCreateContractTest(unittest.TestCase):
    def test_new_server_stays_disabled(self):
        from backend import api
        from backend.db import Database

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "mcp.db")
            try:
                with patch.object(api, "db", database):
                    response = asyncio.run(api.create_mcp_server(
                        api.MCPServerCreateRequest(
                            name="new", transport="stdio", command="C:\\tools\\fake.exe",
                        )
                    ))
                stored = database.get_mcp_server(response["id"])
                self.assertEqual(response["enabled"], 0)
                self.assertEqual(stored["enabled"], 0)
            finally:
                database.close()


class RunStreamTest(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from backend import api
        from backend.app import http_error_handler, validation_error_handler

        self.api = api
        self.db = Mock()
        self.runner = _FakeRunner()
        self.run = {"id": "run-1", "task_id": "task-1", "session_id": "session-1",
                    "status": "completed", "attempt": 1, "wait_reason": None,
                    "created_at": "2026-09-13T00:00:00Z", "started_at": None,
                    "finished_at": "2026-09-13T00:01:00Z"}
        self.events = [
            {"id": 1, "task_id": "task-1", "run_id": "run-1", "type": "run.queued",
             "created_at": "2026-09-13T00:00:00Z", "payload_json": "{}"},
            {"id": 2, "task_id": "task-1", "run_id": "run-1", "type": "message.completed",
             "created_at": "2026-09-13T00:00:01Z",
             "payload_json": json.dumps({"message_id": 7, "hash": "hash", "length": 4})},
            {"id": 3, "task_id": "task-1", "run_id": "run-1", "type": "run.completed",
             "created_at": "2026-09-13T00:00:02Z", "payload_json": "{}"},
        ]
        self.db.get_run.return_value = self.run
        self.db.get_last_event_id.return_value = 3
        self.db.list_events.side_effect = lambda _id, after, _limit: [
            event for event in self.events if event["id"] > after
        ]
        self.db.get_message.return_value = {
            "id": 7, "session_id": "session-1", "task_id": "task-1", "run_id": "run-1",
            "role": "assistant", "content": "done", "created_at": "2026-09-13T00:00:01Z",
        }
        self.db.get_session.return_value = {"id": "session-1", "archived_at": None}
        application = FastAPI()
        application.include_router(api.router)
        application.dependency_overrides[api._require_local_session] = lambda: SimpleNamespace(actor_id="local-host")
        application.add_exception_handler(StarletteHTTPException, http_error_handler)
        application.add_exception_handler(RequestValidationError, validation_error_handler)
        self.client = TestClient(application)
        db_patch = patch.object(api, "db", self.db)
        runner_patch = patch.object(api, "_runner", return_value=self.runner)
        db_patch.start()
        runner_patch.start()
        self.addCleanup(db_patch.stop)
        self.addCleanup(runner_patch.stop)

    def test_replay_envelope_cursor_and_message_repair(self):
        replay = self.client.get("/api/runs/run-1/events?after_id=0")
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.text.count("event: run.completed"), 1)
        self.assertIn("id: 2\nevent: message.completed\n", replay.text)
        frame = next(line[6:] for line in replay.text.splitlines() if line.startswith("data: "))
        self.assertEqual(set(json.loads(frame)), {"v", "event_id", "task_id", "run_id",
                                                "agent_id", "type", "ts", "data"})
        repaired = self.client.get("/api/messages/7")
        self.assertEqual(repaired.json()["content"], "done")
        self.assertEqual(repaired.json()["agent_id"], "session-1")

    def test_header_precedes_query_and_future_cursor_rejected(self):
        replay = self.client.get("/api/runs/run-1/events?after_id=1",
                                 headers={"Last-Event-ID": "2"})
        self.assertNotIn("id: 2\n", replay.text)
        self.assertIn("id: 3\n", replay.text)
        for cursor in ("-1", "4", "not-a-number"):
            denied = self.client.get("/api/runs/run-1/events?after_id=" + cursor)
            self.assertEqual((denied.status_code, denied.json()["code"]),
                             (400, "invalid_cursor"))
            self.assertEqual(set(denied.json()), {"code", "message", "details", "trace_id"})

    def test_subscription_disconnect_does_not_control_run(self):
        self.run["status"] = "running"
        self.events = self.events[:1]
        self.db.get_last_event_id.return_value = 1
        request = SimpleNamespace(is_disconnected=lambda: asyncio.sleep(0, result=True))

        async def disconnect():
            stream = self.api._follow_events(self.run, 0, request)
            first = await anext(stream)
            with self.assertRaises(StopAsyncIteration):
                await anext(stream)
            return first

        first = asyncio.run(disconnect())
        self.assertIn("id: 1\n", first)
        self.assertEqual(self.run["status"], "running")
        self.assertEqual(self.runner.wakes, 0)

    def test_legacy_chat_uses_one_transaction_and_same_run(self):
        self.db.create_legacy_chat_run.return_value = ({"id": "task-1"}, self.run)
        response = self.client.post("/api/chat", json={"session_id": "session-1", "message": "hi"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-ReActron-Run-ID"], "run-1")
        self.assertIn('"type":"text","content":"done"', response.text)
        self.db.create_legacy_chat_run.assert_called_once()
        self.db.add_message.assert_not_called()
        self.assertEqual(self.runner.wakes, 1)

    def test_archived_chat_is_404_without_write(self):
        self.db.get_session.return_value = {"id": "session-1", "archived_at": "2026-09-13T00:00:00Z"}
        response = self.client.post("/api/chat", json={"session_id": "session-1", "message": "hi"})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"))
        self.db.create_legacy_chat_run.assert_not_called()

    def test_control_delegates_and_conflict_is_four_fields(self):
        self.run["status"] = "queued"
        self.runner.control_result = dict(self.run, status="cancelled")
        cancelled = self.client.post("/api/runs/run-1/control", json={"action": "cancel"})
        self.assertEqual(cancelled.status_code, 202)
        self.assertEqual(cancelled.json()["status"], "cancelled")

        self.runner.control_result = dict(self.run, status="paused")
        accepted = self.client.post("/api/runs/run-1/control", json={"action": "pause"})
        self.assertEqual(accepted.status_code, 202)
        self.assertEqual(accepted.json()["status"], "paused")

        class InvalidTransition(ValueError):
            code = "invalid_run_transition"

        self.runner.control_error = InvalidTransition()
        denied = self.client.post("/api/runs/run-1/control", json={"action": "resume"})
        self.assertEqual((denied.status_code, denied.json()["code"]),
                         (409, "invalid_run_transition"))
        self.assertEqual(set(denied.json()), {"code", "message", "details", "trace_id"})

    def test_approval_decision_uses_server_hash_and_keeps_waiting(self):
        self.run["status"] = "waiting"
        approval = {
            "id": "approval-1", "run_id": "run-1", "tool_call_id": "call-1",
            "kind": "resolve_unknown_outcome", "action_hash": "server-hash",
            "request_summary_json": '{"tool":"demo"}', "status": "pending",
            "expires_at": "2099-01-01T00:00:00Z", "decided_at": None,
            "decision_json": None,
        }
        self.db.list_approvals.return_value = [approval]
        self.db.get_approval.return_value = approval
        self.db.decide_approval.return_value = dict(
            approval, status="resolved", decision_json='{"decision":"occurred","note":null}'
        )
        listed = self.client.get("/api/approvals")
        rejected = self.client.post("/api/approvals/approval-1/decision", json={
            "decision": "occurred", "action_hash": "client-hash",
        })
        decided = self.client.post("/api/approvals/approval-1/decision",
                                   json={"decision": "occurred"})
        self.assertEqual(listed.json()["approvals"][0]["action_hash"], "server-hash")
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(decided.status_code, 200)
        self.assertEqual(decided.json()["status"], "resolved")
        self.assertEqual(self.run["status"], "waiting")
        self.db.decide_approval.assert_called_once_with(
            "approval-1", "occurred", "local-host", None
        )
        self.assertEqual(self.runner.approval_calls, ["approval-1"])

        self.db.get_session.return_value = {"id": "session-1", "archived_at": "archived"}
        hidden = self.client.get("/api/approvals")
        blocked = self.client.post("/api/approvals/approval-1/decision",
                                   json={"decision": "occurred"})
        self.assertEqual(hidden.json(), {"approvals": []})
        self.assertEqual((blocked.status_code, blocked.json()["code"]), (404, "not_found"))


class UiProtocolTest(unittest.TestCase):
    def test_event_reducer_deduplicates_persisted_id(self):
        if not shutil.which("node"):
            self.skipTest("Node.js is unavailable")
        script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');
const crypto = require('crypto');
const nodes = new Map();
function node() {
  return {
    children: [], style: {}, classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild(child) { this.children.push(child); return child; },
    replaceChildren() { this.children = []; }, querySelectorAll() { return []; },
  };
}
const context = {
  document: { getElementById(id) { if (!nodes.has(id)) nodes.set(id, node()); return nodes.get(id); },
              createElement: node, querySelectorAll() { return []; } },
  window: {}, localStorage: { getItem() { return null; }, setItem() {}, removeItem() {} },
  fetch: async (url) => url === '/api/messages/7'
    ? { ok: true, json: async () => ({ role: 'assistant', content: 'done' }) }
    : { ok: false, status: 500 },
  crypto: crypto.webcrypto, TextEncoder, console: { log() {} }, setTimeout() {},
};
vm.createContext(context);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8'), context);
vm.runInContext('activeRunId = "run-1"; lastEventIdByRun["run-1"] = 0;', context);
(async () => {
  const event = { event_id: 4, run_id: 'run-1', type: 'model.started', data: {}, ts: '' };
  await vm.runInContext('handleRunEvent', context)(event);
  await vm.runInContext('handleRunEvent', context)(event);
  assert.strictEqual(nodes.get('eventList').children.length, 1);
  const completed = { event_id: 5, run_id: 'run-1', type: 'message.completed', ts: '',
    data: { message_id: 7, length: 4,
      hash: crypto.createHash('sha256').update('done').digest('hex') } };
  await vm.runInContext('handleRunEvent', context)(completed);
  assert(nodes.get('chat').children[0].children[1].children[0].innerHTML.includes('done'));
})().catch((error) => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            ["node", "-e", script, str(ROOT / "static" / "app.js")],
            capture_output=True, text=True, timeout=10, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class ConflictResponseTest(unittest.TestCase):
    def test_chat_missing_project_uses_four_field_error(self):
        from backend import api

        response = asyncio.run(api.chat(api.ChatRequest(message="hello"), SimpleNamespace()))
        body = json.loads(response.body)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(body["code"], "invalid_request")
        self.assertEqual(set(body), {"code", "message", "details", "trace_id"})

    def test_http_provider_conflict_keeps_database_row_unchanged(self):
        from fastapi import FastAPI

        from backend import api, services
        from backend.config import AppConfig
        from backend.db import Database

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "provider.db")
            try:
                database.save_settings(
                    api_key_encrypted="old-ciphertext",
                    base_url="https://api.example/v1",
                    credential_base_url="https://api.example/v1",
                    model="old-model",
                )
                before = database.get_settings()
                application = FastAPI()
                application.include_router(api.router)
                application.dependency_overrides[api._require_local_session] = lambda: None
                config = AppConfig("", "https://api.example/v1", "model", 5, 10.0)
                with (
                    patch.object(services, "db", database),
                    patch.object(services, "load_config", return_value=config),
                    TestClient(application) as client,
                ):
                    response = client.put(
                        "/api/settings",
                        json={
                            "base_url": "https://other.example/v1",
                            "model": "must-not-save",
                        },
                    )

                self.assertEqual(response.status_code, 409)
                self.assertEqual(
                    response.json()["code"], "provider_key_reentry_required"
                )
                self.assertEqual(
                    set(response.json()),
                    {"code", "message", "details", "trace_id"},
                )
                self.assertEqual(database.get_settings(), before)
            finally:
                database.close()

    def test_provider_reentry_conflict_has_four_fields_and_no_key(self):
        from backend import api

        marker = "must-not-leak-provider-key"
        with patch.object(
            api,
            "save_public_settings",
            side_effect=api.ProviderKeyReentryRequired("blocked"),
        ):
            response = asyncio.run(
                api.update_settings(
                    api.SettingsRequest(
                        api_key=marker,
                        base_url="https://other.example/v1",
                    )
                )
            )

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(body["code"], "provider_key_reentry_required")
        self.assertEqual(set(body), {"code", "message", "details", "trace_id"})
        self.assertNotIn(marker, response.body.decode("utf-8"))

    def test_invalid_base_url_has_four_field_error(self):
        from backend import api

        with patch.object(
            api,
            "save_public_settings",
            side_effect=api.InvalidBaseUrl("Base URL 格式无效"),
        ):
            response = asyncio.run(
                api.update_settings(api.SettingsRequest(base_url="not-a-url"))
            )

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 422)
        self.assertEqual(body["code"], "invalid_base_url")
        self.assertEqual(set(body), {"code", "message", "details", "trace_id"})

    def test_default_project_delete_conflict_has_four_fields(self):
        from backend import api

        class FakeDatabase:
            def get_project(self, project_id):
                return {"id": project_id, "is_default": 1}

            def delete_project(self, project_id):
                raise ValueError("默认项目不能删除")

        with patch.object(api, "db", FakeDatabase()):
            response = asyncio.run(api.delete_project("default-project"))

        body = json.loads(response.body)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(body["code"], "default_project_protected")
        self.assertEqual(set(body), {"code", "message", "details", "trace_id"})


class RunBatContractTest(unittest.TestCase):
    def test_repository_venv_and_loopback_contract(self):
        content = (ROOT / "run.bat").read_text(encoding="utf-8").lower()

        self.assertIn('cd /d "%~dp0"', content)
        self.assertIn('%~dp0.venv\\scripts\\python.exe', content)
        self.assertIn('if not exist "%python%"', content)
        self.assertIn("if not defined port", content)
        self.assertIn("--host 127.0.0.1", content)
        self.assertIn("--workers 1", content)
        self.assertNotIn("anaconda", content)
        self.assertNotIn("pip install", content)


if __name__ == "__main__":
    unittest.main()
