from __future__ import annotations

from dataclasses import dataclass

from .database import Database

DEFAULTS = {
    "ollama_url": "http://127.0.0.1:11434",
    "default_model": "",
    "default_context_window": "32768",
    "compact_threshold": "0.78",
    "output_reserve": "4096",
    "max_tool_rounds": "16",
    "max_continuation_segments": "2",
    "change_scope": "standard",
}


@dataclass(slots=True)
class AppSettings:
    database: Database

    def get(self, key: str) -> str:
        return self.database.get_setting(key, DEFAULTS.get(key, ""))

    def set(self, key: str, value: object) -> None:
        self.database.set_setting(key, value)

    @property
    def ollama_url(self) -> str:
        return self.get("ollama_url").rstrip("/")

    @property
    def default_model(self) -> str:
        return self.get("default_model")

    @property
    def default_context_window(self) -> int:
        return max(2048, self._int("default_context_window", 32768))

    @property
    def compact_threshold(self) -> float:
        return min(0.92, max(0.5, self._float("compact_threshold", 0.78)))

    @property
    def output_reserve(self) -> int:
        return max(512, self._int("output_reserve", 4096))

    @property
    def max_tool_rounds(self) -> int:
        return min(30, max(1, self._int("max_tool_rounds", 16)))

    @property
    def max_continuation_segments(self) -> int:
        return min(4, max(1, self._int("max_continuation_segments", 2)))

    @property
    def change_scope(self) -> str:
        value = self.database.get_setting("change_scope", "")
        if value in {"focused", "standard"}:
            return value
        legacy = self.database.get_setting("code_style", "balanced")
        return "focused" if legacy == "ponytail" else "standard"

    def _int(self, key: str, fallback: int) -> int:
        try:
            return int(self.get(key))
        except ValueError:
            return fallback

    def _float(self, key: str, fallback: float) -> float:
        try:
            return float(self.get(key))
        except ValueError:
            return fallback
