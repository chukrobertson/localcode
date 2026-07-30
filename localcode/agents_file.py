from __future__ import annotations

import re
import tempfile
from pathlib import Path

from .projects import detect_project_commands, git_summary, project_tree

START_MARKER = "<!-- localcode:managed:start -->"
END_MARKER = "<!-- localcode:managed:end -->"
AGENTS_FILENAME = "AGENTS.md"


class AgentsFileManager:
    def __init__(self, project_root: Path | str) -> None:
        self.root = Path(project_root).expanduser().resolve()
        self.path = self.root / AGENTS_FILENAME

    def ensure(self) -> Path:
        self._validate_path()
        managed = self._initial_managed_content()
        if not self.path.exists():
            content = (
                "# AGENTS.md\n\n"
                f"{START_MARKER}\n{managed.rstrip()}\n{END_MARKER}\n\n"
                "## Project Notes\n\n"
                "Add durable human-authored instructions here. LocalCode preserves this section.\n"
            )
            self._write(content)
            return self.path

        existing = self.path.read_text(encoding="utf-8", errors="replace")
        marker_count = self._validate_markers(existing)
        if marker_count == 0:
            separator = "" if existing.endswith("\n") else "\n"
            existing += f"{separator}\n{START_MARKER}\n{managed.rstrip()}\n{END_MARKER}\n"
            self._write(existing)
        return self.path

    def read(self, max_chars: int = 16000) -> str:
        self.ensure()
        return self.path.read_text(encoding="utf-8", errors="replace")[:max_chars]

    def current_managed_content(self) -> str:
        content = self.read()
        match = re.search(
            rf"{re.escape(START_MARKER)}\n?(.*?)(?:\n)?{re.escape(END_MARKER)}",
            content,
            flags=re.DOTALL,
        )
        return match.group(1).strip() if match else ""

    def update_prompt(self, changed_files: set[str], commands: list[str]) -> str:
        changed = "\n".join(f"- {path}" for path in sorted(changed_files)) or "- None"
        command_log = "\n".join(f"- `{command}`" for command in commands[-8:]) or "- None"
        return f"""Update the model-managed section of AGENTS.md for this coding project.

Return only Markdown for the managed section. Do not include the marker comments, a top-level
AGENTS.md heading, or a fenced code block. Keep it factual and under 1200 words. Describe the
project structure, important architecture, canonical development commands, conventions, and
durable implementation facts. Do not include chat history, temporary plans, or claims that are
not supported by the project files. Source code is authoritative.

Current managed section:
{self.current_managed_content()}

Files changed in the latest turn:
{changed}

Commands run in the latest turn:
{command_log}

Current project tree:
{project_tree(self.root, max_files=140, max_depth=4)}

Git state:
{git_summary(self.root)}
"""

    def apply_model_update(self, model_content: str) -> bool:
        clean = self._strip_fence(model_content).strip()
        if (
            not clean
            or len(clean) > 50000
            or START_MARKER in clean
            or END_MARKER in clean
        ):
            return False
        self.ensure()
        existing = self.path.read_text(encoding="utf-8", errors="replace")
        self._validate_markers(existing)
        pattern = re.compile(rf"{re.escape(START_MARKER)}.*?{re.escape(END_MARKER)}", re.DOTALL)
        replacement = f"{START_MARKER}\n{clean}\n{END_MARKER}"
        updated, count = pattern.subn(lambda _: replacement, existing, count=1)
        if count != 1 or updated == existing:
            return False
        self._write(updated)
        return True

    def _write(self, content: str) -> None:
        mode = self.path.stat().st_mode if self.path.exists() else None
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=self.root,
            prefix=".AGENTS.md.",
            delete=False,
        ) as handle:
            handle.write(content)
            temporary = Path(handle.name)
        if mode is not None:
            temporary.chmod(mode)
        temporary.replace(self.path)

    def _validate_path(self) -> None:
        if self.path.is_symlink():
            raise ValueError("AGENTS.md must not be a symbolic link.")
        if self.path.exists() and not self.path.is_file():
            raise ValueError("AGENTS.md must be a regular file.")
        try:
            self.path.resolve(strict=False).relative_to(self.root)
        except ValueError as error:
            raise ValueError("AGENTS.md escapes the project root.") from error

    @staticmethod
    def _validate_markers(content: str) -> int:
        starts = content.count(START_MARKER)
        ends = content.count(END_MARKER)
        if starts == ends == 0:
            return 0
        if starts != 1 or ends != 1 or content.index(START_MARKER) > content.index(END_MARKER):
            raise ValueError("AGENTS.md has malformed LocalCode managed markers.")
        return 1

    def _initial_managed_content(self) -> str:
        commands = detect_project_commands(self.root)
        command_lines = "\n".join(f"- `{command}`" for command in commands)
        if not command_lines:
            command_lines = (
                "- Inspect the project manifests before choosing build or test commands."
            )
        return f"""## Working Agreement

- Treat files in this repository as the source of truth.
- Read relevant code before editing and keep changes narrowly scoped.
- Run the closest available checks after modifying code.
- Keep this managed section current when architecture or commands change.

## Project Map

```text
{project_tree(self.root, max_files=80, max_depth=3)}
```

## Development Commands

{command_lines}
"""

    @staticmethod
    def _strip_fence(content: str) -> str:
        stripped = content.strip()
        match = re.fullmatch(r"```(?:markdown|md)?\s*\n(.*)\n```", stripped, re.DOTALL)
        return match.group(1) if match else stripped
