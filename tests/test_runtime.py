"""P1 repository invariants; no model, MCP, or network calls."""

import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.db import (
    Database, EventLimitExceeded, IdempotencyKeyReused, RunAlreadyActive,
    MAX_EVENT_BYTES, MAX_RUN_EVENT_BYTES, TERMINAL_EVENT_RESERVE, _json,
)


class RunRepositoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "run.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)
        self.agent_id = self.db.create_session()["id"]

    def _run(self, task_id=None, key="key-1"):
        task_id = task_id or self.db.create_task(self.agent_id, "Do the thing")["id"]
        return self.db.create_run(task_id, key, config_snapshot={}, tool_snapshot={})

    def test_v3_migration_task_input_constraints_and_reopen_noop(self):
        task = self.db.create_task(self.agent_id, "first", ["done"], {"source": "test"})
        self.assertEqual(json.loads(task["context_json"]), {"source": "test"})
        rows = self.db._query_all("SELECT * FROM messages WHERE task_id=?", (task["id"],))
        self.assertEqual([(r["role"], r["run_id"], r["content"]) for r in rows],
                         [("user", None, "first")])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._execute("""
                INSERT INTO messages (session_id,task_id,role,sequence,created_at)
                VALUES (?,?,'assistant',99,'now')
            """, (self.agent_id, task["id"]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._execute("""
                INSERT INTO messages (session_id,task_id,role,sequence,created_at)
                VALUES (?,?,'user',99,'now')
            """, (self.agent_id, task["id"]))
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._execute("""
                INSERT INTO messages (session_id,task_id,role,sequence,created_at)
                VALUES (?,'missing','user',99,'now')
            """, (self.agent_id,))
        self.assertEqual(self.db._conn.execute("PRAGMA user_version").fetchone()[0], 3)
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(self.db._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.db._init_tables()
        self.assertEqual(len(self.db._query_all("SELECT * FROM messages")), 1)

    def test_queued_run_is_atomic_and_freezes_cutoff_once(self):
        first = self.db.create_task(self.agent_id, "first")
        self.db.add_message(self.agent_id, "assistant", "completed while queued")
        second = self.db.create_task(self.agent_id, "second")
        run, created = self._run(first["id"])
        self.assertTrue(created)
        self.assertEqual(run["context_max_sequence"], 3)
        self.assertEqual(run["attempt"], 1)
        self.assertEqual(run["status"], "queued")
        self.assertEqual(len(self.db.list_events(run["id"])), 1)
        self.assertEqual(self.db.list_events(run["id"])[0]["type"], "run.queued")
        replay, created = self._run(first["id"])
        self.assertFalse(created)
        self.assertEqual(replay, run)
        with self.assertRaises(IdempotencyKeyReused):
            self._run(second["id"])
        with self.assertRaises(RunAlreadyActive) as caught:
            self._run(second["id"], "key-2")
        self.assertEqual(caught.exception.existing_run_id, run["id"])
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM runs")["n"], 1)
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM messages")["n"], 3)

    def test_concurrent_different_keys_and_same_key_are_serialized(self):
        task = self.db.create_task(self.agent_id, "one")
        others = [Database(self.path) for _ in range(9)]
        self.addCleanup(lambda: [other.close() for other in others])
        databases = [self.db, *others]
        barrier = threading.Barrier(10)

        def start(index):
            barrier.wait()
            try:
                return databases[index].create_run(task["id"], f"key-{index}",
                                                   config_snapshot={}, tool_snapshot={})[0]["id"]
            except RunAlreadyActive:
                return None

        with ThreadPoolExecutor(max_workers=10) as pool:
            results = list(pool.map(start, range(10)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM runs")["n"], 1)
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM events")["n"], 1)

        winner_key = self.db._query_one("SELECT idempotency_key FROM runs")["idempotency_key"]
        barrier = threading.Barrier(10)

        def replay(index):
            barrier.wait()
            return databases[index].create_run(task["id"], winner_key,
                                               config_snapshot={"changed": True},
                                               tool_snapshot={"changed": True})[0]["id"]

        with ThreadPoolExecutor(max_workers=10) as pool:
            self.assertEqual(len(set(pool.map(replay, range(10)))), 1)
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM events")["n"], 1)

    def test_legacy_chat_combination_rolls_back_on_event_failure(self):
        self.db._execute("""
            CREATE TRIGGER fail_queued BEFORE INSERT ON events
            WHEN NEW.type='run.queued' BEGIN SELECT RAISE(ABORT,'injected'); END
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_legacy_chat_run(self.agent_id, "atomic", "legacy-key",
                                           config_snapshot={}, tool_snapshot={})
        for table in ("tasks", "runs", "messages", "events"):
            self.assertEqual(self.db._query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"], 0)
        self.db._execute("DROP TRIGGER fail_queued")
        task, run = self.db.create_legacy_chat_run(self.agent_id, "atomic", "legacy-key",
                                                    config_snapshot={}, tool_snapshot={})
        self.assertEqual(self.db.get_latest_run_id(task["id"]), run["id"])
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM messages")["n"], 1)
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM events")["n"], 1)

    def test_event_canonical_bytes_and_terminal_reserve(self):
        run, _ = self._run()
        with patch("backend.db._utc_now", return_value="2026-09-13T00:00:00.000Z"):
            with self.assertRaises(EventLimitExceeded):
                self.db.append_event(run["id"], "model.output.delta", {"text": "x" * MAX_EVENT_BYTES})
            self.assertEqual(len(self.db.list_events(run["id"])), 1)
            next_id = self.db.get_last_event_id(run["id"]) + 1
            envelope = {"v": 1, "event_id": next_id, "task_id": run["task_id"],
                        "run_id": run["id"], "agent_id": self.agent_id,
                        "type": "model.output.delta", "ts": "2026-09-13T00:00:00.000Z",
                        "data": {"text": "x"}}
            event_size = len(_json(envelope).encode())
            self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                             (MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE - event_size,
                              run["id"]))
            self.db.append_event(run["id"], "model.output.delta", {"text": "x"})
            before_delete = self.db.get_run(run["id"])["event_bytes_written"]
            self.db._execute("DELETE FROM events WHERE type='model.output.delta'")
            self.assertEqual(self.db.get_run(run["id"])["event_bytes_written"], before_delete)
            with self.assertRaises(EventLimitExceeded):
                self.db.append_event(run["id"], "model.output.delta", {"text": "x"})
            self.db.append_event(run["id"], "run.completed", {})
            self.assertGreater(self.db.get_run(run["id"])["event_bytes_written"], before_delete)


if __name__ == "__main__":
    unittest.main()
