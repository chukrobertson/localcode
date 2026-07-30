from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcode.database import Database


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


if __name__ == "__main__":
    unittest.main()
