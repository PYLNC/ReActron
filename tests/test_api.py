"""P0 API lifecycle, compatibility gate, and launcher contracts."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient


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
            TestClient(app_module.app) as client,
        ):
            health = client.get("/api/health")
            marker = "validation-secret-marker"
            with self.assertLogs("agent.app", level="WARNING") as logs:
                invalid = client.post("/api/chat", json={"message": {"value": marker}})

        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["status"], "ok")
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
