"""P1 repository invariants; no model, MCP, or network calls."""

import json
import hashlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from backend.db import (
    Database, EventLimitExceeded, IdempotencyKeyReused, InvalidRunTransition,
    ResourceNotFound, RunAlreadyActive,
    MAX_EVENT_BYTES, MAX_RUN_EVENT_BYTES, TERMINAL_EVENT_RESERVE, _json,
)
from backend.context import ContextTooLarge, build_context


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
            self.db.transition_run(run["id"], 0, "cancelled", "run.cancelled", {})
            self.assertGreater(self.db.get_run(run["id"])["event_bytes_written"], before_delete)

    def test_claim_cas_lease_and_recovery_are_single_writer(self):
        run, _ = self._run()
        second = Database(self.path)
        self.addCleanup(second.close)
        barrier = threading.Barrier(2)

        def claim(database, owner):
            barrier.wait()
            return database.claim_next_run(owner)

        with ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(claim, self.db, "worker-a")
            right = pool.submit(claim, second, "worker-b")
            claimed = [left.result(), right.result()]
        self.assertEqual(sum(item is not None for item in claimed), 1)
        active = next(item for item in claimed if item)
        self.assertEqual(active["id"], run["id"])
        self.assertEqual((active["status"], active["version"], active["lease_epoch"]),
                         ("running", 1, 1))
        self.assertEqual([event["type"] for event in self.db.list_events(run["id"])],
                         ["run.queued", "run.started"])
        with self.assertRaises(InvalidRunTransition):
            self.db.heartbeat_run(run["id"], "wrong-owner", 1, 1)
        with self.assertRaises(InvalidRunTransition):
            self.db.heartbeat_run(run["id"], active["lease_owner"], 0, 1)
        renewed = self.db.heartbeat_run(run["id"], active["lease_owner"], 1, 1)
        self.assertEqual(renewed["version"], 2)
        self.db._execute("UPDATE runs SET lease_until='2000-01-01T00:00:00.000Z' WHERE id=?",
                         (run["id"],))
        with self.assertRaises(InvalidRunTransition):
            self.db.heartbeat_run(run["id"], active["lease_owner"], 1, 2)
        with self.assertRaises(InvalidRunTransition):
            self.db.transition_run(run["id"], 2, "completed", "run.completed", {},
                                   lease_owner=active["lease_owner"], lease_epoch=1,
                                   message={"role": "assistant", "content": "stale"})
        self.assertEqual(self.db.recover_expired_leases("1900-01-01T00:00:00.000Z"), [])
        self.assertEqual(self.db.recover_expired_leases("9999-01-01T00:00:00.000Z"),
                         [run["id"]])
        paused = self.db.get_run(run["id"])
        self.assertEqual((paused["status"], paused["wait_reason"]), ("paused", "restart"))
        self.assertIsNone(paused["lease_owner"])
        self.assertEqual(paused["lease_epoch"], 2)
        self.assertIsNone(self.db.claim_next_run("other"))
        self.assertEqual(self.db.recover_expired_leases("9999-01-01T00:00:00.000Z"), [])
        self.assertEqual([event["type"] for event in self.db.list_events(run["id"])],
                         ["run.queued", "run.started", "run.paused"])

    def test_terminal_transition_message_and_event_are_one_transaction(self):
        task = self.db.create_task(self.agent_id, "finish")
        run, _ = self._run(task["id"])
        active = self.db.claim_next_run("worker")
        self.db._execute("""
            CREATE TRIGGER fail_terminal BEFORE INSERT ON events
            WHEN NEW.type='run.completed' BEGIN SELECT RAISE(ABORT,'injected'); END
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.transition_run(run["id"], active["version"], "completed",
                                   "run.completed", {},
                                   lease_owner="worker", lease_epoch=1,
                                   message={"role": "assistant", "content": "final"})
        self.assertEqual(self.db.get_run(run["id"])["status"], "running")
        self.assertEqual(self.db.get_task(task["id"])["status"], "open")
        self.assertEqual(self.db._query_one(
            "SELECT COUNT(*) AS n FROM messages WHERE run_id=?", (run["id"],))["n"], 0)
        self.db._execute("DROP TRIGGER fail_terminal")
        completed, message_id = self.db.transition_run(
            run["id"], active["version"], "completed", "run.completed", {},
            lease_owner="worker", lease_epoch=1,
            message={"role": "assistant", "content": "final"},
        )
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(self.db.get_task(task["id"])["status"], "completed")
        self.assertEqual(self.db.get_message(message_id)["content"], "final")
        events = self.db.list_events(run["id"])
        self.assertEqual([event["type"] for event in events],
                         ["run.queued", "run.started", "message.completed", "run.completed"])
        payload = json.loads(events[-2]["payload_json"])
        self.assertEqual(payload["message_id"], message_id)
        self.assertEqual(payload["hash"], hashlib.sha256(b"final").hexdigest())
        with self.assertRaises(InvalidRunTransition):
            self.db.transition_run(run["id"], completed["version"], "running", "run.started")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._execute("UPDATE tasks SET status='open',finished_at=NULL WHERE id=?", (task["id"],))

    def test_unknown_outcome_waits_for_independent_decision_without_retry(self):
        run, _ = self._run()
        active = self.db.claim_next_run("worker")
        invocation_count = 1  # The gateway has already returned this one outcome.
        outcome = SimpleNamespace(call_id="call-1", status="outcome_unknown",
                                  effect="effectful", quiesced=False,
                                  model_text="secret raw result must not be an event",
                                  result_ref=None, error_code="tool_timeout")
        waiting, approval = self.db.record_unknown_outcome(
            run["id"], active["version"], "worker", active["lease_epoch"],
            outcome, "file.write", hashlib.sha256(b"action").hexdigest(),
            "9999-01-01T00:00:00.000Z",
        )
        self.assertEqual((waiting["status"], waiting["wait_reason"]),
                         ("waiting", "approval"))
        self.assertEqual(approval["kind"], "resolve_unknown_outcome")
        self.assertEqual(invocation_count, 1)
        self.assertIsNone(self.db.claim_next_run("worker"))
        with self.assertRaises(InvalidRunTransition):
            self.db.record_unknown_outcome(run["id"], active["version"], "worker", 1,
                                           outcome, "file.write",
                                           hashlib.sha256(b"action").hexdigest(),
                                           "9999-01-01T00:00:00.000Z")
        decision = self.db.decide_approval(approval["id"], "explicit_retry")
        self.assertEqual(json.loads(decision["decision_json"])["decision"],
                         "explicit_retry")
        self.assertEqual(self.db.get_run(run["id"])["status"], "waiting")
        self.assertEqual(invocation_count, 1)
        self.assertNotIn(outcome.model_text,
                         "".join(event["payload_json"] for event in self.db.list_events(run["id"])))
        with self.assertRaises(ValueError):
            self.db.decide_approval(approval["id"], "approve")
        with self.assertRaises(InvalidRunTransition):
            self.db.decide_approval(approval["id"], "cancel")

    def test_approval_kind_decisions_and_idempotency(self):
        run, _ = self._run()
        approval = self.db.create_approval(run["id"], "call-2", "pre_execution",
                                           "hash", {"name": "safe"},
                                           "9999-01-01T00:00:00.000Z")
        self.assertEqual(self.db.create_approval(
            run["id"], "call-2", "pre_execution", "hash", {"name": "safe"},
            "9999-01-01T00:00:00.000Z")["id"], approval["id"])
        with self.assertRaises(ValueError):
            self.db.decide_approval(approval["id"], "occurred")
        decided = self.db.decide_approval(approval["id"], "deny")
        self.assertEqual(decided["status"], "resolved")
        self.assertEqual(self.db.decide_approval(approval["id"], "deny")["id"],
                         approval["id"])
        self.assertEqual(len(self.db.list_approvals(status="resolved")), 1)

    def test_context_cutoff_task_isolation_groups_and_limits(self):
        first = self.db.create_task(self.agent_id, "first")
        second = self.db.create_task(self.agent_id, "second")
        self.db.add_message(self.agent_id, "assistant", "completed while queued")
        self.db.add_message(self.agent_id, "assistant", None,
                            tool_calls=[{"id": "call-h", "type": "function",
                                         "function": {"name": "safe", "arguments": "{}"}}])
        self.db.add_message(self.agent_id, "tool", "tool result", tool_call_id="call-h")
        run, _ = self._run(first["id"])
        self.db.add_message(self.agent_id, "assistant", "too late")
        context = build_context(self.db, run["id"], "policy")
        self.assertEqual(sum(message["role"] == "user" for message in context), 1)
        self.assertEqual(context[-1]["content"], "first")
        self.assertIn("completed while queued", [message["content"] for message in context])
        self.assertIn("tool result", [message["content"] for message in context])
        self.assertNotIn("second", [message["content"] for message in context])
        self.assertNotIn("too late", [message["content"] for message in context])
        self.assertEqual(self.db.get_latest_run_id(second["id"]), None)
        bounded = build_context(self.db, run["id"], "policy", max_messages=3)
        self.assertLessEqual(len(bounded), 3)
        self.assertLessEqual(len(_json(bounded)) + len(run["tool_snapshot_json"])
                             + len(run["config_snapshot_json"]), 64000)
        grouped = build_context(self.db, run["id"], "policy", max_messages=4)
        self.assertNotIn("tool result", [message["content"] for message in grouped])
        self.assertFalse(any(message.get("tool_calls") for message in grouped))
        with self.assertRaises(ContextTooLarge):
            build_context(self.db, run["id"], "x" * 64000)

    def test_context_never_exceeds_128_messages_or_64000_chars(self):
        task = self.db.create_task(self.agent_id, "bounded")
        for index in range(140):
            self.db.add_message(self.agent_id, "assistant", f"history-{index}:" + "x" * 350)
        run, _ = self._run(task["id"])
        context = build_context(self.db, run["id"], "policy")
        self.assertLessEqual(len(context), 128)
        self.assertLessEqual(len(_json(context)) + len(run["config_snapshot_json"])
                             + len(run["tool_snapshot_json"]), 64000)
        self.assertEqual(context[-1]["content"], "bounded")
        self.assertIn("history-139", context[-2]["content"])

    def test_queued_control_only_and_snapshots_never_store_provider_key(self):
        self.db.save_settings(api_key_encrypted="secret-provider-marker",
                              base_url="https://api.example/v1", model="before")
        task = self.db.create_task(self.agent_id, "snapshot")
        fake_gateway = ModuleType("backend.tool_gateway")
        fake_gateway.snapshot_builtin_tools = lambda: {}
        with patch.dict(sys.modules, {"backend.tool_gateway": fake_gateway}):
            run, _ = self.db.create_run(task["id"], "snapshot-key")
        self.assertNotIn("secret-provider-marker", run["config_snapshot_json"])
        frozen_config, frozen_tools = run["config_snapshot_json"], run["tool_snapshot_json"]
        self.db.save_settings(api_key_encrypted="changed-provider-marker",
                              base_url="https://api.example/v2", model="after")
        replay, created = self.db.create_run(task["id"], "snapshot-key")
        self.assertFalse(created)
        self.assertEqual((replay["config_snapshot_json"], replay["tool_snapshot_json"]),
                         (frozen_config, frozen_tools))
        with self.assertRaises(InvalidRunTransition):
            self.db.request_control(run["id"], "pause")
        cancelled = self.db.request_control(run["id"], "cancel")
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.db.get_task(task["id"])["status"], "cancelled")

    def test_explicit_retry_increments_attempt_and_completed_never_reopens(self):
        task = self.db.create_task(self.agent_id, "retryable")
        first, _ = self._run(task["id"])
        self.db.request_control(first["id"], "cancel")
        with self.assertRaises(InvalidRunTransition):
            self._run(task["id"], "next")
        second, created = self.db.create_run(
            task["id"], "retry-key", {"retry": True},
            config_snapshot={}, tool_snapshot={},
        )
        self.assertTrue(created)
        self.assertEqual((first["attempt"], second["attempt"]), (1, 2))
        self.assertEqual(self.db.get_task(task["id"])["status"], "open")
        self.db.request_control(second["id"], "cancel")
        self.assertEqual(self.db.get_run(first["id"])["attempt"], 1)

    def test_archived_agent_rejects_all_new_writes_without_orphans(self):
        task = self.db.create_task(self.agent_id, "historical")
        run, _ = self._run(task["id"])
        with self.assertRaises(RunAlreadyActive):
            self.db.delete_session(self.agent_id)
        self.db.request_control(run["id"], "cancel")
        self.db.delete_session(self.agent_id)
        self.assertIsNotNone(self.db.get_session(self.agent_id)["archived_at"])
        before = {
            table: self.db._query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in ("tasks", "runs", "messages", "events")
        }
        with self.assertRaises(ResourceNotFound):
            self.db.create_task(self.agent_id, "blocked")
        with self.assertRaises(ResourceNotFound):
            self.db.create_run(task["id"], "new-key", config_snapshot={},
                               tool_snapshot={})
        with self.assertRaises(ResourceNotFound):
            self.db.add_message(self.agent_id, "user", "blocked")
        with self.assertRaises(ResourceNotFound):
            self.db.create_legacy_chat_run(self.agent_id, "blocked", "legacy-key",
                                           config_snapshot={}, tool_snapshot={})
        self.assertEqual(before, {
            table: self.db._query_one(f"SELECT COUNT(*) AS n FROM {table}")["n"]
            for table in before
        })
        old, created = self.db.create_run(task["id"], "key-1", config_snapshot={},
                                          tool_snapshot={})
        self.assertFalse(created)  # Idempotent historical read is not a new write.
        self.assertEqual(old["id"], run["id"])
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
