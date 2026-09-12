from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from localcode.database import BASE_SCHEMA, MIGRATIONS, SCHEMA_VERSION, Database


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = Database(self.root / "localcode.db")
        self.project_root = self.root / "project"
        self.project_root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_project_chat_and_messages_round_trip(self) -> None:
        project = self.database.add_project(self.project_root, model="code-model")
        duplicate = self.database.add_project(self.project_root)
        self.assertEqual(project.id, duplicate.id)

        chat = self.database.create_chat(project.id)
        first = self.database.add_message(chat.id, "user", "Build it")
        second = self.database.add_message(
            chat.id,
            "assistant",
            "Implemented.",
            {"prompt_tokens": 120, "changed_files": ["main.py"]},
        )

        messages = self.database.list_messages(chat.id)
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertEqual(messages[1].metadata["prompt_tokens"], 120)
        self.assertEqual(self.database.active_messages(chat), [first, second])

    def test_compaction_changes_active_view_not_transcript(self) -> None:
        project = self.database.add_project(self.project_root)
        chat = self.database.create_chat(project.id)
        messages = [
            self.database.add_message(chat.id, role, f"message {index}")
            for index, role in enumerate(["user", "assistant", "user", "assistant"], 1)
        ]

        compacted = self.database.compact_chat(chat.id, "Durable handoff", messages[1].id)

        self.assertEqual(len(self.database.list_messages(chat.id)), 4)
        self.assertEqual(
            [message.id for message in self.database.active_messages(compacted)],
            [messages[2].id, messages[3].id],
        )
        self.assertEqual(compacted.compaction_summary, "Durable handoff")

    def test_cascading_project_removal(self) -> None:
        project = self.database.add_project(self.project_root)
        chat = self.database.create_chat(project.id)
        self.database.add_message(chat.id, "user", "hello")
        self.database.remove_project(project.id)
        self.assertIsNone(self.database.get_chat(chat.id))

    def test_fresh_database_has_current_schema_version(self) -> None:
        with self.database.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
        self.assertEqual(version, SCHEMA_VERSION)
        self.assertTrue(
            {
                "projects",
                "chats",
                "messages",
                "activities",
                "settings",
                "providers",
                "working_files",
                "task_checkpoints",
                "project_memories",
                "source_symbols",
            }.issubset(tables)
        )

    def test_migrations_apply_once_and_bump_the_version(self) -> None:
        migration = (2, "CREATE TABLE migration_probe (id INTEGER PRIMARY KEY)")
        with patch("localcode.database.MIGRATIONS", (migration,)):
            migrated = Database(self.root / "migrated.db")
        with migrated.connect() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            probe = connection.execute(
                "SELECT name FROM sqlite_master WHERE name = 'migration_probe'"
            ).fetchone()
        self.assertEqual(version, 2)
        self.assertIsNotNone(probe)

        with patch("localcode.database.MIGRATIONS", (migration,)):
            reopened = Database(self.root / "migrated.db")
        with reopened.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 2)

    def test_existing_database_without_version_gets_base_schema(self) -> None:
        path = self.root / "legacy.db"
        path.touch()
        database = Database(path)
        project = database.add_project(self.project_root)
        self.assertTrue(database.get_project(project.id))
        with database.connect() as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION
            )

    def test_memory_column_is_removed_by_current_migration(self) -> None:
        with self.database.connect() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(projects)")
            }
        self.assertNotIn("memory_enabled", columns)

    def test_memory_removal_migration_preserves_existing_projects(self) -> None:
        path = self.root / "version-one.db"
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.executescript(BASE_SCHEMA)
                connection.execute("PRAGMA user_version = 1")
                connection.execute(
                    """
                    INSERT INTO projects (
                        id, name, path, model, context_window, permission_mode,
                        memory_enabled, created_at, updated_at, last_opened_at
                    ) VALUES ('project-1', 'Existing', ?, 'qwen3:8b', 32768, 'ask', 1, '', '', '')
                    """,
                    (str(self.project_root),),
                )

        migrated = Database(path)
        project = migrated.get_project("project-1")
        self.assertIsNotNone(project)
        self.assertEqual(project.model, "qwen3:8b")
        with migrated.connect() as connection:
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(projects)")
            }
        self.assertNotIn("memory_enabled", columns)

    def test_working_files_upsert_prune_and_cascade(self) -> None:
        project = self.database.add_project(self.project_root)
        chat = self.database.create_chat(project.id)
        first = self.database.remember_working_file(
            chat.id, "main.py", 1, 20, "main.py lines 1-20", "hash-one", limit=2
        )
        replaced = self.database.remember_working_file(
            chat.id, "main.py", 10, 30, "main.py lines 10-30", "hash-two", limit=2
        )
        self.assertNotEqual(first.id, replaced.id)
        self.assertEqual(replaced.start_line, 10)
        self.assertEqual(len(self.database.list_working_files(chat.id)), 1)
        self.database.remember_working_file(
            chat.id, "other.py", 1, 2, "other", "hash-three", limit=2
        )
        self.database.remember_working_file(
            chat.id, "third.py", 1, 2, "third", "hash-four", limit=2
        )
        paths = {item.path for item in self.database.list_working_files(chat.id)}
        self.assertEqual(paths, {"other.py", "third.py"})

        self.database.remove_project(project.id)
        self.assertEqual(self.database.list_working_files(chat.id), [])

    def test_working_files_keep_disjoint_ranges_and_prefer_covering_range(self) -> None:
        project = self.database.add_project(self.project_root)
        chat = self.database.create_chat(project.id)
        broad = self.database.remember_working_file(
            chat.id, "main.py", 1, 100, "broad", "same-hash"
        )
        contained = self.database.remember_working_file(
            chat.id, "main.py", 20, 40, "contained", "same-hash"
        )
        self.assertEqual(contained.id, broad.id)

        self.database.remember_working_file(
            chat.id, "main.py", 150, 200, "later", "same-hash"
        )
        ranges = {
            (item.start_line, item.end_line)
            for item in self.database.list_working_files(chat.id)
        }
        self.assertEqual(ranges, {(1, 100), (150, 200)})

    def test_version_three_working_file_survives_memory_migration(self) -> None:
        path = self.root / "version-three.db"
        with closing(sqlite3.connect(path)) as connection:
            with connection:
                connection.executescript(BASE_SCHEMA)
                connection.executescript(MIGRATIONS[0][1])
                connection.executescript(MIGRATIONS[1][1])
                connection.execute("PRAGMA user_version = 3")
                connection.execute(
                    """
                    INSERT INTO projects (
                        id, name, path, model, context_window, permission_mode,
                        created_at, updated_at, last_opened_at
                    ) VALUES ('p1', 'Existing', ?, '', 32768, 'ask', '', '', '')
                    """,
                    (str(self.project_root),),
                )
                connection.execute(
                    """
                    INSERT INTO chats (id, project_id, title, created_at, updated_at)
                    VALUES ('c1', 'p1', 'Existing chat', '', '')
                    """
                )
                connection.execute(
                    """
                    INSERT INTO working_files (
                        chat_id, path, start_line, end_line, content, source_hash, updated_at
                    ) VALUES ('c1', 'main.py', 1, 2, 'observed', 'hash', '')
                    """
                )

        migrated = Database(path)
        observations = migrated.list_working_files("c1")
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0].content, "observed")
        with migrated.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertIsNotNone(
                connection.execute(
                    "SELECT name FROM sqlite_master WHERE name = 'task_checkpoints'"
                ).fetchone()
            )

    def test_checkpoint_project_memory_and_symbols_round_trip(self) -> None:
        project = self.database.add_project(self.project_root)
        source_chat = self.database.create_chat(project.id, "Build dashboard")
        other_chat = self.database.create_chat(project.id, "Follow-up")
        checkpoint = self.database.upsert_task_checkpoint(
            source_chat.id,
            objective="Build the sensor dashboard",
            status="needs-continuation",
            completed_work="Added the chart shell.",
            changed_files=["app.py", "app.py"],
            commands=["python3 -m unittest"],
            failures=["read_file: missing config"],
            next_step="Wire the sensor endpoint.",
            segment_count=2,
        )
        self.assertEqual(checkpoint.changed_files, ["app.py"])
        self.assertEqual(
            self.database.get_task_checkpoint(source_chat.id).next_step,
            "Wire the sensor endpoint.",
        )

        self.database.remember_project_memory(
            project.id,
            source_chat.id,
            source_chat.title,
            "Sensor dashboard chart shell and endpoint work",
        )
        matches = self.database.search_project_memories(
            project.id, "sensor endpoint", exclude_chat_id=other_chat.id
        )
        self.assertEqual([item.source_chat_id for item in matches], [source_chat.id])

        symbol = self.database.remember_source_symbols(
            project.id, "app.py", "abc123", "function render_dashboard (line 12)"
        )
        self.assertEqual(symbol.path, "app.py")
        self.assertEqual(self.database.list_source_symbols(project.id), [symbol])

        self.database.delete_chat(source_chat.id)
        self.assertIsNone(self.database.get_task_checkpoint(source_chat.id))
        self.assertEqual(
            self.database.search_project_memories(project.id, "sensor endpoint"), []
        )


if __name__ == "__main__":
    unittest.main()
