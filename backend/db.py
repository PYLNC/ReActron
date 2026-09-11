"""SQLite 数据库访问层：会话、消息与设置持久化。"""

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from backend.config import BASE_DIR


DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "countboter.db"
logger = logging.getLogger("agent.db")
SCHEMA_VERSION = 1


def _truncate(text: str, limit: int = 300):
    """截断日志文本。"""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(已截断)"


class Database:
    """基于标准库 sqlite3 的轻量数据访问对象。"""

    def __init__(self, db_path=None):
        self.db_path = Path(db_path) if db_path else DB_PATH
        self._lock = threading.Lock()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
        logger.debug("SQL 执行：%s | params=%s", sql, _truncate(str(params), 300))
        with self._lock:
            try:
                cursor = self._conn.execute(sql, params)
                self._conn.commit()
                return cursor
            except Exception:
                self._conn.rollback()
                raise

    def _query_all(self, sql, params=()):
        logger.debug("SQL 查询：%s | params=%s", sql, _truncate(str(params), 300))
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def _query_one(self, sql, params=()):
        rows = self._query_all(sql, params)
        return rows[0] if rows else None

    def _init_tables(self):
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                self._migrate_0_to_1()
            elif version != SCHEMA_VERSION:
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
            if version == SCHEMA_VERSION:
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
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
        model=None,
        max_rounds=None,
        tool_timeout=None,
    ):
        existing = self.get_settings() or {}
        new_api_key = api_key_encrypted if api_key_encrypted is not None else existing.get("api_key_encrypted")
        new_base_url = base_url if base_url is not None else existing.get("base_url")
        new_model = model if model is not None else existing.get("model")
        new_max_rounds = max_rounds if max_rounds is not None else existing.get("max_rounds")
        new_tool_timeout = tool_timeout if tool_timeout is not None else existing.get("tool_timeout")
        now = self._now()

        if existing:
            self._execute(
                """
                UPDATE settings
                SET api_key_encrypted = ?, base_url = ?, model = ?,
                    max_rounds = ?, tool_timeout = ?, updated_at = ?
                WHERE id = 1
                """,
                (new_api_key, new_base_url, new_model, new_max_rounds, new_tool_timeout, now),
            )
        else:
            self._execute(
                """
                INSERT INTO settings
                    (id, api_key_encrypted, base_url, model, max_rounds, tool_timeout, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?)
                """,
                (new_api_key, new_base_url, new_model, new_max_rounds, new_tool_timeout, now),
            )
        logger.info(
            "DB 设置输出：base_url=%s model=%s max_rounds=%s tool_timeout=%s api_key_encrypted=%s",
            new_base_url,
            new_model,
            new_max_rounds,
            new_tool_timeout,
            bool(new_api_key),
        )

    def list_sessions(self):
        sessions = self._query_all(
            """
            SELECT id, title, created_at, updated_at, project_id
            FROM sessions
            ORDER BY updated_at DESC
            """
        )
        logger.info("DB 会话列表输出：count=%d", len(sessions))
        return sessions

    def get_session(self, session_id):
        return self._query_one(
            """
            SELECT id, title, created_at, updated_at, project_id
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
        self._execute("DELETE FROM sessions WHERE id = ?", (session_id,))
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
        logger.info(
            "DB 写入消息：session_id=%s，sequence=%d，role=%s，content=%s，"
            "tool_calls=%s，tool_call_id=%s",
            session_id,
            next_sequence,
            role,
            _truncate(content or "", 300),
            _truncate(tool_calls_json or "", 300),
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


db = Database()
