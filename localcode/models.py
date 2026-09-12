from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class Project:
    id: str
    name: str
    path: str
    model: str = ""
    context_window: int = 32768
    permission_mode: str = "ask"
    created_at: str = ""
    updated_at: str = ""
    last_opened_at: str = ""


@dataclass(slots=True)
class Chat:
    id: str
    project_id: str
    title: str
    model: str = ""
    compaction_summary: str = ""
    compacted_through: int = 0
    context_used: int = 0
    context_limit: int = 0
    context_state: str = "fresh"
    created_at: str = ""
    updated_at: str = ""


@dataclass(slots=True)
class Message:
    id: int
    chat_id: str
    role: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""


@dataclass(slots=True)
class Activity:
    id: int
    chat_id: str
    kind: str
    title: str
    detail: str = ""
    status: str = "complete"
    created_at: str = ""


@dataclass(slots=True)
class WorkingFile:
    id: int
    chat_id: str
    path: str
    start_line: int
    end_line: int
    content: str
    source_hash: str
    updated_at: str = ""


@dataclass(slots=True)
class TaskCheckpoint:
    chat_id: str
    objective: str = ""
    status: str = "active"
    completed_work: str = ""
    changed_files: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    next_step: str = ""
    segment_count: int = 0
    updated_at: str = ""


@dataclass(slots=True)
class ProjectMemory:
    id: int
    project_id: str
    source_chat_id: str
    title: str
    content: str
    updated_at: str = ""


@dataclass(slots=True)
class SourceSymbols:
    id: int
    project_id: str
    path: str
    source_hash: str
    symbols: str
    updated_at: str = ""


@dataclass(slots=True)
class Provider:
    id: str
    name: str
    endpoint: str = ""
    api_key: str = ""
    is_local: bool = False
    default_context_window: int = 32768


@dataclass(slots=True)
class ContextReport:
    used: int
    limit: int
    estimated: bool = True
    state: str = "fresh"
    reason: str = ""

    @property
    def fraction(self) -> float:
        if self.limit <= 0:
            return 0.0
        return min(1.0, self.used / self.limit)
