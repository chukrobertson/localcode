from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import Activity, Chat, Message, Project
from .paths import database_path, ensure_app_dirs

SCHEMA_VERSION = 1


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path | str | None = None) -> None:
        ensure_app_dirs()
        self.path = Path(path) if path else database_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self.path.chmod(0o600)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode = WAL;
                PRAGMA synchronous = NORMAL;

                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL UNIQUE,
                    model TEXT NOT NULL DEFAULT '',
                    context_window INTEGER NOT NULL DEFAULT 32768,
                    permission_mode TEXT NOT NULL DEFAULT 'ask'
                        CHECK (permission_mode IN ('ask', 'allow', 'read-only')),
                    memory_enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_opened_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS chats (
                    id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    model TEXT NOT NULL DEFAULT '',
                    compaction_summary TEXT NOT NULL DEFAULT '',
                    compacted_through INTEGER NOT NULL DEFAULT 0,
                    context_used INTEGER NOT NULL DEFAULT 0,
                    context_limit INTEGER NOT NULL DEFAULT 0,
                    context_state TEXT NOT NULL DEFAULT 'fresh',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_chats_project_updated
                    ON chats(project_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system', 'event')),
                    content TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_messages_chat_id
                    ON messages(chat_id, id);

                CREATE TABLE IF NOT EXISTS activities (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    detail TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'complete',
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_activities_chat_id
                    ON activities(chat_id, id);

                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def add_project(
        self,
        path: Path | str,
        *,
        name: str | None = None,
        model: str = "",
        context_window: int = 32768,
    ) -> Project:
        project_path = Path(path).expanduser().resolve()
        if not project_path.is_dir():
            raise ValueError(f"Project folder does not exist: {project_path}")
        display_name = (name or project_path.name).strip() or project_path.name
        now = utc_now()
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT * FROM projects WHERE path = ?", (str(project_path),)
            ).fetchone()
            if existing:
                connection.execute(
                    "UPDATE projects SET last_opened_at = ?, updated_at = ? WHERE id = ?",
                    (now, now, existing["id"]),
                )
                return self._project_from_row(
                    connection.execute(
                        "SELECT * FROM projects WHERE id = ?", (existing["id"],)
                    ).fetchone()
                )

            project_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO projects (
                    id, name, path, model, context_window, permission_mode,
                    memory_enabled, created_at, updated_at, last_opened_at
                ) VALUES (?, ?, ?, ?, ?, 'ask', 1, ?, ?, ?)
                """,
                (
                    project_id,
                    display_name,
                    str(project_path),
                    model,
                    max(2048, int(context_window)),
                    now,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._project_from_row(row)

    def list_projects(self) -> list[Project]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM projects ORDER BY last_opened_at DESC, name COLLATE NOCASE"
            ).fetchall()
        return [self._project_from_row(row) for row in rows]

    def get_project(self, project_id: str) -> Project | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        return self._project_from_row(row) if row else None

    def update_project(self, project_id: str, **values: object) -> Project:
        allowed = {
            "name",
            "model",
            "context_window",
            "permission_mode",
            "memory_enabled",
            "last_opened_at",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            project = self.get_project(project_id)
            if project is None:
                raise KeyError(project_id)
            return project
        if "memory_enabled" in updates:
            updates["memory_enabled"] = int(bool(updates["memory_enabled"]))
        if "context_window" in updates:
            updates["context_window"] = max(2048, int(updates["context_window"]))
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE projects SET {assignments} WHERE id = ?",
                (*updates.values(), project_id),
            )
            row = connection.execute(
                "SELECT * FROM projects WHERE id = ?", (project_id,)
            ).fetchone()
        if row is None:
            raise KeyError(project_id)
        return self._project_from_row(row)

    def remove_project(self, project_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM projects WHERE id = ?", (project_id,))

    def create_chat(self, project_id: str, title: str = "New chat", model: str = "") -> Chat:
        chat_id = str(uuid.uuid4())
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO chats (
                    id, project_id, title, model, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, project_id, title.strip() or "New chat", model, now, now),
            )
            row = connection.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return self._chat_from_row(row)

    def list_chats(self, project_id: str) -> list[Chat]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM chats WHERE project_id = ? ORDER BY updated_at DESC",
                (project_id,),
            ).fetchall()
        return [self._chat_from_row(row) for row in rows]

    def get_chat(self, chat_id: str) -> Chat | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        return self._chat_from_row(row) if row else None

    def update_chat(self, chat_id: str, **values: object) -> Chat:
        allowed = {
            "title",
            "model",
            "compaction_summary",
            "compacted_through",
            "context_used",
            "context_limit",
            "context_state",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        updates["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self.connect() as connection:
            connection.execute(
                f"UPDATE chats SET {assignments} WHERE id = ?", (*updates.values(), chat_id)
            )
            row = connection.execute("SELECT * FROM chats WHERE id = ?", (chat_id,)).fetchone()
        if row is None:
            raise KeyError(chat_id)
        return self._chat_from_row(row)

    def delete_chat(self, chat_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM chats WHERE id = ?", (chat_id,))

    def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        metadata: dict | None = None,
    ) -> Message:
        if role not in {"user", "assistant", "system", "event"}:
            raise ValueError(f"Unsupported message role: {role}")
        now = utc_now()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO messages (chat_id, role, content, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, role, content, metadata_json, now),
            )
            connection.execute("UPDATE chats SET updated_at = ? WHERE id = ?", (now, chat_id))
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._message_from_row(row)

    def list_messages(self, chat_id: str, *, after_id: int = 0) -> list[Message]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND id > ? ORDER BY id",
                (chat_id, after_id),
            ).fetchall()
        return [self._message_from_row(row) for row in rows]

    def active_messages(self, chat: Chat) -> list[Message]:
        return [
            message
            for message in self.list_messages(chat.id, after_id=chat.compacted_through)
            if message.role in {"user", "assistant"}
        ]

    def compact_chat(self, chat_id: str, summary: str, through_message_id: int) -> Chat:
        return self.update_chat(
            chat_id,
            compaction_summary=summary,
            compacted_through=max(0, int(through_message_id)),
            context_used=0,
            context_state="compacted",
        )

    def add_activity(
        self,
        chat_id: str,
        kind: str,
        title: str,
        detail: str = "",
        status: str = "complete",
    ) -> Activity:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO activities (chat_id, kind, title, detail, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (chat_id, kind, title, detail, status, now),
            )
            row = connection.execute(
                "SELECT * FROM activities WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return self._activity_from_row(row)

    def list_activities(self, chat_id: str) -> list[Activity]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM activities WHERE chat_id = ? ORDER BY id", (chat_id,)
            ).fetchall()
        return [self._activity_from_row(row) for row in rows]

    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: object) -> None:
        serialized = str(value)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO settings (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, serialized),
            )

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            model=row["model"],
            context_window=row["context_window"],
            permission_mode=row["permission_mode"],
            memory_enabled=bool(row["memory_enabled"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_opened_at=row["last_opened_at"],
        )

    @staticmethod
    def _chat_from_row(row: sqlite3.Row) -> Chat:
        return Chat(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            model=row["model"],
            compaction_summary=row["compaction_summary"],
            compacted_through=row["compacted_through"],
            context_used=row["context_used"],
            context_limit=row["context_limit"],
            context_state=row["context_state"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _message_from_row(row: sqlite3.Row) -> Message:
        try:
            metadata = json.loads(row["metadata_json"])
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return Message(
            id=row["id"],
            chat_id=row["chat_id"],
            role=row["role"],
            content=row["content"],
            metadata=metadata,
            created_at=row["created_at"],
        )

    @staticmethod
    def _activity_from_row(row: sqlite3.Row) -> Activity:
        return Activity(
            id=row["id"],
            chat_id=row["chat_id"],
            kind=row["kind"],
            title=row["title"],
            detail=row["detail"],
            status=row["status"],
            created_at=row["created_at"],
        )
