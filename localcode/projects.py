from __future__ import annotations

import fnmatch
import json
import os
import re
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

SKIP_DIRECTORIES = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "target",
    "build",
    "dist",
    "vendor",
    ".cache",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

BINARY_SUFFIXES = {
    ".7z",
    ".a",
    ".avi",
    ".bin",
    ".class",
    ".db",
    ".dll",
    ".dylib",
    ".exe",
    ".gif",
    ".gz",
    ".ico",
    ".jar",
    ".jpeg",
    ".jpg",
    ".lockb",
    ".mp3",
    ".mp4",
    ".o",
    ".pdf",
    ".png",
    ".pyc",
    ".so",
    ".sqlite",
    ".tar",
    ".woff",
    ".woff2",
    ".zip",
}

MUTATING_TOOLS = {"write_file", "replace_in_file", "delete_file", "run_command"}


@dataclass(slots=True)
class ToolResult:
    name: str
    output: str
    success: bool = True
    changed_file: str = ""


ApprovalCallback = Callable[[str, str], bool]


def resolve_inside(root: Path, relative: str, *, must_exist: bool = False) -> Path:
    if not relative or relative == ".":
        candidate = root.resolve()
    else:
        untrusted = Path(relative)
        if untrusted.is_absolute():
            raise ValueError("Use a path relative to the project root.")
        candidate = (root / untrusted).resolve(strict=False)
    resolved_root = root.resolve()
    try:
        candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError("Path escapes the project root.") from error
    if must_exist and not candidate.exists():
        raise FileNotFoundError(relative)
    return candidate


def reject_symlink_components(root: Path, relative: str) -> None:
    untrusted = Path(relative)
    if untrusted.is_absolute():
        raise ValueError("Use a path relative to the project root.")
    current = root.resolve()
    for part in untrusted.parts:
        if part in {"", "."}:
            continue
        current = current / part
        if current.is_symlink():
            raise ValueError("Mutating files through symbolic links is not allowed.")


def iter_project_files(root: Path, start: Path | None = None):
    scan_root = start or root
    for directory, names, files in os.walk(scan_root, followlinks=False):
        names[:] = sorted(name for name in names if name not in SKIP_DIRECTORIES)
        base = Path(directory)
        for filename in sorted(files):
            path = base / filename
            if path.suffix.casefold() in BINARY_SUFFIXES or path.is_symlink():
                continue
            try:
                path.relative_to(root)
            except ValueError:
                continue
            yield path


def project_tree(root: Path, *, max_files: int = 180, max_depth: int = 4) -> str:
    lines: list[str] = []
    for path in iter_project_files(root):
        relative = path.relative_to(root)
        if len(relative.parts) > max_depth:
            continue
        lines.append(str(relative))
        if len(lines) >= max_files:
            lines.append("... (more files omitted)")
            break
    return "\n".join(lines) or "(empty project)"


def git_summary(root: Path) -> str:
    if not (root / ".git").exists():
        return "Not a Git repository."
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "Git status unavailable."
    return (result.stdout or result.stderr).strip()[:12000] or "Working tree clean."


def detect_project_commands(root: Path) -> list[str]:
    commands: list[str] = []
    package_json = root / "package.json"
    if package_json.is_file():
        try:
            package = json.loads(package_json.read_text(encoding="utf-8"))
            scripts = package.get("scripts") or {}
            package_manager = "pnpm" if (root / "pnpm-lock.yaml").exists() else "npm"
            for name in ("dev", "test", "lint", "build", "typecheck"):
                if name in scripts:
                    command = (
                        f"{package_manager} {name}"
                        if package_manager == "pnpm"
                        else f"npm run {name}"
                    )
                    commands.append(command)
        except (OSError, json.JSONDecodeError):
            pass
    if (root / "Cargo.toml").is_file():
        commands.extend(["cargo test", "cargo clippy", "cargo fmt --check"])
    if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
        if (root / "tests").is_dir():
            commands.append("python3 -m pytest")
        commands.append("python3 -m compileall .")
    if (root / "go.mod").is_file():
        commands.extend(["go test ./...", "go vet ./..."])
    if (root / "Makefile").is_file():
        commands.append("make")
    return list(dict.fromkeys(commands))[:8]


class ProjectTools:
    def __init__(
        self,
        root: Path | str,
        *,
        permission_mode: str = "ask",
        approve: ApprovalCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.permission_mode = permission_mode
        self.approve = approve
        self.cancel = cancel
        self.changed_files: set[str] = set()
        self.commands: list[str] = []

    @staticmethod
    def definitions() -> list[dict[str, Any]]:
        return [
            _tool(
                "list_files",
                "List project files under a relative path. Skips generated and binary files.",
                {
                    "path": {"type": "string", "description": "Relative directory, default ."},
                    "pattern": {"type": "string", "description": "Optional glob such as *.py"},
                },
            ),
            _tool(
                "read_file",
                "Read a UTF-8 text file with line numbers.",
                {
                    "path": {"type": "string", "description": "Project-relative file path"},
                    "start_line": {"type": "integer", "description": "First line, default 1"},
                    "end_line": {"type": "integer", "description": "Last line, at most 600 lines"},
                },
                ["path"],
            ),
            _tool(
                "search_files",
                "Search text files for a literal string or regular expression.",
                {
                    "query": {"type": "string", "description": "Text or regex to find"},
                    "path": {"type": "string", "description": "Relative directory, default ."},
                    "pattern": {"type": "string", "description": "Optional file glob"},
                    "regex": {"type": "boolean", "description": "Interpret query as regex"},
                },
                ["query"],
            ),
            _tool(
                "write_file",
                "Create or replace a text file atomically. Include the entire desired "
                "file content.",
                {
                    "path": {"type": "string", "description": "Project-relative file path"},
                    "content": {"type": "string", "description": "Complete file content"},
                },
                ["path", "content"],
            ),
            _tool(
                "replace_in_file",
                "Replace exact text in a file. Fails when the text is missing or ambiguous.",
                {
                    "path": {"type": "string", "description": "Project-relative file path"},
                    "old_text": {"type": "string", "description": "Exact existing text"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                    "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
                },
                ["path", "old_text", "new_text"],
            ),
            _tool(
                "delete_file",
                "Delete one project file.",
                {"path": {"type": "string", "description": "Project-relative file path"}},
                ["path"],
            ),
            _tool(
                "run_command",
                "Run a shell command inside the project for builds, tests, formatting, "
                "or Git inspection.",
                {
                    "command": {"type": "string", "description": "Shell command"},
                    "cwd": {
                        "type": "string",
                        "description": "Relative working directory, default .",
                    },
                    "timeout": {"type": "integer", "description": "Timeout seconds, max 300"},
                },
                ["command"],
            ),
        ]

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        if self.cancel and self.cancel.is_set():
            return ToolResult(name, "Cancelled before the tool ran.", False)
        handler = getattr(self, f"_tool_{name}", None)
        if not handler or name.startswith("_"):
            return ToolResult(name, f"Unknown tool: {name}", False)
        if name in MUTATING_TOOLS:
            if self.permission_mode == "read-only":
                return ToolResult(name, "Blocked: this project is in read-only mode.", False)
            description = self._describe_mutation(name, arguments)
            needs_approval = self.permission_mode == "ask" or name == "run_command"
            if needs_approval and (
                not self.approve or not self.approve(name, description)
            ):
                return ToolResult(name, "The user declined this action.", False)
            if self.cancel and self.cancel.is_set():
                return ToolResult(name, "Cancelled before the tool ran.", False)
        try:
            return handler(arguments)
        except (OSError, ValueError, re.error, subprocess.SubprocessError) as error:
            return ToolResult(name, f"{type(error).__name__}: {error}", False)

    def _tool_list_files(self, arguments: dict[str, Any]) -> ToolResult:
        start = resolve_inside(self.root, str(arguments.get("path") or "."), must_exist=True)
        if not start.is_dir():
            raise ValueError("path must be a directory")
        pattern = str(arguments.get("pattern") or "")
        files: list[str] = []
        for path in iter_project_files(self.root, start):
            relative = str(path.relative_to(self.root))
            if (
                pattern
                and not fnmatch.fnmatch(path.name, pattern)
                and not fnmatch.fnmatch(relative, pattern)
            ):
                continue
            files.append(relative)
            if len(files) >= 500:
                files.append("... (limit reached)")
                break
        return ToolResult("list_files", "\n".join(files) or "No matching files.")

    def _tool_read_file(self, arguments: dict[str, Any]) -> ToolResult:
        relative = str(arguments.get("path") or "")
        path = resolve_inside(self.root, relative, must_exist=True)
        if not path.is_file() or path.suffix.casefold() in BINARY_SUFFIXES:
            raise ValueError("path must be a readable text file")
        if path.stat().st_size > 2_000_000:
            raise ValueError("file exceeds the 2 MB read limit")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(arguments.get("start_line") or 1))
        requested_end = int(arguments.get("end_line") or (start + 299))
        end = min(len(lines), requested_end, start + 599)
        rendered = "\n".join(f"{index:>6}  {lines[index - 1]}" for index in range(start, end + 1))
        header = f"{relative} lines {start}-{end} of {len(lines)}"
        return ToolResult("read_file", f"{header}\n{rendered}")

    def _tool_search_files(self, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query") or "")
        if not query:
            raise ValueError("query is required")
        start = resolve_inside(self.root, str(arguments.get("path") or "."), must_exist=True)
        pattern = str(arguments.get("pattern") or "")
        use_regex = bool(arguments.get("regex"))
        expression = re.compile(query) if use_regex else None
        matches: list[str] = []
        for path in iter_project_files(self.root, start):
            relative = str(path.relative_to(self.root))
            if (
                pattern
                and not fnmatch.fnmatch(path.name, pattern)
                and not fnmatch.fnmatch(relative, pattern)
            ):
                continue
            try:
                if path.stat().st_size > 1_000_000:
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, 1):
                found = (
                    bool(expression.search(line))
                    if expression
                    else query.casefold() in line.casefold()
                )
                if found:
                    matches.append(f"{relative}:{line_number}: {line[:400]}")
                    if len(matches) >= 200:
                        matches.append("... (match limit reached)")
                        return ToolResult("search_files", "\n".join(matches))
        return ToolResult("search_files", "\n".join(matches) or "No matches.")

    def _tool_write_file(self, arguments: dict[str, Any]) -> ToolResult:
        relative = str(arguments.get("path") or "")
        reject_symlink_components(self.root, relative)
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be text")
        path = resolve_inside(self.root, relative)
        if path == self.root:
            raise ValueError("path must name a file")
        existed = path.exists()
        previous = path.read_text(encoding="utf-8", errors="replace") if existed else None
        if previous == content:
            return ToolResult("write_file", f"No change: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, content)
        self.changed_files.add(relative)
        verb = "Updated" if existed else "Created"
        return ToolResult(
            "write_file", f"{verb} {relative} ({len(content)} characters).", True, relative
        )

    def _tool_replace_in_file(self, arguments: dict[str, Any]) -> ToolResult:
        relative = str(arguments.get("path") or "")
        reject_symlink_components(self.root, relative)
        old_text = arguments.get("old_text")
        new_text = arguments.get("new_text")
        if not isinstance(old_text, str) or not old_text:
            raise ValueError("old_text must be non-empty text")
        if not isinstance(new_text, str):
            raise ValueError("new_text must be text")
        path = resolve_inside(self.root, relative, must_exist=True)
        content = path.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            raise ValueError("old_text was not found")
        replace_all = bool(arguments.get("replace_all"))
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_text occurs {count} times; provide more context or set replace_all"
            )
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        self._atomic_write(path, updated)
        self.changed_files.add(relative)
        replacements = count if replace_all else 1
        return ToolResult(
            "replace_in_file",
            f"Updated {relative} ({replacements} replacement{'s' if replacements != 1 else ''}).",
            True,
            relative,
        )

    def _tool_delete_file(self, arguments: dict[str, Any]) -> ToolResult:
        relative = str(arguments.get("path") or "")
        reject_symlink_components(self.root, relative)
        path = resolve_inside(self.root, relative, must_exist=True)
        if not path.is_file() or path.is_symlink():
            raise ValueError("path must be a regular file")
        path.unlink()
        self.changed_files.add(relative)
        return ToolResult("delete_file", f"Deleted {relative}.", True, relative)

    def _tool_run_command(self, arguments: dict[str, Any]) -> ToolResult:
        command = str(arguments.get("command") or "").strip()
        if not command:
            raise ValueError("command is required")
        cwd = resolve_inside(self.root, str(arguments.get("cwd") or "."), must_exist=True)
        if not cwd.is_dir():
            raise ValueError("cwd must be a directory")
        timeout = min(300, max(1, int(arguments.get("timeout") or 120)))
        before = self._file_state()
        process = subprocess.Popen(
            ["/bin/bash", "-lc", command],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            errors="replace",
            env={**os.environ, "PAGER": "cat", "GIT_PAGER": "cat"},
            start_new_session=True,
        )
        deadline = time.monotonic() + timeout
        cancelled = False
        timed_out = False
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                if self.cancel and self.cancel.is_set():
                    cancelled = True
                elif time.monotonic() >= deadline:
                    timed_out = True
                else:
                    continue
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                    stdout, stderr = process.communicate(timeout=2)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stdout, stderr = process.communicate()
                break
        after = self._file_state()
        self.changed_files.update(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        )
        self.commands.append(command)
        output = (stdout + stderr).strip()
        if len(output) > 30000:
            output = output[:30000] + "\n... (output truncated)"
        if cancelled:
            summary = "Cancelled by user"
        elif timed_out:
            summary = f"Timed out after {timeout} seconds"
        else:
            summary = f"Exit code: {process.returncode}"
        return ToolResult(
            "run_command",
            f"{summary}\n{output}" if output else summary,
            process.returncode == 0 and not cancelled and not timed_out,
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        mode = path.stat().st_mode if path.exists() else None
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(path)

    def _file_state(self) -> dict[str, tuple[int, int]]:
        state: dict[str, tuple[int, int]] = {}
        for path in iter_project_files(self.root):
            try:
                stat = path.stat()
            except OSError:
                continue
            state[str(path.relative_to(self.root))] = (stat.st_mtime_ns, stat.st_size)
        return state

    @staticmethod
    def _describe_mutation(name: str, arguments: dict[str, Any]) -> str:
        if name == "run_command":
            cwd = str(arguments.get("cwd") or ".")
            return (
                "Shell commands are not sandboxed and can access files outside the project.\n"
                f"Run in {cwd}:\n{arguments.get('command', '')}"
            )
        return f"{name.replace('_', ' ').title()}: {arguments.get('path', '')}"


def _tool(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required or [],
            },
        },
    }
