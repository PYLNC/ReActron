"""P0 SQLite 迁移与并发一致性测试。"""

import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from backend.db import (AgentHierarchyError, Database, EventLimitExceeded,
                        InvalidRunTransition, ResourceNotFound,
                        SupervisorAlreadyExists, MAX_RUN_EVENT_BYTES,
                        TERMINAL_EVENT_RESERVE, CHILD_RECEIPT_RESERVE)


NOW = "2026-01-01 00:00:00"


def create_v0_database(path: Path, orphan_message=False):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            project_id TEXT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT,
            tool_calls_json TEXT,
            tool_call_id TEXT,
            sequence INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
    """)
    conn.executemany(
        "INSERT INTO projects VALUES (?, ?, ?, ?)",
        (
            ("default-late", "默认项目", "2026-01-02", "2026-01-02"),
            ("default-early", "默认项目", "2026-01-01", "2026-01-01"),
            ("regular", "工作", "2026-01-03", "2026-01-03"),
        ),
    )
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?)",
        (
            ("valid", "valid", NOW, NOW, "regular"),
            ("null-project", "null", NOW, NOW, None),
            ("dangling-project", "dangling", NOW, NOW, "missing-project"),
        ),
    )
    conn.executemany(
        """
        INSERT INTO messages
            (session_id, role, content, sequence, created_at)
        VALUES (?, 'user', ?, ?, ?)
        """,
        (
            ("valid", "first", 7, NOW),
            ("valid", "second", 7, NOW),
            ("missing-session" if orphan_message else "null-project", "third", 99, NOW),
        ),
    )
    conn.commit()
    conn.close()


def create_v1_settings_database(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            api_key_encrypted TEXT,
            base_url TEXT,
            model TEXT,
            max_rounds INTEGER,
            tool_timeout REAL,
            updated_at TEXT
        );
        INSERT INTO settings VALUES (
            1, 'legacy-ciphertext', 'https://api.example/v1',
            'legacy-model', 5, 10.0, '2026-01-01 00:00:00'
        );
        CREATE TABLE projects (
            id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, is_default INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT,
            tool_calls_json TEXT, tool_call_id TEXT, sequence INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
        );
        INSERT INTO projects VALUES ('default', '默认项目', '2026-01-01', '2026-01-01', 1);
        PRAGMA user_version = 1;
    """)
    conn.close()


def snapshot(db: Database):
    schema = db._query_all("""
        SELECT type, name, tbl_name, sql
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
    """)
    rows = {
        table: db._query_all(f"SELECT * FROM {table} ORDER BY id")
        for table in ("projects", "sessions", "messages")
    }
    return schema, rows


class MigrationTest(unittest.TestCase):
    def test_import_does_not_initialize_production_database(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c",
             "import backend.db as module; assert module.db._instance is None"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_real_v0_migrates_once_and_repairs_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            create_v0_database(path)

            db = Database(path)
            self.assertEqual(db._conn.execute("PRAGMA user_version").fetchone()[0], 4)
            defaults = db._query_all(
                "SELECT id FROM projects WHERE is_default = 1"
            )
            self.assertEqual(defaults, [{"id": "default-early"}])
            sessions = {
                row["id"]: row["project_id"]
                for row in db._query_all("SELECT id, project_id FROM sessions")
            }
            self.assertEqual(sessions["valid"], "regular")
            self.assertEqual(sessions["null-project"], "default-early")
            self.assertEqual(sessions["dangling-project"], "default-early")
            sequences = db._query_all(
                "SELECT id, sequence FROM messages WHERE session_id = 'valid' ORDER BY id"
            )
            self.assertEqual([row["sequence"] for row in sequences], [1, 2])
            self.assertEqual(
                db._conn.execute("PRAGMA foreign_key_check").fetchall(), []
            )
            self.assertEqual(
                db._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok"
            )
            self.assertIn(
                "projects",
                {
                    row["table"]
                    for row in db._conn.execute("PRAGMA foreign_key_list(sessions)")
                },
            )
            with self.assertRaises(sqlite3.IntegrityError):
                db._execute(
                    """
                    INSERT INTO messages
                        (session_id, role, sequence, created_at)
                    VALUES ('valid', 'user', 1, ?)
                    """,
                    (NOW,),
                )

            first_snapshot = snapshot(db)
            backups = list(path.parent.glob("legacy.db.pre-v4-*.bak"))
            self.assertEqual(len(backups), 1)
            with closing(sqlite3.connect(backups[0])) as backup:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 0)
                self.assertEqual(
                    backup.execute("SELECT sequence FROM messages WHERE id=1").fetchone()[0],
                    7,
                )
            db.close()
            reopened = Database(path)
            try:
                self.assertEqual(snapshot(reopened), first_snapshot)
                self.assertEqual(
                    reopened._conn.execute("PRAGMA user_version").fetchone()[0], 4
                )
                self.assertEqual(len(list(path.parent.glob("legacy.db.pre-v4-*.bak"))), 1)
            finally:
                reopened.close()

    def test_existing_v2_migrates_to_v3_without_changing_provider_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v2.db"
            create_v1_settings_database(path)
            with closing(sqlite3.connect(path)) as conn:
                conn.execute("ALTER TABLE settings ADD COLUMN credential_base_url TEXT")
                conn.execute("UPDATE settings SET credential_base_url=base_url")
                conn.execute("PRAGMA user_version=2")
                conn.commit()
            db = Database(path)
            try:
                self.assertEqual(db._conn.execute("PRAGMA user_version").fetchone()[0], 4)
                self.assertEqual(db.get_settings()["credential_base_url"],
                                 "https://api.example/v1")
                self.assertEqual(db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(db._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                backups = list(path.parent.glob("v2.db.pre-v4-*.bak"))
                self.assertEqual(len(backups), 1)
                with closing(sqlite3.connect(backups[0])) as backup:
                    self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 2)
            finally:
                db.close()

    def test_v1_settings_key_migrates_to_nullable_unbound_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v1.db"
            create_v1_settings_database(path)

            db = Database(path)
            try:
                self.assertEqual(
                    db._conn.execute("PRAGMA user_version").fetchone()[0], 4
                )
                settings = db.get_settings()
                self.assertEqual(settings["api_key_encrypted"], "legacy-ciphertext")
                self.assertIsNone(settings["credential_base_url"])
            finally:
                db.close()

            reopened = Database(path)
            try:
                self.assertEqual(
                    reopened._conn.execute("PRAGMA user_version").fetchone()[0], 4
                )
                self.assertIsNone(reopened.get_settings()["credential_base_url"])
            finally:
                reopened.close()

    def test_failed_migration_rolls_back_schema_and_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.db"
            create_v0_database(path, orphan_message=True)

            with self.assertRaises(sqlite3.IntegrityError):
                Database(path)

            conn = sqlite3.connect(path)
            try:
                self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 0)
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(projects)")
                }
                self.assertNotIn("is_default", columns)
                self.assertIsNone(
                    conn.execute(
                        "SELECT project_id FROM sessions WHERE id = 'null-project'"
                    ).fetchone()[0]
                )
            finally:
                conn.close()


class DatabaseConsistencyTest(unittest.TestCase):
    def test_provider_settings_update_rolls_back_the_entire_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "settings-rollback.db")
            try:
                db.save_settings(
                    api_key_encrypted="old-ciphertext",
                    base_url="https://api.example/v1",
                    credential_base_url="https://api.example/v1",
                    model="old-model",
                )
                before = db.get_settings()
                db._execute("""
                    CREATE TRIGGER injected_settings_failure
                    BEFORE UPDATE ON settings
                    WHEN NEW.base_url = 'https://other.example/v1'
                    BEGIN
                        SELECT RAISE(ABORT, 'injected failure');
                    END
                """)

                with self.assertRaises(sqlite3.IntegrityError):
                    db.save_settings(
                        api_key_encrypted="new-ciphertext",
                        base_url="https://other.example/v1",
                        credential_base_url="https://other.example/v1",
                        model="new-model",
                    )

                self.assertEqual(db.get_settings(), before)
            finally:
                db.close()

    def test_message_insert_rolls_back_when_session_touch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "message-rollback.db")
            try:
                session_id = db.create_session()["id"]
                db._execute("""
                    CREATE TRIGGER injected_session_touch_failure
                    BEFORE UPDATE OF updated_at ON sessions
                    BEGIN
                        SELECT RAISE(ABORT, 'injected failure');
                    END
                """)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.add_message(session_id, "user", "must roll back")
                self.assertEqual(
                    db._query_one("SELECT COUNT(*) AS count FROM messages")["count"],
                    0,
                )
            finally:
                db.close()

    def test_100_writers_get_unique_contiguous_sequences(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "writers.db"
            databases = [Database(path) for _ in range(10)]
            try:
                session_id = databases[0].create_session()["id"]
                barrier = threading.Barrier(100)

                def write(index):
                    barrier.wait()
                    databases[index % len(databases)].add_message(
                        session_id, "user", str(index)
                    )

                with ThreadPoolExecutor(max_workers=100) as executor:
                    futures = [executor.submit(write, index) for index in range(100)]
                    for future in futures:
                        future.result()

                rows = databases[0]._query_all(
                    "SELECT id, sequence FROM messages ORDER BY sequence"
                )
                self.assertEqual(len(rows), 100)
                self.assertEqual(
                    [row["sequence"] for row in rows], list(range(1, 101))
                )
                self.assertEqual(
                    [row["id"] for row in rows], sorted(row["id"] for row in rows)
                )
            finally:
                for db in databases:
                    db.close()

    def test_default_protection_project_delete_rollback_and_foreign_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Database(Path(tmp) / "projects.db")
            try:
                default = next(
                    project for project in db.list_projects() if project["is_default"]
                )
                with self.assertRaisesRegex(ValueError, "默认项目不能删除"):
                    db.delete_project(default["id"])
                with self.assertRaises(sqlite3.IntegrityError):
                    db._execute(
                        "UPDATE projects SET is_default = 0 WHERE id = ?",
                        (default["id"],),
                    )

                project = db.create_project("temporary")
                session = db.create_session(project_id=project["id"])
                db._execute(f"""
                    CREATE TRIGGER injected_project_delete_failure
                    BEFORE DELETE ON projects
                    WHEN OLD.id = '{project['id']}'
                    BEGIN
                        SELECT RAISE(ABORT, 'injected failure');
                    END
                """)
                with self.assertRaises(sqlite3.IntegrityError):
                    db.delete_project(project["id"])
                self.assertEqual(
                    db.get_session(session["id"])["project_id"], project["id"]
                )
                self.assertIsNotNone(db.get_project(project["id"]))

                db._execute("DROP TRIGGER injected_project_delete_failure")
                self.assertTrue(db.delete_project(project["id"]))
                self.assertEqual(
                    db.get_session(session["id"])["project_id"], default["id"]
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    db.assign_session_to_project(session["id"], "missing-project")
                self.assertEqual(
                    db.get_session(session["id"])["project_id"], default["id"]
                )
            finally:
                db.close()

    def test_empty_sessions_are_never_reused_across_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sessions.db"
            db = Database(path)
            first_project = db.create_project("first")
            second_project = db.create_project("second")
            first = db.create_session(project_id=first_project["id"])
            second = db.create_session(project_id=second_project["id"])
            self.assertNotEqual(first["id"], second["id"])
            db.close()

            reopened = Database(path)
            try:
                self.assertEqual(
                    reopened.get_session(first["id"])["project_id"],
                    first_project["id"],
                )
                self.assertEqual(
                    reopened.get_session(second["id"])["project_id"],
                    second_project["id"],
                )
            finally:
                reopened.close()


class P2AgentSchemaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "agents.db"
        self.db = Database(self.path)
        self.addCleanup(self.db.close)

    def test_legacy_session_never_becomes_root_without_selection(self):
        legacy = self.db.create_session("legacy")
        self.assertIsNone(self.db.get_supervisor())
        self.assertIsNone(self.db.get_session(legacy["id"])["agent_role"])
        root = self.db.select_supervisor(legacy["id"])
        self.assertEqual((root["agent_role"], root["root_session_id"], root["depth"]),
                         ("supervisor", legacy["id"], 0))
        self.assertEqual(self.db.select_supervisor(legacy["id"])["id"], legacy["id"])
        with self.assertRaises(SupervisorAlreadyExists):
            self.db.create_supervisor("another")
        with self.assertRaises(AgentHierarchyError):
            self.db.delete_session(root["id"])
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_two_connections_cannot_create_two_active_roots(self):
        other = Database(self.path)
        self.addCleanup(other.close)
        barrier = threading.Barrier(2)

        def create(database, title):
            barrier.wait()
            try:
                return database.create_supervisor(title)["id"]
            except SupervisorAlreadyExists:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            left = pool.submit(create, self.db, "left")
            right = pool.submit(create, other, "right")
            results = [left.result(), right.result()]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM sessions WHERE "
                                            "agent_role='supervisor' AND archived_at IS NULL")["n"], 1)
        self.assertEqual(self.db._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_worker_parent_depth_child_budget_and_archive(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        self.assertEqual((worker["parent_session_id"], worker["depth"], worker["can_delegate"]),
                         (root["id"], 1, 0))
        with self.assertRaises(AgentHierarchyError):
            self.db.create_worker(worker["id"], "grandchild")
        delegating = self.db.create_worker(root["id"], "delegating", can_delegate=True)
        grandchild = self.db.create_worker(delegating["id"], "grandchild")
        self.assertEqual((grandchild["depth"], grandchild["root_session_id"]),
                         (2, root["id"]))
        with self.assertRaises(AgentHierarchyError):
            self.db.create_worker(grandchild["id"], "too-deep")
        self.db.create_worker(root["id"], "three")
        self.db.create_worker(root["id"], "four")
        with self.assertRaises(AgentHierarchyError):
            self.db.create_worker(root["id"], "five")
        with self.assertRaises(AgentHierarchyError):
            self.db.archive_worker(delegating["id"])
        self.db.archive_worker(grandchild["id"])
        self.db.archive_worker(delegating["id"])
        with self.assertRaises(ResourceNotFound):
            self.db.rename_agent(delegating["id"], "archived")
        self.assertEqual(len(self.db.list_agent_tree()), 4)
        self.assertEqual(len(self.db.list_agent_tree(include_archived=True)), 6)

    def test_v3_to_v4_preserves_history_and_foreign_keys(self):
        with patch("backend.db.SCHEMA_VERSION", 3):
            old = Database(Path(self.tmp.name) / "v3.db")
            session_id = "historical-v3"
            project_id = old._get_or_create_default_project()["id"]
            old._execute("""
                INSERT INTO sessions(id,title,created_at,updated_at,project_id)
                VALUES (?,?,?,?,?)
            """, (session_id, "historical", NOW, NOW, project_id))
            # Seed the v3 format directly: P2 repository methods run only after migration.
            old._execute("""
                INSERT INTO tasks(id,session_id,goal,created_at,updated_at)
                VALUES ('historical-task',?,'old task',?,?)
            """, (session_id, NOW, NOW))
            old._execute("""
                INSERT INTO runs(id,task_id,session_id,attempt,idempotency_key,
                    request_hash,status,config_snapshot_json,tool_snapshot_json,
                    context_max_sequence,created_at,updated_at)
                VALUES ('historical-run','historical-task',?,1,'old-key','hash',
                    'queued','{}','{}',0,?,?)
            """, (session_id, NOW, NOW))
            old.close()
        upgraded = Database(Path(self.tmp.name) / "v3.db")
        self.addCleanup(upgraded.close)
        self.assertEqual(upgraded._conn.execute("PRAGMA user_version").fetchone()[0], 4)
        self.assertIsNone(upgraded.get_session(session_id)["agent_role"])
        self.assertEqual(upgraded.get_run("historical-run")["effective_grant_json"],
                         '{"grants":["read_only"]}')
        self.assertEqual(upgraded._conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(upgraded._conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_host_delegation_freezes_intersection_and_busy_worker_queues_task(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        first, run = self.db.host_delegate_task(
            root["id"], worker["id"], "first", ["done"],
            {"message_refs": [], "artifact_refs": []},
            {"grants": ["read_only", "control.delegate_task", "control.send_agent_message"]},
            {"max_rounds": 4, "max_tool_calls": 2})
        self.assertEqual(first["session_id"], worker["id"])
        self.assertEqual(first["creator_session_id"], root["id"])
        self.assertIsNotNone(run)
        effective = set(json.loads(run["effective_grant_json"])["grants"])
        self.assertEqual(effective, {"read_only", "control.send_agent_message"})
        self.assertEqual(json.loads(first["limits_json"])["max_tool_calls"], 2)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db._execute("UPDATE tasks SET goal='changed' WHERE id=?", (first["id"],))
        second, queued = self.db.host_delegate_task(root["id"], worker["id"], "second")
        self.assertIsNone(queued)
        self.assertEqual(self.db.get_latest_run_id(second["id"]), None)
        self.db.request_control(run["id"], "cancel")
        scheduled = self.db.schedule_pending_tasks()
        self.assertEqual(len(scheduled), 1)
        self.assertEqual(scheduled[0]["task_id"], second["id"])
        self.assertEqual(self.db.schedule_pending_tasks(), [])
        self.assertEqual([task["id"] for task in self.db.list_tree_tasks(root["id"])],
                         [second["id"], first["id"]])
        with self.assertRaises(ValueError):
            self.db.host_delegate_task(root["id"], worker["id"], "invalid",
                                       requested_grant={"grants": ["filesystem.write"]})
        legacy = self.db.create_session("outside")
        with self.assertRaises(ResourceNotFound):
            self.db.host_delegate_task(legacy["id"], worker["id"], "cross tree")

    def _control_run(self, run, calls, owner="p2-test"):
        claimed = self.db.claim_next_run(owner, 120)
        self.assertEqual(claimed["id"], run["id"])
        checkpoint = {"v": 1, "phase": "tool_pending", "round": 1,
                      "assistant_message_id": None, "next_tool_index": 0}
        claimed, _ = self.db.transition_run(
            claimed["id"], claimed["version"], "running", "model.completed",
            {"round": 1, "tool_count": len(calls)}, checkpoint=checkpoint,
            message={"role": "assistant", "content": None, "tool_calls": calls},
            lease_owner=owner, lease_epoch=claimed["lease_epoch"])
        return claimed

    def _apply_control(self, run, call, owner="p2-test"):
        run = self.db.start_tool_call(
            run["id"], run["version"], owner, run["lease_epoch"], call["id"],
            call["function"]["name"], "effectful", "a" * 64, 8192)
        return self.db.apply_control_tool(
            run["id"], run["version"], owner, run["lease_epoch"],
            call["id"], call["function"]["name"], call["function"]["arguments"])

    @staticmethod
    def _call(call_id, name, arguments):
        return {"id": call_id, "type": "function", "function":
                {"name": name, "arguments": json.dumps(arguments)}}

    def test_question_host_reply_is_atomic_and_does_not_accept_spoofed_target(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        outsider = self.db.create_session("outsider")
        task, run = self.db.host_delegate_task(root["id"], worker["id"], "ask")
        call = self._call("ask-1", "send_agent_message",
                          {"to_agent_id": root["id"], "kind": "question",
                           "content": "Need input"})
        running = self._control_run(run, [call])
        running = self.db.start_tool_call(
            running["id"], running["version"], "p2-test", running["lease_epoch"],
            call["id"], "send_agent_message", "effectful", "a" * 64, 8192)
        with self.assertRaises(InvalidRunTransition):
            self.db.apply_control_tool(running["id"], running["version"], "p2-test",
                running["lease_epoch"], call["id"], "send_agent_message",
                {"to_agent_id": outsider["id"], "kind": "question", "content": "Need input"})
        self.assertEqual(self.db.get_run(run["id"])["status"], "running")
        self.assertEqual(self.db.list_agent_messages(root["id"]), [])
        running, _ = self.db.apply_control_tool(
            running["id"], running["version"], "p2-test", running["lease_epoch"],
            call["id"], "send_agent_message", call["function"]["arguments"])
        self.assertEqual((running["status"], running["wait_reason"]), ("waiting", "input"))
        question = self.db.list_agent_messages(root["id"])[0]
        self.assertEqual((question["session_id"], question["sender_session_id"],
                          question["source_run_id"], question["needs_reply"]),
                         (root["id"], worker["id"], run["id"], True))
        with self.assertRaises(ResourceNotFound):
            self.db.host_reply(worker["id"], question["id"], "wrong recipient")
        reply, resumed = self.db.host_reply(root["id"], question["id"], "answer")
        self.assertEqual((reply["recipient_run_id"], resumed["status"]),
                         (run["id"], "queued"))
        self.assertFalse(self.db.list_agent_messages(root["id"])[0]["needs_reply"])
        with self.assertRaises(InvalidRunTransition):
            self.db.host_reply(root["id"], question["id"], "again")
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_delegate_wait_child_result_wakes_exact_parent_run(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        parent_task = self.db.create_task(root["id"], "coordinate")
        parent_run, _ = self.db.create_run(parent_task["id"], "parent")
        delegate = self._call("delegate-1", "delegate_task",
                              {"assignee_agent_id": worker["id"], "goal": "child",
                               "acceptance": []})
        running = self._control_run(parent_run, [delegate])
        running, message_id = self._apply_control(running, delegate)
        child_id = json.loads(self.db.get_message(message_id)["content"])["task_id"]
        child_run_id = self.db.get_latest_run_id(child_id)
        self.assertEqual(self.db.get_task(child_id)["creator_run_id"], parent_run["id"])
        self.assertEqual(set(json.loads(self.db.get_run(child_run_id)["effective_grant_json"])["grants"]),
                         {"read_only", "control.send_agent_message",
                          "control.get_task_status", "control.wait_for_tasks",
                          "control.request_human_approval"})
        running, _ = self.db.transition_run(
            running["id"], running["version"], "running", "tool.batch_completed",
            {"round": 1}, checkpoint={"v": 1, "phase": "model_pending", "round": 2},
            lease_owner="p2-test", lease_epoch=running["lease_epoch"])
        wait = self._call("wait-1", "wait_for_tasks", {"task_ids": [child_id]})
        running, _ = self.db.transition_run(
            running["id"], running["version"], "running", "model.completed",
            {"round": 2, "tool_count": 1},
            checkpoint={"v": 1, "phase": "tool_pending", "round": 2,
                        "assistant_message_id": None, "next_tool_index": 0},
            message={"role": "assistant", "content": None, "tool_calls": [wait]},
            lease_owner="p2-test", lease_epoch=running["lease_epoch"])
        running, _ = self._apply_control(running, wait)
        self.assertEqual((running["status"], running["wait_reason"]), ("waiting", "children"))
        self.assertEqual(self.db.control_scope(parent_run["id"])["wait_child_task_ids"],
                         frozenset({child_id}))
        self.assertGreater(json.loads(self.db.get_run(parent_run["id"])["usage_json"])
                           ["p2_child_reserve"][child_id], 0)
        before_receipt = (MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE -
                          CHILD_RECEIPT_RESERVE)
        self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                         (before_receipt, parent_run["id"]))
        result = {"summary": "done", "acceptance_results": [], "artifacts": [],
                  "evidence": [], "followups": []}
        result_call = self._call("result-1", "send_agent_message",
                                 {"to_agent_id": root["id"], "kind": "result",
                                  "content": json.dumps(result)})
        child_run = self._control_run(self.db.get_run(child_run_id), [result_call])
        child_run, _ = self._apply_control(child_run, result_call)
        self.assertEqual(child_run["status"], "completed")
        self.assertEqual(self.db.get_task_result(child_id), result)
        self.assertEqual(self.db.get_run(parent_run["id"])["status"], "queued")
        self.assertGreater(self.db.get_run(parent_run["id"])["event_bytes_written"],
                           before_receipt)
        self.assertLessEqual(self.db.get_run(parent_run["id"])["event_bytes_written"],
                             MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE)
        self.assertNotIn(child_id, json.loads(self.db.get_run(parent_run["id"])["usage_json"])
                         ["p2_child_reserve"])
        received = self.db.list_agent_messages(root["id"])
        self.assertEqual((received[0]["kind"], received[0]["recipient_run_id"]),
                         ("result", parent_run["id"]))
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_cross_tree_target_and_forged_sender_have_no_message(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        outsider = self.db.create_session("outside")
        _, run = self.db.host_delegate_task(root["id"], worker["id"], "work")
        call = self._call("cross-1", "send_agent_message",
                          {"to_agent_id": outsider["id"], "kind": "progress",
                           "content": "fake"})
        running = self._control_run(run, [call])
        running = self.db.start_tool_call(running["id"], running["version"],
            "p2-test", running["lease_epoch"], call["id"], "send_agent_message",
            "effectful", "a" * 64, 8192)
        with self.assertRaises(ResourceNotFound):
            self.db.apply_control_tool(running["id"], running["version"],
                "p2-test", running["lease_epoch"], call["id"],
                "send_agent_message", call["function"]["arguments"])
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db._write() as conn:
                self.db._insert_message_tx(conn, root["id"], "user", "forged",
                    sender_session_id=outsider["id"], source_run_id=run["id"],
                    related_task_id=run["task_id"], kind="progress")
        self.assertEqual(self.db.list_agent_messages(root["id"]), [])
        self.assertEqual(self.db.get_run(run["id"])["status"], "running")

    def test_progress_persists_without_waking_waiting_parent(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        task = self.db.create_task(root["id"], "coordinate")
        parent, _ = self.db.create_run(task["id"], "parent")
        delegate = self._call("delegate-1", "delegate_task", {
            "assignee_agent_id": worker["id"], "goal": "child", "acceptance": [],
        })
        parent = self._control_run(parent, [delegate])
        parent, tool_message_id = self._apply_control(parent, delegate)
        child_task_id = json.loads(self.db.get_message(tool_message_id)["content"])["task_id"]
        child_run_id = self.db.get_latest_run_id(child_task_id)
        parent, _ = self.db.transition_run(
            parent["id"], parent["version"], "waiting", "run.waiting",
            {"reason": "children"}, checkpoint={
                "v": 1, "phase": "model_pending", "round": 2,
                "awaited_child_task_ids": [child_task_id],
            }, wait_reason="children", lease_owner="p2-test",
            lease_epoch=parent["lease_epoch"])
        before = self.db.get_run(parent["id"])
        before_event = self.db.get_last_event_id(parent["id"])

        progress = self._call("progress-1", "send_agent_message", {
            "to_agent_id": root["id"], "kind": "progress", "content": "halfway",
        })
        child = self._control_run(self.db.get_run(child_run_id), [progress], "child-test")
        self._apply_control(child, progress, "child-test")

        messages = self.db.list_agent_messages(root["id"])
        self.assertEqual((messages[0]["kind"], messages[0]["content"],
                          messages[0]["recipient_run_id"]),
                         ("progress", "halfway", parent["id"]))
        self.assertEqual(self.db.get_run(parent["id"]), before)
        self.assertEqual(self.db.get_last_event_id(parent["id"]), before_event)

    def test_delegate_event_reserve_rejects_before_write_and_rolls_back(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        task = self.db.create_task(root["id"], "coordinate")
        run, _ = self.db.create_run(task["id"], "parent")
        call = self._call("delegate-1", "delegate_task",
                          {"assignee_agent_id": worker["id"], "goal": "child",
                           "acceptance": []})
        running = self._control_run(run, [call])
        cap = MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE
        self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                         (cap - 1000, run["id"]))
        with self.assertRaises(EventLimitExceeded):
            self.db.start_tool_call(running["id"], running["version"],
                "p2-test", running["lease_epoch"], call["id"], "delegate_task",
                "effectful", "a" * 64, 8192)
        self.assertIsNone(self.db.get_tool_start(run["id"], call["id"]))
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM tasks WHERE "
                                            "creator_run_id=?", (run["id"],))["n"], 0)
        self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                         (cap - 9000, run["id"]))
        running = self.db.start_tool_call(running["id"], running["version"],
            "p2-test", running["lease_epoch"], call["id"], "delegate_task",
            "effectful", "a" * 64, 8192)
        self.db._execute("UPDATE runs SET event_bytes_written=? WHERE id=?",
                         (cap - 5000, run["id"]))
        before = self.db.get_run(run["id"])["event_bytes_written"]
        with self.assertRaises(EventLimitExceeded):
            self.db.apply_control_tool(running["id"], running["version"],
                "p2-test", running["lease_epoch"], call["id"], "delegate_task",
                call["function"]["arguments"])
        self.assertEqual(self.db.get_run(run["id"])["event_bytes_written"], before)
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM tasks WHERE "
                                            "creator_run_id=?", (run["id"],))["n"], 0)
        self.assertEqual(self.db.get_run(run["id"])["status"], "running")

    def test_child_result_uses_reserved_bytes_and_never_wakes_parent_retry(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        task = self.db.create_task(root["id"], "coordinate")
        parent_run, _ = self.db.create_run(task["id"], "parent")
        delegate = self._call("delegate-1", "delegate_task",
                              {"assignee_agent_id": worker["id"], "goal": "child",
                               "acceptance": []})
        running = self._control_run(parent_run, [delegate])
        running, tool_id = self._apply_control(running, delegate)
        child_id = json.loads(self.db.get_message(tool_id)["content"])["task_id"]
        child_run_id = self.db.get_latest_run_id(child_id)
        self.assertEqual(json.loads(running["usage_json"])["p2_child_reserve"][child_id],
                         CHILD_RECEIPT_RESERVE)
        failed, _ = self.db.transition_run(
            running["id"], running["version"], "failed", "run.failed",
            {"code": "parent_failed"}, lease_owner="p2-test",
            lease_epoch=running["lease_epoch"])
        retry, created = self.db.create_run(task["id"], "parent-retry", {"retry": True})
        self.assertTrue(created)
        retry_last_event = self.db.get_last_event_id(retry["id"])
        result = {"summary": "done", "acceptance_results": [], "artifacts": [],
                  "evidence": [], "followups": []}
        result_call = self._call("result-1", "send_agent_message",
                                 {"to_agent_id": root["id"], "kind": "result",
                                  "content": json.dumps(result)})
        child = self._control_run(self.db.get_run(child_run_id), [result_call])
        child, _ = self._apply_control(child, result_call)
        self.assertEqual(child["status"], "completed")
        self.assertEqual(self.db.get_run(retry["id"])["status"], "queued")
        self.assertEqual(self.db.get_last_event_id(retry["id"]), retry_last_event)
        received = self.db.list_agent_messages(root["id"])[0]
        self.assertEqual(received["recipient_run_id"], failed["id"])
        self.assertNotEqual(received["recipient_run_id"], retry["id"])

    def test_child_failure_notifies_only_its_waiting_parent_atomically(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        task = self.db.create_task(root["id"], "coordinate")
        parent, _ = self.db.create_run(task["id"], "parent")
        parent = self.db.claim_next_run("parent-worker", 120)
        with self.db._write() as conn:
            child_task = self.db._insert_task_tx(
                conn, worker["id"], "child", [], {},
                creator_session_id=root["id"], creator_run_id=parent["id"],
                parent_task_id=task["id"])
            self.db._reserve_child_receipt_tx(conn, parent["id"], child_task["id"])
            child, _ = self.db._insert_run_tx(
                conn, child_task, "child-run", {}, None, None)
        parent, _ = self.db.transition_run(
            parent["id"], parent["version"], "waiting", "run.waiting",
            {"reason": "children"}, checkpoint={"v": 1, "phase": "model_pending",
                "round": 1, "awaited_child_task_ids": [child_task["id"]]},
            wait_reason="children", lease_owner="parent-worker",
            lease_epoch=parent["lease_epoch"])
        child = self.db.claim_next_run("child-worker", 120)
        self.assertEqual(child["task_id"], child_task["id"])
        child, _ = self.db.transition_run(
            child["id"], child["version"], "failed", "run.failed",
            {"code": "worker_failed"}, error={"code": "worker_failed"},
            lease_owner="child-worker", lease_epoch=child["lease_epoch"])
        self.assertEqual(self.db.get_task(child_task["id"])["status"], "failed")
        self.assertEqual(self.db.get_run(parent["id"])["status"], "queued")
        inbox = self.db.list_agent_messages(root["id"])
        self.assertEqual(len(inbox), 1)
        self.assertEqual(json.loads(inbox[0]["content"]),
                         {"task_id": child_task["id"], "status": "failed"})
        self.assertEqual(inbox[0]["recipient_run_id"], parent["id"])
        self.assertEqual(self.db._conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_agent_reply_cannot_answer_stale_question_from_same_child_run(self):
        root = self.db.create_supervisor()
        worker = self.db.create_worker(root["id"], "worker")
        parent_task = self.db.create_task(root["id"], "parent")
        parent, _ = self.db.create_run(parent_task["id"], "parent-run")
        parent = self.db.claim_next_run("parent-worker", 120)
        child_task, child = self.db.host_delegate_task(root["id"], worker["id"], "child")
        child = self.db.claim_next_run("child-worker", 120)
        child, _ = self.db.transition_run(
            child["id"], child["version"], "waiting", "run.waiting",
            {"reason": "input"}, wait_reason="input", lease_owner="child-worker",
            lease_epoch=child["lease_epoch"])
        with self.db._write() as conn:
            q1 = self.db._insert_message_tx(
                conn, root["id"], "user", "Q1", sender_session_id=worker["id"],
                source_run_id=child["id"], related_task_id=child_task["id"],
                kind="question", hop=1)
        self.db.host_reply(root["id"], q1, "A1")
        child = self.db.claim_next_run("child-worker", 120)
        child, _ = self.db.transition_run(
            child["id"], child["version"], "waiting", "run.waiting",
            {"reason": "input"}, wait_reason="input", lease_owner="child-worker",
            lease_epoch=child["lease_epoch"])
        with self.db._write() as conn:
            q2 = self.db._insert_message_tx(
                conn, root["id"], "user", "Q2", sender_session_id=worker["id"],
                source_run_id=child["id"], related_task_id=child_task["id"],
                kind="question", hop=1)
        self.assertTrue(self.db.list_agent_messages(root["id"])[1]["needs_reply"])
        stale = self._call("stale-reply", "send_agent_message",
                           {"to_agent_id": worker["id"], "kind": "reply",
                            "content": "wrong", "reply_to_message_id": q1})
        parent, _ = self.db.transition_run(
            parent["id"], parent["version"], "running", "model.completed",
            {"round": 1, "tool_count": 1},
            checkpoint={"v": 1, "phase": "tool_pending", "round": 1,
                        "assistant_message_id": None, "next_tool_index": 0},
            message={"role": "assistant", "content": None, "tool_calls": [stale]},
            lease_owner="parent-worker", lease_epoch=parent["lease_epoch"])
        parent = self.db.start_tool_call(
            parent["id"], parent["version"], "parent-worker", parent["lease_epoch"],
            stale["id"], "send_agent_message", "effectful", "a" * 64, 8192)
        before = self.db._query_one("SELECT COUNT(*) AS n FROM messages WHERE "
                                    "recipient_run_id=? AND kind='reply'",
                                    (child["id"],))["n"]
        with self.assertRaises(InvalidRunTransition):
            self.db.apply_control_tool(parent["id"], parent["version"],
                "parent-worker", parent["lease_epoch"], stale["id"],
                "send_agent_message", stale["function"]["arguments"])
        self.assertEqual(self.db.get_run(child["id"])["status"], "waiting")
        self.assertEqual(self.db._query_one("SELECT COUNT(*) AS n FROM messages WHERE "
                                            "recipient_run_id=? AND kind='reply'",
                                            (child["id"],))["n"], before)
        self.assertEqual(self.db.list_agent_messages(root["id"])[1]["id"], q2)


if __name__ == "__main__":
    unittest.main()
