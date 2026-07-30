from __future__ import annotations

import unittest

from localcode.context import (
    context_state,
    estimate_messages_tokens,
    estimate_request_tokens,
    estimate_text_tokens,
    select_compaction_boundary,
    should_compact,
)
from localcode.models import Message


class ContextTests(unittest.TestCase):
    def test_estimate_is_conservative_and_nonzero(self) -> None:
        self.assertEqual(estimate_text_tokens(""), 0)
        self.assertGreaterEqual(estimate_text_tokens("abcd"), 2)
        self.assertGreater(
            estimate_messages_tokens([{"role": "user", "content": "hello"}]),
            estimate_text_tokens("hello"),
        )

    def test_context_states(self) -> None:
        self.assertEqual(context_state(0, 32000), "fresh")
        self.assertEqual(context_state(16000, 32000), "healthy")
        self.assertEqual(context_state(24000, 32000), "warning")
        self.assertEqual(context_state(29000, 32000), "critical")
        self.assertEqual(context_state(32000, 32000), "critical")

    def test_request_estimate_includes_tool_schema(self) -> None:
        messages = [{"role": "user", "content": "hello"}]
        tools = [{"type": "function", "function": {"name": "read_file"}}]
        self.assertGreater(
            estimate_request_tokens(messages, tools), estimate_messages_tokens(messages)
        )

    def test_compaction_reserves_output_space(self) -> None:
        self.assertTrue(should_compact(22000, 32000, 4096, 0.78))
        self.assertFalse(should_compact(10000, 32000, 4096, 0.78))

    def test_boundary_keeps_recent_complete_turns(self) -> None:
        roles = ["user", "assistant"] * 4
        messages = [
            Message(index + 1, "chat", role, str(index)) for index, role in enumerate(roles)
        ]
        self.assertEqual(select_compaction_boundary(messages, keep_recent=4), 4)
        self.assertEqual(select_compaction_boundary(messages[:4], keep_recent=4), 0)
        self.assertEqual(
            select_compaction_boundary(messages[:2], keep_recent=4, force=True), 2
        )

    def test_boundary_never_splits_a_user_assistant_turn(self) -> None:
        messages = [
            Message(1, "chat", "user", "first"),
            Message(2, "chat", "assistant", "answer"),
            Message(3, "chat", "user", "current"),
        ]
        self.assertEqual(select_compaction_boundary(messages, keep_recent=1), 2)


if __name__ == "__main__":
    unittest.main()
