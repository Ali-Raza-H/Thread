"""Packaging, licence, and documentation gates for the v1 release."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shlex
import tomllib
import unittest
from pathlib import Path

from threadtrace import __version__
from threadtrace.capabilities import CAPABILITIES
from threadtrace.cli import build_parser, main
from threadtrace.config import application_directory
from threadtrace.test_evidence import CAPABILITY_TESTS

ROOT = Path(__file__).resolve().parents[1]
ROOT_DOCS = ("README.md", "USER_MANUAL.md")
PLACEHOLDERS = {
    "PROJECT": "example", "PATH": "sample", "NAME": "name", "ENTRYPOINT": "/",
    "SYMBOL": "app.core.run", "VARIABLE": "value", "MODULE": "app", "CLASS": "app.Service",
    "TERM": "run", "TARGET": "app.core.run", "KIND": "file", "LEVEL": "info",
    "RULE": "rule", "N": "2", "PID": "123", "TRACE_ID": "trace", "RUN_ID": "run",
    "SCAN_A": "scan-a", "SCAN_B": "scan-b", "LABEL": "label", "EVENT_NUMBER": "1",
    "SESSION_ID": "session", "SHA256": "0" * 64, "RESOURCE_KIND": "database",
    "REPORT_KIND": "overview", "TEXT": "text", "COMMAND": "python", "ARGUMENT": "arg",
}
NUMERIC_OPTIONS = {
    "--depth", "--timeout", "--output-cap", "--capture-limit", "--retention-days",
    "--event-cap", "--port",
}


def _balanced(text: str) -> bool:
    return text.count("'") % 2 == 0 and text.count('"') % 2 == 0


def _commands(text: str) -> list[str]:
    """Return the shell commands written in ```console blocks."""
    commands: list[str] = []
    current = ""
    in_console = False
    for line in text.splitlines():
        if line.startswith("```"):
            if in_console and current:
                commands.append(current)
            current = ""
            in_console = line.strip() == "```console"
            continue
        if not in_console:
            continue
        if not current and not line.lstrip().startswith("threadtrace "):
            continue
        current = f"{current} {line.strip()}".strip()
        if current.endswith("\\"):
            current = current[:-1].strip()
        elif _balanced(current):
            commands.append(current)
            current = ""
    return commands


def _argv(command: str) -> list[str]:
    """Turn a documented invocation into concrete argv tokens."""
    command = re.sub(r"\s+>\s+\S+", "", command)
    command = re.sub(r"\[\s*--[^\]]*\]", "", command)
    command = re.sub(r"\[\s*[A-Z][A-Z_ ]*(?:\.\.\.)?\s*\]", "", command)
    tokens = shlex.split(command)
    assert tokens[0] == "threadtrace", command
    result: list[str] = []
    previous: str | None = None
    for token in tokens[1:]:
        token = token.split("|")[0]
        token = PLACEHOLDERS.get(token, token)
        if re.fullmatch(r"[A-Z][A-Z_]+", token):
            token = "value"
        if previous in NUMERIC_OPTIONS and not token.isdigit():
            token = "2"
        if previous == "--kind":
            token = {"entrypoints": "route", "resources": "file"}.get(tokens[1], "function")
        if previous == "--severity":
            token = "high"
        if previous == "--confidence":
            token = "possible"
        if previous == "--to":
            token = "database"
        result.append(token)
        previous = token if token.startswith("-") else None
    return result


class PackagingTests(unittest.TestCase):
    def test_documented_commands_parse(self):
        parser = build_parser()
        for name in ROOT_DOCS:
            commands = _commands((ROOT / name).read_text())
            self.assertTrue(commands, name)
            for command in commands:
                with self.subTest(source=name, command=command):
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                        try:
                            parser.parse_args(_argv(command))
                        except SystemExit as exc:
                            self.assertEqual(exc.code, 0, output.getvalue())

    def test_manual_only_names_registered_commands(self):
        manual = (ROOT / "USER_MANUAL.md").read_text()
        parser = build_parser()
        for command in sorted(set(re.findall(r"`threadtrace ([a-z][a-z-]+)", manual))):
            with self.subTest(command=command):
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as caught:
                        parser.parse_args([command, "--help"])
                self.assertEqual(caught.exception.code, 0)

    def test_documented_entrypoint_kinds_are_filterable(self):
        manual = (ROOT / "USER_MANUAL.md").read_text()
        sentence = re.search(r"Entry-point kinds include (.*?)\.", manual, re.S)
        self.assertIsNotNone(sentence)
        documented = set(re.findall(r"`([a-z]+)`", sentence.group(1)))
        parser = build_parser()
        commands = next(action for action in parser._actions
                        if isinstance(action, argparse._SubParsersAction)).choices
        kinds = next(action for action in commands["entrypoints"]._actions
                     if "--kind" in action.option_strings).choices
        self.assertLessEqual(documented, set(kinds))

    def test_version_output_matches_package_metadata(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as caught:
                main(["--version"])
        self.assertEqual(caught.exception.code, 0)
        self.assertEqual(output.getvalue().strip(), f"threadtrace {__version__}")

    def test_package_metadata_matches_the_verified_product(self):
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["name"], "threadtrace")
        self.assertEqual(project["version"], __version__)
        self.assertEqual(project["license"], "GPL-3.0-only")
        self.assertEqual(project["license-files"], ["LICENSE"])
        self.assertIn(">=3.11", project["requires-python"])
        self.assertEqual(
            [item for item in project["classifiers"] if "Python :: 3." in item],
            ["Programming Language :: Python :: 3.11"],
        )
        dev = json.loads((ROOT / ".dev.json").read_text())
        self.assertEqual(dev["name"], "ThreadTrace")
        self.assertEqual(dev["display_name"], "ThreadTrace")
        self.assertIn("[dev]", dev["commands"]["install"])

    def test_licence_is_complete_gpl_3_only(self):
        licence = (ROOT / "LICENSE").read_text()
        self.assertIn("GNU GENERAL PUBLIC LICENSE", licence)
        self.assertIn("Version 3, 29 June 2007", licence)
        self.assertIn("END OF TERMS AND CONDITIONS", licence)
        self.assertGreater(len(licence.splitlines()), 600)

    def test_documentation_uses_one_version_and_three_modes(self):
        for name in (*ROOT_DOCS, "CHANGELOG.md"):
            with self.subTest(source=name):
                self.assertIn(__version__, (ROOT / name).read_text())
        for name in ROOT_DOCS:
            text = (ROOT / name).read_text()
            self.assertIn("static, dynamic, and Hybrid", text)
            self.assertIn("Only static scans never import or run the target", text)

    def test_documented_data_directory_matches_the_application(self):
        manual = (ROOT / "USER_MANUAL.md").read_text()
        self.assertEqual(application_directory().name, "Thread")
        self.assertIn("`~/.config/Thread/thread.sqlite3`", manual)
        self.assertIn("`config.yaml`", manual)

    def test_capability_audit_matches_the_capability_manifest(self):
        audit = (ROOT / "ThreadTrace_Planned_Documentation/Reference/Capability Audit.md").read_text()
        rows: dict[str, str] = {}
        for line in audit.splitlines():
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            if len(cells) == 3 and cells[1] in {"Full", "Partial", "Unsupported"}:
                rows[cells[0]] = cells[1]
        expected = {
            item["feature"]: item["status"]
            if item["status"] != "Full" or item.get("verified") else "Partial"
            for item in CAPABILITIES
        }
        self.assertEqual(rows, expected)
        for feature, status in expected.items():
            if status == "Full":
                for test in CAPABILITY_TESTS[feature]:
                    self.assertIn(test, audit)


if __name__ == "__main__":
    unittest.main()
