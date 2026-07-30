from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcode.memory import MemPalaceManager, wing_name
from localcode.models import Project


class MemoryTests(unittest.TestCase):
    def test_wing_name_is_stable_and_safe(self) -> None:
        project = Project(id="12345678", name="Renamed Display Label", path="/tmp/My C++ App!")
        self.assertEqual(wing_name(project), "my_c_app_12345678")

    def test_same_folder_names_use_distinct_wings(self) -> None:
        first = Project(id="aaaaaaaa-0000", name="App", path="/one/app")
        second = Project(id="bbbbbbbb-0000", name="App", path="/two/app")
        self.assertNotEqual(wing_name(first), wing_name(second))

    def test_prune_only_cleanup_never_mines_disabled_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            previous = os.environ.get("LOCALCODE_DATA_HOME")
            os.environ["LOCALCODE_DATA_HOME"] = str(Path(temporary) / "data")
            try:
                root = Path(temporary) / "project"
                root.mkdir()
                project = Project(
                    id="cccccccc-0000",
                    name="Project",
                    path=str(root),
                    memory_enabled=False,
                )
                manager = MemPalaceManager()
                calls: list[list[str]] = []

                def fake_run(arguments, **_kwargs):
                    calls.append(arguments)
                    return subprocess.CompletedProcess(arguments, 0, "ok", "")

                with patch.object(manager, "executable", return_value="mempalace"), patch.object(
                    manager, "_run", side_effect=fake_run
                ):
                    success, _detail = manager.prune_project(project)
                self.assertTrue(success)
                self.assertTrue(calls)
                self.assertTrue(all("sync" in arguments for arguments in calls))
                self.assertTrue(all("mine" not in arguments for arguments in calls))
            finally:
                if previous is None:
                    os.environ.pop("LOCALCODE_DATA_HOME", None)
                else:
                    os.environ["LOCALCODE_DATA_HOME"] = previous


if __name__ == "__main__":
    unittest.main()
