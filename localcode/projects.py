from __future__ import annotations

import fnmatch
import hashlib
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from .managed_files import validate_agents_rewrite

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

@dataclass(slots=True)
class FileObservation:
    path: str
    start_line: int
    end_line: int
    content: str
    source_hash: str

    def identity(self) -> tuple[str, int, int, str]:
        return (self.path, self.start_line, self.end_line, self.source_hash)


TOOL_RESULT_STATUSES = {"success", "noop", "skipped", "blocked", "error"}


@dataclass(slots=True)
class ToolResult:
    name: str
    output: str
    status: str = "success"
    changed_files: tuple[str, ...] = ()
    commands: tuple[str, ...] = ()
    observations: tuple[FileObservation, ...] = ()

    def __post_init__(self) -> None:
        if self.status not in TOOL_RESULT_STATUSES:
            raise ValueError(f"Unsupported tool-result status: {self.status}")

    @property
    def success(self) -> bool:
        """Compatibility view for callers that only need pass/fail semantics."""

        return self.status in {"success", "noop", "skipped"}

    @property
    def failed(self) -> bool:
        return self.status in {"blocked", "error"}


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...] = ()
    risk: str = "read"
    phases: tuple[str, ...] = ("inspect",)
    handler: str = ""
    mutates_project: bool = False
    always_approve: bool = False
    records_action: bool = False

    def definition(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }


ApprovalCallback = Callable[[str, str], bool]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(131072), b""):
            digest.update(block)
    return digest.hexdigest()


def _render_numbered_lines(
    lines: list[str], start: int, end: int, *, max_chars: int
) -> tuple[str, int]:
    rendered: list[str] = []
    used = 0
    actual_end = start - 1
    for index in range(start, end + 1):
        row = f"{index:>6}  {lines[index - 1]}"
        remaining = max_chars - used
        if remaining <= 0:
            break
        if len(row) > remaining:
            if not rendered:
                rendered.append(row[: max(0, remaining - 30)] + " ... [line truncated]")
                actual_end = index
            break
        rendered.append(row)
        used += len(row) + 1
        actual_end = index
    if actual_end < end:
        rendered.append("... [output capped; request a narrower line range]")
    return "\n".join(rendered), actual_end


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


def _address_is_blocked(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    if (
        ip.is_loopback
        or ip.is_unspecified
        or ip.is_multicast
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
    ):
        return True
    if ip.version == 4:
        return ip in ipaddress.ip_network("100.64.0.0/10")  # carrier-grade NAT
    if ip.ipv4_mapped is not None:
        return _address_is_blocked(str(ip.ipv4_mapped))
    return False


def validate_web_url(url: str) -> None:
    """Reject URLs that are not plain http(s) or that resolve to private targets."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http and https URLs are allowed.")
    host = parsed.hostname
    if not host:
        raise ValueError("The URL has no hostname to connect to.")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as error:
        raise ValueError(f"Invalid port in URL: {url}") from error
    try:
        entries = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise ValueError(f"Hostname {host} did not resolve.") from error
    if not entries:
        raise ValueError(f"Hostname {host} did not resolve.")
    for entry in entries:
        address = entry[4][0]
        if _address_is_blocked(address):
            raise ValueError(
                f"Access to {host} is blocked because it resolves to {address}, "
                "a loopback or private network address."
            )


class _GuardedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_web_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


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


def _spec(
    name: str,
    description: str,
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
    *,
    risk: str = "read",
    phases: tuple[str, ...] = ("inspect",),
    mutates_project: bool = False,
    always_approve: bool = False,
    records_action: bool = False,
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        properties=properties,
        required=required,
        risk=risk,
        phases=phases,
        handler=f"_tool_{name}",
        mutates_project=mutates_project,
        always_approve=always_approve,
        records_action=records_action,
    )


TOOL_SPECS = (
    _spec(
        "list_files",
        "List project files under a relative path. Skips generated and binary files.",
        {
            "path": {"type": "string", "description": "Relative directory, default ."},
            "pattern": {"type": "string", "description": "Optional glob such as *.py"},
        },
        phases=("inspect", "edit"),
    ),
    _spec(
        "read_file",
        "Read a targeted UTF-8 text-file range with line numbers. Prefer narrow ranges "
        "and do not reread unchanged ranges already present in context.",
        {
            "path": {"type": "string", "description": "Project-relative file path"},
            "start_line": {
                "type": "integer",
                "minimum": 1,
                "description": "First line, default 1",
            },
            "end_line": {
                "type": "integer",
                "minimum": 1,
                "description": "Last line, at most 400 lines after start_line",
            },
        },
        ("path",),
        phases=("inspect", "edit", "verify"),
    ),
    _spec(
        "replace_lines",
        "Replace an inclusive line range read from the current file version. Use this "
        "when exact-text replacement is brittle. The tool rejects stale or unread ranges.",
        {
            "path": {"type": "string", "description": "Project-relative file path"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
            "new_text": {
                "type": "string",
                "description": "Complete replacement text for the selected lines",
            },
        },
        ("path", "start_line", "end_line", "new_text"),
        risk="edit",
        phases=("edit", "verify"),
        mutates_project=True,
        records_action=True,
    ),
    _spec(
        "search_files",
        "Search project text files for a literal string or regular expression.",
        {
            "query": {"type": "string", "minLength": 1, "description": "Text to find"},
            "path": {"type": "string", "description": "Relative directory, default ."},
            "pattern": {"type": "string", "description": "Optional file glob"},
            "regex": {"type": "boolean", "description": "Interpret query as regex"},
        },
        ("query",),
        phases=("inspect", "edit", "verify"),
    ),
    _spec(
        "write_file",
        "Create or replace a text file atomically. Include the complete desired content.",
        {
            "path": {"type": "string", "description": "Project-relative file path"},
            "content": {"type": "string", "description": "Complete file content"},
        },
        ("path", "content"),
        risk="edit",
        phases=("edit", "verify"),
        mutates_project=True,
        records_action=True,
    ),
    _spec(
        "replace_in_file",
        "Replace exact text in a file. Fails when the text is missing or ambiguous.",
        {
            "path": {"type": "string", "description": "Project-relative file path"},
            "old_text": {"type": "string", "minLength": 1, "description": "Exact text"},
            "new_text": {"type": "string", "description": "Replacement text"},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence"},
        },
        ("path", "old_text", "new_text"),
        risk="edit",
        phases=("edit", "verify"),
        mutates_project=True,
        records_action=True,
    ),
    _spec(
        "delete_file",
        "Delete one project file.",
        {"path": {"type": "string", "description": "Project-relative file path"}},
        ("path",),
        risk="edit",
        phases=("edit", "verify"),
        mutates_project=True,
        records_action=True,
    ),
    _spec(
        "run_command",
        "Run an explicitly approved shell command with the project as its working "
        "directory. The command is not confined to the project filesystem.",
        {
            "command": {"type": "string", "minLength": 1, "description": "Shell command"},
            "cwd": {"type": "string", "description": "Relative working directory, default ."},
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": 300,
                "description": "Timeout seconds, default 120",
            },
        },
        ("command",),
        risk="shell",
        phases=("edit", "verify"),
        mutates_project=True,
        always_approve=True,
        records_action=True,
    ),
    _spec(
        "create_directory",
        "Create a directory and its parents inside the project.",
        {"path": {"type": "string", "description": "Project-relative directory path"}},
        ("path",),
        risk="edit",
        phases=("edit", "verify"),
        mutates_project=True,
        records_action=True,
    ),
    _spec(
        "rename_file",
        "Rename or move a file within the project.",
        {
            "source": {"type": "string", "description": "Current project-relative path"},
            "target": {"type": "string", "description": "New project-relative path"},
        },
        ("source", "target"),
        risk="edit",
        phases=("edit", "verify"),
        mutates_project=True,
        records_action=True,
    ),
    _spec(
        "copy_file",
        "Copy one regular file within the project, including binary assets. The target is "
        "created atomically and is not overwritten unless overwrite=true.",
        {
            "source": {"type": "string", "description": "Existing project-relative file"},
            "target": {"type": "string", "description": "New project-relative file"},
            "overwrite": {"type": "boolean", "description": "Allow replacing the target"},
        },
        ("source", "target"),
        risk="edit",
        phases=("edit", "verify"),
        mutates_project=True,
        records_action=True,
    ),
    _spec(
        "git_status",
        "Show the current Git branch plus concise staged, unstaged, and untracked paths.",
        {},
        phases=("inspect", "verify"),
    ),
    _spec(
        "git_diff",
        "Show staged or unstaged Git changes.",
        {
            "path": {"type": "string", "description": "Optional relative path filter"},
            "staged": {"type": "boolean", "description": "Show only staged changes"},
        },
        phases=("inspect", "verify"),
    ),
    _spec(
        "git_log",
        "Show recent Git commit history.",
        {
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
                "description": "Commit count, default 10",
            },
            "path": {"type": "string", "description": "Optional relative path filter"},
        },
    ),
    _spec(
        "project_commands",
        "List bounded test, lint, build, typecheck, and development commands detected from "
        "project manifests without running them.",
        {},
        phases=("inspect", "edit", "verify"),
    ),
    _spec(
        "web_fetch",
        "Fetch bounded text from a specific HTTP or HTTPS URL after approval.",
        {
            "url": {"type": "string", "minLength": 1, "description": "Full URL"},
            "max_chars": {
                "type": "integer",
                "minimum": 500,
                "maximum": 30000,
                "description": "Maximum returned characters, default 8000",
            },
        },
        ("url",),
        risk="network",
        always_approve=True,
    ),
    _spec(
        "read_files",
        "Read the first bounded section of several related files in one call.",
        {
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 8,
                "description": "Project-relative file paths",
            },
        },
        ("paths",),
        phases=("inspect", "edit", "verify"),
    ),
    _spec(
        "edit_file",
        "Apply up to 8 small exact replacements to one file atomically. Keep each old "
        "and new text localized so the JSON tool call remains reliable.",
        {
            "path": {"type": "string", "description": "Project-relative file path"},
            "edits": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_text": {"type": "string", "minLength": 1},
                        "new_text": {"type": "string"},
                        "replace_all": {"type": "boolean"},
                    },
                    "required": ["old_text", "new_text"],
                    "additionalProperties": False,
                },
                "minItems": 1,
                "maxItems": 8,
            },
        },
        ("path", "edits"),
        risk="edit",
        phases=("edit", "verify"),
        mutates_project=True,
        records_action=True,
    ),
    _spec(
        "run_lint",
        "Run one explicitly approved project lint, typecheck, test, or format check. "
        "LocalCode can select a command from project manifests.",
        {
            "kind": {
                "type": "string",
                "enum": [
                    "lint",
                    "typecheck",
                    "test",
                    "format",
                    "build",
                    "compile",
                    "compileall",
                    "custom",
                ],
            },
            "command": {"type": "string", "description": "Optional command override"},
        },
        risk="shell",
        phases=("verify",),
        mutates_project=True,
        always_approve=True,
        records_action=True,
    ),
    _spec(
        "ask_user",
        "Ask the user one question when a material ambiguity blocks safe progress.",
        {
            "question": {"type": "string", "minLength": 1},
            "detail": {"type": "string", "description": "Optional context or choices"},
        },
        ("question",),
        risk="interaction",
        phases=("inspect", "edit", "verify", "interaction"),
    ),
)

TOOL_REGISTRY = {spec.name: spec for spec in TOOL_SPECS}
MUTATING_TOOLS = frozenset(
    spec.name for spec in TOOL_SPECS if spec.mutates_project
)
ALWAYS_APPROVE_TOOLS = frozenset(
    spec.name for spec in TOOL_SPECS if spec.always_approve
)


class ProjectTools:
    def __init__(
        self,
        root: Path | str,
        *,
        permission_mode: str = "ask",
        approve: ApprovalCallback | None = None,
        ask: Callable[[str, str], str | None] | None = None,
        cancel: threading.Event | None = None,
        known_observations: set[tuple[str, int, int, str]] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self.permission_mode = permission_mode
        self.approve = approve
        self.ask = ask
        self.cancel = cancel
        self.changed_files: set[str] = set()
        self.commands: list[str] = []
        self.failures: list[str] = []
        self.completed_actions: list[str] = []
        self.called_tools: set[str] = set()
        self._seen_observations = set(known_observations or ())

    def clear_read_deduplication(self) -> None:
        self._seen_observations.clear()

    def set_known_observations(
        self, observations: set[tuple[str, int, int, str]]
    ) -> None:
        self._seen_observations = set(observations)

    @staticmethod
    def definitions(names: set[str] | None = None) -> list[dict[str, Any]]:
        specs = TOOL_SPECS if names is None else (
            spec for spec in TOOL_SPECS if spec.name in names
        )
        return [spec.definition() for spec in specs]

    @staticmethod
    def definitions_for_phase(
        phase: str,
        *,
        permission_mode: str = "ask",
    ) -> list[dict[str, Any]]:
        if phase not in {"inspect", "edit", "verify"}:
            raise ValueError(f"Unsupported tool phase: {phase}")
        names = {
            spec.name
            for spec in TOOL_SPECS
            if phase in spec.phases
            and not (permission_mode == "read-only" and spec.mutates_project)
        }
        return ProjectTools.definitions(names)

    def execute(self, name: str, arguments: dict[str, Any]) -> ToolResult:
        self.called_tools.add(name)
        spec = TOOL_REGISTRY.get(name)
        if self.cancel and self.cancel.is_set():
            return ToolResult(name, "Cancelled before the tool ran.", status="blocked")
        if spec is None:
            return ToolResult(name, f"Unknown tool: {name}", status="error")
        handler = getattr(self, spec.handler, None)
        if not handler:
            return ToolResult(name, f"Tool handler is unavailable: {name}", status="error")
        if not isinstance(arguments, dict):
            return ToolResult(name, "Tool arguments must be an object.", status="error")
        unexpected = sorted(set(arguments) - set(spec.properties))
        if unexpected:
            return ToolResult(
                name,
                "Unexpected argument(s): " + ", ".join(unexpected),
                status="error",
            )
        if spec.mutates_project and self.permission_mode == "read-only":
            return ToolResult(
                name, "Blocked: this project is in read-only mode.", status="blocked"
            )
        needs_approval = (
            (spec.mutates_project and self.permission_mode == "ask")
            or spec.always_approve
        )
        if needs_approval:
            description = self._describe_mutation(name, arguments)
            if not self.approve or not self.approve(name, description):
                return ToolResult(
                    name, "The user declined this action.", status="blocked"
                )
            if self.cancel and self.cancel.is_set():
                return ToolResult(
                    name, "Cancelled before the tool ran.", status="blocked"
                )
        try:
            result = handler(arguments)
        except (OSError, ValueError, re.error, subprocess.SubprocessError) as error:
            return ToolResult(
                name, f"{type(error).__name__}: {error}", status="error"
            )
        self.changed_files.update(result.changed_files)
        self.commands.extend(result.commands)
        if result.status in {"success", "noop"} and spec.records_action:
            action = " ".join(result.output.split())[:500]
            self.completed_actions.append(f"{name}: {action}")
        if result.success and result.observations:
            identities = {
                observation.identity()
                for observation in result.observations
                if "[output capped" not in observation.content
                and "[line truncated]" not in observation.content
            }
            if (
                spec.risk == "read"
                and identities
                and identities.issubset(self._seen_observations)
            ):
                paths = ", ".join(observation.path for observation in result.observations)
                result.output = (
                    f"Unchanged read omitted: {paths}. The same verified file range is "
                    "already available in this turn's context."
                )
                result.status = "skipped"
                result.observations = ()
            self._seen_observations.update(identities)
        return result

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
        relative = str(path.relative_to(self.root))
        if not path.is_file() or path.suffix.casefold() in BINARY_SUFFIXES:
            raise ValueError("path must be a readable text file")
        if path.stat().st_size > 2_000_000:
            raise ValueError("file exceeds the 2 MB read limit")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(1, int(arguments.get("start_line") or 1))
        if lines and start > len(lines):
            raise ValueError(f"start_line exceeds the file length ({len(lines)})")
        requested_end = int(arguments.get("end_line") or (start + 199))
        end = min(len(lines), requested_end, start + 399)
        rendered, actual_end = _render_numbered_lines(lines, start, end, max_chars=24000)
        header = f"{relative} lines {start}-{actual_end} of {len(lines)}"
        output = f"{header}\n{rendered}"
        observation = FileObservation(
            relative,
            start,
            actual_end,
            output,
            file_sha256(path),
        )
        return ToolResult("read_file", output, observations=(observation,))

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
        return ToolResult(
            "search_files",
            "\n".join(matches)
            or (
                "No matches. Literal search is the default and does not need regex "
                "escaping; remove backslashes from a literal query or set regex=true."
            ),
        )

    def _tool_write_file(self, arguments: dict[str, Any]) -> ToolResult:
        relative = str(arguments.get("path") or "")
        reject_symlink_components(self.root, relative)
        content = arguments.get("content")
        if not isinstance(content, str):
            raise ValueError("content must be text")
        path = resolve_inside(self.root, relative)
        relative = str(path.relative_to(self.root))
        if path == self.root:
            raise ValueError("path must name a file")
        existed = path.exists()
        previous = path.read_text(encoding="utf-8", errors="replace") if existed else None
        if previous == content:
            observations, evidence = self._post_edit_evidence(path, relative)
            return ToolResult(
                "write_file",
                f"No change: {relative}.\n\n{evidence}",
                status="noop",
                observations=observations,
            )
        self._validate_protected_rewrite(relative, previous, content)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(path, content)
        verb = "Updated" if existed else "Created"
        observations, evidence = self._post_edit_evidence(path, relative)
        return ToolResult(
            "write_file",
            f"{verb} {relative} ({len(content)} characters).\n\n{evidence}",
            changed_files=(relative,),
            observations=observations,
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
        relative = str(path.relative_to(self.root))
        content = path.read_text(encoding="utf-8")
        count = content.count(old_text)
        if count == 0:
            raise ValueError("old_text was not found")
        replace_all = bool(arguments.get("replace_all"))
        if count > 1 and not replace_all:
            raise ValueError(
                f"old_text occurs {count} times; provide more context or set replace_all"
            )
        first_offset = content.find(old_text)
        preferred_line = content[:first_offset].count("\n") + 1
        updated = content.replace(old_text, new_text, -1 if replace_all else 1)
        self._validate_protected_rewrite(relative, content, updated)
        self._atomic_write(path, updated)
        replacements = count if replace_all else 1
        observations, evidence = self._post_edit_evidence(
            path, relative, preferred_line=preferred_line
        )
        return ToolResult(
            "replace_in_file",
            f"Updated {relative} ({replacements} replacement"
            f"{'s' if replacements != 1 else ''}).\n\n{evidence}",
            changed_files=(relative,),
            observations=observations,
        )

    def _tool_replace_lines(self, arguments: dict[str, Any]) -> ToolResult:
        relative = str(arguments.get("path") or "")
        reject_symlink_components(self.root, relative)
        path = resolve_inside(self.root, relative, must_exist=True)
        relative = str(path.relative_to(self.root))
        if not path.is_file() or path.suffix.casefold() in BINARY_SUFFIXES:
            raise ValueError("path must be a readable text file")
        try:
            start = int(arguments.get("start_line"))
            end = int(arguments.get("end_line"))
        except (TypeError, ValueError) as error:
            raise ValueError("start_line and end_line must be integers") from error
        if start < 1 or end < start:
            raise ValueError("line range must be ordered and start at line 1 or later")
        if end - start + 1 > 200:
            raise ValueError("replace_lines accepts at most 200 source lines")
        new_text = arguments.get("new_text")
        if not isinstance(new_text, str):
            raise ValueError("new_text must be text")

        original = path.read_text(encoding="utf-8")
        lines = original.splitlines(keepends=True)
        if end > len(lines):
            raise ValueError(f"end_line exceeds the file length ({len(lines)})")
        source_hash = file_sha256(path)
        verified = any(
            observed_path == relative
            and observed_hash == source_hash
            and observed_start <= start
            and observed_end >= end
            for observed_path, observed_start, observed_end, observed_hash
            in self._seen_observations
        )
        if not verified:
            raise ValueError(
                f"lines {start}-{end} are not verified for the current {relative}; "
                "read a range containing those lines before replacing them"
            )

        selected = "".join(lines[start - 1:end])
        replacement = new_text
        if replacement and selected.endswith("\r\n") and not replacement.endswith(("\n", "\r")):
            replacement += "\r\n"
        elif replacement and selected.endswith("\n") and not replacement.endswith(("\n", "\r")):
            replacement += "\n"
        updated = "".join(lines[:start - 1]) + replacement + "".join(lines[end:])
        if updated == original:
            observations, evidence = self._post_edit_evidence(
                path, relative, preferred_line=start
            )
            return ToolResult(
                "replace_lines",
                f"No change: {relative} lines {start}-{end}.\n\n{evidence}",
                status="noop",
                observations=observations,
            )
        self._validate_protected_rewrite(relative, original, updated)
        self._atomic_write(path, updated)
        observations, evidence = self._post_edit_evidence(
            path, relative, preferred_line=start
        )
        return ToolResult(
            "replace_lines",
            f"Updated {relative} lines {start}-{end}.\n\n{evidence}",
            changed_files=(relative,),
            observations=observations,
        )

    def _tool_delete_file(self, arguments: dict[str, Any]) -> ToolResult:
        relative = str(arguments.get("path") or "")
        reject_symlink_components(self.root, relative)
        path = resolve_inside(self.root, relative, must_exist=True)
        relative = str(path.relative_to(self.root))
        if not path.is_file() or path.is_symlink():
            raise ValueError("path must be a regular file")
        if relative == "AGENTS.md":
            raise ValueError("AGENTS.md is managed by LocalCode and cannot be deleted.")
        path.unlink()
        return ToolResult(
            "delete_file", f"Deleted {relative}.", changed_files=(relative,)
        )

    def _tool_run_command(self, arguments: dict[str, Any]) -> ToolResult:
        command = str(arguments.get("command") or "").strip()
        if not command:
            raise ValueError("command is required")
        cwd = resolve_inside(self.root, str(arguments.get("cwd") or "."), must_exist=True)
        if not cwd.is_dir():
            raise ValueError("cwd must be a directory")
        timeout = min(300, max(1, int(arguments.get("timeout") or 120)))
        before = self._snapshot_state()
        agents_before = self._agents_snapshot()
        process = subprocess.Popen(
            ["/bin/bash", "-ilc", f"set -o pipefail\n{command}"],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
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
        agents_error = self._restore_invalid_agents_rewrite(agents_before)
        after = self._snapshot_state()
        changed_files = tuple(sorted(
            path for path in before.keys() | after.keys() if before.get(path) != after.get(path)
        ))
        output = (stdout + stderr).strip()
        if len(output) > 30000:
            output = output[:30000] + "\n... (output truncated)"
        if agents_error:
            summary = f"Blocked protected-file change: {agents_error} Original restored."
        elif cancelled:
            summary = "Cancelled by user"
        elif timed_out:
            summary = f"Timed out after {timeout} seconds"
        else:
            summary = f"Exit code: {process.returncode}"
        if cancelled or agents_error:
            status = "blocked"
        elif timed_out or process.returncode != 0:
            status = "error"
        else:
            status = "success"
        return ToolResult(
            "run_command",
            f"{summary}\n{output}" if output else summary,
            status=status,
            changed_files=changed_files,
            commands=(command,),
        )

    def _tool_create_directory(self, arguments: dict[str, Any]) -> ToolResult:
        relative = str(arguments.get("path") or "")
        if not relative or relative == ".":
            raise ValueError("path must name a directory")
        path = resolve_inside(self.root, relative)
        if path == self.root:
            raise ValueError("path must name a directory, not the project root")
        if path.is_file():
            raise ValueError("path already exists as a file")
        existed = path.is_dir()
        path.mkdir(parents=True, exist_ok=True)
        return ToolResult(
            "create_directory",
            f"{'Already exists' if existed else 'Created'}: {relative}",
            status="noop" if existed else "success",
            changed_files=() if existed else (relative,),
        )

    def _tool_rename_file(self, arguments: dict[str, Any]) -> ToolResult:
        source_rel = str(arguments.get("source") or "")
        target_rel = str(arguments.get("target") or "")
        if not source_rel or not target_rel:
            raise ValueError("source and target are required")
        reject_symlink_components(self.root, source_rel)
        reject_symlink_components(self.root, target_rel)
        source = resolve_inside(self.root, source_rel, must_exist=True)
        if not source.is_file():
            raise ValueError("source must be a file")
        target = resolve_inside(self.root, target_rel)
        source_relative = str(source.relative_to(self.root))
        target_relative = str(target.relative_to(self.root))
        if "AGENTS.md" in {source_relative, target_relative}:
            raise ValueError("AGENTS.md is managed by LocalCode and cannot be renamed.")
        if target.exists():
            raise ValueError("target already exists")
        target.parent.mkdir(parents=True, exist_ok=True)
        source.rename(target)
        source_rel = source_relative
        target_rel = target_relative
        observations, evidence = self._post_edit_evidence(target, target_rel)
        return ToolResult(
            "rename_file",
            f"Moved {source_rel} → {target_rel}.\n\n{evidence}",
            changed_files=(source_rel, target_rel),
            observations=observations,
        )

    def _tool_copy_file(self, arguments: dict[str, Any]) -> ToolResult:
        source_arg = str(arguments.get("source") or "")
        target_arg = str(arguments.get("target") or "")
        if not source_arg or not target_arg:
            raise ValueError("source and target are required")
        reject_symlink_components(self.root, source_arg)
        reject_symlink_components(self.root, target_arg)
        source = resolve_inside(self.root, source_arg, must_exist=True)
        target = resolve_inside(self.root, target_arg)
        if not source.is_file() or source.is_symlink():
            raise ValueError("source must be a regular file")
        if source == target:
            raise ValueError("source and target must be different files")
        if source.stat().st_size > 100_000_000:
            raise ValueError("source exceeds the 100 MB copy limit")
        relative = str(target.relative_to(self.root))
        if relative == "AGENTS.md":
            raise ValueError("AGENTS.md is managed by LocalCode and cannot be copied over.")
        overwrite = bool(arguments.get("overwrite"))
        if target.exists():
            if not target.is_file() or target.is_symlink():
                raise ValueError("target must be a regular file")
            if file_sha256(source) == file_sha256(target):
                observations, evidence = self._post_edit_evidence(target, relative)
                return ToolResult(
                    "copy_file",
                    f"No change: {relative} already matches {source_arg}.\n\n{evidence}",
                    status="noop",
                    observations=observations,
                )
            if not overwrite:
                raise ValueError("target already exists; set overwrite=true to replace it")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", dir=target.parent, prefix=f".{target.name}.", delete=False
            ) as handle:
                temporary = Path(handle.name)
                with source.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, handle)
            temporary.chmod(source.stat().st_mode)
            temporary.replace(target)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        observations, evidence = self._post_edit_evidence(target, relative)
        return ToolResult(
            "copy_file",
            f"Copied {source_arg} → {relative}.\n\n{evidence}",
            changed_files=(relative,),
            observations=observations,
        )

    def _tool_git_status(self, _arguments: dict[str, Any]) -> ToolResult:
        if not (self.root / ".git").exists():
            return ToolResult("git_status", "Not a Git repository.", status="skipped")
        result = subprocess.run(
            ["git", "status", "--short", "--branch", "--untracked-files=all"],
            cwd=self.root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            return ToolResult(
                "git_status",
                (result.stderr or result.stdout).strip() or "git status failed",
                status="error",
            )
        output = (result.stdout or result.stderr).strip()
        if len(output) > 20000:
            output = output[:20000] + "\n... (status truncated)"
        return ToolResult("git_status", output or "Working tree clean.")

    def _tool_git_diff(self, arguments: dict[str, Any]) -> ToolResult:
        if not (self.root / ".git").exists():
            return ToolResult("git_diff", "Not a Git repository.", status="error")
        args = ["git", "diff", "--no-color", "--no-ext-diff"]
        if arguments.get("staged"):
            args.append("--staged")
        path_arg = str(arguments.get("path") or "")
        if path_arg:
            args.extend(["--", path_arg])
        result = subprocess.run(
            args, cwd=self.root, capture_output=True, text=True, errors="replace",
            timeout=15, check=False,
        )
        if result.returncode != 0:
            return ToolResult(
                "git_diff",
                (result.stderr or result.stdout).strip() or "git diff failed",
                status="error",
            )
        output = (result.stdout or result.stderr).strip()
        if len(output) > 30000:
            output = output[:30000] + "\n... (diff truncated)"
        return ToolResult("git_diff", output or "No changes.")

    def _tool_git_log(self, arguments: dict[str, Any]) -> ToolResult:
        if not (self.root / ".git").exists():
            return ToolResult("git_log", "Not a Git repository.", status="error")
        count = min(50, max(1, int(arguments.get("count") or 10)))
        args = [
            "git", "log", f"-{count}", "--oneline", "--no-color",
        ]
        path_arg = str(arguments.get("path") or "")
        if path_arg:
            args.extend(["--", path_arg])
        result = subprocess.run(
            args, cwd=self.root, capture_output=True, text=True, errors="replace",
            timeout=10, check=False,
        )
        if result.returncode != 0:
            return ToolResult(
                "git_log",
                (result.stderr or result.stdout).strip() or "git log failed",
                status="error",
            )
        return ToolResult("git_log", (result.stdout or result.stderr).strip() or "No commits.")

    def _tool_project_commands(self, _arguments: dict[str, Any]) -> ToolResult:
        commands = detect_project_commands(self.root)
        if not commands:
            return ToolResult(
                "project_commands",
                "No standard project commands were detected from supported manifests.",
            )
        return ToolResult(
            "project_commands",
            "Detected project commands (not run):\n"
            + "\n".join(f"{index}. {command}" for index, command in enumerate(commands, 1)),
        )

    def _tool_web_fetch(self, arguments: dict[str, Any]) -> ToolResult:
        url = str(arguments.get("url") or "").strip()
        if not url:
            raise ValueError("url is required")
        try:
            validate_web_url(url)
        except ValueError as error:
            return ToolResult("web_fetch", f"Blocked: {error}", status="blocked")
        max_chars = min(30000, max(500, int(arguments.get("max_chars") or 8000)))
        request = urllib.request.Request(
            url,
            headers={"User-Agent": "LocalCode/0.1", "Accept": "text/plain,text/html"},
        )
        opener = urllib.request.build_opener(_GuardedRedirectHandler)
        try:
            with opener.open(request, timeout=15) as response:
                data = response.read(max_chars + 1)
        except urllib.error.URLError as error:
            return ToolResult(
                "web_fetch", f"Cannot reach {url}: {error.reason}", status="error"
            )
        except ValueError as error:
            return ToolResult("web_fetch", f"Blocked: {error}", status="blocked")
        content = data.decode("utf-8", errors="replace")[:max_chars]
        stripped = _strip_html(content)
        if len(stripped.strip()) < 20:
            stripped = content
        summary = f"Fetched {url} ({len(data)} bytes)"
        return ToolResult("web_fetch", f"{summary}\n\n{stripped}")

    def _tool_read_files(self, arguments: dict[str, Any]) -> ToolResult:
        paths = arguments.get("paths") or []
        if not isinstance(paths, list) or not paths:
            raise ValueError("paths must be a non-empty list of file paths")
        if len(paths) > 8:
            raise ValueError("at most 8 files per batch read")
        parts: list[str] = []
        observations: list[FileObservation] = []
        used_chars = 0
        for relative in paths:
            path = resolve_inside(self.root, str(relative), must_exist=True)
            relative = str(path.relative_to(self.root))
            if not path.is_file() or path.suffix.casefold() in BINARY_SUFFIXES:
                parts.append(f"\n=== {relative} ===\n[not a readable text file]\n")
                continue
            if path.stat().st_size > 2_000_000:
                parts.append(f"\n=== {relative} ===\n[file exceeds 2 MB read limit]\n")
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            requested_end = min(len(lines), 160)
            rendered, actual_end = _render_numbered_lines(
                lines, 1, requested_end, max_chars=8000
            )
            header = f"{relative} lines 1-{actual_end} of {len(lines)}"
            content = f"{header}\n{rendered}"
            part = f"\n=== {relative} ({len(lines)} lines) ===\n{rendered}\n"
            if used_chars + len(part) > 30000:
                parts.append("\n... [batch output capped; request remaining files separately]\n")
                break
            parts.append(part)
            used_chars += len(part)
            observations.append(
                FileObservation(relative, 1, actual_end, content, file_sha256(path))
            )
        return ToolResult("read_files", "".join(parts), observations=tuple(observations))

    def _tool_edit_file(self, arguments: dict[str, Any]) -> ToolResult:
        relative = str(arguments.get("path") or "")
        edits = arguments.get("edits") or []
        if not isinstance(edits, list) or not edits:
            raise ValueError("edits must be a non-empty list of changes")
        if len(edits) > 8:
            raise ValueError("edit_file accepts at most 8 edits per call")
        reject_symlink_components(self.root, relative)
        path = resolve_inside(self.root, relative, must_exist=True)
        relative = str(path.relative_to(self.root))
        if not path.is_file():
            raise ValueError("path must be a file")
        original = path.read_text(encoding="utf-8")
        content = original
        replacements = 0
        first_offset: int | None = None
        for edit in edits:
            old_text = edit.get("old_text")
            new_text = edit.get("new_text")
            if not isinstance(old_text, str) or not old_text:
                raise ValueError("each edit must have non-empty old_text")
            if not isinstance(new_text, str):
                raise ValueError("each edit must have text new_text")
            count = content.count(old_text)
            if count == 0:
                raise ValueError(f"old_text not found: {old_text[:80]}")
            replace_all = bool(edit.get("replace_all"))
            if count > 1 and not replace_all:
                raise ValueError(
                    f"old_text occurs {count} times; provide more context or set replace_all"
                )
            if first_offset is None:
                first_offset = content.find(old_text)
            content = content.replace(old_text, new_text, -1 if replace_all else 1)
            replacements += count if replace_all else 1
        preferred_line = original[: max(0, first_offset or 0)].count("\n") + 1
        if content == original:
            observations, evidence = self._post_edit_evidence(
                path, relative, preferred_line=preferred_line
            )
            return ToolResult(
                "edit_file",
                f"No change: {relative}.\n\n{evidence}",
                status="noop",
                observations=observations,
            )
        self._validate_protected_rewrite(relative, original, content)
        self._atomic_write(path, content)
        observations, evidence = self._post_edit_evidence(
            path, relative, preferred_line=preferred_line
        )
        return ToolResult(
            "edit_file",
            f"Applied {len(edits)} edit specification"
            f"{'s' if len(edits) != 1 else ''} ({replacements} replacement"
            f"{'s' if replacements != 1 else ''}) to {relative}.\n\n{evidence}",
            changed_files=(relative,),
            observations=observations,
        )

    def _tool_run_lint(self, arguments: dict[str, Any]) -> ToolResult:
        custom = str(arguments.get("command") or "")
        if custom:
            command = custom
        else:
            kind = str(arguments.get("kind") or "").casefold()
            commands = detect_project_commands(self.root)
            aliases = {
                "format": ("format", "fmt"),
                "compile": ("compile",),
                "compileall": ("compileall",),
            }
            terms = aliases.get(kind, (kind,))
            candidates = [
                cmd
                for cmd in commands
                if any(term in cmd.casefold() for term in terms)
            ]
            if not candidates:
                return ToolResult(
                    "run_lint",
                    "No matching project command found. Pass an explicit command to override.",
                    status="error",
                )
            command = candidates[0]
        result = self._tool_run_command({"command": command, "timeout": 120})
        return ToolResult(
            "run_lint",
            f"{command}\n{result.output}",
            status=result.status,
            changed_files=result.changed_files,
            commands=result.commands,
        )

    def _tool_ask_user(self, arguments: dict[str, Any]) -> ToolResult:
        question = str(arguments.get("question") or "").strip()
        detail = str(arguments.get("detail") or "")
        if not question:
            raise ValueError("question is required")
        if not self.ask:
            return ToolResult(
                "ask_user", "User interaction is not available.", status="blocked"
            )
        answer = self.ask(question, detail)
        if answer is None:
            return ToolResult(
                "ask_user", "The user dismissed the question.", status="blocked"
            )
        return ToolResult("ask_user", answer)

    @staticmethod
    def _post_edit_evidence(
        path: Path,
        relative: str,
        *,
        preferred_line: int = 1,
    ) -> tuple[tuple[FileObservation, ...], str]:
        source_hash = file_sha256(path)
        if path.suffix.casefold() in BINARY_SUFFIXES or path.stat().st_size > 2_000_000:
            return (
                (),
                f"Post-edit verification: {relative} exists; SHA-256 {source_hash}. "
                "Text preview omitted for a binary or oversized file.",
            )
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if not lines:
            return (), f"Post-edit verification: {relative} is empty; SHA-256 {source_hash}."
        center = min(len(lines), max(1, preferred_line))
        start = max(1, center - 3)
        end = min(len(lines), start + 39)
        rendered, actual_end = _render_numbered_lines(
            lines, start, end, max_chars=8000
        )
        header = f"{relative} lines {start}-{actual_end} of {len(lines)}"
        content = f"{header}\n{rendered}"
        observation = FileObservation(
            relative, start, actual_end, content, source_hash
        )
        return (
            (observation,),
            f"Post-edit verification (SHA-256 {source_hash}):\n{content}",
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

    @staticmethod
    def _validate_protected_rewrite(
        relative: str, previous: str | None, updated: str
    ) -> None:
        if relative == "AGENTS.md":
            validate_agents_rewrite(previous, updated)

    def _agents_snapshot(self) -> str | None:
        path = self.root / "AGENTS.md"
        if not path.is_file() or path.is_symlink():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def _restore_invalid_agents_rewrite(self, previous: str | None) -> str:
        path = self.root / "AGENTS.md"
        current = (
            path.read_text(encoding="utf-8", errors="replace")
            if path.is_file() and not path.is_symlink()
            else None
        )
        if current == previous:
            return ""
        try:
            if current is None:
                raise ValueError("AGENTS.md is managed by LocalCode and cannot be deleted.")
            validate_agents_rewrite(previous, current)
            return ""
        except ValueError as error:
            if previous is None:
                if path.exists() and path.is_file() and not path.is_symlink():
                    path.unlink()
            else:
                self._atomic_write(path, previous)
            return str(error)

    def _snapshot_state(self) -> dict[str, str]:
        """Return a cheap change fingerprint of the project.

        Git repositories use `git status --porcelain`, which is far faster
        than walking every file in large trees. Non-Git projects fall back
        to a full walk of mtime/size pairs. Ignored and generated
        directories are excluded in both cases.
        """
        if (self.root / ".git").exists():
            try:
                result = subprocess.run(
                    ["git", "status", "--porcelain", "--untracked-files=all"],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    timeout=20,
                    check=False,
                )
                if result.returncode == 0:
                    state: dict[str, str] = {}
                    for line in result.stdout.splitlines():
                        if len(line) < 4:
                            continue
                        state[line[3:]] = line[:2]
                    return state
            except (OSError, subprocess.SubprocessError):
                pass
        state: dict[str, str] = {}
        for path in iter_project_files(self.root):
            try:
                stat = path.stat()
            except OSError:
                continue
            state[str(path.relative_to(self.root))] = f"{stat.st_mtime_ns}:{stat.st_size}"
        return state

    @staticmethod
    def _describe_mutation(name: str, arguments: dict[str, Any]) -> str:
        if name == "run_command":
            cwd = str(arguments.get("cwd") or ".")
            return (
                "Shell commands are not sandboxed and can access files outside the project.\n"
                f"Run in {cwd}:\n{arguments.get('command', '')}"
            )
        if name == "run_lint":
            requested = arguments.get("command") or arguments.get("kind") or "auto-detected"
            return f"Run project check: {requested}"
        if name == "web_fetch":
            return f"Fetch external URL: {arguments.get('url', '')}"
        if name in {"rename_file", "copy_file"}:
            return (
                f"{name.replace('_', ' ').title()}: "
                f"{arguments.get('source', '')} → {arguments.get('target', '')}"
            )
        return f"{name.replace('_', ' ').title()}: {arguments.get('path', '')}"


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(text: str) -> str:
    if "<" not in text:
        return text
    parser = _TextHTMLParser()
    try:
        parser.feed(text)
    except Exception:
        return text
    return " ".join(parser.parts)
