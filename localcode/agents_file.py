from __future__ import annotations

import tempfile
from pathlib import Path

from .managed_files import (
    AGENTS_END_MARKER,
    AGENTS_START_MARKER,
    validate_agents_markers,
)
from .projects import detect_project_commands, project_tree

START_MARKER = AGENTS_START_MARKER
END_MARKER = AGENTS_END_MARKER
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

    def read_existing(self, max_chars: int = 16000) -> str:
        """Return the current content without creating or rewriting the file."""
        if not self.path.exists():
            return ""
        self._validate_path()
        return self.path.read_text(encoding="utf-8", errors="replace")[:max_chars]

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
        return validate_agents_markers(content, allow_missing=True)

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
- Edit AGENTS.md only when the user explicitly requests a durable guidance change.

## Project Map

```text
{project_tree(self.root, max_files=80, max_depth=3)}
```

## Development Commands

{command_lines}
"""
