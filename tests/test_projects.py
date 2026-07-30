from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from localcode.projects import ProjectTools, resolve_inside


class ProjectToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_paths_cannot_escape_project(self) -> None:
        with self.assertRaises(ValueError):
            resolve_inside(self.root, "../secret")
        outside = self.root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        (self.root / "link").symlink_to(outside)
        with self.assertRaises(ValueError):
            resolve_inside(self.root, "link")

    def test_mutations_reject_symlinks_even_when_target_is_inside(self) -> None:
        target = self.root / "target.txt"
        target.write_text("keep", encoding="utf-8")
        (self.root / "link.txt").symlink_to(target)
        tools = ProjectTools(self.root, permission_mode="allow")
        result = tools.execute("delete_file", {"path": "link.txt"})
        self.assertFalse(result.success)
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")
        self.assertTrue((self.root / "link.txt").is_symlink())

    def test_write_read_replace_search_and_delete(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        written = tools.execute("write_file", {"path": "src/main.py", "content": "print('one')\n"})
        self.assertTrue(written.success)
        self.assertIn("src/main.py", tools.changed_files)

        read = tools.execute("read_file", {"path": "src/main.py"})
        self.assertIn("print('one')", read.output)
        replaced = tools.execute(
            "replace_in_file",
            {"path": "src/main.py", "old_text": "one", "new_text": "two"},
        )
        self.assertTrue(replaced.success)
        searched = tools.execute("search_files", {"query": "two", "pattern": "*.py"})
        self.assertIn("src/main.py:1", searched.output)
        self.assertTrue(tools.execute("delete_file", {"path": "src/main.py"}).success)

    def test_ask_and_read_only_modes_block_mutations(self) -> None:
        declined = ProjectTools(
            self.root, permission_mode="ask", approve=lambda _name, _body: False
        )
        result = declined.execute("write_file", {"path": "no.txt", "content": "no"})
        self.assertFalse(result.success)
        self.assertFalse((self.root / "no.txt").exists())

        read_only = ProjectTools(self.root, permission_mode="read-only")
        result = read_only.execute("run_command", {"command": "touch forbidden"})
        self.assertFalse(result.success)
        self.assertFalse((self.root / "forbidden").exists())

    def test_run_command_detects_changed_files(self) -> None:
        tools = ProjectTools(
            self.root,
            permission_mode="allow",
            approve=lambda _name, _body: True,
        )
        result = tools.execute("run_command", {"command": "printf test > generated.txt"})
        self.assertTrue(result.success)
        self.assertIn("generated.txt", tools.changed_files)

    def test_shell_command_always_requires_explicit_approval(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        result = tools.execute("run_command", {"command": "touch should-not-exist"})
        self.assertFalse(result.success)
        self.assertFalse((self.root / "should-not-exist").exists())

    def test_running_shell_command_can_be_cancelled(self) -> None:
        cancelled = threading.Event()
        tools = ProjectTools(
            self.root,
            permission_mode="allow",
            approve=lambda _name, _body: True,
            cancel=cancelled,
        )
        results = []
        worker = threading.Thread(
            target=lambda: results.append(
                tools.execute("run_command", {"command": "sleep 10", "timeout": 30})
            )
        )
        started = time.monotonic()
        worker.start()
        time.sleep(0.2)
        cancelled.set()
        worker.join(timeout=3)
        self.assertFalse(worker.is_alive())
        self.assertFalse(results[0].success)
        self.assertIn("Cancelled", results[0].output)
        self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
