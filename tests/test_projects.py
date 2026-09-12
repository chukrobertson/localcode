from __future__ import annotations

import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from localcode import projects as projects_module
from localcode.managed_files import AGENTS_END_MARKER, AGENTS_START_MARKER
from localcode.projects import (
    ProjectTools,
    TOOL_REGISTRY,
    _GuardedRedirectHandler,
    file_sha256,
    resolve_inside,
    validate_web_url,
)


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
        self.assertEqual(written.status, "success")
        self.assertEqual(written.changed_files, ("src/main.py",))
        self.assertTrue(written.observations)
        self.assertIn("print('one')", written.output)
        self.assertIn("src/main.py", tools.changed_files)

        read = tools.execute("read_file", {"path": "src/main.py"})
        self.assertEqual(read.status, "skipped")
        self.assertIn("Unchanged read omitted", read.output)
        replaced = tools.execute(
            "replace_in_file",
            {"path": "src/main.py", "old_text": "one", "new_text": "two"},
        )
        self.assertTrue(replaced.success)
        self.assertEqual(replaced.changed_files, ("src/main.py",))
        self.assertIn("print('two')", replaced.output)
        searched = tools.execute("search_files", {"query": "two", "pattern": "*.py"})
        self.assertIn("src/main.py:1", searched.output)
        self.assertTrue(tools.execute("delete_file", {"path": "src/main.py"}).success)

    def test_tool_registry_is_the_schema_and_policy_source(self) -> None:
        definitions = ProjectTools.definitions()
        self.assertEqual(len(definitions), 20)
        self.assertEqual(
            {item["function"]["name"] for item in definitions},
            set(TOOL_REGISTRY),
        )
        self.assertTrue(
            all(
                item["function"]["parameters"]["additionalProperties"] is False
                for item in definitions
            )
        )
        nested = TOOL_REGISTRY["edit_file"].properties["edits"]["items"]
        self.assertIs(nested["additionalProperties"], False)
        self.assertEqual(
            [item["function"]["name"] for item in ProjectTools.definitions({"read_file"})],
            ["read_file"],
        )
        self.assertTrue(TOOL_REGISTRY["write_file"].mutates_project)
        self.assertTrue(TOOL_REGISTRY["run_command"].always_approve)

    def test_phase_palettes_are_bounded_and_read_only_safe(self) -> None:
        def names(phase: str, permission_mode: str = "allow") -> set[str]:
            return {
                item["function"]["name"]
                for item in ProjectTools.definitions_for_phase(
                    phase, permission_mode=permission_mode
                )
            }

        inspect = names("inspect")
        edit = names("edit")
        verify = names("verify")
        self.assertIn("git_log", inspect)
        self.assertNotIn("write_file", inspect)
        self.assertIn("copy_file", edit)
        self.assertNotIn("run_lint", edit)
        self.assertIn("run_lint", verify)
        self.assertIn("git_status", verify)
        self.assertIn("delete_file", verify)
        self.assertIn("create_directory", verify)
        self.assertIn("rename_file", verify)
        self.assertIn("copy_file", verify)

        read_only = names("edit", "read-only")
        self.assertIn("read_file", read_only)
        self.assertNotIn("write_file", read_only)
        self.assertNotIn("run_command", read_only)
        with self.assertRaises(ValueError):
            ProjectTools.definitions_for_phase("unknown")

    def test_explicit_result_states_and_argument_rejection(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        blocked = tools.execute("write_file", {"path": "x.txt", "content": "x", "typo": 1})
        self.assertEqual(blocked.status, "error")
        self.assertFalse(blocked.success)
        self.assertIn("Unexpected argument", blocked.output)

        first = tools.execute("write_file", {"path": "x.txt", "content": "x"})
        unchanged = tools.execute("write_file", {"path": "x.txt", "content": "x"})
        self.assertEqual(first.status, "success")
        self.assertEqual(unchanged.status, "noop")
        self.assertTrue(unchanged.success)
        self.assertEqual(unchanged.changed_files, ())

    def test_mutations_return_verified_post_edit_observations(self) -> None:
        path = self.root / "source.py"
        path.write_text("before = True\n", encoding="utf-8")
        tools = ProjectTools(self.root, permission_mode="allow")
        result = tools.execute(
            "edit_file",
            {
                "path": "source.py",
                "edits": [{"old_text": "before", "new_text": "after"}],
            },
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.changed_files, ("source.py",))
        self.assertEqual(result.observations[0].source_hash, file_sha256(path))
        self.assertIn("after = True", result.observations[0].content)
        self.assertIn("Post-edit verification", result.output)

    def test_ask_and_read_only_modes_block_mutations(self) -> None:
        declined = ProjectTools(
            self.root, permission_mode="ask", approve=lambda _name, _body: False
        )
        result = declined.execute("write_file", {"path": "no.txt", "content": "no"})
        self.assertFalse(result.success)
        self.assertFalse((self.root / "no.txt").exists())
        editable = self.root / "editable.txt"
        editable.write_text("before", encoding="utf-8")
        result = declined.execute(
            "edit_file",
            {
                "path": "editable.txt",
                "edits": [{"old_text": "before", "new_text": "after"}],
            },
        )
        self.assertFalse(result.success)
        self.assertEqual(editable.read_text(encoding="utf-8"), "before")

        read_only = ProjectTools(self.root, permission_mode="read-only")
        result = read_only.execute("run_command", {"command": "touch forbidden"})
        self.assertFalse(result.success)
        self.assertFalse((self.root / "forbidden").exists())
        result = read_only.execute(
            "edit_file",
            {
                "path": "editable.txt",
                "edits": [{"old_text": "before", "new_text": "after"}],
            },
        )
        self.assertFalse(result.success)
        self.assertEqual(editable.read_text(encoding="utf-8"), "before")

    def test_run_command_detects_changed_files(self) -> None:
        tools = ProjectTools(
            self.root,
            permission_mode="allow",
            approve=lambda _name, _body: True,
        )
        result = tools.execute("run_command", {"command": "printf test > generated.txt"})
        self.assertTrue(result.success)
        self.assertIn("generated.txt", tools.changed_files)

    def test_run_command_reports_pipeline_failure(self) -> None:
        tools = ProjectTools(
            self.root,
            permission_mode="allow",
            approve=lambda _name, _body: True,
        )
        result = tools.execute("run_command", {"command": "false | head -n 1"})
        self.assertFalse(result.success)
        self.assertIn("Exit code: 1", result.output)

    def test_agents_rewrite_must_preserve_markers_and_human_notes(self) -> None:
        original = (
            f"# AGENTS.md\n\n{AGENTS_START_MARKER}\nold managed\n"
            f"{AGENTS_END_MARKER}\n\n## Project Notes\n\nKeep this.\n"
        )
        path = self.root / "AGENTS.md"
        path.write_text(original, encoding="utf-8")
        tools = ProjectTools(self.root, permission_mode="allow")

        malformed = tools.execute(
            "write_file",
            {"path": "AGENTS.md", "content": original.replace(AGENTS_END_MARKER, "")},
        )
        self.assertFalse(malformed.success)
        self.assertIn("malformed", malformed.output)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

        changed_notes = tools.execute(
            "write_file",
            {
                "path": "AGENTS.md",
                "content": original.replace("old managed", "new managed").replace(
                    "Keep this.", "Changed note."
                ),
            },
        )
        self.assertFalse(changed_notes.success)
        self.assertIn("outside", changed_notes.output)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

        valid = tools.execute(
            "replace_in_file",
            {
                "path": "AGENTS.md",
                "old_text": "old managed",
                "new_text": "new managed",
            },
        )
        self.assertTrue(valid.success)
        self.assertIn("new managed", path.read_text(encoding="utf-8"))
        self.assertIn("Keep this.", path.read_text(encoding="utf-8"))

    def test_shell_cannot_damage_agents_markers(self) -> None:
        original = (
            f"# AGENTS.md\n{AGENTS_START_MARKER}\nmanaged\n"
            f"{AGENTS_END_MARKER}\nnotes\n"
        )
        path = self.root / "AGENTS.md"
        path.write_text(original, encoding="utf-8")
        tools = ProjectTools(
            self.root,
            permission_mode="allow",
            approve=lambda _name, _body: True,
        )
        result = tools.execute(
            "run_command",
            {"command": "sed -i '/localcode:managed:end/d' AGENTS.md"},
        )
        self.assertFalse(result.success)
        self.assertIn("Original restored", result.output)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_run_command_uses_git_status_in_git_repositories(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "tracked.txt").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "add", "tracked.txt"], cwd=self.root, check=True)
        tools = ProjectTools(
            self.root,
            permission_mode="allow",
            approve=lambda _name, _body: True,
        )
        result = tools.execute(
            "run_command",
            {"command": "printf changed > tracked.txt && printf new > generated.txt"},
        )
        self.assertTrue(result.success)
        self.assertIn("tracked.txt", tools.changed_files)
        self.assertIn("generated.txt", tools.changed_files)

        quiet = ProjectTools(
            self.root,
            permission_mode="allow",
            approve=lambda _name, _body: True,
        )
        result = quiet.execute("run_command", {"command": "echo untouched"})
        self.assertTrue(result.success)
        self.assertEqual(quiet.changed_files, set())

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
        self.assertEqual(results[0].status, "blocked")
        self.assertIn("Cancelled", results[0].output)
        self.assertLess(time.monotonic() - started, 3)

    def test_create_and_rename_directory_and_file(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        result = tools.execute("create_directory", {"path": "src/lib"})
        self.assertTrue(result.success)
        self.assertTrue((self.root / "src" / "lib").is_dir())
        (self.root / "src" / "lib" / "mod.py").write_text("# module", encoding="utf-8")
        result = tools.execute(
            "rename_file", {"source": "src/lib/mod.py", "target": "src/mod.py"}
        )
        self.assertTrue(result.success)
        self.assertTrue((self.root / "src" / "mod.py").is_file())
        self.assertFalse((self.root / "src" / "lib" / "mod.py").exists())

    def test_copy_file_handles_binary_assets_and_overwrite(self) -> None:
        source = self.root / "image.png"
        source.write_bytes(b"\x89PNG\r\n\x1a\nsource")
        target = self.root / "assets" / "image.png"
        tools = ProjectTools(self.root, permission_mode="allow")

        copied = tools.execute(
            "copy_file", {"source": "image.png", "target": "assets/image.png"}
        )
        self.assertEqual(copied.status, "success")
        self.assertEqual(copied.changed_files, ("assets/image.png",))
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertIn("Text preview omitted", copied.output)

        unchanged = tools.execute(
            "copy_file", {"source": "image.png", "target": "assets/image.png"}
        )
        self.assertEqual(unchanged.status, "noop")
        source.write_bytes(b"\x89PNG\r\n\x1a\nupdated")
        refused = tools.execute(
            "copy_file", {"source": "image.png", "target": "assets/image.png"}
        )
        self.assertEqual(refused.status, "error")
        overwritten = tools.execute(
            "copy_file",
            {
                "source": "image.png",
                "target": "assets/image.png",
                "overwrite": True,
            },
        )
        self.assertEqual(overwritten.status, "success")
        self.assertEqual(target.read_bytes(), source.read_bytes())

    def test_git_status_and_project_commands_are_read_only_observations(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "pyproject.toml").write_text(
            "[project]\nname='demo'\n", encoding="utf-8"
        )
        (self.root / "tests").mkdir()
        tools = ProjectTools(self.root, permission_mode="read-only")

        status = tools.execute("git_status", {})
        commands = tools.execute("project_commands", {})
        self.assertTrue(status.success)
        self.assertEqual(status.status, "success")
        self.assertIn("pyproject.toml", status.output)
        self.assertTrue(commands.success)
        self.assertIn("python3 -m pytest", commands.output)
        self.assertIn("python3 -m compileall .", commands.output)
        self.assertEqual(tools.changed_files, set())
        self.assertEqual(tools.called_tools, {"git_status", "project_commands"})

        plain_root = self.root / "plain"
        plain_root.mkdir()
        not_git = ProjectTools(plain_root, permission_mode="read-only").execute(
            "git_status", {}
        )
        self.assertEqual(not_git.status, "skipped")
        self.assertTrue(not_git.success)

    def test_git_diff_and_log_require_git_repo(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        self.assertFalse(tools.execute("git_diff", {}).success)
        self.assertFalse(tools.execute("git_log", {}).success)

    def test_web_fetch_rejects_loopback(self) -> None:
        tools = ProjectTools(
            self.root, permission_mode="allow", approve=lambda _name, _body: True
        )
        result = tools.execute("web_fetch", {"url": "http://127.0.0.1:9999/test"})
        self.assertFalse(result.success)
        result = tools.execute("web_fetch", {"url": "file:///etc/passwd"})
        self.assertFalse(result.success)

    def test_batch_read_files(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        (self.root / "a.py").write_text("a = 1\n", encoding="utf-8")
        (self.root / "b.py").write_text("b = 2\n", encoding="utf-8")
        result = tools.execute("read_files", {"paths": ["a.py", "b.py"]})
        self.assertTrue(result.success)
        self.assertIn("=== a.py", result.output)
        self.assertIn("=== b.py", result.output)
        self.assertIn("a = 1", result.output)
        self.assertIn("b = 2", result.output)

    def test_repeated_unchanged_read_is_deduplicated(self) -> None:
        (self.root / "large.py").write_text(
            "\n".join(f"value_{index} = {index}" for index in range(300)),
            encoding="utf-8",
        )
        tools = ProjectTools(self.root, permission_mode="allow")
        first = tools.execute("read_file", {"path": "large.py"})
        second = tools.execute("read_file", {"path": "large.py"})
        self.assertTrue(first.observations)
        self.assertIn("value_0", first.output)
        self.assertIn("Unchanged read omitted", second.output)

        (self.root / "large.py").write_text("changed = True\n", encoding="utf-8")
        changed = tools.execute("read_file", {"path": "large.py"})
        self.assertIn("changed = True", changed.output)

    def test_read_file_defaults_to_a_bounded_range(self) -> None:
        (self.root / "large.py").write_text(
            "\n".join(f"line_{index}" for index in range(500)), encoding="utf-8"
        )
        result = ProjectTools(self.root).execute("read_file", {"path": "large.py"})
        observation = result.observations[0]
        self.assertEqual(observation.start_line, 1)
        self.assertEqual(observation.end_line, 200)
        self.assertNotIn("line_250", result.output)

    def test_replace_lines_requires_a_current_verified_read(self) -> None:
        path = self.root / "gui.py"
        path.write_text(
            "class GUI:\n"
            "    def __init__(self, root):\n"
            "        self.root = root\n"
            "        self.root = root\n"
            "        self.root.title = 'demo'\n",
            encoding="utf-8",
        )
        tools = ProjectTools(self.root, permission_mode="allow")
        unread = tools.execute(
            "replace_lines",
            {
                "path": "gui.py",
                "start_line": 4,
                "end_line": 5,
                "new_text": "        self.root.title('demo')",
            },
        )
        self.assertFalse(unread.success)
        self.assertIn("not verified", unread.output)

        tools.execute(
            "read_file",
            {"path": "gui.py", "start_line": 2, "end_line": 5},
        )
        replaced = tools.execute(
            "replace_lines",
            {
                "path": "gui.py",
                "start_line": 4,
                "end_line": 5,
                "new_text": "        self.root.title('demo')",
            },
        )
        self.assertTrue(replaced.success)
        self.assertEqual(
            path.read_text(encoding="utf-8"),
            "class GUI:\n"
            "    def __init__(self, root):\n"
            "        self.root = root\n"
            "        self.root.title('demo')\n",
        )

    def test_replace_lines_rejects_a_stale_read(self) -> None:
        path = self.root / "source.py"
        path.write_text("one\ntwo\nthree\n", encoding="utf-8")
        tools = ProjectTools(self.root, permission_mode="allow")
        tools.execute(
            "read_file",
            {"path": "source.py", "start_line": 1, "end_line": 3},
        )
        path.write_text("inserted\none\ntwo\nthree\n", encoding="utf-8")

        result = tools.execute(
            "replace_lines",
            {
                "path": "source.py",
                "start_line": 2,
                "end_line": 2,
                "new_text": "changed",
            },
        )

        self.assertFalse(result.success)
        self.assertIn("not verified", result.output)
        self.assertEqual(path.read_text(encoding="utf-8"), "inserted\none\ntwo\nthree\n")

    def test_edit_file_applies_multiple_changes(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        (self.root / "cfg.py").write_text("host = 'old'\nport = 3000\n", encoding="utf-8")
        result = tools.execute(
            "edit_file",
            {
                "path": "cfg.py",
                "edits": [
                    {"old_text": "host = 'old'", "new_text": "host = 'new'"},
                    {"old_text": "port = 3000", "new_text": "port = 4000"},
                ],
            },
        )
        self.assertTrue(result.success)
        content = (self.root / "cfg.py").read_text(encoding="utf-8")
        self.assertEqual(content, "host = 'new'\nport = 4000\n")

        too_many = tools.execute(
            "edit_file",
            {
                "path": "cfg.py",
                "edits": [
                    {"old_text": f"missing-{index}", "new_text": "x"}
                    for index in range(9)
                ],
            },
        )
        self.assertFalse(too_many.success)
        self.assertIn("at most 8", too_many.output)

    def test_exact_edits_recover_unique_indentation_drift(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        path = self.root / "providers.ts"
        original = (
            "function getDefaultModel(provider: string): string {\n"
            "  const defaults = {\n"
            "    ollama: 'llama3.1',\n"
            "    openai: 'gpt-4o-mini',\n"
            "  }\n"
            "  return defaults[provider]\n"
            "}\n"
        )
        path.write_text(original, encoding="utf-8")

        edited = tools.execute(
            "edit_file",
            {
                "path": "providers.ts",
                "edits": [
                    {
                        "old_text": (
                            "    const defaults = {\n"
                            "      ollama: 'llama3.1',\n"
                            "      openai: 'gpt-4o-mini',\n"
                            "    }"
                        ),
                        "new_text": (
                            "    const defaults = {\n"
                            "      ollama: 'gemma4:12b',\n"
                            "      openai: 'gpt-4o-mini',\n"
                            "    }"
                        ),
                    }
                ],
            },
        )

        self.assertTrue(edited.success)
        self.assertIn("recovered 1 indentation variation", edited.output)
        self.assertIn("  const defaults = {\n    ollama: 'gemma4:12b',", path.read_text())

        path.write_text(original, encoding="utf-8")
        replaced = tools.execute(
            "replace_in_file",
            {
                "path": "providers.ts",
                "old_text": "      ollama: 'llama3.1',",
                "new_text": "      ollama: 'gemma4:12b',",
            },
        )

        self.assertTrue(replaced.success)
        self.assertIn("recovering indentation drift", replaced.output)
        self.assertIn("    ollama: 'gemma4:12b',", path.read_text())

    def test_whitespace_recovery_rejects_ambiguous_matches(self) -> None:
        path = self.root / "duplicates.py"
        path.write_text("  value = 'old'\n    value = 'old'\n", encoding="utf-8")
        result = ProjectTools(self.root, permission_mode="allow").execute(
            "replace_in_file",
            {
                "path": "duplicates.py",
                "old_text": "      value = 'old'",
                "new_text": "      value = 'new'",
            },
        )

        self.assertFalse(result.success)
        self.assertIn("use replace_lines", result.output)
        self.assertEqual(path.read_text(), "  value = 'old'\n    value = 'old'\n")

        path.write_text("prefixvalue = 'old'suffix\n", encoding="utf-8")
        embedded = ProjectTools(self.root, permission_mode="allow").execute(
            "replace_in_file",
            {
                "path": "duplicates.py",
                "old_text": "value  = 'old'",
                "new_text": "value  = 'new'",
            },
        )
        self.assertFalse(embedded.success)
        self.assertEqual(path.read_text(), "prefixvalue = 'old'suffix\n")

    def test_edit_file_can_replace_every_exact_occurrence(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        path = self.root / "buttons.py"
        path.write_text(
            "font=old\nfont=old\nplay=old\n",
            encoding="utf-8",
        )

        ambiguous = tools.execute(
            "edit_file",
            {
                "path": "buttons.py",
                "edits": [{"old_text": "font=old", "new_text": "font=new"}],
            },
        )
        self.assertFalse(ambiguous.success)
        self.assertIn("set replace_all", ambiguous.output)
        self.assertEqual(path.read_text(encoding="utf-8"), "font=old\nfont=old\nplay=old\n")

        replaced = tools.execute(
            "edit_file",
            {
                "path": "buttons.py",
                "edits": [
                    {
                        "old_text": "font=old",
                        "new_text": "font=new",
                        "replace_all": True,
                    },
                    {"old_text": "play=old", "new_text": "play=new"},
                ],
            },
        )
        self.assertTrue(replaced.success)
        self.assertIn("3 replacements", replaced.output)
        self.assertEqual(path.read_text(encoding="utf-8"), "font=new\nfont=new\nplay=new\n")

    def test_empty_literal_search_explains_regex_escaping(self) -> None:
        (self.root / "sample.py").write_text("font=self.font\n", encoding="utf-8")
        result = ProjectTools(self.root).execute(
            "search_files",
            {"query": r"font=self\.font", "pattern": "*.py"},
        )

        self.assertTrue(result.success)
        self.assertIn("does not need regex escaping", result.output)

    def test_run_lint_uses_autodetected_command(self) -> None:
        (self.root / "pyproject.toml").write_text(
            "[project]\nname='test'\n", encoding="utf-8"
        )
        tools = ProjectTools(
            self.root, permission_mode="allow", approve=lambda _name, _body: True
        )
        result = tools.execute("run_lint", {"kind": "compileall"})
        self.assertIn("compileall", result.output)
        self.assertTrue(result.success)

    def test_ask_user_returns_callback_result(self) -> None:
        tools = ProjectTools(
            self.root,
            permission_mode="allow",
            ask=lambda question, detail: f"answer: {question}",
        )
        result = tools.execute("ask_user", {"question": "which file?", "detail": "a or b"})
        self.assertTrue(result.success)
        self.assertEqual(result.output, "answer: which file?")

    def test_lint_and_web_fetch_always_require_explicit_approval(self) -> None:
        tools = ProjectTools(self.root, permission_mode="allow")
        lint = tools.execute("run_lint", {"command": "touch lint-ran"})
        fetch = tools.execute("web_fetch", {"url": "https://example.com"})
        self.assertFalse(lint.success)
        self.assertFalse(fetch.success)
        self.assertFalse((self.root / "lint-ran").exists())


class WebUrlValidationTests(unittest.TestCase):
    def test_blocks_loopback_and_private_addresses(self) -> None:
        blocked = (
            "http://127.0.0.1/x",
            "http://127.8.8.8/",
            "http://10.0.0.5/",
            "http://172.16.0.1/",
            "http://192.168.1.1/",
            "http://169.254.1.1/",
            "http://0.0.0.0/",
            "http://100.64.0.1/",
            "http://224.0.0.1/",
            "http://240.0.0.1/",
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[fe80::1]/",
            "http://[fc00::1]/",
            "http://[::]/",
        )
        for url in blocked:
            with self.assertRaises(ValueError, msg=url):
                validate_web_url(url)

    def test_rejects_non_http_schemes_and_missing_hosts(self) -> None:
        for url in ("file:///etc/passwd", "ftp://example.com/", "http:///path"):
            with self.assertRaises(ValueError, msg=url):
                validate_web_url(url)

    def test_allows_public_addresses(self) -> None:
        with patch(
            "localcode.projects.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))
            ],
        ):
            validate_web_url("http://example.com/")

    def test_blocks_hostnames_resolving_to_private_addresses(self) -> None:
        with patch(
            "localcode.projects.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.168.0.10", 80))
            ],
        ):
            with self.assertRaises(ValueError):
                validate_web_url("http://rebind.example/")

    def test_blocks_when_any_resolved_address_is_private(self) -> None:
        with patch(
            "localcode.projects.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.1", 80)),
            ],
        ):
            with self.assertRaises(ValueError):
                validate_web_url("http://multi.example/")

    def test_blocks_hostnames_that_do_not_resolve(self) -> None:
        def unresolved(_host, _port, **_kwargs):
            raise socket.gaierror("no address")

        with patch("localcode.projects.socket.getaddrinfo", side_effect=unresolved):
            with self.assertRaises(ValueError):
                validate_web_url("http://does-not-resolve.example/")

    def test_web_fetch_tool_reports_blocked_urls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            tools = ProjectTools(
                directory,
                permission_mode="allow",
                approve=lambda _name, _body: True,
            )
            with patch(
                "localcode.projects.socket.getaddrinfo",
                return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 9999))
                ],
            ):
                result = tools.execute("web_fetch", {"url": "http://host.example:9999/test"})
        self.assertFalse(result.success)
        self.assertIn("Blocked", result.output)


class RedirectHandlerTests(unittest.TestCase):
    def test_redirects_to_private_targets_are_rejected(self) -> None:
        handler = _GuardedRedirectHandler()
        request = urllib.request.Request("http://example.com/page")
        with self.assertRaises(ValueError):
            handler.redirect_request(
                request, None, 302, "Found", {}, "http://127.0.0.1/private"
            )

    def test_redirects_to_public_targets_follow(self) -> None:
        handler = _GuardedRedirectHandler()
        request = urllib.request.Request("http://example.com/page")
        with patch(
            "localcode.projects.socket.getaddrinfo",
            return_value=[
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
        ):
            redirected = handler.redirect_request(
                request, None, 302, "Found", {}, "https://docs.example.com/x"
            )
        self.assertIsInstance(redirected, urllib.request.Request)
        self.assertEqual(redirected.full_url, "https://docs.example.com/x")


if __name__ == "__main__":
    unittest.main()
