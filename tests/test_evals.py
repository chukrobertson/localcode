from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from localcode.evals import (
    SCENARIOS,
    SCENARIO_BY_NAME,
    EvalEvidence,
    EvalRecorder,
    EvalResult,
    build_report,
    main,
    score_scenario,
    write_report,
)


class EvalHarnessTests(unittest.TestCase):
    def test_scenarios_are_unique_and_commands_are_explicitly_whitelisted(self) -> None:
        self.assertEqual(len(SCENARIOS), len(SCENARIO_BY_NAME))
        self.assertEqual(
            {scenario.name for scenario in SCENARIOS},
            {"indentation_edit", "inspect_only", "read_only", "verified_edit"},
        )
        for scenario in SCENARIOS:
            self.assertIn(scenario.permission_mode, {"allow", "read-only"})
            if scenario.verification_argv:
                self.assertTrue(scenario.allowed_agent_commands)
            if scenario.permission_mode == "read-only":
                self.assertFalse(scenario.expect_agents_file)

    def test_approval_only_accepts_an_exact_scenario_command(self) -> None:
        scenario = SCENARIO_BY_NAME["verified_edit"]
        recorder = EvalRecorder(scenario, verbose=False, write_line=lambda _line: None)
        command = "python3 -m unittest -v test_calculator.py"

        self.assertTrue(
            recorder.approval(
                "run_command",
                "Shell commands are not sandboxed and can access files outside the project.\n"
                f"Run in .:\n{command}",
            )
        )
        self.assertFalse(
            recorder.approval(
                "run_command",
                "Shell commands are not sandboxed and can access files outside the project.\n"
                f"Run in .:\n{command} --failfast",
            )
        )
        self.assertFalse(
            recorder.approval(
                "run_command",
                "Shell commands are not sandboxed and can access files outside the project.\n"
                f"Run in .:\nprintf unsafe\\n{command}",
            )
        )
        self.assertFalse(recorder.approval("web_fetch", command))

    def test_verified_edit_scoring_uses_structured_evidence(self) -> None:
        scenario = SCENARIO_BY_NAME["verified_edit"]
        evidence = EvalEvidence(
            actual_changed_files=["calculator.py"],
            checkpoint_status="complete",
            checkpoint_changed_files=["calculator.py"],
            checkpoint_commands=["python3 -m unittest -v test_calculator.py"],
            tool_calls=["read_file", "replace_lines", "run_command"],
            final_response="Fixed and verified.",
            verification_exit_code=0,
            verification_output="OK",
            agents_file_exists=True,
            content_requirements={
                "calculator.py contains 'return left + right'": True,
            },
        )

        checks = score_scenario(scenario, evidence)
        self.assertTrue(all(check.passed for check in checks))

        evidence.actual_changed_files.append("test_calculator.py")
        failed = score_scenario(scenario, evidence)
        self.assertFalse(all(check.passed for check in failed))
        self.assertFalse(
            next(check for check in failed if check.name == "no unexpected project changes").passed
        )

    def test_json_report_round_trip(self) -> None:
        evidence = EvalEvidence(
            checkpoint_status="complete",
            final_response="Inspected.",
            agents_file_exists=False,
        )
        result = EvalResult(
            scenario="inspect_only",
            description="Inspect safely",
            model="fake:latest",
            passed=True,
            duration_seconds=1.25,
            checks=[],
            evidence=evidence,
            events=[],
        )
        report = build_report("fake:latest", [result])

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "report.json"
            write_report(report, path)
            loaded = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(loaded["format_version"], 1)
        self.assertEqual(loaded["summary"]["passed"], 1)
        self.assertEqual(loaded["results"][0]["scenario"], "inspect_only")

    def test_list_mode_does_not_require_a_model(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            status = main(["--list"])

        self.assertEqual(status, 0)
        self.assertIn("verified_edit", output.getvalue())
        self.assertIn("read_only", output.getvalue())


if __name__ == "__main__":
    unittest.main()
