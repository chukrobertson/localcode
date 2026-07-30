from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcode.agents_file import END_MARKER, START_MARKER, AgentsFileManager


class AgentsFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_creates_canonical_agents_file(self) -> None:
        manager = AgentsFileManager(self.root)
        path = manager.ensure()
        content = path.read_text(encoding="utf-8")
        self.assertEqual(path.name, "AGENTS.md")
        self.assertIn(START_MARKER, content)
        self.assertIn(END_MARKER, content)

    def test_model_update_preserves_human_notes(self) -> None:
        manager = AgentsFileManager(self.root)
        manager.ensure()
        with manager.path.open("a", encoding="utf-8") as handle:
            handle.write("\nNever remove this note.\n")

        changed = manager.apply_model_update("```markdown\n## Architecture\n\n- Uses SQLite.\n```")

        self.assertTrue(changed)
        content = manager.read()
        self.assertIn("## Architecture", content)
        self.assertIn("Never remove this note.", content)

    def test_existing_file_gets_managed_section_without_overwrite(self) -> None:
        (self.root / "AGENTS.md").write_text("# Team Rules\n\nDo not rewrite.\n", encoding="utf-8")
        manager = AgentsFileManager(self.root)
        manager.ensure()
        content = manager.read()
        self.assertTrue(content.startswith("# Team Rules"))
        self.assertIn("Do not rewrite.", content)
        self.assertIn(START_MARKER, content)

    def test_rejects_symlinked_agents_file(self) -> None:
        outside = self.root.parent / "outside-agents.md"
        outside.write_text("outside", encoding="utf-8")
        (self.root / "AGENTS.md").symlink_to(outside)
        with self.assertRaises(ValueError):
            AgentsFileManager(self.root).ensure()
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside")

    def test_rejects_malformed_managed_markers(self) -> None:
        (self.root / "AGENTS.md").write_text(
            f"# Rules\n{START_MARKER}\nhuman notes without an end marker\n",
            encoding="utf-8",
        )
        with self.assertRaises(ValueError):
            AgentsFileManager(self.root).ensure()

    def test_update_breaks_hard_link_instead_of_writing_outside(self) -> None:
        outside = self.root.parent / "shared-agents.md"
        outside.write_text("# Shared instructions\n", encoding="utf-8")
        (self.root / "AGENTS.md").hardlink_to(outside)
        manager = AgentsFileManager(self.root)
        manager.ensure()
        self.assertEqual(outside.read_text(encoding="utf-8"), "# Shared instructions\n")
        self.assertIn(START_MARKER, manager.read())


if __name__ == "__main__":
    unittest.main()
