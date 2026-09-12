from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcode.database import Database
from localcode.settings import AppSettings


class AppSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary.name) / "localcode.db")
        self.settings = AppSettings(self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_change_scope_defaults_to_standard(self) -> None:
        self.assertEqual(self.settings.change_scope, "standard")
        self.assertEqual(self.settings.max_tool_rounds, 16)
        self.assertEqual(self.settings.max_continuation_segments, 2)

    def test_legacy_ponytail_scope_is_preserved(self) -> None:
        self.database.set_setting("code_style", "ponytail")
        self.assertEqual(self.settings.change_scope, "focused")

    def test_removed_verbose_scope_becomes_standard(self) -> None:
        self.database.set_setting("code_style", "verbose")
        self.assertEqual(self.settings.change_scope, "standard")

    def test_new_change_scope_overrides_legacy_value(self) -> None:
        self.database.set_setting("code_style", "ponytail")
        self.database.set_setting("change_scope", "standard")
        self.assertEqual(self.settings.change_scope, "standard")


if __name__ == "__main__":
    unittest.main()
