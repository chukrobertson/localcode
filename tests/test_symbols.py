from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from localcode.database import Database
from localcode.symbols import extract_symbols, refresh_symbol_context


class SymbolIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.database = Database(self.root / "localcode.db")
        self.project = self.database.add_project(self.project_root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_python_symbols_include_qualified_names(self) -> None:
        content = """class Dashboard:
    def render(self):
        pass

async def load_sensor():
    pass
"""
        symbols = extract_symbols(Path("app.py"), content)
        self.assertIn("class Dashboard (line 1)", symbols)
        self.assertIn("function Dashboard.render (line 2)", symbols)
        self.assertIn("function load_sensor (line 5)", symbols)

    def test_refresh_is_relevant_bounded_and_invalidates_deleted_files(self) -> None:
        app = self.project_root / "app.py"
        app.write_text(
            "def render_dashboard():\n    return 'ok'\n", encoding="utf-8"
        )
        (self.project_root / "other.js").write_text(
            "function unrelatedWidget() {}\n", encoding="utf-8"
        )

        context = refresh_symbol_context(
            self.database, self.project, "fix render dashboard", token_budget=300
        )
        self.assertIn("app.py", context)
        self.assertIn("render_dashboard", context)
        self.assertNotIn("unrelatedWidget", context)

        app.unlink()
        refresh_symbol_context(self.database, self.project, "dashboard")
        paths = {item.path for item in self.database.list_source_symbols(self.project.id)}
        self.assertNotIn("app.py", paths)


if __name__ == "__main__":
    unittest.main()
