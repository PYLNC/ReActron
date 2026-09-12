"""P0 API lifecycle, compatibility gate, and launcher contracts."""

import asyncio
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from zipfile import ZipFile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException, UploadFile
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException


ROOT = Path(__file__).resolve().parent.parent


class _FakeChatDatabase:
    def __init__(self):
        self.messages = []
        self.get_messages_calls = 0
        self._lock = threading.Lock()

    def get_session(self, session_id):
        if session_id != "session-1":
            return None
        return {"id": session_id, "title": "existing"}

    def add_message(self, session_id, role, content, *unused):
        with self._lock:
            self.messages.append((session_id, role, content))

    def get_messages(self, session_id):
        with self._lock:
            self.get_messages_calls += 1
            return [
                {"role": role, "content": content}
                for current_id, role, content in self.messages
                if current_id == session_id
            ]

    def rename_session(self, session_id, title):
        return self.get_session(session_id)


class _BlockingAgent:
    started = threading.Event()
    release = threading.Event()
    calls = 0

    def __init__(self, **unused):
        type(self).calls += 1

    async def run(self, history):
        type(self).started.set()
        released = await asyncio.to_thread(type(self).release.wait, 5)
        if not released:
            raise TimeoutError("test barrier timed out")
        yield {"type": "done"}


class _FailingAgent:
    async def run(self, history):
        raise RuntimeError("fake model failure")
        yield

    def __init__(self, **unused):
        pass


class _CompletingAgent:
    def __init__(self, **unused):
        pass

    async def run(self, history):
        yield {"type": "done"}


async def _request_and_consume(api_module, message):
    response = await api_module.chat(
        api_module.ChatRequest(session_id="session-1", message=message)
    )
    chunks = []
    if hasattr(response, "body_iterator"):
        async for chunk in response.body_iterator:
            chunks.append(chunk)
    return response, chunks


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

        class FakeDatabase:
            closed = False

            def close(self):
                self.closed = True

        class FakeManager:
            refreshed = False

            async def refresh_registry(self):
                self.refreshed = True
                return {"server_count": 0, "tool_count": 0, "errors": []}

        fake_db = FakeDatabase()
        fake_manager = FakeManager()
        with (
            patch.object(db_module, "db", fake_db),
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
        self.assertTrue(fake_db.closed)

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
        self.assertEqual(payload.context, {})
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
    def test_new_server_stays_disabled_even_when_request_enables_it(self):
        from backend import api
        from backend.db import Database

        with tempfile.TemporaryDirectory() as temp_dir:
            database = Database(Path(temp_dir) / "mcp.db")
            try:
                with patch.object(api, "db", database):
                    response = asyncio.run(api.create_mcp_server(
                        api.MCPServerCreateRequest(
                            name="new", transport="stdio", command="C:\\tools\\fake.exe",
                            enabled=True,
                        )
                    ))
                stored = database.get_mcp_server(response["id"])
                self.assertEqual(response["enabled"], 0)
                self.assertEqual(stored["enabled"], 0)
            finally:
                database.close()


class ChatContextWiringTest(unittest.TestCase):
    def test_context_factory_receives_server_session_id_only(self):
        from backend import api

        make_context = Mock(return_value=object())
        with patch.dict(sys.modules, {"backend.tool_gateway": SimpleNamespace(
            make_builtin_run_context=make_context,
        )}):
            context = api._make_chat_context("session-1")
        self.assertIs(context, make_context.return_value)
        make_context.assert_called_once_with(run_id="session-1")


class ChatGateTest(unittest.TestCase):
    def setUp(self):
        from backend import api

        self.api = api
        self.db = _FakeChatDatabase()
        self.config = SimpleNamespace(
            api_key="test-key",
            base_url="https://example.invalid/v1",
            model="fake",
            max_rounds=1,
            tool_timeout=1.0,
        )
        with api._active_session_lock:
            api._active_session_ids.clear()

    def tearDown(self):
        with self.api._active_session_lock:
            self.api._active_session_ids.clear()

    def test_concurrent_request_is_rejected_before_message_write(self):
        _BlockingAgent.started = threading.Event()
        _BlockingAgent.release = threading.Event()
        _BlockingAgent.calls = 0
        outcome = {}

        def run_first_request():
            try:
                outcome["result"] = asyncio.run(
                    _request_and_consume(self.api, "first")
                )
            except BaseException as exc:  # surfaced in the owning test thread
                outcome["error"] = exc

        with (
            patch.object(self.api, "db", self.db),
            patch.object(self.api, "Agent", _BlockingAgent),
            patch.object(self.api, "_make_chat_context", return_value=object()),
            patch.object(self.api, "get_runtime_config", return_value=self.config),
            patch.object(self.api, "build_system_prompt", return_value=""),
        ):
            first = threading.Thread(target=run_first_request)
            first.start()
            self.assertTrue(_BlockingAgent.started.wait(5), "first request did not start")

            conflict = asyncio.run(
                self.api.chat(
                    self.api.ChatRequest(session_id="session-1", message="second")
                )
            )
            body = json.loads(conflict.body)

            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(body["code"], "run_already_active")
            self.assertEqual(
                set(body), {"code", "message", "details", "trace_id"}
            )
            self.assertEqual(self.db.messages, [("session-1", "user", "first")])
            self.assertEqual(self.db.get_messages_calls, 1)
            self.assertEqual(_BlockingAgent.calls, 1)

            _BlockingAgent.release.set()
            first.join(5)
            self.assertFalse(first.is_alive(), "first request did not finish")
            if "error" in outcome:
                raise outcome["error"]

            completed, _ = asyncio.run(
                _request_and_consume(self.api, "after-release")
            )

        self.assertEqual(completed.status_code, 200)
        self.assertEqual(len(self.db.messages), 2)
        self.assertEqual(_BlockingAgent.calls, 2)

    def test_generator_failure_releases_gate(self):
        with (
            patch.object(self.api, "db", self.db),
            patch.object(self.api, "_make_chat_context", return_value=object()),
            patch.object(self.api, "get_runtime_config", return_value=self.config),
            patch.object(self.api, "build_system_prompt", return_value=""),
        ):
            with patch.object(self.api, "Agent", _FailingAgent):
                _, chunks = asyncio.run(
                    _request_and_consume(self.api, "will-fail")
                )
            with self.api._active_session_lock:
                self.assertNotIn("session-1", self.api._active_session_ids)
            with patch.object(self.api, "Agent", _CompletingAgent):
                response, _ = asyncio.run(
                    _request_and_consume(self.api, "after-failure")
                )

        self.assertTrue(any('"type": "error"' in chunk for chunk in chunks))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.db.messages), 2)


class ConflictResponseTest(unittest.TestCase):
    def test_chat_missing_project_uses_four_field_error(self):
        from backend import api

        response = asyncio.run(api.chat(api.ChatRequest(message="hello")))
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
