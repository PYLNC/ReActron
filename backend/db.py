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
SCHEMA_VERSION = 5
MAX_EVENT_BYTES = 64 * 1024
MAX_RUN_EVENT_BYTES = 4 * 1024 * 1024
TERMINAL_EVENT_RESERVE = 8 * 1024
CHILD_RECEIPT_RESERVE = 4096
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


class WorkspaceUnavailable(InvalidRunTransition):
    pass


class EventLimitExceeded(RepositoryError):
    code = "event_limit_exceeded"


class SupervisorAlreadyExists(RepositoryError):
    code = "supervisor_exists"


class AgentHierarchyError(RepositoryError):
    code = "invalid_agent_hierarchy"


HOST_GRANTS = frozenset({
    "read_only", "control.delegate_task", "control.send_agent_message",
    "control.get_task_status", "control.wait_for_tasks", "control.cancel_task",
    "control.request_human_approval", "filesystem.read", "filesystem.write",
})


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
                    f"{self.db_path.name}.pre-v{SCHEMA_VERSION}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.bak"
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
            if version == 3 and SCHEMA_VERSION >= 4:
                self._migrate_3_to_4()
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 4 and SCHEMA_VERSION >= 5:
                self._migrate_4_to_5()
                version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise RuntimeError(f"不支持的数据库版本：{version}")
            if version >= 5:
                self._conn.execute("""
                    CREATE TRIGGER IF NOT EXISTS trg_task_terminal_retain_checkout
                    AFTER UPDATE OF status ON tasks
                    WHEN OLD.status='open' AND NEW.status<>'open'
                    BEGIN
                        UPDATE worktree_checkouts SET state='retained',updated_at=NEW.updated_at
                        WHERE task_id=NEW.id AND state='active';
                    END
                """)
                self._conn.commit()
            self._verify_integrity()

    def _migrate_0_to_1(self):
        """把无版本旧库原子迁移到 P0 一致性 schema。"""
        conn = self._conn
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            # 另一个进程可能已在等待 writer lock 时完成同一迁移。
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version in (1, 2, 3, 4, SCHEMA_VERSION):
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
            if version in (2, 3, 4, SCHEMA_VERSION):
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
            if version in (3, 4, SCHEMA_VERSION):
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

    def _migrate_3_to_4(self):
        """P2 identity, delegation provenance, and immutable grant snapshots."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version in (4, SCHEMA_VERSION):
                conn.commit()
                return
            if version != 3:
                raise RuntimeError(f"不支持的数据库版本：{version}")
            for sql in (
                "ALTER TABLE sessions ADD COLUMN agent_role TEXT CHECK (agent_role IN ('supervisor','worker'))",
                "ALTER TABLE sessions ADD COLUMN parent_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT",
                "ALTER TABLE sessions ADD COLUMN root_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT",
                "ALTER TABLE sessions ADD COLUMN depth INTEGER NOT NULL DEFAULT 0 CHECK (depth BETWEEN 0 AND 2)",
                "ALTER TABLE sessions ADD COLUMN system_prompt TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE sessions ADD COLUMN capabilities_json TEXT NOT NULL DEFAULT '{\"grants\":[]}'",
                "ALTER TABLE sessions ADD COLUMN can_delegate INTEGER NOT NULL DEFAULT 0 CHECK (can_delegate IN (0,1))",
                "ALTER TABLE tasks ADD COLUMN creator_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT",
                "ALTER TABLE tasks ADD COLUMN creator_run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT",
                "ALTER TABLE tasks ADD COLUMN parent_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT",
                "ALTER TABLE tasks ADD COLUMN requested_grant_json TEXT NOT NULL DEFAULT '{\"grants\":[\"read_only\"]}'",
                "ALTER TABLE tasks ADD COLUMN limits_json TEXT NOT NULL DEFAULT '{}'",
                "ALTER TABLE runs ADD COLUMN effective_grant_json TEXT NOT NULL DEFAULT '{\"grants\":[\"read_only\"]}'",
                "ALTER TABLE messages ADD COLUMN sender_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT",
                "ALTER TABLE messages ADD COLUMN related_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT",
                "ALTER TABLE messages ADD COLUMN source_run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT",
                "ALTER TABLE messages ADD COLUMN recipient_run_id TEXT REFERENCES runs(id) ON DELETE RESTRICT",
                "ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat','delegation','question','progress','result','reply'))",
                "ALTER TABLE messages ADD COLUMN hop INTEGER NOT NULL DEFAULT 0 CHECK (hop BETWEEN 0 AND 2)",
                "ALTER TABLE events ADD COLUMN actor_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT",
                "ALTER TABLE events ADD COLUMN recipient_session_id TEXT REFERENCES sessions(id) ON DELETE RESTRICT",
            ):
                conn.execute(sql)
            conn.execute("""
                CREATE UNIQUE INDEX uq_one_active_supervisor ON sessions(agent_role)
                WHERE agent_role='supervisor' AND archived_at IS NULL
            """)
            conn.execute("CREATE INDEX idx_agent_parent ON sessions(parent_session_id,archived_at)")
            conn.execute("CREATE INDEX idx_tasks_parent ON tasks(parent_task_id,created_at)")
            conn.execute("CREATE INDEX idx_messages_recipient_run ON messages(recipient_run_id,id)")
            conn.execute("""
                CREATE TRIGGER trg_supervisor_no_archive BEFORE UPDATE OF archived_at ON sessions
                WHEN OLD.agent_role='supervisor' AND NEW.archived_at IS NOT NULL
                BEGIN SELECT RAISE(ABORT,'supervisor cannot be archived'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_supervisor_no_delete BEFORE DELETE ON sessions
                WHEN OLD.agent_role='supervisor'
                BEGIN SELECT RAISE(ABORT,'supervisor cannot be deleted'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_agent_identity_immutable
                BEFORE UPDATE OF agent_role,parent_session_id,root_session_id,depth ON sessions
                WHEN OLD.agent_role IS NOT NULL AND (
                    NEW.agent_role IS NOT OLD.agent_role OR
                    NEW.parent_session_id IS NOT OLD.parent_session_id OR
                    NEW.root_session_id IS NOT OLD.root_session_id OR
                    NEW.depth != OLD.depth)
                BEGIN SELECT RAISE(ABORT,'agent lineage is immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_worker_parent_insert BEFORE INSERT ON sessions
                WHEN NEW.agent_role='worker' AND (
                    NEW.parent_session_id IS NULL OR NEW.depth NOT BETWEEN 1 AND 2 OR
                    (SELECT archived_at FROM sessions WHERE id=NEW.parent_session_id) IS NOT NULL OR
                    (SELECT agent_role FROM sessions WHERE id=NEW.parent_session_id)
                        NOT IN ('supervisor','worker') OR
                    (SELECT can_delegate FROM sessions WHERE id=NEW.parent_session_id) != 1 OR
                    NEW.depth != (SELECT depth+1 FROM sessions WHERE id=NEW.parent_session_id) OR
                    NEW.root_session_id IS NOT (SELECT CASE WHEN agent_role='supervisor' THEN id
                        ELSE root_session_id END FROM sessions WHERE id=NEW.parent_session_id) OR
                    (SELECT COUNT(*) FROM sessions WHERE parent_session_id=NEW.parent_session_id
                        AND archived_at IS NULL) >= 4)
                BEGIN SELECT RAISE(ABORT,'invalid worker parent or budget'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_contract_immutable
                BEFORE UPDATE OF goal,acceptance_json,context_json,creator_session_id,
                    creator_run_id,parent_task_id,requested_grant_json,limits_json ON tasks
                WHEN NEW.goal IS NOT OLD.goal OR NEW.acceptance_json IS NOT OLD.acceptance_json OR
                    NEW.context_json IS NOT OLD.context_json OR
                    NEW.creator_session_id IS NOT OLD.creator_session_id OR
                    NEW.creator_run_id IS NOT OLD.creator_run_id OR
                    NEW.parent_task_id IS NOT OLD.parent_task_id OR
                    NEW.requested_grant_json IS NOT OLD.requested_grant_json OR
                    NEW.limits_json IS NOT OLD.limits_json
                BEGIN SELECT RAISE(ABORT,'task contract is immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_run_grant_immutable BEFORE UPDATE OF effective_grant_json ON runs
                WHEN NEW.effective_grant_json IS NOT OLD.effective_grant_json
                BEGIN SELECT RAISE(ABORT,'run grant is immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_agent_message_provenance BEFORE INSERT ON messages
                WHEN (NEW.sender_session_id IS NOT NULL AND
                      (NEW.source_run_id IS NULL OR
                       (SELECT session_id FROM runs WHERE id=NEW.source_run_id)
                           IS NOT NEW.sender_session_id)) OR
                     (NEW.recipient_run_id IS NOT NULL AND
                      (SELECT session_id FROM runs WHERE id=NEW.recipient_run_id)
                          IS NOT NEW.session_id)
                BEGIN SELECT RAISE(ABORT,'invalid agent message provenance'); END
            """)
            conn.execute("PRAGMA user_version = 4")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _migrate_4_to_5(self):
        """P3 task-scoped workspaces, writer ownership, and immutable artifacts."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 5:
                conn.commit()
                return
            if version != 4:
                raise RuntimeError(f"不支持的数据库版本：{version}")
            conn.execute("""
                CREATE TABLE workspaces (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 200),
                    root_path TEXT NOT NULL UNIQUE
                        CHECK (length(trim(root_path)) BETWEEN 1 AND 32768),
                    kind TEXT NOT NULL CHECK (kind IN ('directory','git')),
                    max_mode TEXT NOT NULL CHECK (max_mode IN ('read_only','read_write')),
                    archived_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.execute("ALTER TABLE tasks ADD COLUMN workspace_id TEXT REFERENCES workspaces(id) ON DELETE RESTRICT")
            conn.execute("ALTER TABLE tasks ADD COLUMN workspace_mode TEXT CHECK (workspace_mode IN ('read_only','read_write'))")
            conn.execute("CREATE INDEX idx_tasks_workspace ON tasks(workspace_id,created_at)")
            conn.execute("""
                CREATE TABLE worktree_checkouts (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE RESTRICT,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
                    path TEXT NOT NULL UNIQUE CHECK (length(trim(path)) BETWEEN 1 AND 32768),
                    branch TEXT NOT NULL UNIQUE CHECK (length(trim(branch)) BETWEEN 1 AND 500),
                    base_commit TEXT NOT NULL CHECK (length(trim(base_commit)) BETWEEN 1 AND 200),
                    state TEXT NOT NULL CHECK (state IN
                        ('creating','active','retained','removing','removed','failed')),
                    error_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    CHECK ((state='failed' AND error_code IS NOT NULL AND
                            length(trim(error_code)) BETWEEN 1 AND 200) OR
                           (state<>'failed' AND error_code IS NULL))
                )
            """)
            conn.execute("CREATE INDEX idx_worktree_workspace_state ON worktree_checkouts(workspace_id,state)")
            conn.execute("""
                CREATE TABLE workspace_writer_leases (
                    workspace_id TEXT PRIMARY KEY REFERENCES workspaces(id) ON DELETE RESTRICT,
                    task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE RESTRICT,
                    acquired_at TEXT NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE artifacts (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
                    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE RESTRICT,
                    owner_session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE RESTRICT,
                    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE RESTRICT,
                    kind TEXT NOT NULL CHECK (length(trim(kind)) BETWEEN 1 AND 80),
                    relative_path TEXT NOT NULL
                        CHECK (length(trim(relative_path)) BETWEEN 1 AND 32768),
                    storage_relpath TEXT NOT NULL UNIQUE
                        CHECK (length(trim(storage_relpath)) BETWEEN 1 AND 32768),
                    sha256 TEXT NOT NULL CHECK (
                        length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    mime_type TEXT CHECK (mime_type IS NULL OR
                        length(trim(mime_type)) BETWEEN 1 AND 200),
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id,task_id) REFERENCES runs(id,task_id) ON DELETE RESTRICT
                )
            """)
            conn.execute("CREATE INDEX idx_artifacts_task_created ON artifacts(task_id,created_at,id)")
            conn.execute("""
                CREATE TRIGGER trg_workspace_identity_immutable
                BEFORE UPDATE OF root_path,kind ON workspaces
                WHEN NEW.root_path IS NOT OLD.root_path OR NEW.kind IS NOT OLD.kind
                BEGIN SELECT RAISE(ABORT,'workspace root and kind are immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_workspace_insert BEFORE INSERT ON tasks
                WHEN (NEW.workspace_id IS NULL) != (NEW.workspace_mode IS NULL) OR
                     (NEW.workspace_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM workspaces WHERE id=NEW.workspace_id
                            AND archived_at IS NULL
                            AND (NEW.workspace_mode='read_only' OR max_mode='read_write'))) OR
                     (NEW.workspace_id IS NOT NULL AND NEW.creator_run_id IS NULL AND
                        NOT EXISTS (SELECT 1 FROM sessions WHERE id=NEW.session_id
                            AND agent_role='supervisor' AND archived_at IS NULL)) OR
                     (NEW.creator_run_id IS NOT NULL AND (
                        NEW.parent_task_id IS NOT (
                            SELECT task_id FROM runs WHERE id=NEW.creator_run_id) OR
                        NEW.workspace_id IS NOT (
                            SELECT parent.workspace_id FROM runs
                            JOIN tasks AS parent ON parent.id=runs.task_id
                            WHERE runs.id=NEW.creator_run_id) OR
                        NEW.workspace_mode IS NOT (
                            SELECT parent.workspace_mode FROM runs
                            JOIN tasks AS parent ON parent.id=runs.task_id
                            WHERE runs.id=NEW.creator_run_id)))
                BEGIN SELECT RAISE(ABORT,'invalid task workspace contract'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_workspace_immutable
                BEFORE UPDATE OF workspace_id,workspace_mode ON tasks
                WHEN NEW.workspace_id IS NOT OLD.workspace_id OR
                     NEW.workspace_mode IS NOT OLD.workspace_mode
                BEGIN SELECT RAISE(ABORT,'task workspace is immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_worktree_checkout_insert BEFORE INSERT ON worktree_checkouts
                WHEN NOT EXISTS (
                    SELECT 1 FROM tasks JOIN workspaces ON workspaces.id=tasks.workspace_id
                    WHERE tasks.id=NEW.task_id AND tasks.workspace_id=NEW.workspace_id
                        AND tasks.status='open' AND tasks.workspace_mode='read_write'
                        AND workspaces.kind='git'
                        AND workspaces.archived_at IS NULL)
                BEGIN SELECT RAISE(ABORT,'invalid worktree checkout ownership'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_worktree_checkout_identity_immutable
                BEFORE UPDATE OF task_id,workspace_id,path,branch,base_commit ON worktree_checkouts
                WHEN NEW.task_id IS NOT OLD.task_id OR NEW.workspace_id IS NOT OLD.workspace_id OR
                     NEW.path IS NOT OLD.path OR NEW.branch IS NOT OLD.branch OR
                     NEW.base_commit IS NOT OLD.base_commit
                BEGIN SELECT RAISE(ABORT,'worktree checkout identity is immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_worktree_checkout_transition BEFORE UPDATE OF state ON worktree_checkouts
                WHEN (OLD.state<>NEW.state AND NOT (
                        (OLD.state='creating' AND NEW.state IN ('active','failed')) OR
                        (OLD.state='active' AND NEW.state IN ('retained','removing','failed')) OR
                        (OLD.state='retained' AND NEW.state IN ('active','removing')) OR
                        (OLD.state='removing' AND NEW.state IN ('removed','failed')) OR
                        (OLD.state='failed' AND NEW.state='removing'))) OR
                     (NEW.state='removing' AND EXISTS (
                        SELECT 1 FROM runs WHERE task_id=NEW.task_id
                            AND status IN ('queued','running','waiting','paused')))
                BEGIN SELECT RAISE(ABORT,'invalid worktree checkout transition'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_writer_lease_insert BEFORE INSERT ON workspace_writer_leases
                WHEN NOT EXISTS (
                    SELECT 1 FROM tasks JOIN workspaces ON workspaces.id=tasks.workspace_id
                    WHERE tasks.id=NEW.task_id AND tasks.workspace_id=NEW.workspace_id
                        AND tasks.status='open' AND tasks.workspace_mode='read_write'
                        AND workspaces.kind='directory' AND workspaces.archived_at IS NULL)
                BEGIN SELECT RAISE(ABORT,'invalid workspace writer lease ownership'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_writer_lease_no_update
                BEFORE UPDATE ON workspace_writer_leases
                BEGIN SELECT RAISE(ABORT,'workspace writer lease is immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_task_terminal_release_writer
                AFTER UPDATE OF status ON tasks
                WHEN OLD.status='open' AND NEW.status<>'open'
                BEGIN DELETE FROM workspace_writer_leases WHERE task_id=NEW.id; END
            """)
            conn.execute("""
                CREATE TRIGGER trg_run_release_writer
                AFTER UPDATE OF status ON runs
                WHEN OLD.status='running' AND (
                    NEW.status IN ('paused','completed','failed','cancelled') OR
                    (NEW.status='waiting' AND NEW.wait_reason IN ('input','children')))
                BEGIN DELETE FROM workspace_writer_leases WHERE task_id=NEW.task_id; END
            """)
            conn.execute("""
                CREATE TRIGGER trg_artifact_insert BEFORE INSERT ON artifacts
                WHEN NOT EXISTS (
                    SELECT 1 FROM runs JOIN tasks ON tasks.id=runs.task_id
                    WHERE runs.id=NEW.run_id AND tasks.id=NEW.task_id
                        AND runs.session_id=NEW.owner_session_id
                        AND tasks.workspace_id=NEW.workspace_id)
                BEGIN SELECT RAISE(ABORT,'invalid artifact ownership'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_artifact_no_update BEFORE UPDATE ON artifacts
                BEGIN SELECT RAISE(ABORT,'artifact is immutable'); END
            """)
            conn.execute("""
                CREATE TRIGGER trg_artifact_no_delete BEFORE DELETE ON artifacts
                BEGIN SELECT RAISE(ABORT,'artifact is immutable'); END
            """)
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"数据库外键检查失败：{[tuple(row) for row in violations]}"
                )
            if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise sqlite3.DatabaseError("数据库完整性检查失败")
            conn.execute("PRAGMA user_version = 5")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

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
            SELECT id, title, created_at, updated_at, project_id, archived_at,
                agent_role,parent_session_id,root_session_id,depth,system_prompt,
                capabilities_json,can_delegate
            FROM sessions
            WHERE id = ?
            """,
            (session_id,),
        )

    @staticmethod
    def _capabilities(value, default):
        if value is None:
            value = {"grants": sorted(default)}
        if (not isinstance(value, dict) or set(value) != {"grants"} or
                not isinstance(value["grants"], list) or len(value["grants"]) > 32 or
                any(not isinstance(grant, str) or grant not in HOST_GRANTS
                    for grant in value["grants"])):
            raise ValueError("capabilities 只接受固定 P2 grants")
        return set(value["grants"]) & HOST_GRANTS

    def get_supervisor(self):
        return self._query_one("""
            SELECT * FROM sessions WHERE agent_role='supervisor' AND archived_at IS NULL
        """)

    def create_supervisor(self, title="Supervisor", system_prompt="", capabilities=None):
        if not isinstance(title, str) or not title.strip() or not isinstance(system_prompt, str):
            raise ValueError("无效 Supervisor 标题/说明")
        grants = self._capabilities(capabilities, HOST_GRANTS)
        project_id = self._get_or_create_default_project()["id"]
        with self._write() as conn:
            if conn.execute("""
                SELECT 1 FROM sessions WHERE agent_role='supervisor' AND archived_at IS NULL
            """).fetchone():
                raise SupervisorAlreadyExists("活动根 Supervisor 已存在")
            agent_id, now = str(uuid.uuid4()), _utc_now()
            conn.execute("""
                INSERT INTO sessions(id,title,created_at,updated_at,project_id,
                    agent_role,root_session_id,depth,system_prompt,capabilities_json,can_delegate)
                VALUES (?,?,?,?,?,'supervisor',?,0,?,?,1)
            """, (agent_id, title.strip(), now, now, project_id, agent_id,
                  system_prompt, _json({"grants": sorted(grants)})))
            return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone())

    def select_supervisor(self, agent_id):
        with self._write() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone()
            if not row or row["archived_at"] is not None:
                raise ResourceNotFound("Agent 不存在或已归档")
            existing = conn.execute("""
                SELECT id FROM sessions WHERE agent_role='supervisor' AND archived_at IS NULL
            """).fetchone()
            if existing:
                if existing["id"] == agent_id:
                    return dict(row)
                raise SupervisorAlreadyExists("活动根 Supervisor 已存在")
            if row["agent_role"] is not None:
                raise AgentHierarchyError("只能显式选择旧会话为根 Supervisor")
            conn.execute("""
                UPDATE sessions SET agent_role='supervisor',root_session_id=id,
                    depth=0,can_delegate=1,capabilities_json=?,updated_at=? WHERE id=?
            """, (_json({"grants": sorted(HOST_GRANTS)}), _utc_now(), agent_id))
            return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone())

    def create_worker(self, parent_agent_id, title, system_prompt="",
                      capabilities=None, can_delegate=False):
        if (not isinstance(title, str) or not title.strip() or
                not isinstance(system_prompt, str) or not isinstance(can_delegate, bool)):
            raise ValueError("无效 Worker 标题/说明")
        default = {"read_only", "control.send_agent_message", "control.get_task_status",
                   "control.wait_for_tasks", "control.request_human_approval"}
        if can_delegate:
            default |= {"control.delegate_task", "control.cancel_task"}
        requested = self._capabilities(capabilities, default)
        with self._write() as conn:
            parent = conn.execute("SELECT * FROM sessions WHERE id=?", (parent_agent_id,)).fetchone()
            if not parent or parent["archived_at"] is not None:
                raise ResourceNotFound("父 Agent 不存在或已归档")
            if (parent["agent_role"] not in {"supervisor", "worker"} or
                    not parent["can_delegate"] or parent["depth"] >= 2 or
                    (can_delegate and "control.delegate_task" not in
                     json.loads(parent["capabilities_json"])["grants"])):
                raise AgentHierarchyError("父 Agent 无委派能力或深度已满")
            children = conn.execute("""
                SELECT COUNT(*) FROM sessions WHERE parent_session_id=? AND archived_at IS NULL
            """, (parent_agent_id,)).fetchone()[0]
            if children >= 4:
                raise AgentHierarchyError("Worker 子数已满")
            grants = requested & set(json.loads(parent["capabilities_json"])["grants"])
            if not can_delegate:
                grants.discard("control.delegate_task")
            agent_id, now = str(uuid.uuid4()), _utc_now()
            root_id = (parent["id"] if parent["agent_role"] == "supervisor"
                       else parent["root_session_id"])
            conn.execute("""
                INSERT INTO sessions(id,title,created_at,updated_at,project_id,
                    agent_role,parent_session_id,root_session_id,depth,system_prompt,
                    capabilities_json,can_delegate)
                VALUES (?,?,?,?,?,'worker',?,?,?,?,?,?)
            """, (agent_id, title.strip(), now, now, parent["project_id"],
                  parent_agent_id, root_id, parent["depth"] + 1, system_prompt,
                  _json({"grants": sorted(grants)}), int(can_delegate)))
            return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone())

    def set_agent_filesystem_capability(self, agent_id, filesystem_read,
                                        filesystem_write):
        """Change only future filesystem grants; frozen Runs remain unchanged."""
        if (not isinstance(filesystem_read, bool) or
                not isinstance(filesystem_write, bool) or
                filesystem_write and not filesystem_read):
            raise ValueError("filesystem.write 必须同时启用 filesystem.read")
        with self._write() as conn:
            agent = conn.execute(
                "SELECT * FROM sessions WHERE id=? AND archived_at IS NULL", (agent_id,)
            ).fetchone()
            if not agent or agent["agent_role"] is None:
                raise ResourceNotFound("Agent 不存在或已归档")
            grants = set(json.loads(agent["capabilities_json"])["grants"])
            grants -= {"filesystem.read", "filesystem.write"}
            if filesystem_read:
                grants.add("filesystem.read")
            if filesystem_write:
                grants.add("filesystem.write")
            if agent["agent_role"] == "worker":
                parent = conn.execute(
                    "SELECT capabilities_json FROM sessions WHERE id=?",
                    (agent["parent_session_id"],),
                ).fetchone()
                parent_grants = set(json.loads(parent["capabilities_json"])["grants"])
                if not {"filesystem.read", "filesystem.write"}.intersection(grants) <= parent_grants:
                    raise AgentHierarchyError("Worker filesystem 能力超过父 Agent")
            conn.execute(
                "UPDATE sessions SET capabilities_json=?,updated_at=? WHERE id=?",
                (_json({"grants": sorted(grants)}), _utc_now(), agent_id),
            )
            return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone())

    def list_agent_tree(self, root_agent_id=None, include_archived=False):
        root = self.get_supervisor() if root_agent_id is None else self.get_session(root_agent_id)
        if not root or root["agent_role"] != "supervisor":
            return []
        return self._query_all("""
            SELECT * FROM sessions WHERE root_session_id=?
                AND (? OR archived_at IS NULL)
            ORDER BY depth,created_at,id
        """, (root["id"], int(include_archived)))

    def get_agent_status(self, agent_id):
        agent = self.get_session(agent_id)
        if not agent or agent["agent_role"] is None:
            raise ResourceNotFound("Agent 不存在")
        active = self._query_one("""
            SELECT id,status FROM runs WHERE session_id=? AND
                status IN ('queued','running','waiting','paused') LIMIT 1
        """, (agent_id,))
        queued = self._query_one("""
            SELECT COUNT(*) AS n FROM tasks AS task WHERE task.session_id=?
                AND task.status='open' AND NOT EXISTS (
                    SELECT 1 FROM runs WHERE task_id=task.id)
        """, (agent_id,))["n"]
        return {"agent_id": agent_id,
                "state": "archived" if agent["archived_at"] else
                         active["status"] if active else "queued" if queued else "idle",
                "active_run_id": active["id"] if active else None,
                "queued_task_count": queued}

    def rename_agent(self, agent_id, title):
        if not isinstance(title, str) or not title.strip():
            raise ValueError("标题不能为空")
        with self._write() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone()
            if not row or row["agent_role"] is None or row["archived_at"]:
                raise ResourceNotFound("Agent 不存在或已归档")
            conn.execute("UPDATE sessions SET title=?,updated_at=? WHERE id=?",
                         (title.strip(), _utc_now(), agent_id))
            return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone())

    def archive_worker(self, agent_id):
        with self._write() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone()
            if not row or row["archived_at"]:
                raise ResourceNotFound("Worker 不存在或已归档")
            if row["agent_role"] != "worker":
                raise AgentHierarchyError("根 Supervisor/旧会话不可按 Worker 归档")
            if conn.execute("""
                SELECT 1 FROM sessions WHERE parent_session_id=? AND archived_at IS NULL
            """, (agent_id,)).fetchone():
                raise AgentHierarchyError("先归档子 Worker")
            active = conn.execute("""
                SELECT id FROM runs WHERE session_id=? AND
                    status IN ('queued','running','waiting','paused') LIMIT 1
            """, (agent_id,)).fetchone()
            if active:
                raise RunAlreadyActive(active["id"])
            if conn.execute("""
                SELECT 1 FROM tasks WHERE session_id=? AND status='open' LIMIT 1
            """, (agent_id,)).fetchone():
                raise AgentHierarchyError("Worker 尚有排队或未完成 Task")
            now = _utc_now()
            conn.execute("UPDATE sessions SET archived_at=?,updated_at=? WHERE id=?",
                         (now, now, agent_id))
            return dict(conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone())

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
        agent = self.get_session(session_id)
        if agent and agent["agent_role"] == "supervisor":
            raise AgentHierarchyError("根 Supervisor 不可普通删除或归档")
        if agent and agent["agent_role"] == "worker":
            self.archive_worker(session_id)
            return
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

    @staticmethod
    def _workspace_values(name, root_path, kind, max_mode):
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 200 or
                not isinstance(root_path, str) or
                not 1 <= len(root_path.strip()) <= 32768 or
                kind not in {"directory", "git"} or
                max_mode not in {"read_only", "read_write"}):
            raise ValueError("无效 Workspace 注册信息")
        return name.strip(), root_path.strip(), kind, max_mode

    def create_workspace(self, name, root_path, kind, max_mode):
        name, root_path, kind, max_mode = self._workspace_values(
            name, root_path, kind, max_mode
        )
        workspace_id, now = str(uuid.uuid4()), _utc_now()
        with self._write() as conn:
            conn.execute("""
                INSERT INTO workspaces
                    (id,name,root_path,kind,max_mode,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?)
            """, (workspace_id, name, root_path, kind, max_mode, now, now))
            return dict(conn.execute(
                "SELECT * FROM workspaces WHERE id=?", (workspace_id,)
            ).fetchone())

    def get_workspace(self, workspace_id):
        return self._query_one("SELECT * FROM workspaces WHERE id=?", (workspace_id,))

    def list_workspaces(self, include_archived=False):
        if not isinstance(include_archived, bool):
            raise ValueError("include_archived 必须为布尔值")
        return self._query_all("""
            SELECT * FROM workspaces WHERE (? OR archived_at IS NULL)
            ORDER BY created_at,id
        """, (int(include_archived),))

    def update_workspace(self, workspace_id, name=None, max_mode=None, archive=False):
        if (name is None and max_mode is None and not archive) or not isinstance(archive, bool):
            raise ValueError("Workspace 更新为空或无效")
        if name is not None and (not isinstance(name, str) or
                                 not 1 <= len(name.strip()) <= 200):
            raise ValueError("无效 Workspace 名称")
        if max_mode is not None and max_mode not in {"read_only", "read_write"}:
            raise ValueError("无效 Workspace max_mode")
        with self._write() as conn:
            workspace = conn.execute(
                "SELECT * FROM workspaces WHERE id=?", (workspace_id,)
            ).fetchone()
            if not workspace or workspace["archived_at"]:
                raise ResourceNotFound("Workspace 不存在或已归档")
            if max_mode == "read_only" and conn.execute("""
                SELECT 1 FROM tasks WHERE workspace_id=? AND workspace_mode='read_write'
                    AND status='open' LIMIT 1
            """, (workspace_id,)).fetchone():
                raise InvalidRunTransition("Workspace 仍有 read_write Task")
            if archive and (conn.execute(
                    "SELECT 1 FROM tasks WHERE workspace_id=? AND status='open' LIMIT 1",
                    (workspace_id,),
                ).fetchone() or conn.execute(
                    "SELECT 1 FROM worktree_checkouts WHERE workspace_id=? AND state<>'removed' LIMIT 1",
                    (workspace_id,),
                ).fetchone() or conn.execute(
                    "SELECT 1 FROM workspace_writer_leases WHERE workspace_id=?",
                    (workspace_id,),
                ).fetchone()):
                raise InvalidRunTransition("Workspace 仍有活动 Task 或未清理写资源")
            now = _utc_now()
            conn.execute("""
                UPDATE workspaces SET name=COALESCE(?,name),max_mode=COALESCE(?,max_mode),
                    archived_at=CASE WHEN ? THEN ? ELSE archived_at END,updated_at=?
                WHERE id=?
            """, (name.strip() if name is not None else None, max_mode,
                  int(archive), now, now, workspace_id))
            return dict(conn.execute(
                "SELECT * FROM workspaces WHERE id=?", (workspace_id,)
            ).fetchone())

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
                           run_id=None, tool_calls=None, tool_call_id=None,
                           sender_session_id=None, related_task_id=None,
                           source_run_id=None, recipient_run_id=None,
                           kind="chat", hop=0):
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
                 tool_call_id,sequence,created_at,sender_session_id,
                 related_task_id,source_run_id,recipient_run_id,kind,hop)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (session_id, task_id, run_id, role, content,
              _json(tool_calls) if tool_calls is not None else None,
              tool_call_id, sequence, now, sender_session_id,
              related_task_id, source_run_id, recipient_run_id, kind, hop))
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

    @staticmethod
    def _limits(value):
        if value is None:
            return {}
        if not isinstance(value, dict) or set(value) - {"max_rounds", "max_tool_calls"}:
            raise ValueError("无效 Task limits")
        for name, maximum in (("max_rounds", 20), ("max_tool_calls", 32)):
            if name in value and (not isinstance(value[name], int) or
                                  isinstance(value[name], bool) or
                                  not 1 <= value[name] <= maximum):
                raise ValueError("无效 Task limits")
        return value

    @staticmethod
    def _refs_context(value):
        value = {} if value is None else value
        if not isinstance(value, dict) or set(value) - {"message_refs", "artifact_refs"}:
            raise ValueError("context 仅接受 message_refs/artifact_refs")
        for refs in value.values():
            if (not isinstance(refs, list) or len(refs) > 16 or
                    any(not isinstance(item, str) or not 1 <= len(item) <= 200
                        for item in refs)):
                raise ValueError("context refs 无效")
        return {"message_refs": value.get("message_refs", []),
                "artifact_refs": value.get("artifact_refs", [])}

    @staticmethod
    def _workspace_contract_tx(conn, workspace_id, workspace_mode):
        if (workspace_id is None) != (workspace_mode is None):
            raise ValueError("workspace_id/workspace_mode 必须同时为空或同时提供")
        if workspace_id is None:
            return None, None
        if (not isinstance(workspace_id, str) or not workspace_id or
                workspace_mode not in {"read_only", "read_write"}):
            raise ValueError("无效 Task Workspace")
        workspace = conn.execute(
            "SELECT * FROM workspaces WHERE id=? AND archived_at IS NULL",
            (workspace_id,),
        ).fetchone()
        if not workspace:
            raise ResourceNotFound("Workspace 不存在或已归档")
        if workspace_mode == "read_write" and workspace["max_mode"] != "read_write":
            raise AgentHierarchyError("Task Workspace mode 超过注册上限")
        return workspace_id, workspace_mode

    def _insert_task_tx(self, conn, agent_id, goal, acceptance, context, *,
                        creator_session_id=None, creator_run_id=None,
                        parent_task_id=None, requested_grant=None, limits=None,
                        workspace_id=None, workspace_mode=None):
        goal, acceptance_json, context_json = self._task_values(
            goal, acceptance, context
        )
        agent = conn.execute(
            "SELECT * FROM sessions WHERE id=? AND archived_at IS NULL", (agent_id,)
        ).fetchone()
        if not agent:
            raise ResourceNotFound("Agent 不存在")
        default_grants = (set(json.loads(agent["capabilities_json"])["grants"])
                          if agent["agent_role"] else {"read_only"})
        requested = self._capabilities(requested_grant, default_grants)
        task_limits = self._limits(limits)
        workspace_id, workspace_mode = self._workspace_contract_tx(
            conn, workspace_id, workspace_mode
        )
        if (workspace_id is not None and creator_run_id is None and
                agent["agent_role"] != "supervisor"):
            raise AgentHierarchyError("只有活动根 Supervisor 可绑定 Workspace")
        task_id, now = str(uuid.uuid4()), _utc_now()
        conn.execute("""
            INSERT INTO tasks
                (id,session_id,goal,acceptance_json,context_json,status,
                 created_at,updated_at,creator_session_id,creator_run_id,
                 parent_task_id,requested_grant_json,limits_json,
                 workspace_id,workspace_mode)
            VALUES (?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?)
        """, (task_id, agent_id, goal, acceptance_json, context_json, now, now,
              creator_session_id, creator_run_id, parent_task_id,
              _json({"grants": sorted(requested)}), _json(task_limits),
              workspace_id, workspace_mode))
        self._insert_message_tx(conn, agent_id, "user", goal, task_id=task_id)
        return dict(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())

    def create_task(self, agent_id, goal, acceptance=None, context=None,
                    workspace_id=None, workspace_mode=None):
        with self._write() as conn:
            return self._insert_task_tx(
                conn, agent_id, goal, acceptance, context,
                workspace_id=workspace_id, workspace_mode=workspace_mode,
            )

    def get_task(self, task_id):
        return self._query_one("SELECT * FROM tasks WHERE id=?", (task_id,))

    def task_has_active_run(self, task_id):
        if not self.get_task(task_id):
            raise ResourceNotFound("Task 不存在")
        return self._query_one("""
            SELECT 1 AS active FROM runs WHERE task_id=?
                AND status IN ('queued','running','waiting','paused') LIMIT 1
        """, (task_id,)) is not None

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

    def list_tree_tasks(self, root_id, limit=50):
        if not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit 必须为 1..100")
        root = self.get_session(root_id)
        if not root or root["agent_role"] != "supervisor" or root["archived_at"]:
            raise ResourceNotFound("根 Supervisor 不存在")
        return self._query_all("""
            SELECT tasks.* FROM tasks JOIN sessions ON sessions.id=tasks.session_id
            WHERE sessions.root_session_id=?
            ORDER BY tasks.created_at DESC,tasks.id DESC LIMIT ?
        """, (root_id, limit))

    def host_delegate_task(self, creator_agent_id, assignee_agent_id, goal,
                           acceptance=None, context=None, requested_grant=None,
                           limits=None):
        context = self._refs_context(context)
        if (acceptance is not None and (not isinstance(acceptance, list) or
                len(acceptance) > 32 or any(not isinstance(item, str) or
                                           not 1 <= len(item) <= 500
                                           for item in acceptance))):
            raise ValueError("无效验收条件")
        with self._write() as conn:
            creator = conn.execute("SELECT * FROM sessions WHERE id=?",
                                   (creator_agent_id,)).fetchone()
            assignee = conn.execute("SELECT * FROM sessions WHERE id=?",
                                    (assignee_agent_id,)).fetchone()
            if (not creator or creator["agent_role"] != "supervisor" or
                    creator["archived_at"] is not None or not assignee or
                    assignee["agent_role"] != "worker" or
                    assignee["archived_at"] is not None or
                    assignee["root_session_id"] != creator_agent_id):
                raise ResourceNotFound("委派目标不存在或跨树")
            task = self._insert_task_tx(
                conn, assignee_agent_id, goal, acceptance, context,
                creator_session_id=creator_agent_id,
                requested_grant=requested_grant, limits=limits)
            busy = conn.execute("""
                SELECT 1 FROM runs WHERE session_id=? AND
                    status IN ('queued','running','waiting','paused') LIMIT 1
            """, (assignee_agent_id,)).fetchone()
            run = None
            if not busy:
                try:
                    run, _ = self._insert_run_tx(
                        conn, task, f"delegate:{task['id']}:1", {}, None, None
                    )
                except WorkspaceUnavailable:
                    pass
            return task, run

    def schedule_pending_tasks(self):
        """Give each idle Worker its oldest Task; SQLite remains the queue."""
        scheduled = []
        with self._write() as conn:
            agents = conn.execute("""
                SELECT id FROM sessions WHERE agent_role='worker' AND archived_at IS NULL
                ORDER BY created_at,id
            """).fetchall()
            for agent in agents:
                if conn.execute("""
                    SELECT 1 FROM runs WHERE session_id=? AND
                        status IN ('queued','running','waiting','paused') LIMIT 1
                """, (agent["id"],)).fetchone():
                    continue
                task = conn.execute("""
                    SELECT * FROM tasks WHERE session_id=? AND status='open'
                        AND NOT EXISTS (SELECT 1 FROM runs WHERE task_id=tasks.id)
                    ORDER BY created_at,id LIMIT 1
                """, (agent["id"],)).fetchone()
                if task:
                    try:
                        run, _ = self._insert_run_tx(
                            conn, task, f"delegate:{task['id']}:1", {}, None, None
                        )
                    except WorkspaceUnavailable:
                        continue
                    scheduled.append(run)
        return scheduled

    @staticmethod
    def _prepare_workspace_run_tx(conn, task):
        """Atomically reserve the task's executable workspace, if it is ready."""
        if task["workspace_id"] is None:
            return True
        workspace = conn.execute(
            "SELECT * FROM workspaces WHERE id=? AND archived_at IS NULL",
            (task["workspace_id"],),
        ).fetchone()
        if not workspace:
            return False
        if task["workspace_mode"] == "read_only":
            return True
        if workspace["max_mode"] != "read_write":
            return False
        if workspace["kind"] == "git":
            return conn.execute("""
                SELECT 1 FROM worktree_checkouts
                WHERE task_id=? AND workspace_id=? AND state='active'
            """, (task["id"], workspace["id"])).fetchone() is not None
        lease = conn.execute(
            "SELECT task_id FROM workspace_writer_leases WHERE workspace_id=?",
            (workspace["id"],),
        ).fetchone()
        if lease:
            return lease["task_id"] == task["id"]
        conn.execute("""
            INSERT INTO workspace_writer_leases(workspace_id,task_id,acquired_at)
            VALUES (?,?,?)
        """, (workspace["id"], task["id"], _utc_now()))
        return True

    def create_worktree_checkout(self, task_id, path, branch, base_commit):
        if any(not isinstance(value, str) or not value.strip()
               for value in (task_id, path, branch, base_commit)):
            raise ValueError("无效 worktree checkout")
        if len(path.strip()) > 32768 or len(branch.strip()) > 500 or len(base_commit.strip()) > 200:
            raise ValueError("worktree checkout 字段过长")
        with self._write() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ResourceNotFound("Task 不存在")
            now = _utc_now()
            conn.execute("""
                INSERT INTO worktree_checkouts
                    (task_id,workspace_id,path,branch,base_commit,state,created_at,updated_at)
                VALUES (?,?,?,?,?,'creating',?,?)
            """, (task_id, task["workspace_id"], path.strip(), branch.strip(),
                  base_commit.strip(), now, now))
            return dict(conn.execute(
                "SELECT * FROM worktree_checkouts WHERE task_id=?", (task_id,)
            ).fetchone())

    def get_worktree_checkout(self, task_id):
        return self._query_one(
            "SELECT * FROM worktree_checkouts WHERE task_id=?", (task_id,)
        )

    def transition_worktree_checkout(self, task_id, state, error_code=None):
        if state not in {"active", "retained", "removing", "removed", "failed"}:
            raise ValueError("无效 worktree checkout 状态")
        if ((state == "failed") != (isinstance(error_code, str) and
                                     1 <= len(error_code.strip()) <= 200)):
            raise ValueError("failed checkout 必须且只能包含 error_code")
        with self._write() as conn:
            checkout = conn.execute(
                "SELECT * FROM worktree_checkouts WHERE task_id=?", (task_id,)
            ).fetchone()
            if not checkout:
                raise ResourceNotFound("worktree checkout 不存在")
            if state == "removing" and conn.execute("""
                SELECT 1 FROM runs WHERE task_id=?
                    AND status IN ('queued','running','waiting','paused') LIMIT 1
            """, (task_id,)).fetchone():
                raise InvalidRunTransition("活动 Run 的 worktree 不可清理")
            conn.execute("""
                UPDATE worktree_checkouts SET state=?,error_code=?,updated_at=?
                WHERE task_id=?
            """, (state, error_code.strip() if error_code else None,
                  _utc_now(), task_id))
            return dict(conn.execute(
                "SELECT * FROM worktree_checkouts WHERE task_id=?", (task_id,)
            ).fetchone())

    def acquire_workspace_writer_lease(self, task_id):
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id 不能为空")
        with self._write() as conn:
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if not task:
                raise ResourceNotFound("Task 不存在")
            existing = conn.execute(
                "SELECT * FROM workspace_writer_leases WHERE workspace_id=?",
                (task["workspace_id"],),
            ).fetchone()
            if existing:
                if existing["task_id"] == task_id:
                    return dict(existing)
                raise AgentHierarchyError("Workspace 已由其他 Task 持有写租约")
            conn.execute("""
                INSERT INTO workspace_writer_leases(workspace_id,task_id,acquired_at)
                VALUES (?,?,?)
            """, (task["workspace_id"], task_id, _utc_now()))
            return dict(conn.execute(
                "SELECT * FROM workspace_writer_leases WHERE task_id=?", (task_id,)
            ).fetchone())

    def release_workspace_writer_lease(self, task_id):
        with self._write() as conn:
            return bool(conn.execute(
                "DELETE FROM workspace_writer_leases WHERE task_id=?", (task_id,)
            ).rowcount)

    def register_artifact(self, run_id, kind, relative_path, storage_relpath,
                          sha256, size_bytes, mime_type=None):
        if (not isinstance(run_id, str) or not run_id or
                not isinstance(kind, str) or not 1 <= len(kind.strip()) <= 80 or
                not isinstance(relative_path, str) or
                not 1 <= len(relative_path.strip()) <= 32768 or
                not isinstance(storage_relpath, str) or
                not 1 <= len(storage_relpath.strip()) <= 32768 or
                not isinstance(sha256, str) or
                re.fullmatch(r"[0-9a-f]{64}", sha256) is None or
                not isinstance(size_bytes, int) or isinstance(size_bytes, bool) or
                size_bytes < 0 or
                (mime_type is not None and (not isinstance(mime_type, str) or
                 not 1 <= len(mime_type.strip()) <= 200))):
            raise ValueError("无效 Artifact 元数据")
        with self._write() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise ResourceNotFound("Run 不存在")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
            if not task or task["workspace_id"] is None:
                raise AgentHierarchyError("Artifact 必须归属 Task Workspace")
            artifact_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO artifacts
                    (id,task_id,run_id,owner_session_id,workspace_id,kind,
                     relative_path,storage_relpath,sha256,size_bytes,mime_type,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (artifact_id, task["id"], run_id, run["session_id"],
                  task["workspace_id"], kind.strip(), relative_path.strip(),
                  storage_relpath.strip(), sha256, size_bytes,
                  mime_type.strip() if mime_type is not None else None, _utc_now()))
            return dict(conn.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone())

    def get_artifact(self, artifact_id):
        return self._query_one("SELECT * FROM artifacts WHERE id=?", (artifact_id,))

    def list_task_artifacts(self, task_id):
        return self._query_all("""
            SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at,id
        """, (task_id,))

    def run_workspace_scope(self, run_id):
        """Return trusted stored scope; path interpretation belongs to Workspace policy."""
        with self._lock:
            row = self._conn.execute("""
                SELECT tasks.id AS task_id,tasks.workspace_id,tasks.workspace_mode,
                       workspaces.kind,workspaces.root_path,workspaces.max_mode,
                       workspaces.archived_at
                FROM runs JOIN tasks ON tasks.id=runs.task_id
                LEFT JOIN workspaces ON workspaces.id=tasks.workspace_id
                WHERE runs.id=?
            """, (run_id,)).fetchone()
            if not row:
                raise ResourceNotFound("Run 不存在")
            if row["workspace_id"] is None or row["workspace_mode"] is None or row["archived_at"]:
                return None
            if row["workspace_mode"] == "read_write" and row["max_mode"] != "read_write":
                return None
            effective_root = row["root_path"]
            if row["workspace_mode"] == "read_write":
                if row["kind"] == "git":
                    checkout = self._conn.execute("""
                        SELECT path FROM worktree_checkouts
                        WHERE task_id=? AND workspace_id=? AND state='active'
                    """, (row["task_id"], row["workspace_id"])).fetchone()
                    if not checkout:
                        return None
                    effective_root = checkout["path"]
                elif not self._conn.execute("""
                    SELECT 1 FROM workspace_writer_leases
                    WHERE workspace_id=? AND task_id=?
                """, (row["workspace_id"], row["task_id"])).fetchone():
                    return None
            return {"workspace_id": row["workspace_id"], "kind": row["kind"],
                    "effective_root": effective_root, "mode": row["workspace_mode"]}

    @staticmethod
    def _validated_result(value):
        if isinstance(value, str):
            if len(value) > 8000:
                raise ValueError("结果过长")
            value = json.loads(value)
        fields = {"summary", "acceptance_results", "artifacts", "evidence", "followups"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("结果必须为固定结构")
        if not isinstance(value["summary"], str) or len(value["summary"]) > 2000:
            raise ValueError("无效结果摘要")
        for field in fields - {"summary"}:
            if not isinstance(value[field], list) or len(value[field]) > 32:
                raise ValueError("结果数组过长")
        if any(not isinstance(item, dict) or set(item) != {"criterion", "ok"} or
               not isinstance(item["criterion"], str) or
               not isinstance(item["ok"], bool)
               for item in value["acceptance_results"]):
            raise ValueError("无效验收结果")
        if any(not isinstance(item, dict) or set(item) != {"artifact_id", "kind"} or
               any(not isinstance(v, str) or len(v) > 200 for v in item.values())
               for item in value["artifacts"]):
            raise ValueError("无效产物引用")
        if any(not isinstance(item, dict) or set(item) != {"kind", "command", "exit_code"} or
               not isinstance(item["kind"], str) or not isinstance(item["command"], str) or
               len(item["command"]) > 1000 or not isinstance(item["exit_code"], int)
               for item in value["evidence"]):
            raise ValueError("无效验证证据")
        if any(not isinstance(item, str) or len(item) > 1000
               for item in value["followups"]):
            raise ValueError("无效后续项")
        return value

    def get_task_result(self, task_id):
        task = self.get_task(task_id)
        if not task:
            raise ResourceNotFound("Task 不存在")
        return self._validated_result(task["result_json"]) if task["result_json"] else None

    def list_agent_messages(self, agent_id, after_id=0, limit=100):
        if (not isinstance(after_id, int) or after_id < 0 or
                not isinstance(limit, int) or not 1 <= limit <= 100):
            raise ValueError("无效 Message cursor/limit")
        agent = self.get_session(agent_id)
        if not agent or agent["agent_role"] is None:
            raise ResourceNotFound("Agent 不存在")
        rows = self._query_all("""
            SELECT messages.*, CASE WHEN messages.kind='question' AND
                runs.status='waiting' AND runs.wait_reason='input' AND
                messages.id=(SELECT MAX(newer.id) FROM messages AS newer
                    WHERE newer.source_run_id=messages.source_run_id
                        AND newer.kind='question') AND
                NOT EXISTS (SELECT 1 FROM messages AS reply
                    WHERE reply.kind='reply' AND reply.related_task_id=messages.related_task_id
                        AND reply.recipient_run_id=messages.source_run_id
                        AND reply.content IS NOT NULL
                        AND reply.id>messages.id)
                THEN 1 ELSE 0 END AS needs_reply
            FROM messages LEFT JOIN runs ON runs.id=messages.source_run_id
            WHERE messages.session_id=? AND messages.kind<>'chat' AND messages.id>?
            ORDER BY messages.id LIMIT ?
        """, (agent_id, after_id, limit))
        return [{**row, "needs_reply": bool(row["needs_reply"])} for row in rows]

    def host_reply(self, agent_id, reply_to_message_id, content):
        if not isinstance(content, str) or not 1 <= len(content.strip()) <= 8000:
            raise ValueError("回复必须为 1..8000 字符")
        with self._write() as conn:
            agent = conn.execute("SELECT * FROM sessions WHERE id=?", (agent_id,)).fetchone()
            question = conn.execute("SELECT * FROM messages WHERE id=?", (reply_to_message_id,)).fetchone()
            if (not agent or agent["agent_role"] != "supervisor" or
                    agent["archived_at"] is not None or not question or
                    question["kind"] != "question" or question["session_id"] != agent_id):
                raise ResourceNotFound("待回复问题不存在")
            run = conn.execute("SELECT * FROM runs WHERE id=?", (question["source_run_id"],)).fetchone()
            latest = conn.execute("""
                SELECT MAX(id) FROM messages WHERE source_run_id=? AND kind='question'
            """, (question["source_run_id"],)).fetchone()[0]
            if (not run or run["session_id"] != question["sender_session_id"] or
                    run["status"] != "waiting" or run["wait_reason"] != "input" or
                    latest != question["id"]):
                raise InvalidRunTransition("问题已回复或 Run 不再等待输入")
            recipient = conn.execute("SELECT root_session_id,archived_at FROM sessions WHERE id=?",
                                     (run["session_id"],)).fetchone()
            if not recipient or recipient["root_session_id"] != agent_id or recipient["archived_at"]:
                raise ResourceNotFound("发问 Agent 不在当前树")
            message_id = self._insert_message_tx(
                conn, run["session_id"], "user", content.strip(),
                sender_session_id=None, related_task_id=run["task_id"],
                recipient_run_id=run["id"], kind="reply", hop=question["hop"])
            now = _utc_now()
            conn.execute("""
                UPDATE runs SET status='queued',wait_reason=NULL,version=version+1,
                    updated_at=? WHERE id=? AND status='waiting' AND wait_reason='input'
            """, (now, run["id"]))
            self._append_event_tx(conn, run["id"], "run.resumed",
                                  {"message_id": message_id, "reason": "host_reply"})
            return (dict(conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()),
                    dict(conn.execute("SELECT * FROM runs WHERE id=?", (run["id"],)).fetchone()))

    def control_scope(self, run_id):
        """Trusted, advisory Gateway scope. Mutations recheck it in the write txn."""
        with self._lock:
            conn = self._conn
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise ResourceNotFound("Run 不存在")
            agent = conn.execute("SELECT * FROM sessions WHERE id=?", (run["session_id"],)).fetchone()
            empty = {"agent_id": run["session_id"], "role": None,
                     "parent_agent_id": None, "root_agent_id": None,
                     "can_delegate": False, "delegate_agent_ids": frozenset(),
                     "message_agent_ids": frozenset(), "status_task_ids": frozenset(),
                     "wait_child_task_ids": frozenset(), "cancel_task_ids": frozenset(),
                     "remaining_depth": 0, "remaining_children": 0,
                     "remaining_hops": 0}
            if not agent or agent["agent_role"] is None or agent["archived_at"]:
                return empty
            root_id = agent["root_session_id"]
            peers = conn.execute("""
                SELECT id,parent_session_id FROM sessions
                WHERE root_session_id=? AND archived_at IS NULL
            """, (root_id,)).fetchall()
            children = {row["id"] for row in peers if row["parent_session_id"] == agent["id"]}
            messages = set(children)
            if agent["parent_session_id"]:
                messages.add(agent["parent_session_id"])
            tasks = conn.execute("""
                SELECT tasks.id,tasks.creator_run_id,tasks.status FROM tasks
                JOIN sessions ON sessions.id=tasks.session_id
                WHERE sessions.root_session_id=?
            """, (root_id,)).fetchall()
            child_ids = {row["id"] for row in tasks if row["creator_run_id"] == run_id}
            child_count = conn.execute("""
                SELECT COUNT(*) FROM tasks WHERE creator_run_id=?
            """, (run_id,)).fetchone()[0]
            allowed_delegate = bool(agent["can_delegate"] and agent["depth"] < 2 and
                                    "control.delegate_task" in
                                    json.loads(run["effective_grant_json"])["grants"])
            return {"agent_id": agent["id"], "role": agent["agent_role"],
                    "parent_agent_id": agent["parent_session_id"],
                    "root_agent_id": root_id,
                    "can_delegate": allowed_delegate,
                    "delegate_agent_ids": frozenset(children if allowed_delegate else ()),
                    "message_agent_ids": frozenset(messages),
                    "status_task_ids": frozenset(row["id"] for row in tasks),
                    "wait_child_task_ids": frozenset(child_ids),
                    "cancel_task_ids": frozenset(row["id"] for row in tasks
                                                 if row["id"] in child_ids and row["status"] == "open"),
                    "remaining_depth": 2 - agent["depth"] if allowed_delegate else 0,
                    "remaining_children": max(0, 4 - child_count) if allowed_delegate else 0,
                    "remaining_hops": max(0, 2 - agent["depth"])}

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

    def _default_run_snapshots_tx(self, conn, grants=frozenset({"read_only"})):
        """Freeze safe metadata, never a Provider key or executable handler."""
        from backend.config import load_config
        from backend import tool_gateway

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
        specs = dict(tool_gateway.snapshot_builtin_tools())
        if hasattr(tool_gateway, "snapshot_internal_tools"):
            specs.update(tool_gateway.snapshot_internal_tools())
        tools = {name: {
            "schema": spec.schema,
            "effects": sorted(spec.effects),
            "path_fields": list(spec.path_fields),
            "idempotent": spec.idempotent,
            "approval": spec.approval,
            "timeout_seconds": spec.timeout_seconds,
            "origin": spec.origin,
        } for name, spec in specs.items()}
        return config, {"v": 1, "specs": tools, "grants": sorted(grants)}

    @staticmethod
    def _effective_grants_tx(conn, task):
        assignee = conn.execute("SELECT * FROM sessions WHERE id=?",
                                (task["session_id"],)).fetchone()
        if assignee["agent_role"] is None:
            return {"read_only"}
        parents = HOST_GRANTS
        if task["creator_run_id"]:
            parent = conn.execute("SELECT effective_grant_json FROM runs WHERE id=?",
                                  (task["creator_run_id"],)).fetchone()
            if not parent:
                raise ResourceNotFound("创建 Run 不存在")
            parents = set(json.loads(parent["effective_grant_json"])["grants"])
        elif task["creator_session_id"]:
            creator = conn.execute("SELECT capabilities_json FROM sessions WHERE id=?",
                                   (task["creator_session_id"],)).fetchone()
            parents = set(json.loads(creator["capabilities_json"])["grants"])
        grants = (set(parents) & set(json.loads(assignee["capabilities_json"])["grants"])
                  & set(json.loads(task["requested_grant_json"])["grants"]) & HOST_GRANTS)
        if task["workspace_id"] is None:
            grants -= {"filesystem.read", "filesystem.write"}
        elif task["workspace_mode"] == "read_only":
            grants.discard("filesystem.write")
        if not assignee["can_delegate"]:
            grants.discard("control.delegate_task")
        return grants

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
        if not self._prepare_workspace_run_tx(conn, task):
            raise WorkspaceUnavailable("Task Workspace 写资源尚未就绪")
        grants = self._effective_grants_tx(conn, task)
        if config_snapshot is None or tool_snapshot is None:
            frozen_config, frozen_tools = self._default_run_snapshots_tx(conn, grants)
            config_snapshot = frozen_config if config_snapshot is None else config_snapshot
            tool_snapshot = frozen_tools if tool_snapshot is None else tool_snapshot
        if isinstance(tool_snapshot, dict) and "grants" in tool_snapshot:
            tool_snapshot = {**tool_snapshot,
                             "grants": sorted(set(tool_snapshot["grants"]) & grants)}
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
                 created_at,updated_at,effective_grant_json)
            VALUES (?,?,?,?,?,?,'queued',?,?,?,?,?,?)
        """, (run_id, task["id"], task["session_id"], attempt, key, fingerprint,
              _json(config_snapshot), _json(tool_snapshot), cutoff, now, now,
              _json({"grants": sorted(grants)})))
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
                         *, terminal_reserve=False, child_receipt_task_id=None,
                         actor_session_id=None, recipient_session_id=None):
        if not isinstance(event_type, str) or not 1 <= len(event_type) <= 80:
            raise ValueError("无效 Event type")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValueError("Event payload 必须为对象")
        run = conn.execute(
            "SELECT task_id,session_id,status,event_bytes_written,usage_json FROM runs WHERE id=?",
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
            INSERT INTO events (task_id,run_id,type,dedupe_key,payload_json,created_at,
                                actor_session_id,recipient_session_id)
            VALUES (?,?,?,?,?,?,?,?)
        """, (run["task_id"], run_id, event_type, dedupe_key, payload_json, now,
              actor_session_id, recipient_session_id))
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
        usage = json.loads(run["usage_json"] or "{}")
        reserved = usage.get("p2_child_reserve", {})
        if child_receipt_task_id is not None and child_receipt_task_id not in reserved:
            raise InvalidRunTransition("子 Task 无 Event 收件预留")
        held = sum(reserved.values()) - reserved.get(child_receipt_task_id, 0)
        limit = (MAX_RUN_EVENT_BYTES if event_type in TERMINAL_EVENTS or terminal_reserve
                 else MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE - held)
        if size > MAX_EVENT_BYTES or run["event_bytes_written"] + size > limit:
            raise EventLimitExceeded("Event 超出持久化字节额度")
        conn.execute(
            "UPDATE runs SET event_bytes_written=event_bytes_written+? WHERE id=?",
            (size, run_id),
        )
        if child_receipt_task_id is not None:
            del reserved[child_receipt_task_id]
            usage["p2_child_reserve"] = reserved
            conn.execute("UPDATE runs SET usage_json=? WHERE id=?", (_json(usage), run_id))
        return dict(conn.execute("SELECT * FROM events WHERE id=?", (cursor.lastrowid,)).fetchone())

    @staticmethod
    def _reserve_child_receipt_tx(conn, run_id, task_id):
        run = conn.execute("SELECT event_bytes_written,usage_json FROM runs WHERE id=?",
                           (run_id,)).fetchone()
        usage = json.loads(run["usage_json"] or "{}")
        reserved = usage.setdefault("p2_child_reserve", {})
        if task_id not in reserved:
            if len(reserved) >= 4:
                raise AgentHierarchyError("单 Run 最多等待 4 个子 Task")
            reserved[task_id] = CHILD_RECEIPT_RESERVE
        # Leave another 4 KiB for the parent's own tool message/checkpoint Event.
        if (run["event_bytes_written"] + sum(reserved.values()) + 4096 >
                MAX_RUN_EVENT_BYTES - TERMINAL_EVENT_RESERVE):
            raise EventLimitExceeded("父 Run 的子结果 Event 预留空间不足")
        conn.execute("UPDATE runs SET usage_json=? WHERE id=?", (_json(usage), run_id))

    def _notify_parent_child_terminal_tx(self, conn, task_id, source_run_id, status):
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task or not task["creator_run_id"]:
            return
        parent = conn.execute("SELECT * FROM runs WHERE id=?",
                              (task["creator_run_id"],)).fetchone()
        recipient = conn.execute("SELECT archived_at FROM sessions WHERE id=?",
                                 (parent["session_id"],)).fetchone()
        if not recipient or recipient["archived_at"]:
            return  # A terminal/archived parent is never reactivated by a child.
        message_id = self._insert_message_tx(
            conn, parent["session_id"], "user",
            _json({"task_id": task_id, "status": status}),
            sender_session_id=task["session_id"], related_task_id=task_id,
            source_run_id=source_run_id, recipient_run_id=parent["id"],
            kind="result")
        if parent["status"] in {"completed", "failed", "cancelled"}:
            return
        self._append_event_tx(conn, parent["id"], "agent.child_terminal",
                              {"message_id": message_id, "task_id": task_id,
                               "status": status},
                              child_receipt_task_id=task_id,
                              actor_session_id=task["session_id"],
                              recipient_session_id=parent["session_id"])
        if parent["status"] == "waiting" and parent["wait_reason"] == "children":
            awaited = json.loads(parent["checkpoint_json"] or "{}").get(
                "awaited_child_task_ids", [])
            if task_id in awaited:
                conn.execute("""
                    UPDATE runs SET status='queued',wait_reason=NULL,
                        version=version+1,updated_at=? WHERE id=?
                """, (_utc_now(), parent["id"]))

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
            candidates = conn.execute("""
                SELECT runs.* FROM runs
                WHERE runs.status='queued'
                ORDER BY runs.created_at,runs.id
            """).fetchall()
            candidate = None
            for queued in candidates:
                task = conn.execute(
                    "SELECT * FROM tasks WHERE id=?", (queued["task_id"],)
                ).fetchone()
                conn.execute("SAVEPOINT claim_one")
                if task and self._prepare_workspace_run_tx(conn, task):
                    candidate = queued
                    break
                conn.execute("RELEASE claim_one")
            if not candidate:
                return None
            now = _utc_now()
            checkpoint = candidate["checkpoint_json"] or _json({
                "v": 1, "phase": "model_pending", "round": 1
            })
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
                updated_task = conn.execute("""
                    UPDATE tasks SET status='failed',finished_at=?,updated_at=?
                    WHERE id=? AND status='open'
                """, (now, now, candidate["task_id"]))
                if updated_task.rowcount:
                    self._notify_parent_child_terminal_tx(conn, candidate["task_id"],
                                                          candidate["id"], "failed")
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
                updated_task = conn.execute("""
                    UPDATE tasks SET status=?,finished_at=?,updated_at=?
                    WHERE id=? AND status='open'
                """, (status, now, now, run["task_id"]))
                if updated_task.rowcount:
                    self._notify_parent_child_terminal_tx(conn, run["task_id"],
                                                          run_id, status)
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), message_id

    def apply_control_tool(self, run_id, expected_version, lease_owner, lease_epoch,
                           call_id, tool_name, arguments, model_text=None):
        """Apply a Gateway-approved internal tool, including its checkpoint, once."""
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        if not isinstance(arguments, dict) or tool_name not in {
            "delegate_task", "send_agent_message", "get_task_status",
            "wait_for_tasks", "cancel_task", "request_human_approval",
        }:
            raise ValueError("无效内部控制工具")
        with self._write() as conn:
            run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if not run:
                raise ResourceNotFound("Run 不存在")
            if (run["status"] != "running" or run["version"] != expected_version or
                    run["lease_owner"] != lease_owner or run["lease_epoch"] != lease_epoch or
                    run["lease_until"] <= _utc_now() or run["control_requested"]):
                raise InvalidRunTransition("Run lease/version/control 已变化")
            agent = conn.execute("SELECT * FROM sessions WHERE id=?", (run["session_id"],)).fetchone()
            if (not agent or agent["agent_role"] is None or agent["archived_at"] or
                    f"control.{tool_name}" not in json.loads(run["effective_grant_json"])["grants"]):
                raise AgentHierarchyError("Agent 无此控制权限")
            checkpoint = json.loads(run["checkpoint_json"] or "{}")
            if checkpoint.get("phase") != "tool_pending":
                raise InvalidRunTransition("Run 不在工具检查点")
            assistant = conn.execute("SELECT * FROM messages WHERE id=?",
                                     (checkpoint.get("assistant_message_id"),)).fetchone()
            if not assistant or assistant["run_id"] != run_id:
                raise InvalidRunTransition("工具调用缺少已提交 assistant 消息")
            calls = json.loads(assistant["tool_calls_json"] or "[]")
            index = checkpoint.get("next_tool_index")
            if not isinstance(index, int) or not 0 <= index < len(calls):
                raise InvalidRunTransition("工具调用检查点无效")
            call = calls[index]
            stored = call["function"]["arguments"]
            stored = json.loads(stored) if isinstance(stored, str) else stored
            if (call.get("id") != call_id or call["function"].get("name") != tool_name or
                    _json(stored) != _json(arguments)):
                raise InvalidRunTransition("控制参数与已提交调用不一致")
            started = conn.execute("""
                SELECT 1 FROM events WHERE run_id=? AND type='tool.started'
                    AND json_extract(payload_json,'$.call_id')=?
            """, (run_id, call_id)).fetchone()
            result = conn.execute("""
                SELECT 1 FROM messages WHERE run_id=? AND role='tool' AND tool_call_id=?
            """, (run_id, call_id)).fetchone()
            if not started or result:
                raise InvalidRunTransition("工具未启动或已经完成")
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
            now = _utc_now()
            status, wait_reason = "running", None
            payload = {"call_id": call_id, "name": tool_name, "status": "ok"}
            text = ""
            recipient = None
            if tool_name == "delegate_task":
                if (set(arguments) - {"assignee_agent_id", "goal", "acceptance", "context", "limits"} or
                        not agent["can_delegate"] or agent["depth"] >= 2):
                    raise AgentHierarchyError("委派参数或层级无效")
                assignee = conn.execute("SELECT * FROM sessions WHERE id=?",
                                        (arguments.get("assignee_agent_id"),)).fetchone()
                if (not assignee or assignee["parent_session_id"] != agent["id"] or
                        assignee["archived_at"] or
                        assignee["root_session_id"] != agent["root_session_id"]):
                    raise ResourceNotFound("委派目标不在当前树")
                count = conn.execute("SELECT COUNT(*) FROM tasks WHERE creator_run_id=?",
                                     (run_id,)).fetchone()[0]
                if count >= 4:
                    raise AgentHierarchyError("单 Run 子 Task 数量已满")
                context = self._refs_context(arguments.get("context"))
                acceptance = arguments.get("acceptance")
                if (not isinstance(acceptance, list) or len(acceptance) > 16 or
                        any(not isinstance(item, str) or len(item) > 1000 for item in acceptance)):
                    raise ValueError("无效委派验收条件")
                child = self._insert_task_tx(
                    conn, assignee["id"], arguments.get("goal"), acceptance, context,
                    creator_session_id=agent["id"], creator_run_id=run_id,
                    parent_task_id=run["task_id"],
                    requested_grant=json.loads(run["effective_grant_json"]),
                    limits=arguments.get("limits"),
                    workspace_id=task["workspace_id"],
                    workspace_mode=task["workspace_mode"])
                self._reserve_child_receipt_tx(conn, run_id, child["id"])
                busy = conn.execute("""
                    SELECT 1 FROM runs WHERE session_id=? AND
                        status IN ('queued','running','waiting','paused') LIMIT 1
                """, (assignee["id"],)).fetchone()
                child_run = None
                if not busy:
                    try:
                        child_run, _ = self._insert_run_tx(
                            conn, child, f"delegate:{child['id']}:1", {}, None, None
                        )
                    except WorkspaceUnavailable:
                        pass
                text = _json({"task_id": child["id"],
                              "run_id": child_run["id"] if child_run else None,
                              "status": "queued"})
                payload["related_task_id"] = child["id"]
            elif tool_name == "send_agent_message":
                if set(arguments) - {"to_agent_id", "kind", "content", "related_task_id", "reply_to_message_id"}:
                    raise ValueError("消息参数无效")
                kind, content = arguments.get("kind"), arguments.get("content")
                recipient = conn.execute("SELECT * FROM sessions WHERE id=?",
                                         (arguments.get("to_agent_id"),)).fetchone()
                if (kind not in {"question", "progress", "result", "reply"} or
                        not isinstance(content, str) or not 1 <= len(content) <= 8000 or
                        not recipient or recipient["archived_at"] or
                        recipient["root_session_id"] != agent["root_session_id"]):
                    raise ResourceNotFound("消息收件方不存在或参数无效")
                if kind in {"question", "progress", "result"}:
                    if recipient["id"] != agent["parent_session_id"]:
                        raise ResourceNotFound("只能向直属父 Agent 报告")
                    if arguments.get("related_task_id", run["task_id"]) != run["task_id"]:
                        raise AgentHierarchyError("消息 Task 不属于发方 Run")
                    parent_run_id = task["creator_run_id"]
                    message_id = self._insert_message_tx(
                        conn, recipient["id"], "user", content,
                        sender_session_id=agent["id"], related_task_id=run["task_id"],
                        source_run_id=run_id, recipient_run_id=parent_run_id,
                        kind=kind, hop=agent["depth"])
                    payload["message_id"] = message_id
                    if kind == "question":
                        status, wait_reason = "waiting", "input"
                    elif kind == "result":
                        validated = self._validated_result(content)
                        status = "completed"
                        conn.execute("""
                            UPDATE tasks SET status='completed',result_json=?,
                                finished_at=?,updated_at=? WHERE id=? AND status='open'
                        """, (_json(validated), now, now, run["task_id"]))
                    if parent_run_id:
                        parent = conn.execute("SELECT * FROM runs WHERE id=?",
                                              (parent_run_id,)).fetchone()
                        if parent and parent["status"] not in {"completed", "failed", "cancelled"}:
                            if kind in {"question", "result"}:
                                self._append_event_tx(
                                    conn, parent_run_id, f"agent.{kind}",
                                    {"message_id": message_id, "task_id": run["task_id"]},
                                    child_receipt_task_id=(run["task_id"] if kind == "result" else None),
                                    actor_session_id=agent["id"],
                                    recipient_session_id=recipient["id"])
                                if (parent["status"] == "waiting" and
                                        parent["wait_reason"] == "children"):
                                    awaited = json.loads(parent["checkpoint_json"] or "{}").get(
                                        "awaited_child_task_ids", [])
                                    if run["task_id"] in awaited:
                                        conn.execute("""
                                            UPDATE runs SET status='queued',wait_reason=NULL,
                                                version=version+1,updated_at=? WHERE id=?
                                        """, (now, parent_run_id))
                    text = _json({"message_id": message_id, "kind": kind})
                else:
                    question = conn.execute("SELECT * FROM messages WHERE id=?",
                                            (arguments.get("reply_to_message_id"),)).fetchone()
                    child_run = conn.execute("SELECT * FROM runs WHERE id=?",
                                             (question["source_run_id"],)).fetchone() if question else None
                    latest_question_id = conn.execute("""
                        SELECT MAX(id) FROM messages WHERE source_run_id=? AND kind='question'
                    """, (question["source_run_id"],)).fetchone()[0] if question else None
                    if (not question or question["kind"] != "question" or
                            question["id"] != latest_question_id or
                            question["session_id"] != agent["id"] or
                            question["sender_session_id"] != recipient["id"] or
                            not child_run or child_run["status"] != "waiting" or
                            child_run["wait_reason"] != "input" or
                            child_run["session_id"] != recipient["id"]):
                        raise InvalidRunTransition("回复目标不再等待输入")
                    message_id = self._insert_message_tx(
                        conn, recipient["id"], "user", content,
                        sender_session_id=agent["id"], related_task_id=child_run["task_id"],
                        source_run_id=run_id, recipient_run_id=child_run["id"],
                        kind="reply", hop=agent["depth"])
                    conn.execute("""
                        UPDATE runs SET status='queued',wait_reason=NULL,version=version+1,
                            updated_at=? WHERE id=?
                    """, (now, child_run["id"]))
                    self._append_event_tx(conn, child_run["id"], "run.resumed",
                                          {"message_id": message_id, "reason": "agent_reply"})
                    text = _json({"message_id": message_id, "kind": "reply"})
            elif tool_name == "get_task_status":
                if set(arguments) != {"task_id"}:
                    raise ValueError("任务状态参数无效")
                target = conn.execute("""
                    SELECT tasks.* FROM tasks JOIN sessions ON sessions.id=tasks.session_id
                    WHERE tasks.id=? AND sessions.root_session_id=?
                """, (arguments["task_id"], agent["root_session_id"])).fetchone()
                if not target:
                    raise ResourceNotFound("Task 不在当前树")
                text = _json({"task_id": target["id"], "status": target["status"],
                              "assignee_agent_id": target["session_id"]})
            elif tool_name == "wait_for_tasks":
                ids = arguments.get("task_ids")
                if (set(arguments) != {"task_ids"} or not isinstance(ids, list) or
                        not 1 <= len(ids) <= 4 or len(set(ids)) != len(ids)):
                    raise ValueError("无效等待 Task 列表")
                rows = conn.execute("SELECT id,status FROM tasks WHERE creator_run_id=?",
                                    (run_id,)).fetchall()
                owned = {row["id"]: row["status"] for row in rows}
                if any(task_id not in owned for task_id in ids):
                    raise ResourceNotFound("子 Task 不属于此 Run")
                outstanding = [task_id for task_id in ids if owned[task_id] == "open"]
                for child_id in outstanding:
                    reserved = json.loads(run["usage_json"] or "{}").get("p2_child_reserve", {})
                    if child_id not in reserved:
                        raise EventLimitExceeded("子结果未预留 Event 空间")
                if outstanding:
                    status, wait_reason = "waiting", "children"
                checkpoint["awaited_child_task_ids"] = ids
                text = _json({"task_ids": ids, "outstanding_task_ids": outstanding})
            elif tool_name == "cancel_task":
                if set(arguments) != {"task_id"}:
                    raise ValueError("取消参数无效")
                child = conn.execute("SELECT * FROM tasks WHERE id=? AND creator_run_id=?",
                                     (arguments["task_id"], run_id)).fetchone()
                if not child or child["status"] != "open":
                    raise ResourceNotFound("可取消的子 Task 不存在")
                child_run = conn.execute("""
                    SELECT * FROM runs WHERE task_id=? ORDER BY attempt DESC LIMIT 1
                """, (child["id"],)).fetchone()
                if child_run and child_run["status"] != "queued":
                    raise InvalidRunTransition("运行或未知结果的子 Task 不可由此工具直接取消")
                if child_run:
                    conn.execute("""
                        UPDATE runs SET status='cancelled',finished_at=?,updated_at=?,
                            version=version+1 WHERE id=?
                    """, (now, now, child_run["id"]))
                    self._append_event_tx(conn, child_run["id"], "run.cancelled",
                                          {"reason": "parent_cancel"})
                conn.execute("""
                    UPDATE tasks SET status='cancelled',finished_at=?,updated_at=? WHERE id=?
                """, (now, now, child["id"]))
                self._append_event_tx(conn, run_id, "agent.child_cancelled",
                                      {"task_id": child["id"]},
                                      child_receipt_task_id=child["id"])
                text = _json({"task_id": child["id"], "status": "cancelled"})
            else:
                if set(arguments) != {"reason"} or not isinstance(arguments["reason"], str):
                    raise ValueError("审批参数无效")
                reason = arguments["reason"]
                if not 1 <= len(reason) <= 2000:
                    raise ValueError("审批理由无效")
                approval_id = str(uuid.uuid4())
                expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
                conn.execute("""
                    INSERT INTO approvals
                        (id,run_id,tool_call_id,kind,action_hash,request_summary_json,
                         status,expires_at,created_at)
                    VALUES (?,?,?,'pre_execution',?,?,'pending',?,?)
                """, (approval_id, run_id, call_id,
                      hashlib.sha256(reason.encode()).hexdigest(),
                      _json({"reason": reason}), expires, now))
                status, wait_reason = "waiting", "approval"
                text = _json({"approval_id": approval_id, "status": "pending"})
            if len(text) > 8000:
                raise ValueError("控制工具结果过长")
            checkpoint = {**checkpoint, "next_tool_index": index + 1}
            if status != "waiting":
                checkpoint.pop("awaited_child_task_ids", None)
            terminal = status == "completed"
            conn.execute("""
                UPDATE runs SET status=?,wait_reason=?,version=version+1,
                    lease_owner=?,lease_until=?,checkpoint_json=?,updated_at=?,
                    finished_at=CASE WHEN ? THEN ? ELSE finished_at END
                WHERE id=? AND version=?
            """, (status, wait_reason, lease_owner if status == "running" else None,
                  run["lease_until"] if status == "running" else None,
                  _json(checkpoint), now, terminal, now, run_id, expected_version))
            tool_message_id = self._insert_message_tx(
                conn, run["session_id"], "tool", text,
                task_id=run["task_id"], run_id=run_id, tool_call_id=call_id)
            self._append_event_tx(conn, run_id, "message.completed",
                                  {"message_id": tool_message_id,
                                   "hash": hashlib.sha256(text.encode()).hexdigest(),
                                   "length": len(text)},
                                  f"{run_id}:message.completed:{tool_message_id}",
                                  terminal_reserve=terminal)
            self._append_event_tx(conn, run_id,
                                  "run.completed" if terminal else "tool.completed",
                                  {"call_id": call_id, "name": tool_name,
                                   "status": "ok"} if not terminal else
                                  {"reason": "child_result", "call_id": call_id},
                                  f"{run_id}:run.completed" if terminal else None)
            return dict(conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), tool_message_id

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
                    updated_task = conn.execute("""
                        UPDATE tasks SET status='failed',finished_at=?,updated_at=?
                        WHERE id=? AND status='open'
                    """, (now, now, row["task_id"]))
                    if updated_task.rowcount:
                        self._notify_parent_child_terminal_tx(conn, row["task_id"],
                                                              row["id"], "failed")
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
                updated_task = conn.execute("""
                    UPDATE tasks SET status='cancelled',finished_at=?,updated_at=?
                    WHERE id=? AND status='open'
                """, (now, now, run["task_id"]))
                if updated_task.rowcount:
                    self._notify_parent_child_terminal_tx(conn, run["task_id"],
                                                          run_id, "cancelled")
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
