from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from localcode.agent import AgentCallbacks, AgentRunner
from localcode.agents_file import AgentsFileManager
from localcode.database import Database
from localcode.models import ContextReport
from localcode.projects import ProjectTools, file_sha256
from localcode.providers import ProviderChatResult, ProviderError, ProviderToolCall
from localcode.settings import AppSettings


def fake_run_chat_factory():
    calls = {"count": 0}

    def run_chat(model, settings, *, messages, context_window, output_tokens,
                 tools=None, on_chunk=None, cancel=None, **_kwargs):
        calls["count"] += 1
        if any(message.get("role") == "tool" for message in messages):
            content = "Created `hello.txt` and verified the requested content."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=700,
                eval_tokens=20,
                done_reason="stop",
                effective_context=context_window or 32768,
            )
        return ProviderChatResult(
            "",
            tool_calls=[
                ProviderToolCall(
                    id="call_1",
                    name="write_file",
                    arguments={"path": "hello.txt", "content": "hello from local model\n"},
                )
            ],
            prompt_tokens=500,
            eval_tokens=10,
            done_reason="stop",
            effective_context=context_window or 32768,
        )

    return run_chat, calls


def fake_run_complete(model, settings, *, system, prompt, context_window,
                      output_tokens=2048, cancel=None, **_kwargs):
    return ProviderChatResult(
        "## Architecture\n\n- `hello.txt` is the generated project artifact.\n",
        prompt_tokens=400,
        eval_tokens=30,
        done_reason="stop",
        effective_context=context_window or 32768,
    )


def fake_show_model_info(model, settings):
    return 32768, True


def fake_run_chat_no_usage_factory():
    def run_chat(model, settings, *, messages, context_window, output_tokens,
                 tools=None, on_chunk=None, cancel=None, **_kwargs):
        content = "The API did not report token usage."
        if on_chunk:
            on_chunk(content)
        return ProviderChatResult(
            content,
            prompt_tokens=0,
            eval_tokens=0,
            done_reason="stop",
            effective_context=context_window or 32768,
            counts_exact=False,
            usage_unavailable=True,
        )

    return run_chat


def read_then_edit_factory():
    calls = {"count": 0, "messages": [], "tools": []}

    def run_chat(model, settings, *, messages, context_window, output_tokens,
                 tools=None, on_chunk=None, cancel=None, **_kwargs):
        calls["count"] += 1
        calls["messages"].append([dict(message) for message in messages])
        calls["tools"].append(
            [item["function"]["name"] for item in (tools or [])]
        )
        if calls["count"] == 1:
            return ProviderChatResult(
                "",
                tool_calls=[
                    ProviderToolCall(id="c1", name="read_file", arguments={"path": "index.html"})
                ],
                prompt_tokens=400,
                eval_tokens=8,
                done_reason="stop",
                effective_context=context_window or 32768,
            )
        if calls["count"] == 2:
            return ProviderChatResult(
                "",
                tool_calls=[
                    ProviderToolCall(
                        id="c2",
                        name="replace_in_file",
                        arguments={
                            "path": "index.html",
                            "old_text": "Welcome to the site",
                            "new_text": "Hello from LocalCode",
                        },
                    )
                ],
                prompt_tokens=500,
                eval_tokens=9,
                done_reason="stop",
                effective_context=context_window or 32768,
            )
        content = "Inspected index.html and updated the headline."
        if on_chunk:
            on_chunk(content)
        return ProviderChatResult(
            content,
            prompt_tokens=600,
            eval_tokens=12,
            done_reason="stop",
            effective_context=context_window or 32768,
        )

    return run_chat, calls


def failing_edit_factory():
    calls = {"count": 0}

    def run_chat(model, settings, *, messages, context_window, output_tokens,
                 tools=None, on_chunk=None, cancel=None, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return ProviderChatResult(
                "",
                tool_calls=[
                    ProviderToolCall(
                        id="c1",
                        name="replace_in_file",
                        arguments={
                            "path": "index.html",
                            "old_text": "Text that does not exist",
                            "new_text": "replacement",
                        },
                    )
                ],
                prompt_tokens=400,
                eval_tokens=8,
                done_reason="stop",
                effective_context=context_window or 32768,
            )
        content = "The requested text was not found in index.html."
        if on_chunk:
            on_chunk(content)
        return ProviderChatResult(
            content,
            prompt_tokens=500,
            eval_tokens=10,
            done_reason="stop",
            effective_context=context_window or 32768,
        )

    return run_chat, calls


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

    def test_agent_writes_code_preserves_agents_and_transcript(self) -> None:
        chunks: list[str] = []
        reports: list[ContextReport] = []
        errors: list[str] = []
        callbacks = AgentCallbacks(
            chunk=chunks.append, context=reports.append, error=errors.append
        )
        runner = AgentRunner(self.database, AppSettings(self.database))

        with (
            patch("localcode.agent.run_chat", fake_run_chat_factory()[0]),
            patch("localcode.agent.run_complete") as automatic_completion,
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(self.chat.id, "Create hello.txt", callbacks)

        self.assertEqual(errors, [])
        self.assertEqual(
            (self.project_root / "hello.txt").read_text(encoding="utf-8"),
            "hello from local model\n",
        )
        agents = (self.project_root / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("## Working Agreement", agents)
        self.assertNotIn("hello.txt", agents)
        self.assertFalse(automatic_completion.called)
        messages = self.database.list_messages(self.chat.id)
        self.assertEqual([message.role for message in messages], ["user", "assistant"])
        self.assertIn("Created", messages[-1].content)
        self.assertTrue(reports)
        transcript = (
            self.root / "data" / "transcripts" / self.project.id / f"{self.chat.id}.jsonl"
        )
        self.assertTrue(transcript.is_file())

    def test_agent_auto_compacts_without_deleting_messages(self) -> None:
        self.project = self.database.update_project(self.project.id, context_window=4096)
        for index in range(12):
            role = "user" if index % 2 == 0 else "assistant"
            self.database.add_message(self.chat.id, role, f"old-{index}: " + ("x" * 700))
        before = len(self.database.list_messages(self.chat.id))
        notices: list[tuple[str, str, str]] = []
        runner = AgentRunner(self.database, AppSettings(self.database))

        with (
            patch("localcode.agent.run_chat", fake_run_chat_factory()[0]),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
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
        self.assertTrue(
            any("compacted" in title.casefold() for _level, title, _body in notices)
        )

    def test_read_only_turn_does_not_mutate_the_project(self) -> None:
        self.project = self.database.update_project(self.project.id, permission_mode="read-only")
        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))

        with (
            patch("localcode.agent.run_chat", fake_run_chat_factory()[0]),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(self.chat.id, "Create hello.txt", AgentCallbacks(error=errors.append))

        self.assertEqual(errors, [])
        self.assertFalse((self.project_root / "hello.txt").exists())
        self.assertFalse((self.project_root / "AGENTS.md").exists())
        messages = self.database.list_messages(self.chat.id)
        self.assertEqual([message.role for message in messages], ["user", "assistant"])

    def test_missing_usage_is_reported_as_an_estimate_never_exact_zero(self) -> None:
        reports: list[ContextReport] = []
        runner = AgentRunner(self.database, AppSettings(self.database))

        with (
            patch("localcode.agent.run_chat", fake_run_chat_no_usage_factory()),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id, "Change the homepage wording.", AgentCallbacks(context=reports.append)
            )

        final = reports[-1]
        self.assertTrue(final.estimated)
        self.assertGreater(final.used, 0)
        self.assertIn("did not report", final.reason)

    def test_empty_provider_stream_reports_error_and_next_turn_recovers(self) -> None:
        def broken_run_chat(model, settings, *, messages, context_window, output_tokens,
                            tools=None, on_chunk=None, cancel=None, **_kwargs):
            raise ProviderError("The API returned an empty or unusable stream.")

        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", broken_run_chat),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(self.chat.id, "Inspect the site.", AgentCallbacks(error=errors.append))

        self.assertEqual(len(errors), 1)
        self.assertIn("empty", errors[0].casefold())

        follow_up_errors: list[str] = []
        with (
            patch("localcode.agent.run_chat", fake_run_chat_factory()[0]),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Create hello.txt",
                AgentCallbacks(error=follow_up_errors.append),
            )

        self.assertEqual(follow_up_errors, [])
        self.assertTrue((self.project_root / "hello.txt").exists())

    def test_multi_round_tool_loop_inspects_then_edits(self) -> None:
        (self.project_root / "index.html").write_text(
            "<h1>Welcome to the site</h1>\n", encoding="utf-8"
        )
        run_chat, calls = read_then_edit_factory()
        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))

        with (
            patch("localcode.agent.run_chat", run_chat),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Update the headline on index.html",
                AgentCallbacks(error=errors.append),
            )

        self.assertEqual(errors, [])
        self.assertEqual(calls["count"], 3)
        updated = (self.project_root / "index.html").read_text(encoding="utf-8")
        self.assertIn("Hello from LocalCode", updated)
        self.assertNotIn("Welcome to the site", updated)
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(assistant.role, "assistant")
        self.assertIn("index.html", assistant.metadata["changed_files"])
        first_tool_result = next(
            message
            for message in calls["messages"][1]
            if message.get("role") == "tool"
        )
        self.assertEqual(first_tool_result["tool_call_id"], "c1")
        self.assertEqual(first_tool_result["tool_name"], "read_file")
        working_files = self.database.list_working_files(self.chat.id)
        index_observation = next(
            item for item in working_files if item.path == "index.html"
        )
        self.assertEqual(
            index_observation.source_hash,
            file_sha256(self.project_root / "index.html"),
        )
        self.assertIn("Hello from LocalCode", index_observation.content)
        self.assertIn("copy_file", calls["tools"][0])
        self.assertNotIn("run_lint", calls["tools"][0])
        self.assertIn("run_lint", calls["tools"][2])
        self.assertIn("git_status", calls["tools"][2])
        self.assertIn("copy_file", calls["tools"][2])

    def test_initial_tool_phase_follows_intent_and_permissions(self) -> None:
        self.assertEqual(
            AgentRunner._initial_tool_phase("Inspect and explain main.py", permission_mode="allow"),
            "inspect",
        )
        self.assertEqual(
            AgentRunner._initial_tool_phase("Fix and test main.py", permission_mode="allow"),
            "edit",
        )
        self.assertEqual(
            AgentRunner._initial_tool_phase("Run the tests", permission_mode="allow"),
            "verify",
        )
        self.assertEqual(
            AgentRunner._initial_tool_phase("Fix main.py", permission_mode="read-only"),
            "inspect",
        )

    def test_explicit_mutating_tool_name_forces_edit_phase(self) -> None:
        prompt = (
            "Continue from the verified source. Do not call edit_file. "
            "Call replace_lines exactly once, then summarize the verification results."
        )

        phase = AgentRunner._initial_tool_phase(prompt, permission_mode="allow")
        tools = {
            item["function"]["name"]
            for item in ProjectTools.definitions_for_phase(
                phase, permission_mode="allow"
            )
        }

        self.assertEqual(phase, "edit")
        self.assertIn("replace_lines", tools)
        self.assertEqual(
            AgentRunner._explicitly_requested_mutating_tools(prompt),
            {"replace_lines"},
        )

    def test_multi_step_edit_can_copy_after_first_mutation(self) -> None:
        calls = {"count": 0, "palettes": []}

        def create_then_copy(model, settings, *, messages, context_window,
                             output_tokens, tools=None, on_chunk=None,
                             cancel=None, **_kwargs):
            calls["count"] += 1
            calls["palettes"].append(
                [item["function"]["name"] for item in (tools or [])]
            )
            if calls["count"] == 1:
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id="write-1",
                            name="write_file",
                            arguments={"path": "source.txt", "content": "smoke\n"},
                        )
                    ],
                    prompt_tokens=300,
                    eval_tokens=8,
                    done_reason="stop",
                    effective_context=context_window,
                )
            if calls["count"] == 2:
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id="copy-1",
                            name="copy_file",
                            arguments={"source": "source.txt", "target": "copy.txt"},
                        )
                    ],
                    prompt_tokens=400,
                    eval_tokens=8,
                    done_reason="stop",
                    effective_context=context_window,
                )
            content = "Created source.txt and copied it to copy.txt."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=450,
                eval_tokens=10,
                done_reason="stop",
                effective_context=context_window,
            )

        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", create_then_copy),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Create source.txt, then copy it to copy.txt.",
                AgentCallbacks(error=errors.append),
            )

        self.assertEqual(errors, [])
        self.assertEqual(calls["count"], 3)
        self.assertIn("copy_file", calls["palettes"][1])
        self.assertEqual(
            (self.project_root / "copy.txt").read_text(encoding="utf-8"),
            "smoke\n",
        )
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(assistant.metadata["failures"], [])

    def test_failed_tool_does_not_break_the_conversation(self) -> None:
        (self.project_root / "index.html").write_text(
            "<h1>Welcome to the site</h1>\n", encoding="utf-8"
        )
        run_chat, calls = failing_edit_factory()
        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))

        with (
            patch("localcode.agent.run_chat", run_chat),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Fix the headline",
                AgentCallbacks(error=errors.append),
            )

        self.assertEqual(errors, [])
        self.assertEqual(calls["count"], 2)
        self.assertEqual(
            (self.project_root / "index.html").read_text(encoding="utf-8"),
            "<h1>Welcome to the site</h1>\n",
        )
        tool_activities = [
            activity
            for activity in self.database.list_activities(self.chat.id)
            if activity.kind == "tool"
        ]
        self.assertEqual(len(tool_activities), 1)
        self.assertEqual(tool_activities[0].status, "error")
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertIn("not found", assistant.content)

    def test_denied_approval_leaves_files_untouched(self) -> None:
        self.project = self.database.update_project(self.project.id, permission_mode="ask")
        run_chat, calls = fake_run_chat_factory()
        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))

        with (
            patch("localcode.agent.run_chat", run_chat),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Create hello.txt",
                AgentCallbacks(approval=lambda _name, _body: False, error=errors.append),
            )

        self.assertEqual(errors, [])
        self.assertFalse((self.project_root / "hello.txt").exists())
        tool_activities = [
            activity
            for activity in self.database.list_activities(self.chat.id)
            if activity.kind == "tool"
        ]
        self.assertTrue(tool_activities)
        self.assertTrue(all(activity.status == "error" for activity in tool_activities))
        self.assertEqual(self.database.list_messages(self.chat.id)[-1].role, "assistant")

    def test_cancelling_between_tool_calls_is_recoverable(self) -> None:
        (self.project_root / "index.html").write_text("<h1>Site</h1>\n", encoding="utf-8")
        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))

        def request_write(model, settings, *, messages, context_window, output_tokens,
                          tools=None, on_chunk=None, cancel=None, **_kwargs):
            return ProviderChatResult(
                "",
                tool_calls=[
                    ProviderToolCall(
                        id="c1",
                        name="write_file",
                        arguments={"path": "index.html", "content": "<h1>Changed</h1>\n"},
                    )
                ],
                prompt_tokens=400,
                eval_tokens=8,
                done_reason="stop",
                effective_context=context_window or 32768,
            )

        def cancel_after_tool_completes(kind, title, detail, status):
            if kind == "tool" and status == "complete":
                runner.cancel()

        with (
            patch("localcode.agent.run_chat", request_write),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Change the site",
                AgentCallbacks(activity=cancel_after_tool_completes, error=errors.append),
            )

        self.assertEqual(len(errors), 1)
        self.assertIn("cancelled", errors[0].casefold())
        self.assertEqual(
            (self.project_root / "index.html").read_text(encoding="utf-8"), "<h1>Changed</h1>\n"
        )

        runner.reset_cancel()
        follow_up_errors: list[str] = []
        with (
            patch("localcode.agent.run_chat", fake_run_chat_factory()[0]),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id, "Create hello.txt", AgentCallbacks(error=follow_up_errors.append)
            )
        self.assertEqual(follow_up_errors, [])
        self.assertTrue((self.project_root / "hello.txt").exists())

    def test_context_guard_stops_tool_rounds_before_truncation(self) -> None:
        notices: list[tuple[str, str, str]] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        tools = ProjectTools(self.project_root, permission_mode="allow")
        messages = [
            {"role": "system", "content": "x" * 50},
            {"role": "user", "content": "y" * 4000},
        ]

        content, result = runner._tool_loop(
            "fake-model",
            messages,
            context_window=2048,
            output_tokens=512,
            tools=tools,
            chat=self.chat,
            callbacks=AgentCallbacks(
                notice=lambda level, title, body: notices.append((level, title, body))
            ),
        )

        self.assertEqual(result.done_reason, "context_guard")
        self.assertEqual(content, "")
        self.assertFalse(result.counts_exact)
        self.assertTrue(
            any("context limit" in title.casefold() for _level, title, _body in notices)
        )

    def test_verified_working_file_is_reused_and_invalidated_on_change(self) -> None:
        source = self.project_root / "main.py"
        source.write_text("answer = 42\n", encoding="utf-8")
        self.database.remember_working_file(
            self.chat.id,
            "main.py",
            1,
            1,
            "main.py lines 1-1 of 1\n     1  answer = 42",
            file_sha256(source),
        )
        runner = AgentRunner(self.database, AppSettings(self.database))
        context, known = runner._working_set_context(
            self.chat, self.project, context_window=32768
        )
        self.assertIn("answer = 42", context)
        self.assertEqual(len(known), 1)

        source.write_text("answer = 43\n", encoding="utf-8")
        context, known = runner._working_set_context(
            self.chat, self.project, context_window=32768
        )
        self.assertEqual(context, "")
        self.assertEqual(known, set())
        self.assertEqual(self.database.list_working_files(self.chat.id), [])

    def test_live_tool_context_releases_old_file_reads(self) -> None:
        messages = [{"role": "system", "content": "system"}]
        for index in range(5):
            messages.append(
                {
                    "role": "tool",
                    "tool_name": "read_file",
                    "content": f"file{index}.py lines 1-200 of 200\n" + ("x" * 5000),
                }
            )
        pruned = AgentRunner._prune_tool_context(
            messages, ProjectTools.definitions(), target_tokens=4000
        )
        self.assertGreater(pruned, 0)
        self.assertIn("released from live context", messages[1]["content"])
        self.assertGreater(len(messages[-1]["content"]), 500)
        self.assertGreater(len(messages[-2]["content"]), 500)

    def test_temporary_tool_pressure_does_not_force_chat_compaction(self) -> None:
        def high_usage_result(model, settings, *, messages, context_window, output_tokens,
                              tools=None, on_chunk=None, cancel=None, **_kwargs):
            content = "Finished the bounded task."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=28000,
                eval_tokens=20,
                done_reason="stop",
                effective_context=context_window,
            )

        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", high_usage_result),
            patch("localcode.agent.run_complete") as compact,
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(self.chat.id, "Inspect briefly.", AgentCallbacks())

        self.assertFalse(compact.called)
        refreshed = self.database.get_chat(self.chat.id)
        self.assertEqual(refreshed.compacted_through, 0)

    def test_step_limit_automatically_continues_with_fresh_checkpoint(self) -> None:
        source = self.project_root / "main.py"
        source.write_text("def answer():\n    return 42\n", encoding="utf-8")
        calls: list[list[dict]] = []

        def continue_after_read(model, settings, *, messages, context_window,
                                output_tokens, tools=None, on_chunk=None,
                                cancel=None, **_kwargs):
            calls.append(messages)
            if len(calls) == 1:
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id="read-1",
                            name="read_file",
                            arguments={"path": "main.py", "start_line": 1, "end_line": 2},
                        )
                    ],
                    prompt_tokens=500,
                    eval_tokens=8,
                    done_reason="stop",
                    effective_context=context_window,
                )
            content = "Finished after continuing from verified source."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=600,
                eval_tokens=10,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_tool_rounds", 1)
        self.database.set_setting("max_continuation_segments", 2)
        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", continue_after_read),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Inspect main.py and explain answer.",
                AgentCallbacks(error=errors.append),
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2)
        second_context = "\n".join(
            str(message.get("content", "")) for message in calls[1]
        )
        self.assertIn("LOCALCODE CONTINUATION CHECKPOINT", second_context)
        self.assertIn("def answer", second_context)
        self.assertFalse(any(message.get("role") == "tool" for message in calls[1]))
        self.assertEqual(
            sum(message.get("role") == "user" for message in calls[1]), 1
        )
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(assistant.metadata["segments_used"], 2)
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.status, "complete")
        self.assertEqual(checkpoint.segment_count, 2)
        memories = self.database.search_project_memories(
            self.project.id, "explain answer"
        )
        self.assertEqual([memory.source_chat_id for memory in memories], [self.chat.id])

    def test_output_limit_automatically_continues_with_action_focused_checkpoint(self) -> None:
        calls: list[list[dict]] = []

        def capped_then_write(model, settings, *, messages, context_window,
                              output_tokens, tools=None, on_chunk=None,
                              cancel=None, **_kwargs):
            calls.append(messages)
            if len(calls) == 1:
                content = "I have analyzed the request and will now edit the file with"
                if on_chunk:
                    on_chunk(content)
                return ProviderChatResult(
                    content,
                    prompt_tokens=14840,
                    eval_tokens=output_tokens,
                    done_reason="length",
                    effective_context=48128,
                )
            if len(calls) == 2:
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id="write-after-cap",
                            name="write_file",
                            arguments={"path": "result.txt", "content": "finished\n"},
                        )
                    ],
                    prompt_tokens=900,
                    eval_tokens=12,
                    done_reason="tool_calls",
                    effective_context=context_window,
                )
            content = "Created `result.txt` and finished the focused task."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=1000,
                eval_tokens=18,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_continuation_segments", 2)
        activities: list[tuple[str, str, str, str]] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", capped_then_write),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", lambda *_args: (48128, True)),
        ):
            runner.run_turn(
                self.chat.id,
                "Create result.txt with the word finished.",
                AgentCallbacks(
                    activity=lambda kind, title, detail, status: activities.append(
                        (kind, title, detail, status)
                    )
                ),
            )

        self.assertEqual(len(calls), 3)
        continuation = "\n".join(
            str(message.get("content", "")) for message in calls[1]
        )
        self.assertIn("Stop reason: length", continuation)
        self.assertIn("response token limit, not the context-window limit", continuation)
        self.assertIn("Do not repeat its analysis", continuation)
        self.assertEqual(
            (self.project_root / "result.txt").read_text(encoding="utf-8"),
            "finished\n",
        )
        self.assertTrue(
            any(title == "Continuing after output limit" for _kind, title, _detail, _status in activities)
        )
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(assistant.metadata["done_reason"], "stop")
        self.assertFalse(assistant.metadata["context_exhausted"])
        self.assertEqual(assistant.metadata["segments_used"], 2)
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.status, "complete")
        self.assertTrue(checkpoint.completed_work.startswith("Verified actions:"))

    def test_repeated_output_limit_is_never_saved_as_complete(self) -> None:
        calls: list[list[dict]] = []

        def always_capped(model, settings, *, messages, context_window,
                          output_tokens, tools=None, on_chunk=None,
                          cancel=None, **_kwargs):
            calls.append(messages)
            content = "Still analyzing the possible implementation and next I would"
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=14840,
                eval_tokens=output_tokens,
                done_reason="length",
                effective_context=48128,
            )

        self.database.set_setting("max_continuation_segments", 2)
        notices: list[tuple[str, str, str]] = []
        reports: list[ContextReport] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", always_capped),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", lambda *_args: (48128, True)),
        ):
            runner.run_turn(
                self.chat.id,
                "Fix the shutdown implementation.",
                AgentCallbacks(
                    notice=lambda level, title, body: notices.append(
                        (level, title, body)
                    ),
                    context=reports.append,
                ),
            )

        self.assertEqual(len(calls), 2)
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(assistant.metadata["done_reason"], "length")
        self.assertFalse(assistant.metadata["context_exhausted"])
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.status, "needs-continuation")
        self.assertEqual(
            checkpoint.completed_work,
            "No project changes were completed before the turn stopped.",
        )
        self.assertEqual(reports[-1].reason, "Generation reached provider output limit")
        self.assertNotEqual(reports[-1].state, "exhausted")
        self.assertTrue(
            any(title == "Model reached its output limit" for _level, title, _body in notices)
        )

    def test_context_exhaustion_remains_distinct_from_output_limit(self) -> None:
        calls: list[list[dict]] = []

        def exhausted_then_finish(model, settings, *, messages, context_window,
                                  output_tokens, tools=None, on_chunk=None,
                                  cancel=None, **_kwargs):
            calls.append(messages)
            if len(calls) == 1:
                return ProviderChatResult(
                    "",
                    prompt_tokens=44000,
                    eval_tokens=4127,
                    done_reason="length",
                    effective_context=48128,
                )
            content = "Finished after recovering from context exhaustion."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=900,
                eval_tokens=12,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_continuation_segments", 2)
        activities: list[tuple[str, str, str, str]] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", exhausted_then_finish),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", lambda *_args: (48128, True)),
        ):
            runner.run_turn(
                self.chat.id,
                "Inspect the project and report the result.",
                AgentCallbacks(
                    activity=lambda kind, title, detail, status: activities.append(
                        (kind, title, detail, status)
                    )
                ),
            )

        self.assertEqual(len(calls), 2)
        continuation = "\n".join(
            str(message.get("content", "")) for message in calls[1]
        )
        self.assertIn("Stop reason: context_exhausted", continuation)
        self.assertNotIn("not the context-window limit", continuation)
        self.assertTrue(
            any(title == "Continuing in a fresh segment" for _kind, title, _detail, _status in activities)
        )
        self.assertFalse(
            any(title == "Continuing after output limit" for _kind, title, _detail, _status in activities)
        )

    def test_progress_guard_stops_repeated_tools_without_another_segment(self) -> None:
        source = self.project_root / "main.py"
        source.write_text("answer = 42\n", encoding="utf-8")
        calls = {"count": 0}

        def repeat_read(model, settings, *, messages, context_window, output_tokens,
                        tools=None, on_chunk=None, cancel=None, **_kwargs):
            calls["count"] += 1
            return ProviderChatResult(
                "",
                tool_calls=[
                    ProviderToolCall(
                        id=f"read-{calls['count']}",
                        name="read_file",
                        arguments={"path": "main.py"},
                    )
                ],
                prompt_tokens=500,
                eval_tokens=8,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_tool_rounds", 16)
        self.database.set_setting("max_continuation_segments", 2)
        notices: list[tuple[str, str, str]] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", repeat_read),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Inspect main.py.",
                AgentCallbacks(
                    notice=lambda level, title, body: notices.append((level, title, body))
                ),
            )

        self.assertEqual(calls["count"], 3)
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(assistant.metadata["done_reason"], "stalled")
        self.assertEqual(assistant.metadata["segments_used"], 1)
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.failures, [])
        self.assertEqual(
            checkpoint.completed_work,
            "No project changes were completed before the turn stopped.",
        )
        self.assertTrue(any("repeating tools" in title for _level, title, _body in notices))

    def test_verification_guard_spans_edits_and_stops_repeated_failures(self) -> None:
        calls = {"count": 0}
        failing_command = 'python3 -c "raise SystemExit(1)"'

        def edit_then_fail(model, settings, *, messages, context_window,
                           output_tokens, tools=None, on_chunk=None,
                           cancel=None, **_kwargs):
            calls["count"] += 1
            call_number = calls["count"]
            if call_number in {1, 3, 5}:
                version = (call_number + 1) // 2
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id=f"write-{version}",
                            name="write_file",
                            arguments={
                                "path": "main.py",
                                "content": f"answer = {version}\n",
                            },
                        )
                    ],
                    prompt_tokens=500,
                    eval_tokens=8,
                    done_reason="tool_calls",
                    effective_context=context_window,
                )
            return ProviderChatResult(
                "",
                tool_calls=[
                    ProviderToolCall(
                        id=f"verify-{call_number // 2}",
                        name="run_command",
                        arguments={"command": failing_command},
                    )
                ],
                prompt_tokens=500,
                eval_tokens=8,
                done_reason="tool_calls",
                effective_context=context_window,
            )

        self.database.set_setting("max_tool_rounds", 16)
        self.database.set_setting("max_continuation_segments", 4)
        notices: list[tuple[str, str, str]] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", edit_then_fail),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Repair main.py until its focused verification passes.",
                AgentCallbacks(
                    approval=lambda _name, _body: True,
                    notice=lambda level, title, body: notices.append(
                        (level, title, body)
                    ),
                ),
            )

        self.assertEqual(calls["count"], 6)
        self.assertEqual(
            (self.project_root / "main.py").read_text(encoding="utf-8"),
            "answer = 3\n",
        )
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(assistant.metadata["done_reason"], "verification_loop")
        self.assertEqual(assistant.metadata["segments_used"], 1)
        self.assertEqual(assistant.metadata["commands"], [failing_command] * 3)
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.status, "needs-continuation")
        self.assertIn("do not repeat", checkpoint.next_step.casefold())
        activities = self.database.list_activities(self.chat.id)
        self.assertEqual(
            sum(
                activity.title == "Verification guard stopped the loop"
                for activity in activities
            ),
            1,
        )
        self.assertFalse(
            any(activity.title.startswith("Continuing") for activity in activities)
        )
        self.assertTrue(
            any(
                title == "Agent stopped after repeated verification failures"
                for _level, title, _body in notices
            )
        )

    def test_progress_guard_allows_distinct_silent_verification_commands(self) -> None:
        calls = {"count": 0}
        commands = [f"true verification-{index}" for index in range(1, 6)]

        def edit_then_verify(model, settings, *, messages, context_window,
                             output_tokens, tools=None, on_chunk=None,
                             cancel=None, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id="write-1",
                            name="write_file",
                            arguments={"path": "main.py", "content": "answer = 42\n"},
                        )
                    ],
                    prompt_tokens=500,
                    eval_tokens=8,
                    done_reason="stop",
                    effective_context=context_window,
                )
            command_index = calls["count"] - 2
            if command_index < len(commands):
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id=f"command-{command_index + 1}",
                            name="run_command",
                            arguments={"command": commands[command_index]},
                        )
                    ],
                    prompt_tokens=500,
                    eval_tokens=8,
                    done_reason="stop",
                    effective_context=context_window,
                )
            content = "Created main.py and completed the verification commands."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=600,
                eval_tokens=10,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_tool_rounds", 16)
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", edit_then_verify),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Create main.py and verify it.",
                AgentCallbacks(approval=lambda _name, _body: True),
            )

        self.assertEqual(calls["count"], 7)
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(assistant.metadata["done_reason"], "stop")
        self.assertEqual(assistant.metadata["commands"], commands)
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.status, "complete")
        self.assertFalse(
            any(
                activity.title == "Progress guard stopped the loop"
                for activity in self.database.list_activities(self.chat.id)
            )
        )

    def test_stalled_changed_turn_does_not_refresh_agents_file(self) -> None:
        calls = {"count": 0}

        def change_then_repeat(model, settings, *, messages, context_window,
                               output_tokens, tools=None, on_chunk=None,
                               cancel=None, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id="write-1",
                            name="write_file",
                            arguments={"path": "main.py", "content": "answer = 42\n"},
                        )
                    ],
                    prompt_tokens=500,
                    eval_tokens=8,
                    done_reason="stop",
                    effective_context=context_window,
                )
            return ProviderChatResult(
                "",
                tool_calls=[
                    ProviderToolCall(
                        id=f"read-{calls['count']}",
                        name="read_file",
                        arguments={"path": "main.py"},
                    )
                ],
                prompt_tokens=500,
                eval_tokens=8,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_tool_rounds", 16)
        manager = AgentsFileManager(self.project_root)
        manager.ensure()
        original_agents = manager.path.read_text(encoding="utf-8")
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", change_then_repeat),
            patch("localcode.agent.run_complete") as automatic_update,
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Create main.py and verify it.",
                AgentCallbacks(),
            )

        assistant = self.database.list_messages(self.chat.id)[-1]
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(assistant.metadata["done_reason"], "stalled")
        self.assertEqual(checkpoint.status, "needs-continuation")
        self.assertTrue((self.project_root / "main.py").exists())
        self.assertFalse(automatic_update.called)
        self.assertEqual(manager.path.read_text(encoding="utf-8"), original_agents)

    def test_project_context_ignores_incomplete_cross_chat_memory(self) -> None:
        incomplete = self.database.create_chat(self.project.id, "Incomplete dashboard")
        failed = self.database.create_chat(self.project.id, "Failed dashboard")
        fabricated = self.database.create_chat(self.project.id, "Fabricated dashboard")
        complete = self.database.create_chat(self.project.id, "Completed dashboard")
        self.database.remember_project_memory(
            self.project.id,
            incomplete.id,
            incomplete.title,
            "Objective: Fix dashboard\nStatus: needs-continuation\nOutcome: stale failure",
        )
        self.database.remember_project_memory(
            self.project.id,
            failed.id,
            failed.title,
            "Objective: Fix dashboard\nStatus: complete\nOutcome: misleading\n"
            "Failures: edit_file failed",
        )
        self.database.remember_project_memory(
            self.project.id,
            fabricated.id,
            fabricated.title,
            "Objective: Fix dashboard\nStatus: complete\n"
            "Outcome: Changed files: dashboard.py\nBoth checks passed.\n"
            "Changed files: (none)\nCommands: (none)\nFailures: (none)",
        )
        self.database.remember_project_memory(
            self.project.id,
            complete.id,
            complete.title,
            "Objective: Fix dashboard\nStatus: complete\nOutcome: added sensor chart",
        )
        runner = AgentRunner(self.database, AppSettings(self.database))

        context = runner._project_memory_context(
            self.project, self.chat, "dashboard sensor chart"
        )

        self.assertIn("added sensor chart", context)
        self.assertNotIn("stale failure", context)
        self.assertNotIn("misleading", context)
        self.assertNotIn("Both checks passed", context)

    def test_direct_agents_edit_skips_redundant_automatic_rewrite(self) -> None:
        manager = AgentsFileManager(self.project_root)
        manager.ensure()
        original = manager.path.read_text(encoding="utf-8")
        updated = original.replace("## Working Agreement", "## Working Agreement\n\nProject-specific.")
        calls = {"count": 0}

        def edit_agents(model, settings, *, messages, context_window, output_tokens,
                        tools=None, on_chunk=None, cancel=None, **_kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id="agents-edit",
                            name="write_file",
                            arguments={"path": "AGENTS.md", "content": updated},
                        )
                    ],
                    prompt_tokens=500,
                    eval_tokens=8,
                    done_reason="stop",
                    effective_context=context_window,
                )
            content = "Updated only the managed AGENTS.md section."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=600,
                eval_tokens=10,
                done_reason="stop",
                effective_context=context_window,
            )

        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", edit_agents),
            patch("localcode.agent.run_complete") as automatic_update,
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Update only AGENTS.md.",
                AgentCallbacks(error=errors.append),
            )

        self.assertEqual(errors, [])
        self.assertFalse(automatic_update.called)
        self.assertIn("Project-specific.", manager.path.read_text(encoding="utf-8"))

    def test_malformed_ollama_tool_call_retries_in_fresh_segment(self) -> None:
        calls: list[list[dict]] = []

        def malformed_then_complete(model, settings, *, messages, context_window,
                                    output_tokens, tools=None, on_chunk=None,
                                    cancel=None, **_kwargs):
            calls.append(messages)
            if len(calls) == 1:
                raise RuntimeError(
                    'llama server returned invalid tool call arguments for "edit_file": '
                    "unexpected end of JSON input"
                )
            content = "Recovered and completed the smaller edit."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=500,
                eval_tokens=9,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_continuation_segments", 2)
        errors: list[str] = []
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", malformed_then_complete),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Make the requested edits.",
                AgentCallbacks(error=errors.append),
            )

        self.assertEqual(errors, [])
        self.assertEqual(len(calls), 2)
        retry_context = "\n".join(
            str(message.get("content", "")) for message in calls[1]
        )
        self.assertIn("invalid tool call arguments", retry_context)
        self.assertIn("use multiple", retry_context.casefold())
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.status, "complete")
        self.assertTrue(
            any("invalid tool call" in failure for failure in checkpoint.failures)
        )
        malformed_activities = [
            activity
            for activity in self.database.list_activities(self.chat.id)
            if activity.title == "Malformed tool call"
        ]
        self.assertEqual(len(malformed_activities), 1)
        self.assertEqual(malformed_activities[0].status, "error")

    def test_completion_claim_patterns_cover_pass_15c_failure_wording(self) -> None:
        content = (
            "**Changes made to `main.py`:**\n"
            "- Removed the unused import.\n"
            "- Replaced lines 37-57 with the corrected control flow.\n"
            "The fix has been applied correctly."
        )

        problem = AgentRunner._completion_claim_problem(
            content,
            has_changed_files=False,
            has_commands=False,
            mutation_expected=True,
        )

        self.assertIn("claimed project changes", problem)
        self.assertEqual(
            AgentRunner._completion_claim_problem(
                content,
                has_changed_files=False,
                has_commands=False,
                mutation_expected=False,
            ),
            "",
        )

    def test_completion_requires_an_explicitly_requested_mutating_tool_call(self) -> None:
        def fabricate_fix(model, settings, *, messages, context_window,
                          output_tokens, tools=None, on_chunk=None,
                          cancel=None, **_kwargs):
            content = (
                "Changes made to `main.py`:\n"
                "- Replaced the shutdown block.\n"
                "The fix has been applied correctly."
            )
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=500,
                eval_tokens=20,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_continuation_segments", 1)
        discarded = {"count": 0}
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", fabricate_fix),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Repair main.py using replace_lines.",
                AgentCallbacks(
                    discard=lambda: discarded.__setitem__(
                        "count", discarded["count"] + 1
                    )
                ),
            )

        self.assertEqual(discarded["count"], 1)
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(
            assistant.metadata["done_reason"], "unverified_completion"
        )
        self.assertEqual(assistant.metadata["changed_files"], [])
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.status, "needs-continuation")
        rejected = [
            activity
            for activity in self.database.list_activities(self.chat.id)
            if activity.title == "Unverified completion rejected"
        ]
        self.assertEqual(len(rejected), 1)
        self.assertIn("replace_lines", rejected[0].detail)
        self.assertIn("did not call", rejected[0].detail)

    def test_unverified_completion_retries_and_requires_real_tool_evidence(self) -> None:
        calls: list[list[dict]] = []

        def fabricate_then_write(model, settings, *, messages, context_window,
                                 output_tokens, tools=None, on_chunk=None,
                                 cancel=None, **_kwargs):
            calls.append(messages)
            if len(calls) == 1:
                content = (
                    "Changed files: `main.py`\n"
                    "Commands run: `python3 -m py_compile main.py` — passed.\n"
                    "Both checks pass."
                )
                if on_chunk:
                    on_chunk(content)
                return ProviderChatResult(
                    content,
                    prompt_tokens=500,
                    eval_tokens=20,
                    done_reason="stop",
                    effective_context=context_window,
                )
            if len(calls) == 2:
                return ProviderChatResult(
                    "",
                    tool_calls=[
                        ProviderToolCall(
                            id="write-verified",
                            name="write_file",
                            arguments={"path": "main.py", "content": "answer = 42\n"},
                        )
                    ],
                    prompt_tokens=500,
                    eval_tokens=8,
                    done_reason="stop",
                    effective_context=context_window,
                )
            content = "Created `main.py` using the completed file tool."
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=600,
                eval_tokens=12,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_continuation_segments", 2)
        errors: list[str] = []
        discarded = {"count": 0}
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", fabricate_then_write),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Create main.py and verify it.",
                AgentCallbacks(
                    error=errors.append,
                    discard=lambda: discarded.__setitem__(
                        "count", discarded["count"] + 1
                    ),
                ),
            )

        self.assertEqual(errors, [])
        self.assertEqual(discarded["count"], 1)
        self.assertEqual(len(calls), 3)
        retry_context = "\n".join(
            str(message.get("content", "")) for message in calls[1]
        )
        self.assertIn("previous response claimed", retry_context)
        self.assertEqual(
            (self.project_root / "main.py").read_text(encoding="utf-8"),
            "answer = 42\n",
        )
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(assistant.metadata["done_reason"], "stop")
        self.assertEqual(assistant.metadata["segments_used"], 2)
        self.assertNotIn("Commands run:", assistant.content)
        self.assertTrue(
            any(
                failure.startswith("unverified completion:")
                for failure in assistant.metadata["failures"]
            )
        )
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.status, "complete")
        self.assertTrue(checkpoint.completed_work.startswith("Verified actions:"))
        rejected = [
            activity
            for activity in self.database.list_activities(self.chat.id)
            if activity.title == "Unverified completion rejected"
        ]
        self.assertEqual(len(rejected), 1)

    def test_repeated_unverified_completion_is_not_accepted(self) -> None:
        calls = {"count": 0}

        def always_fabricate(model, settings, *, messages, context_window,
                             output_tokens, tools=None, on_chunk=None,
                             cancel=None, **_kwargs):
            calls["count"] += 1
            content = (
                "Changed files: `gui.py`\n"
                "Commands run: `python3 -m py_compile gui.py` — passed.\n"
                "Both checks passed successfully."
            )
            if on_chunk:
                on_chunk(content)
            return ProviderChatResult(
                content,
                prompt_tokens=500,
                eval_tokens=20,
                done_reason="stop",
                effective_context=context_window,
            )

        self.database.set_setting("max_continuation_segments", 2)
        notices: list[tuple[str, str, str]] = []
        discarded = {"count": 0}
        runner = AgentRunner(self.database, AppSettings(self.database))
        with (
            patch("localcode.agent.run_chat", always_fabricate),
            patch("localcode.agent.run_complete", fake_run_complete),
            patch("localcode.agent.show_model_info", fake_show_model_info),
        ):
            runner.run_turn(
                self.chat.id,
                "Remove the unused imports from gui.py.",
                AgentCallbacks(
                    notice=lambda level, title, body: notices.append(
                        (level, title, body)
                    ),
                    discard=lambda: discarded.__setitem__(
                        "count", discarded["count"] + 1
                    ),
                ),
            )

        self.assertEqual(calls["count"], 2)
        self.assertEqual(discarded["count"], 2)
        self.assertFalse((self.project_root / "gui.py").exists())
        assistant = self.database.list_messages(self.chat.id)[-1]
        self.assertEqual(
            assistant.metadata["done_reason"], "unverified_completion"
        )
        self.assertEqual(assistant.metadata["changed_files"], [])
        self.assertEqual(assistant.metadata["commands"], [])
        self.assertNotIn("Changed files:", assistant.content)
        self.assertIn("could not verify", assistant.content)
        checkpoint = self.database.get_task_checkpoint(self.chat.id)
        self.assertEqual(checkpoint.status, "needs-continuation")
        self.assertEqual(
            checkpoint.completed_work,
            "No project changes were completed before the turn stopped.",
        )
        self.assertTrue(
            any(title == "Unverified completion" for _level, title, _body in notices)
        )
        other_chat = self.database.create_chat(self.project.id, "Other chat")
        other = self.database.get_chat(other_chat.id)
        self.assertEqual(
            runner._project_memory_context(
                self.project, other, "unused imports gui"
            ),
            "",
        )


if __name__ == "__main__":
    unittest.main()
