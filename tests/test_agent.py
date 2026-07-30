from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcode.agent import AgentCallbacks, AgentRunner
from localcode.database import Database
from localcode.models import ContextReport
from localcode.ollama import ChatResult, ModelInfo, ToolCall
from localcode.settings import AppSettings


class FakeOllamaClient:
    calls = 0

    def __init__(self, _endpoint: str) -> None:
        pass

    def list_models(self) -> list[ModelInfo]:
        return [ModelInfo("fake-code", context_length=32768, capabilities=["completion", "tools"])]

    def show_model(self, model: str) -> ModelInfo:
        return ModelInfo(model, context_length=32768, capabilities=["completion", "tools"])

    def chat(self, *, messages, on_chunk=None, **_kwargs) -> ChatResult:
        self.__class__.calls += 1
        if any(message.get("role") == "tool" for message in messages):
            content = "Created `hello.txt` and verified the requested content."
            if on_chunk:
                on_chunk(content)
            return ChatResult(
                content,
                prompt_tokens=700,
                eval_tokens=20,
                done_reason="stop",
                effective_context=32768,
            )
        return ChatResult(
            "",
            tool_calls=[
                ToolCall(
                    "call_1",
                    "write_file",
                    {"path": "hello.txt", "content": "hello from local model\n"},
                )
            ],
            prompt_tokens=500,
            eval_tokens=10,
            done_reason="stop",
            effective_context=32768,
        )

    def complete(self, **_kwargs) -> ChatResult:
        return ChatResult(
            "## Architecture\n\n- `hello.txt` is the generated project artifact.\n",
            prompt_tokens=400,
            eval_tokens=30,
            done_reason="stop",
            effective_context=32768,
        )


class FakeMemory:
    def recall(self, _query, _project, **_kwargs) -> str:
        return ""

    def sync_in_background(self, _project, _callback=None):
        return None


class AgentRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.old_data_home = os.environ.get("LOCALCODE_DATA_HOME")
        os.environ["LOCALCODE_DATA_HOME"] = str(self.root / "data")
        self.project_root = self.root / "project"
        self.project_root.mkdir()
        self.database = Database(self.root / "localcode.db")
        self.project = self.database.add_project(self.project_root, model="fake-code")
        self.project = self.database.update_project(self.project.id, permission_mode="allow")
        self.chat = self.database.create_chat(self.project.id)

    def tearDown(self) -> None:
        if self.old_data_home is None:
            os.environ.pop("LOCALCODE_DATA_HOME", None)
        else:
            os.environ["LOCALCODE_DATA_HOME"] = self.old_data_home
        self.temporary.cleanup()

    def test_agent_writes_code_updates_agents_and_preserves_transcript(self) -> None:
        chunks: list[str] = []
        reports: list[ContextReport] = []
        errors: list[str] = []
        callbacks = AgentCallbacks(chunk=chunks.append, context=reports.append, error=errors.append)
        runner = AgentRunner(self.database, AppSettings(self.database), FakeMemory())

        with patch("localcode.agent.OllamaClient", FakeOllamaClient):
            runner.run_turn(self.chat.id, "Create hello.txt", callbacks)

        self.assertEqual(errors, [])
        self.assertEqual(
            (self.project_root / "hello.txt").read_text(encoding="utf-8"),
            "hello from local model\n",
        )
        agents = (self.project_root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("hello.txt", agents)
        messages = self.database.list_messages(self.chat.id)
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertIn("Created", messages[-1].content)
        self.assertTrue(reports)
        transcript = self.root / "data" / "transcripts" / self.project.id / f"{self.chat.id}.jsonl"
        self.assertTrue(transcript.is_file())

    def test_agent_auto_compacts_without_deleting_messages(self) -> None:
        self.project = self.database.update_project(self.project.id, context_window=4096)
        for index in range(12):
            role = "user" if index % 2 == 0 else "assistant"
            self.database.add_message(self.chat.id, role, f"old-{index}: " + ("x" * 700))
        before = len(self.database.list_messages(self.chat.id))
        notices: list[tuple[str, str, str]] = []
        runner = AgentRunner(self.database, AppSettings(self.database), FakeMemory())

        with patch("localcode.agent.OllamaClient", FakeOllamaClient):
            runner.run_turn(
                self.chat.id,
                "Create hello.txt",
                AgentCallbacks(
                    notice=lambda level, title, body: notices.append((level, title, body))
                ),
            )

        compacted = self.database.get_chat(self.chat.id)
        self.assertIsNotNone(compacted)
        self.assertGreater(compacted.compacted_through, 0)
        self.assertGreater(len(self.database.list_messages(self.chat.id)), before)
        self.assertTrue(any("compacted" in title.casefold() for _level, title, _body in notices))


if __name__ == "__main__":
    unittest.main()
