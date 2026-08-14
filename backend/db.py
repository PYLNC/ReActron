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
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._init_tables()
        logger.info("SQLite 数据库已打开：%s", self.db_path)

    def _execute(self, sql, params=()):
        logger.debug("SQL 执行：%s | params=%s", sql, _truncate(str(params), 300))
        with self._lock:
            cursor = self._conn.execute(sql, params)
            self._conn.commit()
            return cursor

    def _query_all(self, sql, params=()):
        logger.debug("SQL 查询：%s | params=%s", sql, _truncate(str(params), 300))
        with self._lock:
            cursor = self._conn.execute(sql, params)
            return [dict(row) for row in cursor.fetchall()]

    def _query_one(self, sql, params=()):
        rows = self._query_all(sql, params)
        return rows[0] if rows else None

    def _init_tables(self):
        self._execute("""
            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                api_key_encrypted TEXT,
                base_url TEXT,
                model TEXT,
                max_rounds INTEGER,
                tool_timeout REAL,
                updated_at TEXT
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        self._execute("""
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
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS audit_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                action TEXT NOT NULL,
                tool_name TEXT,
                arguments_json TEXT,
                result_json TEXT,
                ok INTEGER NOT NULL
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS uploaded_files (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                stored_path TEXT NOT NULL,
                size INTEGER NOT NULL,
                mime_type TEXT,
                parsed_json TEXT,
                created_at TEXT NOT NULL
            )
        """)
        self._execute("""
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
        """)
        # 迁移：为旧库中的 sessions 表补充 project_id 字段
        columns = self._query_all("PRAGMA table_info(sessions)")
        if not any(column["name"] == "project_id" for column in columns):
            self._execute("ALTER TABLE sessions ADD COLUMN project_id TEXT")
        # 所有旧会话都归入默认项目，保证每个会话都有项目
        default_project = self._query_one(
            "SELECT id FROM projects WHERE name = '默认项目' LIMIT 1"
        )
        if not default_project:
            default_project = self.create_project("默认项目")
        self._execute(
            "UPDATE sessions SET project_id = ? WHERE project_id IS NULL",
            (default_project["id"],),
        )

        # WAL 模式减少读写锁冲突，索引加速按会话读取消息
        self._execute("PRAGMA journal_mode=WAL")
        self._execute("PRAGMA synchronous=NORMAL")
        self._execute("PRAGMA busy_timeout=5000")
        self._execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session_sequence "
            "ON messages(session_id, sequence)"
        )
        self._execute(
            "CREATE INDEX IF NOT EXISTS idx_audit_logs_created_at "
            "ON audit_logs(created_at)"
        )
        self._execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_project_id "
            "ON sessions(project_id)"
        )
        self._cleanup_empty_sessions()

    @staticmethod
    def _now():
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _cleanup_empty_sessions(self):
        """只保留一个空会话，避免数据库堆积无内容的会话。"""
        rows = self._query_all("""
            SELECT s.id
            FROM sessions s
            WHERE NOT EXISTS (
                SELECT 1 FROM messages m WHERE m.session_id = s.id
            )
            ORDER BY s.created_at DESC
        """)
        if len(rows) <= 1:
            return
        keep = rows[0]["id"]
        delete_ids = [row["id"] for row in rows[1:]]
        placeholders = ",".join("?" * len(delete_ids))
        self._execute(
            f"DELETE FROM sessions WHERE id IN ({placeholders})",
            delete_ids,
        )
        logger.info("清理多余空会话：删除 %d 个，保留 %s", len(delete_ids), keep)

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

    def get_empty_session(self):
        return self._query_one("""
            SELECT id, title, created_at, updated_at, project_id
            FROM sessions s
            WHERE NOT EXISTS (
                SELECT 1 FROM messages m WHERE m.session_id = s.id
            )
            ORDER BY created_at DESC
            LIMIT 1
        """)

    def create_session(self, title="新会话", project_id=None):
        # 全系统最多保留一个空会话，避免被脚本批量创建空会话
        if not project_id:
            project_id = self._get_or_create_default_project()["id"]
        empty = self.get_empty_session()
        if empty:
            if title and title != "新会话":
                self.rename_session(empty["id"], title)
            if project_id and empty.get("project_id") != project_id:
                self.assign_session_to_project(empty["id"], project_id)
            return self.get_session(empty["id"])

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
                (SELECT COUNT(*) FROM sessions s WHERE s.project_id = p.id) AS session_count
            FROM projects p
            ORDER BY p.updated_at DESC
        """)

    def _get_or_create_default_project(self):
        project = self._query_one(
            "SELECT id, name, created_at, updated_at FROM projects WHERE name = '默认项目' LIMIT 1"
        )
        if not project:
            project = self.create_project("默认项目")
        return project

    def get_project(self, project_id):
        return self._query_one(
            "SELECT id, name, created_at, updated_at FROM projects WHERE id = ?",
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
        default_project = self._get_or_create_default_project()
        self._execute(
            "UPDATE sessions SET project_id = ? WHERE project_id = ?",
            (default_project["id"], project_id),
        )
        self._execute("DELETE FROM projects WHERE id = ?", (project_id,))
        logger.info("DB 删除项目：project_id=%s", project_id)

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
        next_sequence = self._query_one(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM messages WHERE session_id = ?",
            (session_id,),
        )["next_sequence"]
        tool_calls_json = (
            json.dumps(tool_calls, ensure_ascii=False, default=str) if tool_calls else None
        )
        self._execute(
            """
            INSERT INTO messages
                (session_id, role, content, tool_calls_json, tool_call_id, sequence, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                tool_calls_json,
                tool_call_id,
                next_sequence,
                self._now(),
            ),
        )
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
        self.touch_session(session_id)

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
