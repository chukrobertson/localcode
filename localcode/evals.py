from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Iterator, Sequence

from .agent import AgentCallbacks, AgentRunner
from .database import Database
from .models import ContextReport, TaskCheckpoint
from .settings import AppSettings


IGNORED_SNAPSHOT_PARTS = frozenset({".git", "__pycache__"})
MUTATION_TOOLS = frozenset(
    {
        "copy_file",
        "create_directory",
        "delete_file",
        "edit_file",
        "rename_file",
        "replace_in_file",
        "replace_lines",
        "write_file",
    }
)


@dataclass(frozen=True, slots=True)
class EvalScenario:
    name: str
    description: str
    prompt: str
    permission_mode: str
    files: tuple[tuple[str, str], ...]
    required_changed_files: tuple[str, ...] = ()
    allowed_changed_files: tuple[str, ...] = ()
    allowed_agent_commands: tuple[str, ...] = ()
    verification_argv: tuple[str, ...] = ()
    required_tool_groups: tuple[tuple[str, ...], ...] = ()
    required_file_contents: tuple[tuple[str, str], ...] = ()
    expected_checkpoint_status: str = "complete"
    expect_agents_file: bool = True
    suite: str = "smoke"


SCENARIOS = (
    EvalScenario(
        name="verified_edit",
        description="Repair a focused defect and prove it with the requested test command.",
        prompt=(
            "Fix calculator.add so the existing test passes. Make the smallest scoped source "
            "edit; do not change the test. Run exactly "
            "`python3 -m unittest -v test_calculator.py`, then briefly summarize the verified "
            "result."
        ),
        permission_mode="allow",
        files=(
            (
                "calculator.py",
                "def add(left: int, right: int) -> int:\n"
                "    \"\"\"Return the sum of two integers.\"\"\"\n"
                "    return left - right\n",
            ),
            (
                "test_calculator.py",
                "import unittest\n\n"
                "from calculator import add\n\n\n"
                "class CalculatorTests(unittest.TestCase):\n"
                "    def test_adds_positive_integers(self):\n"
                "        self.assertEqual(add(2, 3), 5)\n\n\n"
                "if __name__ == \"__main__\":\n"
                "    unittest.main()\n",
            ),
        ),
        required_changed_files=("calculator.py",),
        allowed_changed_files=("calculator.py",),
        allowed_agent_commands=("python3 -m unittest -v test_calculator.py",),
        verification_argv=("python3", "-m", "unittest", "-v", "test_calculator.py"),
        required_tool_groups=(
            tuple(sorted(MUTATION_TOOLS)),
            ("run_command",),
        ),
        required_file_contents=(("calculator.py", "return left + right"),),
    ),
    EvalScenario(
        name="indentation_edit",
        description="Recover a small exact edit when the model drifts on indentation width.",
        prompt="Update the default Ollama model to gemma4:12b and make no other changes.",
        permission_mode="allow",
        files=(
            (
                "src/lib/llm/providers.ts",
                "import type { LLMProvider } from '@/lib/domain/types'\n\n"
                "function getDefaultModel(provider: LLMProvider): string {\n"
                "  const defaults: Record<LLMProvider, string> = {\n"
                "    ollama: 'llama3.1',\n"
                "    openai: 'gpt-4o-mini',\n"
                "    anthropic: 'claude-3-haiku-20240307',\n"
                "  }\n"
                "  return defaults[provider]\n"
                "}\n",
            ),
        ),
        required_changed_files=("src/lib/llm/providers.ts",),
        allowed_changed_files=("src/lib/llm/providers.ts",),
        required_tool_groups=(tuple(sorted(MUTATION_TOOLS)),),
        required_file_contents=(
            ("src/lib/llm/providers.ts", "ollama: 'gemma4:12b'"),
        ),
    ),
    EvalScenario(
        name="inspect_only",
        description="Inspect a small module without changing it or running commands.",
        prompt=(
            "Inspect service.py and explain in one short paragraph what normalize_email does. "
            "Do not modify files and do not run shell commands."
        ),
        permission_mode="read-only",
        files=(
            (
                "service.py",
                "def normalize_email(value: str) -> str:\n"
                "    \"\"\"Return a comparison-safe email address.\"\"\"\n"
                "    return value.strip().casefold()\n",
            ),
        ),
        required_tool_groups=(("read_file", "read_files", "search_files"),),
        expect_agents_file=False,
    ),
    EvalScenario(
        name="read_only",
        description="Verify that an edit request cannot mutate a read-only project.",
        prompt=(
            "Change config.py so MODE is 'production'. This project is read-only; explain the "
            "blocker if you cannot make the requested change."
        ),
        permission_mode="read-only",
        files=(("config.py", "MODE = \"development\"\n"),),
        expect_agents_file=False,
    ),
)

SCENARIO_BY_NAME = {scenario.name: scenario for scenario in SCENARIOS}


@dataclass(slots=True)
class EvalCheck:
    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class EvalEvidence:
    callback_errors: list[str] = field(default_factory=list)
    actual_changed_files: list[str] = field(default_factory=list)
    checkpoint_status: str = "missing"
    checkpoint_changed_files: list[str] = field(default_factory=list)
    checkpoint_commands: list[str] = field(default_factory=list)
    checkpoint_failures: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    done_reason: str = ""
    segments_used: int = 0
    prompt_tokens: int = 0
    eval_tokens: int = 0
    streamed_chunks: int = 0
    streamed_characters: int = 0
    final_response: str = ""
    verification_exit_code: int | None = None
    verification_output: str = ""
    agents_file_exists: bool = False
    content_requirements: dict[str, bool] = field(default_factory=dict)


@dataclass(slots=True)
class EvalResult:
    scenario: str
    description: str
    model: str
    passed: bool
    duration_seconds: float
    checks: list[EvalCheck]
    evidence: EvalEvidence
    events: list[dict[str, object]]
    workdir: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class EvalRecorder:
    def __init__(
        self,
        scenario: EvalScenario,
        *,
        verbose: bool,
        write_line: Callable[[str], None],
    ) -> None:
        self.scenario = scenario
        self.verbose = verbose
        self.write_line = write_line
        self.events: list[dict[str, object]] = []
        self.errors: list[str] = []
        self.notices: list[tuple[str, str, str]] = []
        self.contexts: list[ContextReport] = []
        self.final_response = ""
        self.streamed_chunks = 0
        self.streamed_characters = 0

    def _event(self, kind: str, **values: object) -> None:
        self.events.append({"kind": kind, **values})

    def phase(self, value: str) -> None:
        self._event("phase", value=value)
        if self.verbose:
            self.write_line(f"    phase: {value}")

    def chunk(self, value: str) -> None:
        self.streamed_chunks += 1
        self.streamed_characters += len(value)

    def activity(self, kind: str, title: str, detail: str, status: str) -> None:
        self._event(
            "activity",
            activity_kind=kind,
            title=title,
            detail=detail,
            status=status,
        )
        if self.verbose and status != "running":
            summary = " ".join(detail.split())[:160]
            self.write_line(f"    {status}: {title} — {summary}")

    def context(self, report: ContextReport) -> None:
        self.contexts.append(report)
        self._event(
            "context",
            used=report.used,
            limit=report.limit,
            estimated=report.estimated,
            state=report.state,
            reason=report.reason,
        )

    def notice(self, level: str, title: str, body: str) -> None:
        self.notices.append((level, title, body))
        self._event("notice", level=level, title=title, body=body)

    def discard(self) -> None:
        self._event("discard")

    def complete(self, value: str) -> None:
        self.final_response = value
        self._event(
            "complete",
            characters=len(value),
            streamed_chunks=self.streamed_chunks,
            streamed_characters=self.streamed_characters,
        )

    def error(self, value: str) -> None:
        self.errors.append(value)
        self._event("error", value=value)

    def approval(self, tool: str, detail: str) -> bool:
        command = _command_from_approval_detail(tool, detail)
        approved = tool in {"run_command", "run_lint"} and any(
            detail == _expected_approval_detail(tool, allowed)
            for allowed in self.scenario.allowed_agent_commands
        )
        self._event("approval", tool=tool, command=command, approved=approved)
        if self.verbose:
            decision = "approved" if approved else "denied"
            self.write_line(f"    approval: {tool} {decision} — {command or '(no command)'}")
        return approved

    def ask_user(self, question: str, detail: str) -> str:
        answer = "Use the exact task requirements and do not broaden the requested scope."
        self._event("ask_user", question=question, detail=detail, answer=answer)
        return answer

    def callbacks(self) -> AgentCallbacks:
        return AgentCallbacks(
            phase=self.phase,
            chunk=self.chunk,
            activity=self.activity,
            context=self.context,
            notice=self.notice,
            discard=self.discard,
            complete=self.complete,
            error=self.error,
            approval=self.approval,
            ask_user=self.ask_user,
        )


def _expected_approval_detail(tool: str, command: str) -> str:
    if tool == "run_command":
        return (
            "Shell commands are not sandboxed and can access files outside the project.\n"
            f"Run in .:\n{command}"
        )
    if tool == "run_lint":
        return f"Run project check: {command}"
    return ""


def _command_from_approval_detail(tool: str, detail: str) -> str:
    if tool == "run_command":
        prefix = (
            "Shell commands are not sandboxed and can access files outside the project.\n"
            "Run in .:\n"
        )
    elif tool == "run_lint":
        prefix = "Run project check: "
    else:
        return ""
    return detail.removeprefix(prefix) if detail.startswith(prefix) else ""


def _safe_model_name(model: str) -> str:
    rendered = re.sub(r"[^A-Za-z0-9_.-]+", "-", model).strip("-")
    return rendered or "model"


def _write_fixture(root: Path, scenario: EvalScenario) -> None:
    for relative, content in scenario.files:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _snapshot(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if any(part in IGNORED_SNAPSHOT_PARTS for part in relative.parts):
            continue
        if path.suffix.casefold() in {".pyc", ".pyo"} or relative.as_posix() == "AGENTS.md":
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result[relative.as_posix()] = digest
    return result


def _changed_files(before: dict[str, str], after: dict[str, str]) -> list[str]:
    return sorted(
        path
        for path in before.keys() | after.keys()
        if before.get(path) != after.get(path)
    )


@contextmanager
def _isolated_environment(root: Path) -> Iterator[None]:
    names = ("LOCALCODE_DATA_HOME", "LOCALCODE_CONFIG_HOME", "LOCALCODE_CACHE_HOME")
    old = {name: os.environ.get(name) for name in names}
    os.environ["LOCALCODE_DATA_HOME"] = str(root / "data")
    os.environ["LOCALCODE_CONFIG_HOME"] = str(root / "config")
    os.environ["LOCALCODE_CACHE_HOME"] = str(root / "cache")
    try:
        yield
    finally:
        for name, value in old.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _checkpoint_evidence(checkpoint: TaskCheckpoint | None, evidence: EvalEvidence) -> None:
    if checkpoint is None:
        return
    evidence.checkpoint_status = checkpoint.status
    evidence.checkpoint_changed_files = checkpoint.changed_files
    evidence.checkpoint_commands = checkpoint.commands
    evidence.checkpoint_failures = checkpoint.failures
    evidence.segments_used = checkpoint.segment_count


def score_scenario(scenario: EvalScenario, evidence: EvalEvidence) -> list[EvalCheck]:
    checks = [
        EvalCheck(
            "agent completed without runtime error",
            not evidence.callback_errors,
            "; ".join(evidence.callback_errors) or "no callback errors",
        ),
        EvalCheck(
            "checkpoint status",
            evidence.checkpoint_status == scenario.expected_checkpoint_status,
            f"expected {scenario.expected_checkpoint_status}, got {evidence.checkpoint_status}",
        ),
    ]

    actual = set(evidence.actual_changed_files)
    required = set(scenario.required_changed_files)
    allowed = set(scenario.allowed_changed_files)
    checks.append(
        EvalCheck(
            "required project changes",
            required.issubset(actual),
            f"required={sorted(required)}, actual={sorted(actual)}",
        )
    )
    checks.append(
        EvalCheck(
            "no unexpected project changes",
            actual.issubset(allowed),
            f"allowed={sorted(allowed)}, actual={sorted(actual)}",
        )
    )

    if required:
        recorded = set(evidence.checkpoint_changed_files)
        checks.append(
            EvalCheck(
                "checkpoint recorded required changes",
                required.issubset(recorded),
                f"required={sorted(required)}, recorded={sorted(recorded)}",
            )
        )

    for group in scenario.required_tool_groups:
        called = set(evidence.tool_calls)
        checks.append(
            EvalCheck(
                "required tool group: " + " | ".join(group),
                bool(called.intersection(group)),
                f"called={sorted(called)}",
            )
        )

    for command in scenario.allowed_agent_commands:
        checks.append(
            EvalCheck(
                f"command evidence: {command}",
                command in evidence.checkpoint_commands,
                f"recorded={evidence.checkpoint_commands}",
            )
        )

    if scenario.verification_argv:
        checks.append(
            EvalCheck(
                "independent verification",
                evidence.verification_exit_code == 0,
                evidence.verification_output or "verification did not run",
            )
        )

    for requirement, matched in evidence.content_requirements.items():
        checks.append(
            EvalCheck(
                f"required file content: {requirement}",
                matched,
                "matched" if matched else "missing",
            )
        )

    checks.append(
        EvalCheck(
            "AGENTS.md policy",
            evidence.agents_file_exists == scenario.expect_agents_file,
            (
                f"expected exists={scenario.expect_agents_file}, "
                f"got {evidence.agents_file_exists}"
            ),
        )
    )
    checks.append(
        EvalCheck(
            "assistant returned a response",
            bool(evidence.final_response.strip()),
            f"response characters={len(evidence.final_response)}",
        )
    )
    return checks


def run_scenario(
    scenario: EvalScenario,
    model: str,
    *,
    context_window: int = 48128,
    keep_workdir: bool = False,
    verbose: bool = False,
    write_line: Callable[[str], None] = print,
) -> EvalResult:
    temporary: tempfile.TemporaryDirectory[str] | None = None
    if keep_workdir:
        root = Path(tempfile.mkdtemp(prefix=f"localcode-eval-{scenario.name}-"))
    else:
        temporary = tempfile.TemporaryDirectory(prefix=f"localcode-eval-{scenario.name}-")
        root = Path(temporary.name)
    project_root = root / "project"
    project_root.mkdir()
    _write_fixture(project_root, scenario)
    before = _snapshot(project_root)
    recorder = EvalRecorder(scenario, verbose=verbose, write_line=write_line)
    started = time.monotonic()

    with _isolated_environment(root / "app"):
        database = Database(root / "localcode.db")
        project = database.add_project(
            project_root,
            model=model,
            context_window=context_window,
        )
        database.update_project(project.id, permission_mode=scenario.permission_mode)
        chat = database.create_chat(project.id)
        settings = AppSettings(database)
        settings.set("max_continuation_segments", 2)
        settings.set("max_tool_rounds", 16)
        runner = AgentRunner(database, settings)
        runner.run_turn(chat.id, scenario.prompt, recorder.callbacks())

        after = _snapshot(project_root)
        messages = database.list_messages(chat.id)
        assistant = next(
            (message for message in reversed(messages) if message.role == "assistant"),
            None,
        )
        activities = database.list_activities(chat.id)
        checkpoint = database.get_task_checkpoint(chat.id)

    evidence = EvalEvidence(
        callback_errors=list(recorder.errors),
        actual_changed_files=_changed_files(before, after),
        tool_calls=[item.title for item in activities if item.kind == "tool"],
        streamed_chunks=recorder.streamed_chunks,
        streamed_characters=recorder.streamed_characters,
        final_response=recorder.final_response,
        agents_file_exists=(project_root / "AGENTS.md").is_file(),
        content_requirements={
            f"{path} contains {text!r}": (
                (project_root / path).is_file()
                and text in (project_root / path).read_text(encoding="utf-8", errors="replace")
            )
            for path, text in scenario.required_file_contents
        },
    )
    _checkpoint_evidence(checkpoint, evidence)
    if assistant is not None:
        evidence.done_reason = str(assistant.metadata.get("done_reason") or "")
        evidence.segments_used = int(
            assistant.metadata.get("segments_used") or evidence.segments_used
        )
        evidence.prompt_tokens = int(assistant.metadata.get("prompt_tokens") or 0)
        evidence.eval_tokens = int(assistant.metadata.get("eval_tokens") or 0)
        if not evidence.final_response:
            evidence.final_response = assistant.content

    if scenario.verification_argv:
        verification = subprocess.run(
            scenario.verification_argv,
            cwd=project_root,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=60,
            check=False,
        )
        evidence.verification_exit_code = verification.returncode
        evidence.verification_output = (verification.stdout + verification.stderr).strip()[-4000:]

    checks = score_scenario(scenario, evidence)
    result = EvalResult(
        scenario=scenario.name,
        description=scenario.description,
        model=model,
        passed=all(check.passed for check in checks),
        duration_seconds=round(time.monotonic() - started, 3),
        checks=checks,
        evidence=evidence,
        events=recorder.events,
        workdir=str(root) if keep_workdir else "",
    )
    if temporary is not None:
        temporary.cleanup()
    return result


def build_report(model: str, results: Sequence[EvalResult]) -> dict[str, object]:
    passed = sum(result.passed for result in results)
    return {
        "format_version": 1,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "model": model,
        "summary": {
            "passed": passed,
            "failed": len(results) - passed,
            "total": len(results),
            "duration_seconds": round(sum(item.duration_seconds for item in results), 3),
        },
        "results": [result.to_dict() for result in results],
    }


def write_report(report: dict[str, object], path: Path) -> None:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
        handle.write(payload)
    temporary.replace(path)


def _default_report_path(model: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("eval-results") / f"{_safe_model_name(model)}-{stamp}.json"


def _print_summary(results: Sequence[EvalResult], write_line: Callable[[str], None]) -> None:
    scenario_width = max(13, *(len(result.scenario) for result in results))
    write_line("")
    write_line(f"{'Scenario':<{scenario_width}}  Result  Seconds  Status              Changes")
    write_line(
        f"{'-' * scenario_width}  ------  -------  ------------------  "
        "------------------------"
    )
    for result in results:
        evidence = result.evidence
        changes = ", ".join(evidence.actual_changed_files) or "(none)"
        write_line(
            f"{result.scenario:<{scenario_width}}  "
            f"{'PASS' if result.passed else 'FAIL':<6}  "
            f"{result.duration_seconds:>7.1f}  {evidence.checkpoint_status:<18}  {changes}"
        )
        if not result.passed:
            for check in result.checks:
                if not check.passed:
                    write_line(f"  - {check.name}: {check.detail}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run repeatable LocalCode agent evaluations in disposable projects."
    )
    parser.add_argument("--model", help="Installed Ollama model name, such as qwen3:8b")
    parser.add_argument(
        "--scenario",
        action="append",
        choices=sorted(SCENARIO_BY_NAME),
        help="Scenario to run; repeat the option to select more than one (default: all).",
    )
    parser.add_argument(
        "--context-window",
        type=int,
        default=48128,
        help="Maximum context window supplied to each disposable project (default: 48128).",
    )
    parser.add_argument("--output", type=Path, help="JSON report path")
    parser.add_argument(
        "--keep-workdirs",
        action="store_true",
        help="Keep disposable project directories and include their paths in the report.",
    )
    parser.add_argument("--verbose", action="store_true", help="Show phases and tool results")
    parser.add_argument("--list", action="store_true", help="List scenarios and exit")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if arguments.list:
        for scenario in SCENARIOS:
            print(f"{scenario.name:<13} [{scenario.suite}] {scenario.description}")
        return 0
    if not arguments.model:
        parser.error("--model is required unless --list is used")
    if arguments.context_window < 2048:
        parser.error("--context-window must be at least 2048")

    selected = arguments.scenario or [scenario.name for scenario in SCENARIOS]
    results: list[EvalResult] = []
    for name in selected:
        scenario = SCENARIO_BY_NAME[name]
        print(f"Running {scenario.name} with {arguments.model}...")
        results.append(
            run_scenario(
                scenario,
                arguments.model,
                context_window=arguments.context_window,
                keep_workdir=arguments.keep_workdirs,
                verbose=arguments.verbose,
            )
        )

    _print_summary(results, print)
    report = build_report(arguments.model, results)
    output = arguments.output or _default_report_path(arguments.model)
    write_report(report, output)
    print("")
    print(f"Report: {output.expanduser().resolve()}")
    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
