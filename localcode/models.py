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
    memory_enabled: bool = True
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
