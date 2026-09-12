from __future__ import annotations


AGENTS_START_MARKER = "<!-- localcode:managed:start -->"
AGENTS_END_MARKER = "<!-- localcode:managed:end -->"


def validate_agents_markers(content: str, *, allow_missing: bool = False) -> int:
    """Validate the single LocalCode-managed section in a root AGENTS.md file."""
    starts = content.count(AGENTS_START_MARKER)
    ends = content.count(AGENTS_END_MARKER)
    if starts == ends == 0 and allow_missing:
        return 0
    if (
        starts != 1
        or ends != 1
        or content.index(AGENTS_START_MARKER) > content.index(AGENTS_END_MARKER)
    ):
        raise ValueError("AGENTS.md has malformed LocalCode managed markers.")
    return 1


def validate_agents_rewrite(previous: str | None, updated: str) -> None:
    """Reject a rewrite that damages markers or edits human-owned surrounding text."""
    validate_agents_markers(updated)
    if previous is None:
        return
    validate_agents_markers(previous)
    old_prefix, old_rest = previous.split(AGENTS_START_MARKER, 1)
    _old_managed, old_suffix = old_rest.split(AGENTS_END_MARKER, 1)
    new_prefix, new_rest = updated.split(AGENTS_START_MARKER, 1)
    _new_managed, new_suffix = new_rest.split(AGENTS_END_MARKER, 1)
    if old_prefix != new_prefix or old_suffix != new_suffix:
        raise ValueError(
            "AGENTS.md text outside the LocalCode managed markers must be preserved."
        )
