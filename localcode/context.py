from __future__ import annotations

import json
import math
from collections.abc import Iterable

from .models import ContextReport, Message


def estimate_text_tokens(text: str) -> int:
    """Return a deliberately conservative tokenizer-independent estimate."""
    if not text:
        return 0
    byte_count = len(text.encode("utf-8", errors="replace"))
    return max(1, math.ceil(byte_count / 3.2))


def estimate_messages_tokens(messages: Iterable[dict[str, object]]) -> int:
    total = 3
    for message in messages:
        total += 8
        total += estimate_text_tokens(str(message.get("role", "")))
        total += estimate_text_tokens(str(message.get("content", "")))
        tool_calls = message.get("tool_calls")
        if tool_calls:
            total += estimate_text_tokens(str(tool_calls))
    return total


def estimate_tools_tokens(tools: Iterable[dict[str, object]]) -> int:
    items = list(tools)
    if not items:
        return 0
    serialized = json.dumps(items, ensure_ascii=False, separators=(",", ":"))
    return estimate_text_tokens(serialized) + 24


def estimate_request_tokens(
    messages: Iterable[dict[str, object]],
    tools: Iterable[dict[str, object]] = (),
) -> int:
    return estimate_messages_tokens(messages) + estimate_tools_tokens(tools)


def context_state(used: int, limit: int) -> str:
    if used <= 0 or limit <= 0:
        return "fresh"
    fraction = used / limit
    if fraction >= 0.88:
        return "critical"
    if fraction >= 0.70:
        return "warning"
    return "healthy"


def make_report(
    used: int,
    limit: int,
    *,
    estimated: bool,
    reason: str = "",
) -> ContextReport:
    return ContextReport(
        used=max(0, used),
        limit=max(0, limit),
        estimated=estimated,
        state=context_state(used, limit),
        reason=reason,
    )


def should_compact(
    estimated_prompt_tokens: int,
    context_limit: int,
    output_reserve: int,
    threshold: float,
) -> bool:
    if context_limit <= 0:
        return False
    guarded_usage = estimated_prompt_tokens + output_reserve
    return guarded_usage >= int(context_limit * threshold)


def select_compaction_boundary(
    messages: list[Message], keep_recent: int = 4, *, force: bool = False
) -> int:
    """Return the last message id to compact while retaining recent turns."""
    eligible = messages if force else messages[: max(0, len(messages) - keep_recent)]
    assistants = [message for message in eligible if message.role == "assistant"]
    return assistants[-1].id if assistants else 0


def format_token_count(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}m"
    if value >= 1_000:
        precision = 0 if value >= 10_000 else 1
        return f"{value / 1_000:.{precision}f}k"
    return str(value)
