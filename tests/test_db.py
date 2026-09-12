"""P0 SQLite 迁移与并发一致性测试。"""

import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from backend.db import Database


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
    def test_real_v0_migrates_once_and_repairs_existing_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            create_v0_database(path)

            db = Database(path)
            self.assertEqual(db._conn.execute("PRAGMA user_version").fetchone()[0], 2)
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
            db.close()
            reopened = Database(path)
            try:
                self.assertEqual(snapshot(reopened), first_snapshot)
                self.assertEqual(
                    reopened._conn.execute("PRAGMA user_version").fetchone()[0], 2
                )
            finally:
                reopened.close()

    def test_v1_settings_key_migrates_to_nullable_unbound_origin(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "v1.db"
            create_v1_settings_database(path)

            db = Database(path)
            try:
                self.assertEqual(
                    db._conn.execute("PRAGMA user_version").fetchone()[0], 2
                )
                settings = db.get_settings()
                self.assertEqual(settings["api_key_encrypted"], "legacy-ciphertext")
                self.assertIsNone(settings["credential_base_url"])
            finally:
                db.close()

            reopened = Database(path)
            try:
                self.assertEqual(
                    reopened._conn.execute("PRAGMA user_version").fetchone()[0], 2
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


if __name__ == "__main__":
    unittest.main()
