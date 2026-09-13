"""SQLite 数据库访问层：会话、消息与设置持久化。"""

import json
import hashlib
import logging
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from backend.config import BASE_DIR


DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "countboter.db"
logger = logging.getLogger("agent.db")
SCHEMA_VERSION = 3
MAX_EVENT_BYTES = 64 * 1024
MAX_RUN_EVENT_BYTES = 4 * 1024 * 1024
TERMINAL_EVENT_RESERVE = 8 * 1024
TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})


class RepositoryError(ValueError):
    code = "repository_error"


class ResourceNotFound(RepositoryError):
    code = "not_found"


class IdempotencyKeyReused(RepositoryError):
    code = "idempotency_key_reused"


class RunAlreadyActive(RepositoryError):
    code = "run_already_active"

    def __init__(self, existing_run_id):
        super().__init__("该 Agent 已有活动 Run")
        self.existing_run_id = existing_run_id


class InvalidRunTransition(RepositoryError):
    code = "invalid_run_transition"


class EventLimitExceeded(RepositoryError):
    code = "event_limit_exceeded"


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Database:
    """基于标准库 sqlite3 的轻量数据访问对象。"""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._existing_file = self.db_path.exists()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        try:
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA busy_timeout = 5000")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._init_tables()
        except Exception:
            self._conn.close()
            raise
        logger.info("SQLite 数据库已打开：%s", self.db_path)

    def _execute(self, sql, params=()):
        logger.debug("SQL 执行：%s", sql)
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return cursor
            except Exception:
                self._conn.rollback()
                raise

    def _query_all(self, sql, params=()):
        logger.debug("SQL 查询：%s", sql)
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def _query_one(self, sql, params=()):
        rows = self._query_all(sql, params)
        return rows[0] if rows else None

    @contextmanager
    def _write(self):
        """A single SQLite writer transaction; never nest `_execute` inside it."""
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def _init_tables(self):
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version < SCHEMA_VERSION and self._existing_file:
                backup_path = self.db_path.with_name(
                    f"{self.db_path.name}.pre-v3-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.bak"
                )
                backup = sqlite3.connect(str(backup_path))
                try:
                    self._conn.backup(backup)
                finally:
                    backup.close()
            if version == 0:
                self._migrate_0_to_1()
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                self._migrate_1_to_2()
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 2:
                self._migrate_2_to_3()
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"不支持的数据库版本：{version}")
            self._verify_integrity()

    def _migrate_0_to_1(self):
        """把无版本旧库原子迁移到 P0 一致性 schema。"""
        conn = self._conn
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 另一个进程可能已在等待 writer lock 时完成同一迁移。
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version in (1, 2, SCHEMA_VERSION):
                conn.commit()
                return
            if version != 0:
                raise RuntimeError(f"不支持的数据库版本：{version}")

            for sql in (
                """
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    api_key_encrypted TEXT,
                    base_url TEXT,
                    model TEXT,
                    max_rounds INTEGER,
                    tool_timeout REAL,
                    updated_at TEXT
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT,
                    tool_calls_json TEXT,
                    tool_call_id TEXT,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    action TEXT NOT NULL,
                    tool_name TEXT,
                    arguments_json TEXT,
                    result_json TEXT,
                    ok INTEGER NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS uploaded_files (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    stored_path TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mime_type TEXT,
                    parsed_json TEXT,
                    created_at TEXT NOT NULL
                )
                """,
                """
                CREATE TABLE IF NOT EXISTS mcp_servers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    transport TEXT NOT NULL,
                    command TEXT,
                    args_json TEXT,
                    url TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """,
            ):
                conn.execute(sql)

            project_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(projects)")
            }
            if "is_default" not in project_columns:
                conn.execute(
                    "ALTER TABLE projects ADD COLUMN is_default INTEGER NOT NULL "
                    "DEFAULT 0 CHECK (is_default IN (0, 1))"
                )

            conn.execute("DROP TRIGGER IF EXISTS trg_projects_keep_default_delete")
            conn.execute("DROP TRIGGER IF EXISTS trg_projects_keep_default_update")
            conn.execute("DROP INDEX IF EXISTS uq_projects_one_default")
            default_project = conn.execute(
                """
                SELECT id FROM projects
                WHERE is_default = 1 OR name = '默认项目'
                ORDER BY is_default DESC, created_at, id
                LIMIT 1
                """
            ).fetchone()
            if default_project:
                default_id = default_project["id"]
            else:
                default_id = str(uuid.uuid4())
                now = self._now()
                conn.execute(
                    """
                    INSERT INTO projects
                        (id, name, created_at, updated_at, is_default)
                    VALUES (?, '默认项目', ?, ?, 0)
                    """,
                    (default_id, now, now),
                )
            conn.execute(
                "UPDATE projects SET is_default = CASE WHEN id = ? THEN 1 ELSE 0 END",
                (default_id,),
            )

            session_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(sessions)")
            }
            conn.execute("DROP TABLE IF EXISTS sessions_v1")
            conn.execute("""
                CREATE TABLE sessions_v1 (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    project_id TEXT NOT NULL
                        REFERENCES projects(id) ON DELETE RESTRICT
                )
            """)
            if "project_id" in session_columns:
                conn.execute("""
                    INSERT INTO sessions_v1
                        (id, title, created_at, updated_at, project_id)
                    SELECT s.id, s.title, s.created_at, s.updated_at,
                           CASE WHEN EXISTS (
                               SELECT 1 FROM projects p WHERE p.id = s.project_id
                           ) THEN s.project_id ELSE ? END
                    FROM sessions AS s
                """, (default_id,))
            else:
                conn.execute("""
                    INSERT INTO sessions_v1
                        (id, title, created_at, updated_at, project_id)
                    SELECT id, title, created_at, updated_at, ? FROM sessions
                """, (default_id,))
            conn.execute("DROP TABLE sessions")
            conn.execute("ALTER TABLE sessions_v1 RENAME TO sessions")

            conn.execute("""
                WITH ranked AS (
                    SELECT id,
                           ROW_NUMBER() OVER (
                               PARTITION BY session_id ORDER BY id
                           ) AS new_sequence
                    FROM messages
                )
                UPDATE messages
                SET sequence = (
                    SELECT new_sequence FROM ranked WHERE ranked.id = messages.id
                )
            """)
            conn.execute("DROP INDEX IF EXISTS idx_messages_session_sequence")
            conn.execute("DROP INDEX IF EXISTS uq_messages_session_sequence")
            conn.execute("""
                CREATE UNIQUE INDEX uq_messages_session_sequence
                ON messages(session_id, sequence)
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_project_id "
                "ON sessions(project_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at "
                "ON audit_logs(created_at)"
            )
            conn.execute("""
                CREATE UNIQUE INDEX uq_projects_one_default
                ON projects(is_default) WHERE is_default = 1
            """)
            conn.execute("""
                CREATE TRIGGER trg_projects_keep_default_delete
                BEFORE DELETE ON projects
                WHEN OLD.is_default = 1
                BEGIN
                    SELECT RAISE(ABORT, 'default project cannot be deleted');
                END
            """)
            conn.execute("""
                CREATE TRIGGER trg_projects_keep_default_update
                BEFORE UPDATE OF is_default ON projects
                WHEN OLD.is_default = 1 AND NEW.is_default <> 1
                BEGIN
                    SELECT RAISE(ABORT, 'default project cannot be cleared');
                END
            """)

            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"数据库外键检查失败：{[tuple(row) for row in violations]}"
                )
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(f"数据库完整性检查失败：{integrity}")
            conn.execute("PRAGMA user_version = 1")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def _migrate_1_to_2(self):
        """给旧设置行增加 nullable 的 Provider 凭据 origin 绑定。"""
        conn = self._conn
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version in (2, SCHEMA_VERSION):
                conn.commit()
                return
            if version != 1:
                raise RuntimeError(f"不支持的数据库版本：{version}")

            columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
            if "credential_base_url" not in columns:
                conn.execute("ALTER TABLE settings ADD COLUMN credential_base_url TEXT")
            conn.execute("PRAGMA user_version = 2")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _migrate_2_to_3(self):
        """在现有 Provider v2 之上增加最小 P1 持久内核。"""
        conn = self._conn
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                conn.commit()
                return
            if version != 2:
                raise RuntimeError(f"不支持的数据库版本：{version}")
            conn.execute("ALTER TABLE sessions ADD COLUMN archived_at TEXT")
            conn.execute("""
                CREATE TABLE tasks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
                    goal TEXT NOT NULL CHECK (length(trim(goal)) BETWEEN 1 AND 32768),
                    acceptance_json TEXT NOT NULL DEFAULT '[]',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK (status IN ('open','completed','failed','cancelled')),
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    finished_at TEXT,
                    UNIQUE (id, session_id),
                    CHECK ((status = 'open' AND finished_at IS NULL) OR
                           (status <> 'open' AND finished_at IS NOT NULL))
                )
            """)
            conn.execute("""
                CREATE TABLE runs (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    attempt INTEGER NOT NULL CHECK (attempt >= 1),
                    idempotency_key TEXT NOT NULL
                        CHECK (length(trim(idempotency_key)) BETWEEN 1 AND 200),
                    request_hash TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN
                        ('queued','running','waiting','paused','completed','failed','cancelled')),
                    wait_reason TEXT,
                    control_requested TEXT,
                    version INTEGER NOT NULL DEFAULT 0 CHECK (version >= 0),
                    lease_epoch INTEGER NOT NULL DEFAULT 0 CHECK (lease_epoch >= 0),
                    lease_owner TEXT,
                    lease_until TEXT,
                    config_snapshot_json TEXT NOT NULL,
                    tool_snapshot_json TEXT NOT NULL,
                    context_max_sequence INTEGER NOT NULL CHECK (context_max_sequence >= 0),
                    event_bytes_written INTEGER NOT NULL DEFAULT 0
                        CHECK (event_bytes_written BETWEEN 0 AND 4194304),
                    checkpoint_json TEXT,
                    usage_json TEXT,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    heartbeat_at TEXT,
                    finished_at TEXT,
                    FOREIGN KEY (task_id, session_id)
                        REFERENCES tasks(id, session_id) ON DELETE RESTRICT,
                    UNIQUE (task_id, attempt),
                    UNIQUE (session_id, idempotency_key),
                    UNIQUE (id, task_id),
                    CHECK (wait_reason IS NULL OR
                           (status = 'waiting' AND wait_reason IN ('approval','input','children')) OR
                           (status = 'paused' AND wait_reason = 'restart')),
                    CHECK (control_requested IS NULL OR
                           (status = 'running' AND control_requested IN ('pause','cancel'))),
                    CHECK ((status = 'running' AND lease_owner IS NOT NULL AND
                            lease_until IS NOT NULL AND checkpoint_json IS NOT NULL) OR
                           (status <> 'running' AND lease_owner IS NULL AND lease_until IS NULL)),
                    CHECK ((status IN ('completed','failed','cancelled') AND finished_at IS NOT NULL) OR
                           (status NOT IN ('completed','failed','cancelled') AND finished_at IS NULL))
                )
            """)
            conn.execute("""
                CREATE UNIQUE INDEX uq_runs_one_live_per_session ON runs(session_id)
                WHERE status IN ('queued','running','waiting','paused')
            """)
            conn.execute("CREATE INDEX idx_runs_claim ON runs(created_at,id) WHERE status='queued'")
            conn.execute("CREATE INDEX idx_runs_expired_lease ON runs(lease_until) WHERE status='running'")
            conn.execute("CREATE INDEX idx_tasks_session_created ON tasks(session_id,created_at)")
            conn.execute("""
                CREATE TABLE events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    type TEXT NOT NULL CHECK (length(type) BETWEEN 1 AND 80),
                    dedupe_key TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id, task_id) REFERENCES runs(id, task_id) ON DELETE RESTRICT
                )
            """)
            conn.execute("CREATE UNIQUE INDEX uq_events_dedupe_key ON events(dedupe_key) WHERE dedupe_key IS NOT NULL")
            conn.execute("CREATE INDEX idx_events_run_cursor ON events(run_id,id)")
            conn.execute("""
                CREATE TABLE approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
                    tool_call_id TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK (kind IN ('pre_execution','resolve_unknown_outcome')),
                    action_hash TEXT NOT NULL,
                    request_summary_json TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending','resolved','expired')),
                    expires_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    decision_json TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id,tool_call_id,kind,action_hash)
                )
            """)
            conn.execute("CREATE INDEX idx_approvals_status ON approvals(status,expires_at)")
            conn.execute("""
                CREATE TABLE messages_v3 (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    task_id TEXT,
                    run_id TEXT,
                    role TEXT NOT NULL CHECK (role IN ('system','user','assistant','tool')),
                    content TEXT,
                    tool_calls_json TEXT,
                    tool_call_id TEXT,
                    sequence INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE,
                    FOREIGN KEY (task_id,session_id) REFERENCES tasks(id,session_id) ON DELETE RESTRICT,
                    FOREIGN KEY (run_id,task_id) REFERENCES runs(id,task_id) ON DELETE RESTRICT,
                    UNIQUE (session_id,sequence),
                    CHECK (run_id IS NULL OR task_id IS NOT NULL),
                    CHECK (run_id IS NOT NULL OR task_id IS NULL OR role = 'user'),
                    CHECK (role <> 'tool' OR
                           (tool_call_id IS NOT NULL AND length(tool_call_id) > 0))
                )
            """)
            conn.execute("""
                INSERT INTO messages_v3
                    (id,session_id,role,content,tool_calls_json,tool_call_id,sequence,created_at)
                SELECT id,session_id,role,content,tool_calls_json,tool_call_id,sequence,created_at
                FROM messages
            """)
            conn.execute("DROP TABLE messages")
            conn.execute("ALTER TABLE messages_v3 RENAME TO messages")
            conn.execute("CREATE UNIQUE INDEX uq_messages_task_input ON messages(task_id) WHERE task_id IS NOT NULL AND run_id IS NULL")
            conn.execute("CREATE INDEX idx_messages_run_id ON messages(run_id,id)")
            conn.execute("""
                CREATE TRIGGER trg_tasks_status_transition BEFORE UPDATE OF status ON tasks
                WHEN OLD.status <> NEW.status AND NOT (
                    (OLD.status = 'open' AND NEW.status IN ('completed','failed','cancelled')) OR
                    (OLD.status IN ('failed','cancelled') AND NEW.status = 'open'))
                BEGIN SELECT RAISE(ABORT,'invalid task status transition'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_runs_status_transition BEFORE UPDATE OF status ON runs
                WHEN OLD.status <> NEW.status AND NOT (
                    (OLD.status = 'queued' AND NEW.status IN ('running','cancelled')) OR
                    (OLD.status = 'running' AND NEW.status IN ('waiting','paused','completed','failed','cancelled')) OR
                    (OLD.status IN ('waiting','paused') AND NEW.status IN ('queued','failed','cancelled')))
                BEGIN SELECT RAISE(ABORT,'invalid run status transition'); END
            """)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(f"数据库外键检查失败：{[tuple(row) for row in violations]}")
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("数据库完整性检查失败")
            conn.execute("PRAGMA user_version = 3")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def _verify_integrity(self):
        violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError(
                f"数据库外键检查失败：{[tuple(row) for row in violations]}"
            )
        integrity = self._conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise sqlite3.DatabaseError(f"数据库完整性检查失败：{integrity}")

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def close(self):
        with self._lock:
            self._conn.close()

    def get_settings(self):
        return self._query_one("SELECT * FROM settings WHERE id = 1")

    def save_settings(
        self,
        api_key_encrypted=None,
        base_url=None,
        credential_base_url=None,
        model=None,
        max_rounds=None,
        tool_timeout=None,
    ):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                row = self._conn.execute(
                    "SELECT * FROM settings WHERE id = 1"
                ).fetchone()
                existing = dict(row) if row else {}
                new_api_key = (
                    api_key_encrypted
                    if api_key_encrypted is not None
                    else existing.get("api_key_encrypted")
                )
                new_base_url = (
                    base_url if base_url is not None else existing.get("base_url")
                )
                new_credential_base_url = (
                    credential_base_url
                    if credential_base_url is not None
                    else existing.get("credential_base_url")
                )
                new_model = model if model is not None else existing.get("model")
                new_max_rounds = (
                    max_rounds
                    if max_rounds is not None
                    else existing.get("max_rounds")
                )
                new_tool_timeout = (
                    tool_timeout
                    if tool_timeout is not None
                    else existing.get("tool_timeout")
                )
                now = self._now()

                if existing:
                    self._conn.execute(
                        """
                        UPDATE settings
                        SET api_key_encrypted = ?, base_url = ?,
                            credential_base_url = ?, model = ?, max_rounds = ?,
                            tool_timeout = ?, updated_at = ?
                        WHERE id = 1
                        """,
                        (
                            new_api_key,
                            new_base_url,
                            new_credential_base_url,
                            new_model,
                            new_max_rounds,
                            new_tool_timeout,
                            now,
                        ),
                    )
                else:
                    self._conn.execute(
                        """
                        INSERT INTO settings
                            (id, api_key_encrypted, base_url,
                             credential_base_url, model, max_rounds,
                             tool_timeout, updated_at)
                        VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            new_api_key,
                            new_base_url,
                            new_credential_base_url,
                            new_model,
                            new_max_rounds,
                            new_tool_timeout,
                            now,
                        ),
                    )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        logger.info(
            "DB 设置输出：base_url=%s model=%s max_rounds=%s tool_timeout=%s "
            "api_key_encrypted=%s credential_bound=%s",
            new_base_url,
            new_model,
            new_max_rounds,
            new_tool_timeout,
            bool(new_api_key),
            bool(new_credential_base_url),
        )

    def list_sessions(self):
        sessions = self._query_all(
            """
            SELECT id, title, created_at, updated_at, project_id
            FROM sessions
            WHERE archived_at IS NULL
            ORDER BY updated_at DESC
            """
        )
        logger.info("DB 会话列表输出：count=%d", len(sessions))
        return sessions

    def get_session(self, session_id):
        return self._query_one(
            """
            SELECT id, title, created_at, updated_at, project_id, archived_at
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        )

    def create_session(self, title="新会话", project_id=None):
        if not project_id:
            project_id = self._get_or_create_default_project()["id"]
        session_id = str(uuid.uuid4())
        now = self._now()
        self._execute(
            """
            INSERT INTO sessions (id, title, created_at, updated_at, project_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, title, now, now, project_id),
        )
        logger.info("DB 创建会话：session_id=%s，title=%s", session_id, title)
        return self.get_session(session_id)

    def rename_session(self, session_id, title):
        self._execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, self._now(), session_id),
        )
        logger.info("DB 重命名会话：session_id=%s，title=%s", session_id, title)
        return self.get_session(session_id)

    def touch_session(self, session_id):
        self._execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (self._now(), session_id),
        )
        logger.debug("DB 更新会话时间：session_id=%s", session_id)

    def delete_session(self, session_id):
        with self._write() as conn:
            active = conn.execute("""
                SELECT id FROM runs WHERE session_id=?
                    AND status IN ('queued','running','waiting','paused') LIMIT 1
            """, (session_id,)).fetchone()
            if active:
                raise RunAlreadyActive(active["id"])
            has_task = conn.execute(
                "SELECT 1 FROM tasks WHERE session_id=? LIMIT 1", (session_id,)
            ).fetchone()
            if has_task:
                conn.execute(
                    "UPDATE sessions SET archived_at=?, updated_at=? WHERE id=?",
                    (_utc_now(), _utc_now(), session_id),
                )
            else:
                conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        logger.info("DB 删除会话：session_id=%s", session_id)

    def assign_session_to_project(self, session_id, project_id):
        if not project_id:
            project_id = self._get_or_create_default_project()["id"]
        self._execute(
            "UPDATE sessions SET project_id = ?, updated_at = ? WHERE id = ?",
            (project_id, self._now(), session_id),
        )
        logger.info(
            "DB 会话归入项目：session_id=%s，project_id=%s",
            session_id,
            project_id,
        )

    def list_projects(self):
        return self._query_all("""
            SELECT
                p.id,
                p.name,
                p.created_at,
                p.updated_at,
                p.is_default,
                (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) AS session_count
            FROM projects p
            ORDER BY p.updated_at DESC
        """)

    def _get_or_create_default_project(self):
        project = self._query_one(
            """
            SELECT id, name, created_at, updated_at, is_default
            FROM projects
            WHERE is_default = 1
            """
        )
        if not project:
            raise RuntimeError("默认项目不存在")
        return project

    def get_project(self, project_id):
        return self._query_one(
            "SELECT id, name, created_at, updated_at, is_default FROM projects WHERE id = ?",
            (project_id,),
        )

    def create_project(self, name):
        project_id = str(uuid.uuid4())
        now = self._now()
        self._execute(
            "INSERT INTO projects (id, name, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (project_id, name, now, now),
        )
        logger.info("DB 创建项目：project_id=%s，name=%s", project_id, name)
        return self.get_project(project_id)

    def rename_project(self, project_id, name):
        self._execute(
            "UPDATE projects SET name = ?, updated_at = ? WHERE id = ?",
            (name, self._now(), project_id),
        )
        return self.get_project(project_id)

    def delete_project(self, project_id):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                project = self._conn.execute(
                    "SELECT is_default FROM projects WHERE id = ?", (project_id,)
                ).fetchone()
                if not project:
                    self._conn.rollback()
                    return False
                if project["is_default"]:
                    raise ValueError("默认项目不能删除")
                default_project = self._conn.execute(
                    "SELECT id FROM projects WHERE is_default = 1"
                ).fetchone()
                if not default_project:
                    raise RuntimeError("默认项目不存在")
                self._conn.execute(
                    "UPDATE sessions SET project_id = ? WHERE project_id = ?",
                    (default_project["id"], project_id),
                )
                self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        logger.info("DB 删除项目：project_id=%s", project_id)
        return True

    def register_upload(self, file_id, filename, stored_path, size, mime_type, parsed):
        parsed_json = (
            json.dumps(parsed, ensure_ascii=False, default=str) if parsed else None
        )
        self._execute(
            """
            INSERT INTO uploaded_files
                (id, filename, stored_path, size, mime_type, parsed_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                file_id,
                filename,
                str(stored_path),
                size,
                mime_type,
                parsed_json,
                self._now(),
            ),
        )
        logger.info("DB 上传文件注册：file_id=%s，filename=%s", file_id, filename)

    def get_upload(self, file_id):
        return self._query_one(
            """
            SELECT id, filename, stored_path, size, mime_type, parsed_json, created_at
            FROM uploaded_files
            WHERE id = ?
            """,
            (file_id,),
        )

    def list_mcp_servers(self):
        return self._query_all("""
            SELECT id, name, transport, command, args_json, url, enabled, created_at, updated_at
            FROM mcp_servers
            ORDER BY created_at DESC
        """)

    def get_mcp_server(self, server_id):
        return self._query_one(
            """
            SELECT id, name, transport, command, args_json, url, enabled, created_at, updated_at
            FROM mcp_servers
            WHERE id = ?
            """,
            (server_id,),
        )

    def create_mcp_server(
        self,
        name,
        transport,
        command=None,
        args=None,
        url=None,
        enabled=True,
    ):
        server_id = str(uuid.uuid4())
        now = self._now()
        args_json = json.dumps(args or [], ensure_ascii=False)
        self._execute(
            """
            INSERT INTO mcp_servers
                (id, name, transport, command, args_json, url, enabled, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                server_id,
                name,
                transport,
                command,
                args_json,
                url,
                1 if enabled else 0,
                now,
                now,
            ),
        )
        logger.info("DB 创建 MCP Server：server_id=%s，name=%s", server_id, name)
        return self.get_mcp_server(server_id)

    def delete_mcp_server(self, server_id):
        self._execute("DELETE FROM mcp_servers WHERE id = ?", (server_id,))
        logger.info("DB 删除 MCP Server：server_id=%s", server_id)

    def get_messages(self, session_id):
        rows = self._query_all(
            """
            SELECT role, content, tool_calls_json, tool_call_id
            FROM messages
            WHERE session_id = ?
            ORDER BY sequence
            """,
            (session_id,),
        )
        messages = []
        for row in rows:
            message = {
                "role": row["role"],
                "content": row["content"],
            }
            if row["tool_calls_json"]:
                message["tool_calls"] = json.loads(row["tool_calls_json"])
            if row["tool_call_id"]:
                message["tool_call_id"] = row["tool_call_id"]
            messages.append(message)
        logger.info("DB 读取消息输出：session_id=%s，messages=%d", session_id, len(messages))
        return messages

    def add_message(
        self,
        session_id,
        role,
        content=None,
        tool_calls=None,
        tool_call_id=None,
    ):
        tool_calls_json = (
            json.dumps(tool_calls, ensure_ascii=False, default=str) if tool_calls else None
        )
        now = self._now()
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                next_sequence = self._conn.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM messages
                    WHERE session_id = ?
                    """,
                    (session_id,),
                ).fetchone()[0]
                session = self._conn.execute(
                    "SELECT archived_at FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
                if not session or session["archived_at"] is not None:
                    raise ResourceNotFound("Agent 不存在或已归档")
                self._conn.execute(
                    """
                    INSERT INTO messages
                        (session_id, role, content, tool_calls_json, tool_call_id,
                         sequence, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        session_id,
                        role,
                        content,
                        tool_calls_json,
                        tool_call_id,
                        next_sequence,
                        now,
                    ),
                )
                self._conn.execute(
                    "UPDATE sessions SET updated_at = ? WHERE id = ?",
                    (now, session_id),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        logger.debug(
            "DB 写入消息：session_id=%s，sequence=%d，role=%s，tool_call_id=%s",
            session_id,
            next_sequence,
            role,
            tool_call_id,
        )

    def add_audit_log(
        self,
        action,
        tool_name,
        arguments=None,
        result=None,
        ok=True,
    ):
        """写入一条审计记录。"""
        arguments_json = (
            json.dumps(arguments, ensure_ascii=False, default=str) if arguments else None
        )
        result_json = (
            json.dumps(result, ensure_ascii=False, default=str) if result else None
        )
        self._execute(
            """
            INSERT INTO audit_logs
                (created_at, action, tool_name, arguments_json, result_json, ok)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                self._now(),
                action,
                tool_name,
                arguments_json,
                result_json,
                1 if ok else 0,
            ),
        )
        logger.info(
            "DB 审计写入：action=%s，tool=%s，ok=%s",
            action,
            tool_name,
            ok,
        )

    def count_audit_logs(self):
        return self._query_one("SELECT COUNT(*) AS count FROM audit_logs")["count"]

    def get_audit_logs(self, limit=50):
        return self._query_all(
            """
            SELECT created_at, action, tool_name, arguments_json, result_json, ok
            FROM audit_logs
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        )

    # P1 persistent Run kernel. agent_id is the existing sessions.id.
    @staticmethod
    def _insert_message_tx(conn, session_id, role, content=None, *, task_id=None,
                           run_id=None, tool_calls=None, tool_call_id=None):
        session = conn.execute(
            "SELECT archived_at FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if not session or session["archived_at"] is not None:
            raise ResourceNotFound("Agent 不存在或已归档")
        sequence = conn.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM messages WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
        now = _utc_now()
        cursor = conn.execute("""
            INSERT INTO messages
                (session_id,task_id,run_id,role,content,tool_calls_json,
                 tool_call_id,sequence,created_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (session_id, task_id, run_id, role, content,
              _json(tool_calls) if tool_calls is not None else None,
              tool_call_id, sequence, now))
        conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session_id))
        return cursor.lastrowid

    @staticmethod
    def _task_values(goal, acceptance, context):
        if not isinstance(goal, str) or not 1 <= len(goal.strip()) <= 32768:
            raise ValueError("goal 必须为 1..32768 字符")
        if acceptance is None:
            acceptance = []
        if context is None:
            context = {}
        if not isinstance(acceptance, list) or not isinstance(context, dict):
            raise ValueError("acceptance 必须为数组，context 必须为对象")
        return goal.strip(), _json(acceptance), _json(context)

    def _insert_task_tx(self, conn, agent_id, goal, acceptance, context):
        goal, acceptance_json, context_json = self._task_values(
            goal, acceptance, context
        )
        if not conn.execute(
            "SELECT 1 FROM sessions WHERE id=? AND archived_at IS NULL", (agent_id,)
        ).fetchone():
            raise ResourceNotFound("Agent 不存在")
        task_id, now = str(uuid.uuid4()), _utc_now()
        conn.execute("""
            INSERT INTO tasks
                (id,session_id,goal,acceptance_json,context_json,status,
                 created_at,updated_at)
            VALUES (?,?,?,?,?,'open',?,?)
        """, (task_id, agent_id, goal, acceptance_json, context_json, now, now))
        self._insert_message_tx(conn, agent_id, "user", goal, task_id=task_id)
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def create_task(self, agent_id, goal, acceptance=None, context=None):
        with self._write() as conn:
            return self._insert_task_tx(conn, agent_id, goal, acceptance, context)

    def get_task(self, task_id):
        return self._query_one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def list_tasks(self, agent_id=None, status=None, limit=50):
        if status is not None and status not in {"open", "completed", "failed", "cancelled"}:
            raise ValueError("无效 Task 状态")
        if not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1..100")
        return self._query_all("""
            SELECT * FROM tasks
            WHERE (? IS NULL OR session_id=?) AND (? IS NULL OR status=?)
            ORDER BY created_at DESC,id DESC LIMIT ?
        """, (agent_id, agent_id, status, status, limit))

    def get_latest_run_id(self, task_id):
        row = self._query_one(
            "SELECT id FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1", (task_id,)
        )
        return row["id"] if row else None

    @staticmethod
    def _request_hash(session_id, task_id, options):
        if not isinstance(options, dict):
            raise ValueError("Run options 必须为对象")
        return hashlib.sha256(_json({
            "session_id": session_id, "task_id": task_id, "options": options
        }).encode("utf-8")).hexdigest()

    def _default_run_snapshots_tx(self, conn):
        """Freeze safe metadata, never a Provider key or executable handler."""
        from backend.config import load_config
        from backend.tool_gateway import snapshot_builtin_tools

        cfg = load_config()
        settings = conn.execute("SELECT * FROM settings WHERE id=1").fetchone()
        settings = dict(settings) if settings else {}
        credential = settings.get("api_key_encrypted") or cfg.api_key
        config = {
            "v": 1,
            "base_url": settings.get("base_url") or cfg.base_url,
            "model": settings.get("model") or cfg.model,
            "max_rounds": settings.get("max_rounds") or cfg.max_rounds,
            "tool_timeout": settings.get("tool_timeout") or cfg.tool_timeout,
            "credential_fingerprint": hashlib.sha256(credential.encode()).hexdigest()
                if credential else None,
        }
        specs = snapshot_builtin_tools()
        tools = {name: {
            "schema": spec.schema,
            "effects": sorted(spec.effects),
            "path_fields": list(spec.path_fields),
            "idempotent": spec.idempotent,
            "approval": spec.approval,
            "timeout_seconds": spec.timeout_seconds,
            "origin": spec.origin,
        } for name, spec in specs.items()}
        return config, {"v": 1, "specs": tools, "grants": ["read_only"]}

    def _insert_run_tx(self, conn, task, idempotency_key, options,
                       config_snapshot, tool_snapshot, *, request_hash=None):
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 200:
            raise ValueError("Idempotency-Key 必须为 1..200 字符")
        key = idempotency_key.strip()
        fingerprint = request_hash or self._request_hash(task["session_id"], task["id"], options)
        existing = conn.execute(
            "SELECT * FROM runs WHERE session_id=? AND idempotency_key=?",
            (task["session_id"], key),
        ).fetchone()
        if existing:
            if existing["request_hash"] != fingerprint:
                raise IdempotencyKeyReused("Idempotency-Key 已用于不同请求")
            return dict(existing), False
        session = conn.execute(
            "SELECT archived_at FROM sessions WHERE id=?", (task["session_id"],)
        ).fetchone()
        if not session or session["archived_at"] is not None:
            raise ResourceNotFound("Agent 不存在或已归档")
        active = conn.execute("""
            SELECT id FROM runs WHERE session_id=? AND
                status IN ('queued','running','waiting','paused') LIMIT 1
        """, (task["session_id"],)).fetchone()
        if active:
            raise RunAlreadyActive(active["id"])
        retry = isinstance(options, dict) and options.get("retry") is True
        if task["status"] == "open" and retry:
            raise InvalidRunTransition("open Task 不需要 retry")
        if task["status"] != "open":
            if task["status"] not in {"failed", "cancelled"} or not retry:
                raise InvalidRunTransition("Task 已结束；仅 failed/cancelled 可显式 retry")
            conn.execute("""
                UPDATE tasks SET status='open',finished_at=NULL,result_json=NULL,
                    updated_at=? WHERE id=?
            """, (_utc_now(), task["id"]))
        if config_snapshot is None or tool_snapshot is None:
            frozen_config, frozen_tools = self._default_run_snapshots_tx(conn)
            config_snapshot = frozen_config if config_snapshot is None else config_snapshot
            tool_snapshot = frozen_tools if tool_snapshot is None else tool_snapshot
        cutoff = conn.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM messages WHERE session_id=?",
            (task["session_id"],),
        ).fetchone()[0]
        attempt = conn.execute(
            "SELECT COALESCE(MAX(attempt),0)+1 FROM runs WHERE task_id=?", (task["id"],)
        ).fetchone()[0]
        run_id, now = str(uuid.uuid4()), _utc_now()
        conn.execute("""
            INSERT INTO runs
                (id,task_id,session_id,attempt,idempotency_key,request_hash,status,
                 config_snapshot_json,tool_snapshot_json,context_max_sequence,
                 created_at,updated_at)
            VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?)
        """, (run_id, task["id"], task["session_id"], attempt, key, fingerprint,
              _json(config_snapshot), _json(tool_snapshot), cutoff, now, now))
        self._append_event_tx(conn, run_id, "run.queued", {"attempt": attempt},
                              dedupe_key=f"{run_id}:run.queued")
        return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), True

    def create_run(self, task_id, idempotency_key, options=None,
                   config_snapshot=None, tool_snapshot=None):
        with self._write() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ResourceNotFound("Task 不存在")
            return self._insert_run_tx(conn, task, idempotency_key,
                                       {} if options is None else options,
                                       config_snapshot, tool_snapshot)

    def create_legacy_chat_run(self, agent_id, goal, idempotency_key, options=None,
                               config_snapshot=None, tool_snapshot=None):
        options = {} if options is None else options
        if not isinstance(options, dict):
            raise ValueError("Run options 必须为对象")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 200:
            raise ValueError("Idempotency-Key 必须为 1..200 字符")
        goal, _, _ = self._task_values(goal, None, None)
        fingerprint = hashlib.sha256(_json({
            "session_id": agent_id, "goal": goal, "options": options,
        }).encode("utf-8")).hexdigest()
        with self._write() as conn:
            existing = conn.execute(
                "SELECT * FROM runs WHERE session_id=? AND idempotency_key=?",
                (agent_id, idempotency_key.strip()),
            ).fetchone()
            if existing:
                if existing["request_hash"] != fingerprint:
                    raise IdempotencyKeyReused("Idempotency-Key 已用于不同请求")
                task = conn.execute("SELECT * FROM tasks WHERE id=?", (existing["task_id"],)).fetchone()
                return dict(task), dict(existing)
            task = self._insert_task_tx(conn, agent_id, goal, None, None)
            run, _ = self._insert_run_tx(conn, task, idempotency_key, options,
                                         config_snapshot, tool_snapshot,
                                         request_hash=fingerprint)
            return task, run

    def get_run(self, run_id):
        return self._query_one("SELECT * FROM runs WHERE id=?", (run_id,))

    @staticmethod
    def _append_event_tx(conn, run_id, event_type, payload=None, dedupe_key=None,
                         *, terminal_reserve=False):
        if not isinstance(event_type, str) or not 1 <= len(event_type) <= 80:
            raise ValueError("无效 Event type")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("Event payload 必须为对象")
        run = conn.execute(
            "SELECT task_id,session_id,status,event_bytes_written FROM runs WHERE id=?",
            (run_id,),
        ).fetchone()
        if not run:
            raise ResourceNotFound("Run 不存在")
        if event_type in TERMINAL_EVENTS and run["status"] != event_type.removeprefix("run."):
            raise InvalidRunTransition("终态 Event 必须与 Run 状态同事务提交")
        if terminal_reserve and run["status"] not in {"completed", "failed", "cancelled"}:
            raise InvalidRunTransition("非终态 Event 不得使用终态预留额度")
        if dedupe_key is not None:
            existing = conn.execute(
                "SELECT * FROM events WHERE dedupe_key=?", (dedupe_key,)
            ).fetchone()
            if existing:
                if existing["run_id"] != run_id or existing["type"] != event_type or existing["payload_json"] != _json(payload):
                    raise InvalidRunTransition("Event dedupe key 冲突")
                return dict(existing)
        now = _utc_now()
        payload_json = _json(payload)
        cursor = conn.execute("""
            INSERT INTO events (task_id,run_id,type,dedupe_key,payload_json,created_at)
            VALUES (?,?,?,?,?,?)
        """, (run["task_id"], run_id, event_type, dedupe_key, payload_json, now))
        envelope = {
            "v": 1,
            "event_id": cursor.lastrowid,
            "task_id": run["task_id"],
            "run_id": run_id,
            "agent_id": run["session_id"],
            "type": event_type,
            "ts": now,
            "data": payload,
        }
        size = len(_json(envelope).encode("utf-8"))
        limit = (MAX_RUN_EVENT_BYTES if event_type in TERMINAL_EVENTS or terminal_reserve
                 else MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE)
        if size > MAX_EVENT_BYTES or run["event_bytes_written"] + size > limit:
            raise EventLimitExceeded("Event 超出持久化字节额度")
        conn.execute(
            "UPDATE runs SET event_bytes_written=event_bytes_written+? WHERE id=?",
            (size, run_id),
        )
        return dict(conn.execute("SELECT * FROM events WHERE id=?", (cursor.lastrowid,)).fetchone())

    def append_event(self, run_id, event_type, payload=None, dedupe_key=None):
        if isinstance(event_type, str) and event_type.startswith("run."):
            raise InvalidRunTransition("Run 状态 Event 必须与状态同事务提交")
        with self._write() as conn:
            return self._append_event_tx(conn, run_id, event_type, payload, dedupe_key)

    def list_events(self, run_id, after_id=0, limit=100):
        if not isinstance(after_id, int) or after_id < 0 or not 1 <= limit <= 1000:
            raise ValueError("无效 Event cursor/limit")
        return self._query_all("""
            SELECT * FROM events WHERE run_id=? AND id>?
            ORDER BY id LIMIT ?
        """, (run_id, after_id, limit))

    def get_last_event_id(self, run_id):
        row = self._query_one(
            "SELECT COALESCE(MAX(id),0) AS last_event_id FROM events WHERE run_id=?",
            (run_id,),
        )
        return row["last_event_id"]

    def get_tool_start(self, run_id, call_id):
        return self._query_one("""
            SELECT * FROM events WHERE run_id=? AND type='tool.started'
                AND json_extract(payload_json,'$.call_id')=?
            ORDER BY id DESC LIMIT 1
        """, (run_id, call_id))

    def get_message(self, message_id):
        return self._query_one("SELECT * FROM messages WHERE id=?", (message_id,))

    @staticmethod
    def _lease_deadline(seconds):
        if not 1 <= seconds <= 3600:
            raise ValueError("lease_seconds 必须为 1..3600")
        return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(
            timespec="milliseconds"
        ).replace("+00:00", "Z")

    def claim_next_run(self, lease_owner, lease_seconds=30):
        if not isinstance(lease_owner, str) or not lease_owner:
            raise ValueError("lease_owner 不能为空")
        deadline = self._lease_deadline(lease_seconds)
        with self._write() as conn:
            candidate = conn.execute("""
                SELECT * FROM runs WHERE status='queued'
                ORDER BY created_at,id LIMIT 1
            """).fetchone()
            if not candidate:
                return None
            now = _utc_now()
            checkpoint = candidate["checkpoint_json"] or _json({
                "v": 1, "phase": "model_pending", "round": 1
            })
            conn.execute("SAVEPOINT claim_one")
            updated = conn.execute("""
                UPDATE runs SET status='running', version=version+1,
                    lease_epoch=lease_epoch+1, lease_owner=?, lease_until=?,
                    checkpoint_json=?, started_at=COALESCE(started_at,?),
                    heartbeat_at=?, updated_at=?
                WHERE id=? AND status='queued' AND version=?
            """, (lease_owner, deadline, checkpoint, now, now, now,
                  candidate["id"], candidate["version"]))
            if updated.rowcount != 1:
                conn.execute("ROLLBACK TO claim_one")
                conn.execute("RELEASE claim_one")
                return None
            try:
                self._append_event_tx(conn, candidate["id"], "run.started",
                                      {"lease_epoch": candidate["lease_epoch"] + 1},
                                      f"{candidate['id']}:run.started:{candidate['lease_epoch'] + 1}")
            except EventLimitExceeded:
                conn.execute("ROLLBACK TO claim_one")
                conn.execute("RELEASE claim_one")
                conn.execute("""
                    UPDATE runs SET status='running',version=version+1,
                        lease_epoch=lease_epoch+1,lease_owner=?,lease_until=?,
                        checkpoint_json=?,started_at=COALESCE(started_at,?),
                        heartbeat_at=?,updated_at=?
                    WHERE id=? AND status='queued' AND version=?
                """, (lease_owner, deadline, checkpoint, now, now, now,
                      candidate["id"], candidate["version"]))
                conn.execute("""
                    UPDATE runs SET status='failed',version=version+1,
                        lease_owner=NULL,lease_until=NULL,error_json=?,
                        finished_at=?,updated_at=?
                    WHERE id=? AND status='running'
                """, (_json({"code": "event_limit_exceeded"}), now, now,
                      candidate["id"]))
                self._append_event_tx(conn, candidate["id"], "run.failed",
                                      {"code": "event_limit_exceeded"},
                                      f"{candidate['id']}:run.failed")
                conn.execute("""
                    UPDATE tasks SET status='failed',finished_at=?,updated_at=?
                    WHERE id=? AND status='open'
                """, (now, now, candidate["task_id"]))
                return None
            conn.execute("RELEASE claim_one")
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (candidate["id"],)).fetchone())

    def heartbeat_run(self, run_id, lease_owner, lease_epoch, expected_version,
                      lease_seconds=30):
        deadline = self._lease_deadline(lease_seconds)
        with self._write() as conn:
            now = _utc_now()
            updated = conn.execute("""
                UPDATE runs SET lease_until=?,heartbeat_at=?,updated_at=?,version=version+1
                WHERE id=? AND status='running' AND lease_owner=?
                    AND lease_epoch=? AND version=? AND lease_until>?
            """, (deadline, now, now, run_id, lease_owner, lease_epoch,
                  expected_version, now))
            if updated.rowcount != 1:
                raise InvalidRunTransition("Run lease/version 已变化")
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def transition_run(self, run_id, expected_version, status, event_type,
                       payload=None, *, checkpoint=None, message=None,
                       lease_owner=None, lease_epoch=None, wait_reason=None,
                       error=None, clear_deltas=False):
        """One CAS commit for status, checkpoint, message, and semantic events."""
        if status in {"completed", "failed", "cancelled"} and event_type != f"run.{status}":
            raise InvalidRunTransition("终态 Event type 不匹配")
        if status not in {"completed", "failed", "cancelled"} and event_type in TERMINAL_EVENTS:
            raise InvalidRunTransition("非终态不可写终态 Event")
        if checkpoint is not None and len(_json(checkpoint).encode()) > MAX_EVENT_BYTES:
            raise ValueError("checkpoint 超过 64 KiB")
        with self._write() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise ResourceNotFound("Run 不存在")
            if run["version"] != expected_version:
                raise InvalidRunTransition("Run version 已变化")
            if run["status"] == "running" and (
                lease_owner != run["lease_owner"] or lease_epoch != run["lease_epoch"]
                or run["lease_until"] <= _utc_now()
            ):
                raise InvalidRunTransition("Run lease 已变化")
            if status == "running" and run["status"] != "running":
                raise InvalidRunTransition("只能由 claim 进入 running")
            now = _utc_now()
            terminal = status in {"completed", "failed", "cancelled"}
            updated = conn.execute("""
                UPDATE runs SET status=?,wait_reason=?,
                    control_requested=CASE WHEN ?='running' THEN control_requested ELSE NULL END,
                    version=version+1,lease_owner=?,lease_until=?,
                    checkpoint_json=COALESCE(?,checkpoint_json),
                    error_json=COALESCE(?,error_json),updated_at=?,
                    finished_at=CASE WHEN ? THEN ? ELSE finished_at END
                WHERE id=? AND version=?
            """, (status, wait_reason, status,
                  run["lease_owner"] if status == "running" else None,
                  run["lease_until"] if status == "running" else None,
                  _json(checkpoint) if checkpoint is not None else None,
                  _json(error) if error is not None else None,
                  now, terminal, now, run_id, expected_version))
            if updated.rowcount != 1:
                raise InvalidRunTransition("Run version 已变化")
            message_id = None
            if message is not None:
                if not isinstance(message, dict):
                    raise ValueError("message 必须为对象")
                message_id = self._insert_message_tx(
                    conn, run["session_id"], message.get("role"), message.get("content"),
                    task_id=run["task_id"], run_id=run_id,
                    tool_calls=message.get("tool_calls"),
                    tool_call_id=message.get("tool_call_id"),
                )
                if checkpoint is not None and "assistant_message_id" in checkpoint and checkpoint["assistant_message_id"] is None:
                    checkpoint = {**checkpoint, "assistant_message_id": message_id}
                    conn.execute("UPDATE runs SET checkpoint_json=? WHERE id=?",
                                 (_json(checkpoint), run_id))
                content = message.get("content") or ""
                self._append_event_tx(conn, run_id, "message.completed", {
                    "message_id": message_id,
                    "hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "length": len(content),
                }, f"{run_id}:message.completed:{message_id}",
                    terminal_reserve=terminal)
            self._append_event_tx(conn, run_id, event_type, payload,
                                  f"{run_id}:{event_type}" if terminal else None)
            if clear_deltas:
                if ((message is None or message.get("role") != "assistant") and
                        status not in {"paused", "cancelled", "failed"}):
                    raise InvalidRunTransition("只能在完整消息或步骤终止时压缩 delta")
                conn.execute("DELETE FROM events WHERE run_id=? AND type='model.output.delta'",
                             (run_id,))
            if terminal:
                conn.execute("""
                    UPDATE tasks SET status=?,finished_at=?,updated_at=?
                    WHERE id=? AND status='open'
                """, (status, now, now, run["task_id"]))
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), message_id

    def start_tool_call(self, run_id, expected_version, lease_owner, lease_epoch,
                        call_id, name, effect, action_hash, min_result_bytes=4096):
        """Durable pre-invocation marker; budget the uncertain result before side effects."""
        if (not 4096 <= min_result_bytes <= MAX_EVENT_BYTES or
                not isinstance(call_id, str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", call_id) or
                not isinstance(name, str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name) or
                effect not in {"read_only", "effectful"} or
                not isinstance(action_hash, str) or
                not re.fullmatch(r"[0-9a-f]{64}", action_hash)):
            raise ValueError("无效工具启动参数")
        with self._write() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise ResourceNotFound("Run 不存在")
            if (run["status"] != "running" or run["version"] != expected_version or
                    run["lease_owner"] != lease_owner or run["lease_epoch"] != lease_epoch or
                    run["lease_until"] <= _utc_now() or run["control_requested"]):
                raise InvalidRunTransition("Run lease/version/control 已变化")
            started = conn.execute("""
                SELECT id FROM events WHERE run_id=? AND type='tool.started'
                    AND json_extract(payload_json,'$.call_id')=?
                ORDER BY id DESC LIMIT 1
            """, (run_id, call_id)).fetchone()
            if started:
                raise InvalidRunTransition("tool_call_id 已启动；必须先核实结果")
            self._append_event_tx(conn, run_id, "tool.started", {
                "call_id": call_id, "name": name, "effect": effect,
                "action_hash": action_hash,
            }, f"{run_id}:{call_id}:tool.started")
            used = conn.execute("SELECT event_bytes_written FROM runs WHERE id=?",
                                (run_id,)).fetchone()[0]
            if used + min_result_bytes > MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE:
                raise EventLimitExceeded("工具结果 Event 空间不足，拒绝调用")
            now = _utc_now()
            conn.execute("UPDATE runs SET version=version+1,updated_at=? WHERE id=?",
                         (now, run_id))
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())

    def recover_expired_leases(self, now=None):
        """Startup scan pauses, never re-executes an uncertain tool."""
        now = now or _utc_now()
        recovered = []
        with self._write() as conn:
            rows = conn.execute("""
                SELECT id,task_id,version,lease_epoch FROM runs
                WHERE status='running' AND lease_until<=?
                ORDER BY lease_until,id
            """, (now,)).fetchall()
            for row in rows:
                conn.execute("SAVEPOINT recovery_one")
                try:
                    updated = conn.execute("""
                        UPDATE runs SET status='paused',wait_reason='restart',
                            control_requested=NULL,lease_owner=NULL,lease_until=NULL,
                            version=version+1,lease_epoch=lease_epoch+1,updated_at=?
                        WHERE id=? AND status='running' AND version=?
                    """, (now, row["id"], row["version"]))
                    if updated.rowcount != 1:
                        conn.execute("RELEASE recovery_one")
                        continue
                    self._append_event_tx(conn, row["id"], "run.paused", {"reason": "restart"},
                                          f"{row['id']}:run.paused:{row['lease_epoch'] + 1}")
                except EventLimitExceeded:
                    # A nonterminal pause cannot borrow the 8 KiB terminal reserve.
                    conn.execute("ROLLBACK TO recovery_one")
                    conn.execute("RELEASE recovery_one")
                    unresolved = conn.execute("""
                        SELECT 1 FROM events AS started
                        WHERE started.run_id=? AND started.type='tool.started'
                            AND json_extract(started.payload_json,'$.effect')='effectful'
                            AND NOT EXISTS (
                                SELECT 1 FROM messages AS result
                                WHERE result.run_id=started.run_id AND result.role='tool'
                                    AND result.tool_call_id=
                                        json_extract(started.payload_json,'$.call_id'))
                        LIMIT 1
                    """, (row["id"],)).fetchone()
                    if unresolved:
                        # An exhausted nonterminal quota cannot justify false failure.
                        # The expired lease remains, and no worker can reclaim it blindly.
                        continue
                    conn.execute("""
                        UPDATE runs SET status='failed',wait_reason=NULL,
                            control_requested=NULL,lease_owner=NULL,lease_until=NULL,
                            version=version+1,lease_epoch=lease_epoch+1,
                            error_json=?,finished_at=?,updated_at=?
                        WHERE id=? AND status='running' AND version=?
                    """, (_json({"code": "event_limit_exceeded"}), now, now,
                          row["id"], row["version"]))
                    self._append_event_tx(conn, row["id"], "run.failed",
                                          {"code": "event_limit_exceeded"},
                                          f"{row['id']}:run.failed")
                    conn.execute("""
                        UPDATE tasks SET status='failed',finished_at=?,updated_at=?
                        WHERE id=? AND status='open'
                    """, (now, now, row["task_id"]))
                else:
                    conn.execute("RELEASE recovery_one")
                recovered.append(row["id"])
        return recovered

    def create_approval(self, run_id, tool_call_id, kind, action_hash,
                        request_summary, expires_at):
        if kind not in {"pre_execution", "resolve_unknown_outcome"}:
            raise ValueError("无效 Approval kind")
        if not isinstance(tool_call_id, str) or not tool_call_id or not action_hash:
            raise ValueError("tool_call_id/action_hash 不能为空")
        if not isinstance(request_summary, dict) or not isinstance(expires_at, str):
            raise ValueError("无效 Approval 请求")
        summary_json = _json(request_summary)
        if len(summary_json.encode()) > MAX_EVENT_BYTES:
            raise ValueError("Approval 摘要过大")
        with self._write() as conn:
            if not conn.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise ResourceNotFound("Run 不存在")
            existing = conn.execute("""
                SELECT * FROM approvals WHERE run_id=? AND tool_call_id=?
                    AND kind=? AND action_hash=?
            """, (run_id, tool_call_id, kind, action_hash)).fetchone()
            if existing:
                if existing["request_summary_json"] != summary_json:
                    raise InvalidRunTransition("Approval action_hash 摘要冲突")
                return dict(existing)
            approval_id, now = str(uuid.uuid4()), _utc_now()
            conn.execute("""
                INSERT INTO approvals
                    (id,run_id,tool_call_id,kind,action_hash,request_summary_json,
                     status,expires_at,created_at)
                VALUES (?,?,?,?,?,?,'pending',?,?)
            """, (approval_id, run_id, tool_call_id, kind, action_hash,
                  summary_json, expires_at, now))
            return dict(conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())

    def get_approval(self, approval_id):
        return self._query_one("SELECT * FROM approvals WHERE id=?", (approval_id,))

    def list_approvals(self, run_id=None, status="pending", limit=50):
        if status not in {"pending", "resolved", "expired", None} or not 1 <= limit <= 100:
            raise ValueError("无效 Approval 查询")
        return self._query_all("""
            SELECT * FROM approvals
            WHERE (? IS NULL OR run_id=?) AND (? IS NULL OR status=?)
            ORDER BY created_at,id LIMIT ?
        """, (run_id, run_id, status, status, limit))

    def decide_approval(self, approval_id, decision, decided_by="local-host", note=None):
        allowed = {
            "pre_execution": {"approve", "deny"},
            "resolve_unknown_outcome": {"occurred", "not_occurred", "explicit_retry", "cancel"},
        }
        if note is not None and (not isinstance(note, str) or len(note) > 2000):
            raise ValueError("Approval note 过长")
        if not isinstance(decided_by, str) or not decided_by:
            raise ValueError("decided_by 不能为空")
        with self._write() as conn:
            row = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if not row:
                raise ResourceNotFound("Approval 不存在")
            if decision not in allowed[row["kind"]]:
                raise ValueError("该 Approval 不接受此 decision")
            decision_json = _json({"decision": decision, "note": note})
            if row["status"] == "resolved" and row["decision_json"] == decision_json:
                return dict(row)
            if row["status"] != "pending" or row["expires_at"] <= _utc_now():
                raise InvalidRunTransition("Approval 已决定或已过期")
            now = _utc_now()
            updated = conn.execute("""
                UPDATE approvals SET status='resolved',decided_at=?,decided_by=?,
                    decision_json=? WHERE id=? AND status='pending'
            """, (now, decided_by, decision_json, approval_id))
            if updated.rowcount != 1:
                raise InvalidRunTransition("Approval 已决定")
            # Decision is evidence only. In particular, explicit_retry never calls a tool.
            return dict(conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())

    def expire_approvals(self, now=None):
        now = now or _utc_now()
        with self._write() as conn:
            return conn.execute("""
                UPDATE approvals SET status='expired'
                WHERE status='pending' AND expires_at<=?
            """, (now,)).rowcount

    def record_unknown_outcome(self, run_id, expected_version, lease_owner,
                               lease_epoch, outcome, tool_name, action_hash,
                               expires_at):
        """Persist uncertainty and independent resolution request; never retry."""
        if not (outcome.status == "outcome_unknown" or
                (outcome.status == "timeout" and outcome.effect == "effectful"
                 and not outcome.quiesced)):
            raise ValueError("ToolOutcome 不是未知副作用")
        if not isinstance(action_hash, str) or len(action_hash) != 64 or any(
            char not in "0123456789abcdef" for char in action_hash
        ):
            raise ValueError("action_hash 必须为 SHA-256 hex")
        if not isinstance(tool_name, str) or not tool_name or not outcome.call_id:
            raise ValueError("tool_name/call_id 不能为空")
        with self._write() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise ResourceNotFound("Run 不存在")
            if (run["status"] != "running" or run["version"] != expected_version or
                    run["lease_owner"] != lease_owner or run["lease_epoch"] != lease_epoch or
                    run["lease_until"] <= _utc_now()):
                raise InvalidRunTransition("Run lease/version 已变化")
            now, approval_id = _utc_now(), str(uuid.uuid4())
            summary = _json({"tool_name": tool_name, "effect": outcome.effect,
                             "error_code": outcome.error_code})
            conn.execute("""
                INSERT INTO approvals
                    (id,run_id,tool_call_id,kind,action_hash,request_summary_json,
                     status,expires_at,created_at)
                VALUES (?,?,?,'resolve_unknown_outcome',?,?,'pending',?,?)
            """, (approval_id, run_id, outcome.call_id, action_hash,
                  summary, expires_at, now))
            updated = conn.execute("""
                UPDATE runs SET status='waiting',wait_reason='approval',
                    control_requested=NULL,lease_owner=NULL,lease_until=NULL,
                    version=version+1,updated_at=?
                WHERE id=? AND status='running' AND version=?
            """, (now, run_id, expected_version))
            if updated.rowcount != 1:
                raise InvalidRunTransition("Run version 已变化")
            self._append_event_tx(conn, run_id, "tool.outcome_unknown", {
                "call_id": outcome.call_id, "name": tool_name,
                "effect": outcome.effect, "error_code": outcome.error_code,
                "approval_id": approval_id,
            }, f"{run_id}:{outcome.call_id}:tool.outcome_unknown")
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), dict(
                conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone())

    def request_control(self, run_id, action, expected_version=None):
        """Persist intent under CAS; the worker honors active controls at a safe point."""
        if action not in {"pause", "resume", "cancel"}:
            raise ValueError("无效 Run 控制动作")
        with self._write() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise ResourceNotFound("Run 不存在")
            if expected_version is not None and run["version"] != expected_version:
                raise InvalidRunTransition("Run version 已变化")
            old = run["status"]
            if old == "running" and action in {"pause", "cancel"}:
                if run["control_requested"]:
                    raise InvalidRunTransition("Run 已有控制请求")
                conn.execute("""
                    UPDATE runs SET control_requested=?,version=version+1,updated_at=?
                    WHERE id=? AND status='running' AND version=?
                """, (action, _utc_now(), run_id, run["version"]))
                self._append_event_tx(conn, run_id, "run.control_requested",
                                      {"action": action})
            elif old == "paused" and action == "resume":
                conn.execute("""
                    UPDATE runs SET status='queued',wait_reason=NULL,version=version+1,
                        updated_at=? WHERE id=? AND status='paused' AND version=?
                """, (_utc_now(), run_id, run["version"]))
                self._append_event_tx(conn, run_id, "run.resumed", {})
            elif old in {"queued", "paused"} and action == "cancel":
                now = _utc_now()
                conn.execute("""
                    UPDATE runs SET status='cancelled',wait_reason=NULL,version=version+1,
                        updated_at=?,finished_at=?
                    WHERE id=? AND status=? AND version=?
                """, (now, now, run_id, old, run["version"]))
                self._append_event_tx(conn, run_id, "run.cancelled",
                                      {"reason": "user_request"})
                conn.execute("""
                    UPDATE tasks SET status='cancelled',finished_at=?,updated_at=?
                    WHERE id=? AND status='open'
                """, (now, now, run["task_id"]))
            else:
                raise InvalidRunTransition("Run 状态不允许此控制动作")
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())


class _LazyDatabase:
    """Keep production migration out of module import; lifespan opens it explicitly."""

    def __init__(self):
        self._instance = None
        self._lock = threading.Lock()

    def initialize(self):
        with self._lock:
            if self._instance is None:
                self._instance = Database()
            return self._instance

    def close(self):
        with self._lock:
            instance, self._instance = self._instance, None
        if instance is not None:
            instance.close()

    def __getattr__(self, name):
        return getattr(self.initialize(), name)


db = _LazyDatabase()
