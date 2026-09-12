from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from .models import (
    Activity,
    Chat,
    Message,
    Project,
    ProjectMemory,
    Provider,
    SourceSymbols,
    TaskCheckpoint,
    WorkingFile,
)
from .paths import database_path, ensure_app_dirs

SCHEMA_VERSION = 4

BASE_SCHEMA = """
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

CREATE TABLE IF NOT EXISTS providers (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    endpoint TEXT NOT NULL DEFAULT '',
    api_key TEXT NOT NULL DEFAULT '',
    is_local INTEGER NOT NULL DEFAULT 0,
    default_context_window INTEGER NOT NULL DEFAULT 32768
);
"""

# Ordered migrations applied after BASE_SCHEMA. Append a new tuple
# (next_version, script) whenever the schema shape changes and bump
# SCHEMA_VERSION to match the latest entry.
MIGRATIONS: tuple[tuple[int, str], ...] = (
    (2, "ALTER TABLE projects DROP COLUMN memory_enabled;"),
    (
        3,
        """
        CREATE TABLE IF NOT EXISTS working_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            content TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(chat_id, path)
        );
        CREATE INDEX IF NOT EXISTS idx_working_files_chat_updated
            ON working_files(chat_id, updated_at DESC);
        """,
    ),
    (
        4,
        """
        DROP INDEX IF EXISTS idx_working_files_chat_updated;
        ALTER TABLE working_files RENAME TO working_files_v3;
        CREATE TABLE working_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            content TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(chat_id, path, start_line, end_line)
        );
        INSERT INTO working_files (
            id, chat_id, path, start_line, end_line, content, source_hash, updated_at
        )
        SELECT id, chat_id, path, start_line, end_line, content, source_hash, updated_at
        FROM working_files_v3;
        DROP TABLE working_files_v3;
        CREATE INDEX idx_working_files_chat_updated
            ON working_files(chat_id, updated_at DESC);

        CREATE TABLE task_checkpoints (
            chat_id TEXT PRIMARY KEY REFERENCES chats(id) ON DELETE CASCADE,
            objective TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active',
            completed_work TEXT NOT NULL DEFAULT '',
            changed_files_json TEXT NOT NULL DEFAULT '[]',
            commands_json TEXT NOT NULL DEFAULT '[]',
            failures_json TEXT NOT NULL DEFAULT '[]',
            next_step TEXT NOT NULL DEFAULT '',
            segment_count INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE project_memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            source_chat_id TEXT NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, source_chat_id)
        );
        CREATE INDEX idx_project_memories_project_updated
            ON project_memories(project_id, updated_at DESC);

        CREATE TABLE source_symbols (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            path TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            symbols TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(project_id, path)
        );
        CREATE INDEX idx_source_symbols_project_path
            ON source_symbols(project_id, path);
        """,
    ),
)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class Database:
    def __init__(self, path: Path | str | None = None) -> None:
        self.memory_fts_available = False
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
            current = connection.execute("PRAGMA user_version").fetchone()[0]
            if current < 1:
                connection.executescript(BASE_SCHEMA)
                connection.execute("PRAGMA user_version = 1")
            for version, script in MIGRATIONS:
                if version > current:
                    connection.executescript(script)
                    connection.execute(f"PRAGMA user_version = {version}")
            self._initialize_memory_search(connection)

    def _initialize_memory_search(self, connection: sqlite3.Connection) -> None:
        try:
            connection.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS project_memory_fts
                USING fts5(title, content)
                """
            )
            connection.execute("DELETE FROM project_memory_fts")
            connection.execute(
                """
                INSERT INTO project_memory_fts(rowid, title, content)
                SELECT id, title, content FROM project_memories
                """
            )
        except sqlite3.OperationalError:
            self.memory_fts_available = False
        else:
            self.memory_fts_available = True

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
                    created_at, updated_at, last_opened_at
                ) VALUES (?, ?, ?, ?, ?, 'ask', ?, ?, ?)
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
            "last_opened_at",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            project = self.get_project(project_id)
            if project is None:
                raise KeyError(project_id)
            return project
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

    def remember_working_file(
        self,
        chat_id: str,
        path: str,
        start_line: int,
        end_line: int,
        content: str,
        source_hash: str,
        *,
        limit: int = 24,
    ) -> WorkingFile:
        now = utc_now()
        start_line = max(1, int(start_line))
        end_line = max(start_line, int(end_line))
        with self.connect() as connection:
            existing = connection.execute(
                """
                SELECT * FROM working_files
                WHERE chat_id = ? AND path = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (chat_id, path),
            ).fetchall()
            if any(row["source_hash"] != source_hash for row in existing):
                connection.execute(
                    "DELETE FROM working_files WHERE chat_id = ? AND path = ?",
                    (chat_id, path),
                )
                existing = []
            for row in existing:
                if row["start_line"] <= start_line and row["end_line"] >= end_line:
                    connection.execute(
                        "UPDATE working_files SET updated_at = ? WHERE id = ?",
                        (now, row["id"]),
                    )
                    refreshed = connection.execute(
                        "SELECT * FROM working_files WHERE id = ?", (row["id"],)
                    ).fetchone()
                    return self._working_file_from_row(refreshed)
                if start_line <= row["start_line"] and end_line >= row["end_line"]:
                    connection.execute("DELETE FROM working_files WHERE id = ?", (row["id"],))
            connection.execute(
                """
                INSERT INTO working_files (
                    chat_id, path, start_line, end_line, content, source_hash, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id, path, start_line, end_line) DO UPDATE SET
                    content = excluded.content,
                    source_hash = excluded.source_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    path,
                    start_line,
                    end_line,
                    content,
                    source_hash,
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM working_files
                WHERE chat_id = ? AND id NOT IN (
                    SELECT id FROM working_files
                    WHERE chat_id = ?
                    ORDER BY updated_at DESC, id DESC
                    LIMIT ?
                )
                """,
                (chat_id, chat_id, max(1, int(limit))),
            )
            row = connection.execute(
                """
                SELECT * FROM working_files
                WHERE chat_id = ? AND path = ? AND start_line = ? AND end_line = ?
                """,
                (chat_id, path, start_line, end_line),
            ).fetchone()
        if row is None:
            raise KeyError((chat_id, path))
        return self._working_file_from_row(row)

    def list_working_files(self, chat_id: str) -> list[WorkingFile]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM working_files
                WHERE chat_id = ?
                ORDER BY updated_at DESC, id DESC
                """,
                (chat_id,),
            ).fetchall()
        return [self._working_file_from_row(row) for row in rows]

    def forget_working_files(self, chat_id: str, paths: list[str] | None = None) -> None:
        with self.connect() as connection:
            if paths:
                placeholders = ", ".join("?" for _path in paths)
                connection.execute(
                    f"DELETE FROM working_files WHERE chat_id = ? "
                    f"AND path IN ({placeholders})",
                    (chat_id, *paths),
                )
            elif paths is None:
                connection.execute("DELETE FROM working_files WHERE chat_id = ?", (chat_id,))

    def upsert_task_checkpoint(
        self,
        chat_id: str,
        *,
        objective: str,
        status: str,
        completed_work: str,
        changed_files: list[str],
        commands: list[str],
        failures: list[str],
        next_step: str,
        segment_count: int,
    ) -> TaskCheckpoint:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO task_checkpoints (
                    chat_id, objective, status, completed_work,
                    changed_files_json, commands_json, failures_json,
                    next_step, segment_count, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    objective = excluded.objective,
                    status = excluded.status,
                    completed_work = excluded.completed_work,
                    changed_files_json = excluded.changed_files_json,
                    commands_json = excluded.commands_json,
                    failures_json = excluded.failures_json,
                    next_step = excluded.next_step,
                    segment_count = excluded.segment_count,
                    updated_at = excluded.updated_at
                """,
                (
                    chat_id,
                    objective[:4000],
                    status,
                    completed_work[:6000],
                    json.dumps(sorted(set(changed_files)), ensure_ascii=False),
                    json.dumps(commands[-12:], ensure_ascii=False),
                    json.dumps(failures[-12:], ensure_ascii=False),
                    next_step[:2000],
                    max(0, int(segment_count)),
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM task_checkpoints WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        if row is None:
            raise KeyError(chat_id)
        return self._task_checkpoint_from_row(row)

    def get_task_checkpoint(self, chat_id: str) -> TaskCheckpoint | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_checkpoints WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return self._task_checkpoint_from_row(row) if row else None

    def remember_project_memory(
        self,
        project_id: str,
        source_chat_id: str,
        title: str,
        content: str,
    ) -> ProjectMemory:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO project_memories (
                    project_id, source_chat_id, title, content, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, source_chat_id) DO UPDATE SET
                    title = excluded.title,
                    content = excluded.content,
                    updated_at = excluded.updated_at
                """,
                (project_id, source_chat_id, title[:300], content[:12000], now),
            )
            row = connection.execute(
                """
                SELECT * FROM project_memories
                WHERE project_id = ? AND source_chat_id = ?
                """,
                (project_id, source_chat_id),
            ).fetchone()
            if row is not None and self.memory_fts_available:
                try:
                    connection.execute(
                        "DELETE FROM project_memory_fts WHERE rowid = ?", (row["id"],)
                    )
                    connection.execute(
                        """
                        INSERT INTO project_memory_fts(rowid, title, content)
                        VALUES (?, ?, ?)
                        """,
                        (row["id"], row["title"], row["content"]),
                    )
                except sqlite3.OperationalError:
                    self.memory_fts_available = False
        if row is None:
            raise KeyError((project_id, source_chat_id))
        return self._project_memory_from_row(row)

    def search_project_memories(
        self,
        project_id: str,
        query: str,
        *,
        exclude_chat_id: str = "",
        limit: int = 4,
    ) -> list[ProjectMemory]:
        terms = [
            term.casefold()
            for term in re.findall(r"[A-Za-z0-9_]{3,}", query)
            if term.casefold()
            not in {
                "and", "the", "this", "that", "with", "from", "into", "continue",
                "project", "projects", "file", "files", "code", "work", "working",
                "need", "needs", "then", "only", "please", "current", "correct",
                "fixed", "change", "changes", "update", "updated",
            }
        ][:10]
        if not terms:
            return []
        rows: list[sqlite3.Row] = []
        with self.connect() as connection:
            if self.memory_fts_available:
                match = " OR ".join(f'"{term}"' for term in terms)
                try:
                    rows = connection.execute(
                        """
                        SELECT pm.*
                        FROM project_memory_fts AS fts
                        JOIN project_memories AS pm ON pm.id = fts.rowid
                        WHERE project_memory_fts MATCH ?
                          AND pm.project_id = ?
                          AND pm.source_chat_id != ?
                        ORDER BY bm25(project_memory_fts), pm.updated_at DESC
                        LIMIT ?
                        """,
                        (match, project_id, exclude_chat_id, max(1, int(limit))),
                    ).fetchall()
                except sqlite3.OperationalError:
                    self.memory_fts_available = False
            if not rows:
                clauses = " OR ".join(
                    "(lower(title) LIKE ? OR lower(content) LIKE ?)" for _term in terms
                )
                patterns = [value for term in terms for value in (f"%{term}%", f"%{term}%")]
                rows = connection.execute(
                    f"""
                    SELECT * FROM project_memories
                    WHERE project_id = ? AND source_chat_id != ? AND ({clauses})
                    ORDER BY updated_at DESC
                    LIMIT ?
                    """,
                    (project_id, exclude_chat_id, *patterns, max(1, int(limit))),
                ).fetchall()
        return [self._project_memory_from_row(row) for row in rows]

    def remember_source_symbols(
        self,
        project_id: str,
        path: str,
        source_hash: str,
        symbols: str,
    ) -> SourceSymbols:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_symbols (
                    project_id, path, source_hash, symbols, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(project_id, path) DO UPDATE SET
                    source_hash = excluded.source_hash,
                    symbols = excluded.symbols,
                    updated_at = excluded.updated_at
                """,
                (project_id, path, source_hash, symbols[:16000], now),
            )
            row = connection.execute(
                "SELECT * FROM source_symbols WHERE project_id = ? AND path = ?",
                (project_id, path),
            ).fetchone()
        if row is None:
            raise KeyError((project_id, path))
        return self._source_symbols_from_row(row)

    def list_source_symbols(self, project_id: str) -> list[SourceSymbols]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM source_symbols WHERE project_id = ? ORDER BY path",
                (project_id,),
            ).fetchall()
        return [self._source_symbols_from_row(row) for row in rows]

    def forget_source_symbols(self, project_id: str, paths: list[str]) -> None:
        if not paths:
            return
        placeholders = ", ".join("?" for _path in paths)
        with self.connect() as connection:
            connection.execute(
                f"DELETE FROM source_symbols WHERE project_id = ? "
                f"AND path IN ({placeholders})",
                (project_id, *paths),
            )

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

    def add_provider(
        self,
        name: str,
        *,
        endpoint: str = "",
        api_key: str = "",
        is_local: bool = False,
        context_window: int = 32768,
    ) -> Provider:
        provider_id = str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO providers
                    (id, name, endpoint, api_key, is_local, default_context_window)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    provider_id,
                    name.strip(),
                    endpoint.rstrip("/"),
                    api_key,
                    int(is_local),
                    max(2048, int(context_window)),
                ),
            )
            row = connection.execute(
                "SELECT * FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
        return self._provider_from_row(row)

    def list_providers(self) -> list[Provider]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM providers ORDER BY is_local DESC, name COLLATE NOCASE"
            ).fetchall()
        return [self._provider_from_row(row) for row in rows]

    def get_provider(self, provider_id: str) -> Provider | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM providers WHERE id = ?", (provider_id,)
            ).fetchone()
        return self._provider_from_row(row) if row else None

    def remove_provider(self, provider_id: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM providers WHERE id = ?", (provider_id,))

    @staticmethod
    def _project_from_row(row: sqlite3.Row) -> Project:
        return Project(
            id=row["id"],
            name=row["name"],
            path=row["path"],
            model=row["model"],
            context_window=row["context_window"],
            permission_mode=row["permission_mode"],
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

    @staticmethod
    def _working_file_from_row(row: sqlite3.Row) -> WorkingFile:
        return WorkingFile(
            id=row["id"],
            chat_id=row["chat_id"],
            path=row["path"],
            start_line=row["start_line"],
            end_line=row["end_line"],
            content=row["content"],
            source_hash=row["source_hash"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _task_checkpoint_from_row(row: sqlite3.Row) -> TaskCheckpoint:
        def load_list(column: str) -> list[str]:
            try:
                value = json.loads(row[column])
            except (TypeError, json.JSONDecodeError):
                return []
            return [str(item) for item in value] if isinstance(value, list) else []

        return TaskCheckpoint(
            chat_id=row["chat_id"],
            objective=row["objective"],
            status=row["status"],
            completed_work=row["completed_work"],
            changed_files=load_list("changed_files_json"),
            commands=load_list("commands_json"),
            failures=load_list("failures_json"),
            next_step=row["next_step"],
            segment_count=row["segment_count"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _project_memory_from_row(row: sqlite3.Row) -> ProjectMemory:
        return ProjectMemory(
            id=row["id"],
            project_id=row["project_id"],
            source_chat_id=row["source_chat_id"],
            title=row["title"],
            content=row["content"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _source_symbols_from_row(row: sqlite3.Row) -> SourceSymbols:
        return SourceSymbols(
            id=row["id"],
            project_id=row["project_id"],
            path=row["path"],
            source_hash=row["source_hash"],
            symbols=row["symbols"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _provider_from_row(row: sqlite3.Row) -> Provider:
        return Provider(
            id=row["id"],
            name=row["name"],
            endpoint=row["endpoint"],
            api_key=row["api_key"],
            is_local=bool(row["is_local"]),
            default_context_window=row["default_context_window"],
        )
