"""P1 repository invariants; no model, MCP, or network calls."""

import asyncio
import json
import hashlib
import sqlite3
import sys
import uuid
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import patch

from backend.db import (
    Database, EventLimitExceeded, IdempotencyKeyReused, InvalidRunTransition,
    ResourceNotFound, RunAlreadyActive,
    MAX_EVENT_BYTES, MAX_RUN_EVENT_BYTES, TERMINAL_EVENT_RESERVE, _json,
)
from backend.context import ContextTooLarge, build_context
from backend.runtime import TaskRunner
from backend.tool_gateway import ToolOutcome, ToolSpec, make_run_context


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

    def _next_event_size(self, run, event_type, payload, timestamp):
        return len(_json({
            "v": 1, "event_id": self.db.get_last_event_id(run["id"]) + 1,
            "task_id": run["task_id"], "run_id": run["id"],
            "agent_id": run["session_id"], "type": event_type,
            "ts": timestamp, "data": payload,
        }).encode("utf-8"))

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
        self.assertEqual(self.db._conn.execute("PRAGMA user_version").fetchone()[0], 4)
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

    def test_tool_preflight_exact_boundary_and_unknown_waiting(self):
        cap = MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE
        timestamp = "2026-09-13T00:00:00.000Z"
        payload = {"call_id": "call_boundary", "name": "danger",
                   "effect": "effectful", "action_hash": "a" * 64}
        for offset in (0, 1):
            agent_id = self.db.create_session()["id"]
            task = self.db.create_task(agent_id, "boundary")
            run, _ = self.db.create_run(task["id"], f"boundary-{offset}",
                                        config_snapshot={}, tool_snapshot={})
            active = self.db.claim_next_run(f"worker-{offset}")
            event_size = self._next_event_size(active, "tool.started", payload, timestamp)
            self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                             (cap - 4096 - event_size + offset, run["id"]))
            with patch("backend.db._utc_now", return_value=timestamp):
                if offset:
                    with self.assertRaises(EventLimitExceeded):
                        self.db.start_tool_call(run["id"], active["version"],
                                                f"worker-{offset}", active["lease_epoch"],
                                                "call_boundary", "danger", "effectful", "a" * 64)
                    self.assertFalse(any(e["type"] == "tool.started" for e in
                                         self.db.list_events(run["id"])))
                    self.db.transition_run(run["id"], active["version"], "failed",
                                           "run.failed", {"code": "event_limit_exceeded"},
                                           lease_owner=f"worker-{offset}",
                                           lease_epoch=active["lease_epoch"])
                    self.assertEqual(self.db.get_run(run["id"])["status"], "failed")
                else:
                    started = self.db.start_tool_call(
                        run["id"], active["version"], f"worker-{offset}",
                        active["lease_epoch"], "call_boundary", "danger",
                        "effectful", "a" * 64)
                    outcome = ToolOutcome("call_boundary", "outcome_unknown", "effectful",
                                          False, "未确认", None, "timeout_unknown_effect")
                    waiting, approval = self.db.record_unknown_outcome(
                        run["id"], started["version"], f"worker-{offset}",
                        started["lease_epoch"], outcome, "danger", "a" * 64,
                        "9999-01-01T00:00:00.000Z")
                    self.assertEqual((waiting["status"], approval["status"]),
                                     ("waiting", "pending"))
                    self.assertLessEqual(waiting["event_bytes_written"], cap)

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

    def test_unknown_event_obeys_nonterminal_cap_and_rolls_back_approval(self):
        cap = MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE
        timestamp = "2026-09-13T00:00:00.000Z"
        outcome = SimpleNamespace(call_id="call-1", status="outcome_unknown",
                                  effect="effectful", quiesced=False,
                                  model_text="not an event", result_ref=None,
                                  error_code="tool_timeout")
        action_hash = hashlib.sha256(b"action").hexdigest()
        for offset, expected_status in ((0, "waiting"), (1, "running")):
            agent_id = self.db.create_session()["id"]
            task = self.db.create_task(agent_id, "unknown")
            run, _ = self.db.create_run(task["id"], f"key-{offset}",
                                        config_snapshot={}, tool_snapshot={})
            active = self.db.claim_next_run(f"worker-{offset}")
            approval_id = str(uuid.UUID(int=offset + 1))
            payload = {"call_id": outcome.call_id, "name": "file.write",
                       "effect": outcome.effect, "error_code": outcome.error_code,
                       "approval_id": approval_id}
            size = self._next_event_size(run, "tool.outcome_unknown", payload, timestamp)
            before = cap - size + offset
            self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                             (before, run["id"]))
            with (patch("backend.db._utc_now", return_value=timestamp),
                  patch("backend.db.uuid.uuid4", return_value=uuid.UUID(int=offset + 1))):
                if offset:
                    with self.assertRaises(EventLimitExceeded):
                        self.db.record_unknown_outcome(
                            run["id"], active["version"], f"worker-{offset}",
                            active["lease_epoch"], outcome, "file.write",
                            action_hash, "9999-01-01T00:00:00.000Z")
                else:
                    self.db.record_unknown_outcome(
                        run["id"], active["version"], f"worker-{offset}",
                        active["lease_epoch"], outcome, "file.write",
                        action_hash, "9999-01-01T00:00:00.000Z")
            state = self.db.get_run(run["id"])
            self.assertEqual(state["status"], expected_status)
            self.assertEqual(state["event_bytes_written"], cap if not offset else before)
            self.assertEqual(len(self.db.list_approvals(run_id=run["id"])), 0 if offset else 1)
            self.assertEqual(len(self.db.list_events(run["id"])), 2 if offset else 3)

    def test_recovery_event_obeys_nonterminal_cap_and_rolls_back_status(self):
        cap = MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE
        timestamp = "2026-09-13T00:00:00.000Z"
        for offset, expected_status in ((0, "paused"), (1, "failed")):
            agent_id = self.db.create_session()["id"]
            task = self.db.create_task(agent_id, "recover")
            run, _ = self.db.create_run(task["id"], f"recovery-{offset}",
                                        config_snapshot={}, tool_snapshot={})
            self.db.claim_next_run(f"worker-{offset}")
            self.db._execute("UPDATE runs SET lease_until='2000-01-01T00:00:00.000Z' WHERE id=?",
                             (run["id"],))
            size = self._next_event_size(run, "run.paused", {"reason": "restart"}, timestamp)
            before = cap - size + offset
            self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                             (before, run["id"]))
            with patch("backend.db._utc_now", return_value=timestamp):
                self.assertEqual(self.db.recover_expired_leases(
                    "9999-01-01T00:00:00.000Z"), [run["id"]])
            state = self.db.get_run(run["id"])
            self.assertEqual(state["status"], expected_status)
            if offset:
                self.assertGreater(state["event_bytes_written"], before)
                self.assertLessEqual(state["event_bytes_written"], MAX_RUN_EVENT_BYTES)
            else:
                self.assertEqual(state["event_bytes_written"], cap)
            self.assertEqual(len(self.db.list_events(run["id"])), 3)

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


class RunnerCoreTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Database(Path(self.tmp.name) / "runner.db")
        self.addCleanup(self.db.close)
        self.agent_id = self.db.create_session()["id"]
        self.task = self.db.create_task(self.agent_id, "answer")
        self.run, _ = self.db.create_run(
            self.task["id"], "run-1",
            config_snapshot={"model": "fake", "max_rounds": 2}, tool_snapshot={})

    async def test_one_model_invocation_commits_message_and_terminal_event(self):
        calls = []

        def factory(key, url, model):
            calls.append((key, url, model))

            async def step(messages, **kwargs):
                self.assertEqual(sum(m["role"] == "user" for m in messages), 1)
                await kwargs["on_delta"]("partial")
                return {"content": "answer", "tool_calls": []}
            return step

        runner = TaskRunner(
            self.db, model_factory=factory,
            provider_config=lambda _: SimpleNamespace(api_key="fake-key", base_url="https://fake"),
            tool_context_factory=lambda run: make_run_context(run["id"], run["task_id"]))
        self.assertTrue(await runner.run_once())
        self.assertFalse(await runner.run_once())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.db.get_run(self.run["id"])["status"], "completed")
        events = self.db.list_events(self.run["id"])
        self.assertEqual([e["type"] for e in events],
                         ["run.queued", "run.started", "message.completed", "run.completed"])
        self.assertEqual(self.db._query_one("SELECT content FROM messages WHERE run_id=?",
                                             (self.run["id"],))["content"], "answer")
        self.assertGreater(self.db.get_run(self.run["id"])["event_bytes_written"], 0)

    async def test_p2_control_results_and_wakeups_survive_runner_restarts(self):
        self.db.request_control(self.run["id"], "cancel")
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "Worker")
        task = self.db.create_task(root["id"], "coordinate")
        parent, _ = self.db.create_run(task["id"], "parent-run")
        child_task_id = None
        observed_delegate_result = None

        def tool_call(call_id, name, arguments):
            return {"id": call_id, "type": "function", "function": {
                "name": name, "arguments": _json(arguments)}}

        def factory(*_):
            async def step(messages, *, run_id, round_index, **_kwargs):
                nonlocal child_task_id, observed_delegate_result
                run = self.db.get_run(run_id)
                if run["session_id"] == root["id"]:
                    if round_index == 1:
                        return {"content": None, "tool_calls": [tool_call(
                            "delegate-1", "delegate_task", {
                                "assignee_agent_id": worker["id"],
                                "goal": "answer child task", "acceptance": [],
                            })]}
                    if round_index == 2:
                        payloads = [json.loads(message["content"])
                                    for message in messages if message["role"] == "tool"]
                        observed_delegate_result = next(
                            payload for payload in payloads if "run_id" in payload)
                        child_task_id = observed_delegate_result["task_id"]
                    if self.db.get_task(child_task_id)["status"] == "open":
                        return {"content": None, "tool_calls": [tool_call(
                            f"wait-{round_index}", "wait_for_tasks",
                            {"task_ids": [child_task_id]})]}
                    self.assertTrue(any("child done" in (message.get("content") or "")
                                        for message in messages))
                    return {"content": "parent done", "tool_calls": []}
                if round_index == 1:
                    return {"content": None, "tool_calls": [tool_call(
                        "question-1", "send_agent_message", {
                            "to_agent_id": root["id"], "kind": "question",
                            "content": "need input",
                        })]}
                self.assertTrue(any(message.get("content") == "approved"
                                    for message in messages))
                result = {"summary": "child done", "acceptance_results": [],
                          "artifacts": [], "evidence": [], "followups": []}
                return {"content": None, "tool_calls": [tool_call(
                    "result-1", "send_agent_message", {
                        "to_agent_id": root["id"], "kind": "result",
                        "content": _json(result),
                    })]}
            return step

        def restarted_runner():
            return TaskRunner(
                self.db, model_factory=factory,
                provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"))

        await restarted_runner().run_once()
        waiting_parent = self.db.get_run(parent["id"])
        self.assertEqual((waiting_parent["status"], waiting_parent["lease_owner"]),
                         ("waiting", None))
        self.assertEqual(set(observed_delegate_result), {"task_id", "run_id", "status"})
        self.assertEqual(observed_delegate_result["status"], "queued")
        self.assertIsNotNone(observed_delegate_result["run_id"])
        await restarted_runner().run_once()
        question = self.db.list_agent_messages(root["id"])[0]
        self.assertEqual((question["kind"], question["needs_reply"]), ("question", True))
        waiting_child = self.db.get_run(observed_delegate_result["run_id"])
        self.assertEqual((waiting_child["status"], waiting_child["lease_owner"]),
                         ("waiting", None))
        self.db.host_reply(root["id"], question["id"], "approved")
        await restarted_runner().run_once()  # Parent observes the question, then waits again.
        waiting_parent = self.db.get_run(parent["id"])
        self.assertEqual((waiting_parent["status"], waiting_parent["lease_owner"]),
                         ("waiting", None))
        await restarted_runner().run_once()  # Child observes the reply and sends its result.
        self.assertEqual(self.db.get_task(child_task_id)["status"], "completed")
        self.assertEqual(self.db.get_run(parent["id"])["status"], "queued")
        await restarted_runner().run_once()
        self.assertEqual(self.db.get_run(parent["id"])["status"], "completed")
        self.assertNotEqual(observed_delegate_result, {"accepted": True})

    async def test_background_runner_bounds_parallel_workers_and_drains_queue(self):
        self.db.request_control(self.run["id"], "cancel")
        root = self.db.create_supervisor()
        first_worker = self.db.create_worker(root["id"], "First")
        second_worker = self.db.create_worker(root["id"], "Second")
        first, first_run = self.db.host_delegate_task(root["id"], first_worker["id"], "one")
        queued, queued_run = self.db.host_delegate_task(root["id"], first_worker["id"], "two")
        second, second_run = self.db.host_delegate_task(root["id"], second_worker["id"], "three")
        self.assertIsNotNone(first_run)
        self.assertIsNone(queued_run)
        self.assertIsNotNone(second_run)
        active = {}
        max_by_agent = {}
        max_parallel = 0
        two_entered, release = asyncio.Event(), asyncio.Event()

        def factory(*_):
            async def step(_messages, *, run_id, **_kwargs):
                nonlocal max_parallel
                agent_id = self.db.get_run(run_id)["session_id"]
                active[agent_id] = active.get(agent_id, 0) + 1
                max_by_agent[agent_id] = max(max_by_agent.get(agent_id, 0),
                                              active[agent_id])
                max_parallel = max(max_parallel, sum(active.values()))
                if len([count for count in active.values() if count]) >= 2:
                    two_entered.set()
                try:
                    await release.wait()
                finally:
                    active[agent_id] -= 1
                return {"content": "done", "tool_calls": []}
            return step

        runner = TaskRunner(
            self.db, model_factory=factory,
            provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"))
        await runner.start()
        try:
            await asyncio.wait_for(two_entered.wait(), 2)
            release.set()
            for _ in range(200):
                if all(self.db.get_task(item["id"])["status"] == "completed"
                       for item in (first, queued, second)):
                    break
                await asyncio.sleep(0.01)
        finally:
            release.set()
            await runner.close()
        queued_run = self.db.get_run(self.db.get_latest_run_id(queued["id"]))
        self.assertEqual(queued_run["status"], "completed")
        self.assertGreaterEqual(max_parallel, 2)
        self.assertEqual(max_by_agent[first_worker["id"]], 1)
        self.assertEqual(max_by_agent[second_worker["id"]], 1)

    async def test_provider_denial_never_constructs_model_client(self):
        calls = []

        def factory(*args):
            calls.append(args)
            raise AssertionError("client constructed")

        def deny(_):
            raise ValueError("provider_snapshot_mismatch")

        runner = TaskRunner(self.db, model_factory=factory, provider_config=deny,
                            tool_context_factory=lambda run: make_run_context(run["id"]))
        await runner.run_once()
        self.assertEqual(calls, [])
        run = self.db.get_run(self.run["id"])
        self.assertEqual((run["status"], json.loads(run["error_json"])["code"]),
                         ("failed", "provider_snapshot_mismatch"))

    async def test_default_provider_gate_uses_frozen_row_before_client(self):
        created = []

        def factory(*args):
            created.append(args)
            raise AssertionError("model client must not be constructed")

        runner = TaskRunner(self.db, model_factory=factory,
                            tool_context_factory=lambda run: make_run_context(run["id"]))
        live = SimpleNamespace(api_key="changed-key", base_url="https://changed",
                               model="changed-model")
        with patch("backend.security.verify_frozen_provider", create=True,
                   return_value=False) as verify, patch(
                       "backend.services.get_runtime_config", return_value=live):
            await runner.run_once()
        self.assertEqual(created, [])
        self.assertEqual(verify.call_args.args[0],
                         json.loads(self.run["config_snapshot_json"]))
        self.assertEqual(verify.call_args.kwargs["runtime_config"], live)
        self.assertEqual(verify.call_args.kwargs["settings"], self.db.get_settings())
        self.assertEqual(self.db.get_run(self.run["id"])["status"], "failed")

    async def test_model_exception_or_timeout_clears_delta_without_refunding_bytes(self):
        for index, failure in enumerate((RuntimeError("model broke"),
                                         asyncio.TimeoutError("model timeout"))):
            if index:
                task = self.db.create_task(self.agent_id, "second")
                run, _ = self.db.create_run(task["id"], "run-2",
                                            config_snapshot={"model": "fake", "max_rounds": 2},
                                            tool_snapshot={})
            else:
                run = self.run
            during = []

            def factory(*_):
                async def step(_messages, **kwargs):
                    await kwargs["on_delta"]("unfinished")
                    during.append(self.db.get_run(run["id"])["event_bytes_written"])
                    raise failure
                return step

            runner = TaskRunner(self.db, model_factory=factory,
                                provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                                tool_context_factory=lambda row: make_run_context(row["id"]))
            await runner.run_once()
            final = self.db.get_run(run["id"])
            self.assertEqual(final["status"], "failed")
            self.assertGreater(final["event_bytes_written"], during[0])
            events = self.db.list_events(run["id"])
            self.assertFalse(any(event["type"] == "model.output.delta" for event in events))
            self.assertEqual(sum(event["type"] == "run.failed" for event in events), 1)
            self.assertFalse(self.db._query_all("SELECT * FROM messages WHERE run_id=?",
                                                (run["id"],)))

    async def test_model_error_after_control_honors_intent_not_failed(self):
        for index, action in enumerate(("pause", "cancel")):
            if index:
                other_agent = self.db.create_session()["id"]
                task = self.db.create_task(other_agent, "cancel after model error")
                run, _ = self.db.create_run(task["id"], "run-cancel",
                                            config_snapshot={"model": "fake", "max_rounds": 2},
                                            tool_snapshot={})
            else:
                run = self.run
            entered, release = asyncio.Event(), asyncio.Event()

            def factory(*_):
                async def step(_messages, **kwargs):
                    await kwargs["on_delta"]("partial")
                    entered.set()
                    await release.wait()
                    raise RuntimeError("model failed after control intent")
                return step

            runner = TaskRunner(self.db, model_factory=factory,
                                provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                                tool_context_factory=lambda row: make_run_context(row["id"]))
            worker = asyncio.create_task(runner.run_once())
            await asyncio.wait_for(entered.wait(), 2)
            await runner.control(run["id"], action)
            release.set()
            await worker
            final = self.db.get_run(run["id"])
            self.assertEqual(final["status"], "paused" if action == "pause" else "cancelled")
            self.assertFalse(any(e["type"] == "model.output.delta" for e in
                                 self.db.list_events(run["id"])))
            self.assertFalse(any(e["type"] == "run.failed" for e in
                                 self.db.list_events(run["id"])))

    async def test_pause_resume_same_run_and_attempt_at_model_boundary(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []

        def factory(*_):
            async def step(messages, **kwargs):
                calls.append(messages)
                if len(calls) == 1:
                    await kwargs["on_delta"]("unfinished")
                    entered.set()
                    await release.wait()
                return {"content": "done", "tool_calls": []}
            return step

        runner = TaskRunner(self.db, model_factory=factory,
                            provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                            tool_context_factory=lambda run: make_run_context(run["id"]))
        work = asyncio.create_task(runner.run_once())
        await asyncio.wait_for(entered.wait(), 2)
        requested = await runner.control(self.run["id"], "pause")
        self.assertEqual(requested["control_requested"], "pause")
        release.set()
        await work
        paused = self.db.get_run(self.run["id"])
        self.assertEqual((paused["status"], paused["attempt"]), ("paused", 1))
        self.assertFalse(any(event["type"] == "model.output.delta" for event in
                             self.db.list_events(self.run["id"])))
        resumed = await runner.control(self.run["id"], "resume")
        self.assertEqual((resumed["status"], resumed["id"], resumed["attempt"]),
                         ("queued", self.run["id"], 1))
        await runner.run_once()
        self.assertEqual(self.db.get_run(self.run["id"])["status"], "completed")
        self.assertEqual(len(calls), 2)

    async def test_unknown_outcome_and_resolved_retry_never_reinvoke(self):
        calls = []
        tool_call = {"id": "call_safe_1", "type": "function",
                     "function": {"name": "danger", "arguments": "{}"}}
        spec = ToolSpec(name="danger", schema={"type": "object"},
                        handler=lambda: None, effects=frozenset({"filesystem.write"}))

        def factory(*_):
            async def step(*_args, **_kwargs):
                return {"content": None, "tool_calls": [tool_call]}
            return step

        async def executor(ctx, name, args):
            calls.append((ctx.call_id, name, args))
            return ToolOutcome(ctx.call_id, "outcome_unknown", "effectful", False,
                               "未确认", None, "timeout_unknown_effect")

        runner = TaskRunner(self.db, model_factory=factory, tool_executor=executor,
                            provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                            tool_context_factory=lambda run: replace(
                                make_run_context(run["id"]), tool_specs={"danger": spec}))
        await runner.run_once()
        waiting = self.db.get_run(self.run["id"])
        self.assertEqual((waiting["status"], waiting["wait_reason"]), ("waiting", "approval"))
        approvals = self.db.list_approvals(self.run["id"])
        self.assertEqual(len(approvals), 1)
        self.assertEqual(len(calls), 1)
        self.db.decide_approval(approvals[0]["id"], "explicit_retry")
        await runner.approval_decided(approvals[0]["id"])
        self.assertFalse(await runner.run_once())
        self.assertEqual((self.db.get_run(self.run["id"])["status"], len(calls)),
                         ("waiting", 1))

    async def test_cancel_waits_for_inflight_tool_outcome_then_stops(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        tool_call = {"id": "call_cancel_1", "type": "function",
                     "function": {"name": "clock", "arguments": "{}"}}

        def factory(*_):
            async def step(*_args, **_kwargs):
                return {"content": None, "tool_calls": [tool_call]}
            return step

        async def executor(ctx, _name, _args):
            calls.append(ctx.call_id)
            entered.set()
            await release.wait()
            return ToolOutcome(ctx.call_id, "ok", "read_only", True,
                               "safe", None, None)

        runner = TaskRunner(self.db, model_factory=factory, tool_executor=executor,
                            provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                            tool_context_factory=lambda run: make_run_context(run["id"]))
        work = asyncio.create_task(runner.run_once())
        await asyncio.wait_for(entered.wait(), 2)
        request = await runner.control(self.run["id"], "cancel")
        self.assertEqual((request["status"], request["control_requested"]),
                         ("running", "cancel"))
        release.set()
        await work
        final = self.db.get_run(self.run["id"])
        self.assertEqual(final["status"], "cancelled")
        self.assertEqual(calls, ["call_cancel_1"])
        self.assertEqual(len([event for event in self.db.list_events(final["id"])
                              if event["type"] == "run.cancelled"]), 1)

    async def test_effectful_executor_exception_becomes_unknown_not_failed(self):
        call = {"id": "call_raise_1", "type": "function",
                "function": {"name": "danger", "arguments": "{}"}}
        spec = ToolSpec(name="danger", schema={"type": "object"},
                        handler=lambda: None, effects=frozenset({"filesystem.write"}))
        invoked = []

        def factory(*_):
            async def step(*_args, **_kwargs):
                return {"content": None, "tool_calls": [call]}
            return step

        async def executor(*_):
            invoked.append(1)
            raise RuntimeError("possibly after side effect")

        runner = TaskRunner(self.db, model_factory=factory, tool_executor=executor,
                            provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                            tool_context_factory=lambda run: replace(
                                make_run_context(run["id"]), tool_specs={"danger": spec}))
        await runner.run_once()
        self.assertEqual(invoked, [1])
        self.assertEqual(self.db.get_run(self.run["id"])["status"], "waiting")
        self.assertEqual(self.db.list_approvals(self.run["id"])[0]["kind"],
                         "resolve_unknown_outcome")

    async def test_unrecordable_effectful_exception_never_false_fails_or_reinvokes(self):
        call = {"id": "call_hold_1", "type": "function",
                "function": {"name": "danger", "arguments": "{}"}}
        spec = ToolSpec(name="danger", schema={"type": "object"},
                        handler=lambda: None, effects=frozenset({"filesystem.write"}))
        invoked = []

        def factory(*_):
            async def step(*_args, **_kwargs):
                return {"content": None, "tool_calls": [call]}
            return step

        async def executor(*_):
            invoked.append(1)
            raise RuntimeError("possibly after side effect")

        runner = TaskRunner(self.db, model_factory=factory, tool_executor=executor,
                            provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                            tool_context_factory=lambda run: replace(
                                make_run_context(run["id"]), tool_specs={"danger": spec}))
        with patch.object(self.db, "record_unknown_outcome",
                          side_effect=EventLimitExceeded("injected persistence failure")):
            await runner.run_once()
        self.assertEqual(invoked, [1])
        self.assertEqual(self.db.get_run(self.run["id"])["status"], "running")
        self.assertFalse(self.db.list_approvals(self.run["id"]))
        self.db._execute("UPDATE runs SET lease_until='2000-01-01T00:00:00.000Z',"
                         "event_bytes_written=? WHERE id=?",
                         (MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE, self.run["id"]))
        self.assertEqual(self.db.recover_expired_leases(), [])
        self.assertEqual(self.db.get_run(self.run["id"])["status"], "running")
        self.assertFalse(await runner.run_once())
        self.assertEqual(invoked, [1])

    async def test_preinvocation_event_budget_blocks_tool_and_keeps_terminal_reserve(self):
        calls = []
        tool_call = {"id": "call_safe_2", "type": "function",
                     "function": {"name": "danger", "arguments": "{}"}}

        def factory(*_):
            async def step(*_args, **_kwargs):
                return {"content": None, "tool_calls": [tool_call]}
            return step

        async def executor(*_):
            calls.append(1)
            raise AssertionError("tool must not execute")

        runner = TaskRunner(self.db, model_factory=factory, tool_executor=executor,
                            provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                            tool_context_factory=lambda run: make_run_context(run["id"]))
        active = self.db.claim_next_run(runner._owner, lease_seconds=120)
        self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                         (MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE - 4300,
                          self.run["id"]))
        await runner._process(active)
        final = self.db.get_run(self.run["id"])
        self.assertEqual((final["status"], json.loads(final["error_json"])["code"]),
                         ("failed", "event_limit_exceeded"))
        self.assertEqual(calls, [])
        self.assertFalse(any(e["type"] == "tool.started" for e in
                             self.db.list_events(self.run["id"])))
        self.assertGreater(final["event_bytes_written"],
                           MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE - 4300)

    async def test_disconnected_waiter_does_not_cancel_run(self):
        entered, release = asyncio.Event(), asyncio.Event()

        def factory(*_):
            async def step(*_args, **_kwargs):
                entered.set()
                await release.wait()
                return {"content": "done", "tool_calls": []}
            return step

        runner = TaskRunner(self.db, model_factory=factory,
                            provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                            tool_context_factory=lambda run: make_run_context(run["id"]))
        worker = asyncio.create_task(runner.run_once())
        await asyncio.wait_for(entered.wait(), 2)
        after_id = self.db.get_last_event_id(self.run["id"])
        waiter = asyncio.create_task(runner.wait_for_change(self.run["id"], after_id, 5))
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        release.set()
        await worker
        self.assertEqual(self.db.get_run(self.run["id"])["status"], "completed")

    async def test_restart_after_tool_started_requires_unknown_approval_not_reinvoke(self):
        call = {"id": "call_restart_1", "type": "function",
                "function": {"name": "danger", "arguments": "{}"}}
        active = self.db.claim_next_run("old-worker", lease_seconds=120)
        checkpoint = {"v": 1, "phase": "tool_pending", "round": 1,
                      "assistant_message_id": None, "next_tool_index": 0}
        active, message_id = self.db.transition_run(
            active["id"], active["version"], "running", "model.completed",
            {"round": 1, "tool_count": 1}, checkpoint=checkpoint,
            message={"role": "assistant", "content": None, "tool_calls": [call]},
            lease_owner="old-worker", lease_epoch=active["lease_epoch"])
        self.assertEqual(json.loads(active["checkpoint_json"])["assistant_message_id"], message_id)
        active = self.db.start_tool_call(active["id"], active["version"],
                                         "old-worker", active["lease_epoch"],
                                         "call_restart_1", "danger", "effectful", "a" * 64)
        self.db._execute("UPDATE runs SET lease_until='2000-01-01T00:00:00.000Z' WHERE id=?",
                         (active["id"],))
        self.assertEqual(self.db.recover_expired_leases(), [active["id"]])
        self.db.request_control(active["id"], "resume")
        spec = ToolSpec(name="danger", schema={"type": "object"},
                        handler=lambda: None, effects=frozenset({"filesystem.write"}))
        invoked = []

        async def executor(*_):
            invoked.append(1)
            raise AssertionError("recovered effectful call must not execute")

        runner = TaskRunner(self.db, tool_executor=executor,
                            provider_config=lambda _: SimpleNamespace(api_key="k", base_url="u"),
                            tool_context_factory=lambda run: replace(
                                make_run_context(run["id"]), tool_specs={"danger": spec}))
        await runner.run_once()
        self.assertEqual(invoked, [])
        waiting = self.db.get_run(active["id"])
        self.assertEqual((waiting["status"], waiting["attempt"]), ("waiting", 1))
        self.assertEqual(len(self.db.list_approvals(active["id"])), 1)
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    async def test_recovery_at_nonterminal_cap_uses_only_terminal_reserve(self):
        active = self.db.claim_next_run("old-worker")
        self.db._execute("UPDATE runs SET lease_until='2000-01-01T00:00:00.000Z',"
                         "event_bytes_written=? WHERE id=?",
                         (MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE, active["id"]))
        self.assertEqual(self.db.recover_expired_leases(), [active["id"]])
        final = self.db.get_run(active["id"])
        self.assertEqual((final["status"], self.db.get_task(self.task["id"])["status"]),
                         ("failed", "failed"))
        self.assertGreater(final["event_bytes_written"],
                           MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE)
        self.assertLessEqual(final["event_bytes_written"], MAX_RUN_EVENT_BYTES)
        self.assertEqual(self.db.list_events(active["id"])[-1]["type"], "run.failed")

    async def test_claim_at_nonterminal_cap_fails_without_model_invocation(self):
        self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                         (MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE, self.run["id"]))
        self.assertIsNone(self.db.claim_next_run("worker"))
        final = self.db.get_run(self.run["id"])
        self.assertEqual(final["status"], "failed")
        self.assertEqual(json.loads(final["error_json"])["code"], "event_limit_exceeded")
        self.assertEqual(self.db.list_events(final["id"])[-1]["type"], "run.failed")


if __name__ == "__main__":
    unittest.main()
