from __future__ import annotations

import errno
import json
import os
import select
import signal
import tempfile
import sys
import time
import unittest

if os.name == "posix":
    import fcntl
    import pty
    import struct
    import termios
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import MagicMock, patch

from threadtrace.cli import main
from threadtrace.engine import Engine
from threadtrace.query import QueryService
from threadtrace.store import Store


class EngineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "sample"
        self.root.mkdir()
        (self.root / "app").mkdir()
        (self.root / "app" / "__init__.py").write_text("")
        (self.root / "app" / "helpers.py").write_text(
            "def format_name(value: str) -> str:\n    return value.strip()\n"
        )
        (self.root / "app" / "main.py").write_text(
            "from .helpers import format_name\n\n"
            "def main(value: str = ' demo ') -> str:\n"
            "    return format_name(value)\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n"
        )
        (self.root / "app" / "api.py").write_text(
            "@app.get('/items')\n"
            "def list_items(limit: int = 10):\n"
            "    return limit\n"
        )
        (self.root / "app" / "resources.py").write_text(
            "import os\n\n"
            "def load_token():\n"
            "    return os.getenv('TOKEN')\n"
        )
        (self.root / "app" / "core.py").write_text(
            "from .helpers import format_name\n\n"
            "class Service:\n"
            "    category = 'demo'\n"
            "    def process(self, value):\n"
            "        cleaned = format_name(value)\n"
            "        self.last = cleaned\n"
            "        return cleaned\n"
        )
        (self.root / "app" / "cycle.py").write_text("from . import core\n")
        (self.root / "app" / "cycle_a.py").write_text("from . import cycle_b\n")
        (self.root / "app" / "cycle_b.py").write_text("from . import cycle_a\n")
        (self.root / "app" / "broken.py").write_text("def invalid(:\n    pass\n")
        (self.root / "app" / "side_effect.py").write_text(
            "from pathlib import Path\nPath('scan-should-not-execute').write_text('no')\n"
        )
        (self.root / "pyproject.toml").write_text("[project]\nname = 'sample'\nversion = '0'\ndependencies = ['requests>=2']\n[project.scripts]\nsample = 'app.main:main'\n")
        (self.root / "app" / "network.py").write_text("import requests\n")
        self.store = Store(Path(self.temp.name) / "thread.sqlite3")
        self.engine = Engine(self.store)
        self.project = self.engine.add_project(str(self.root), "sample")
        self.engine.scan("sample")
        self.queries = QueryService(self.engine)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def test_scans_symbols_calls_classes_and_flow(self) -> None:
        functions = self.queries.functions("sample")
        self.assertIn("app.core.Service.process", {item["qualified_name"] for item in functions})
        process = next(item for item in functions if item["qualified_name"] == "app.core.Service.process")
        self.assertGreaterEqual(process["line_end"], process["line_start"])
        classes = self.queries.classes("sample")
        service = next(item for item in classes if item["qualified_name"] == "app.core.Service")
        self.assertEqual(service["attributes"][0]["name"], "category")
        self.assertEqual(service["methods"][0]["name"], "process")
        flow = self.queries.flow("sample", "app.core.Service.process")
        self.assertTrue({"parameter", "assignment", "read", "return"}.issubset({item["event_kind"] for item in flow}))
        calls = self.queries.callees("sample", "app.core.Service.process")
        self.assertEqual(calls["calls"][0]["callee"], "app.helpers.format_name")

    def test_dependency_and_search(self) -> None:
        dependencies = self.queries.dependencies("sample", "app.core")
        self.assertEqual(dependencies["edges"][0]["to"], "app.helpers")
        symbols = self.queries.search("sample", "format_name")
        self.assertEqual(symbols[0]["qualified_name"], "app.helpers.format_name")

    def test_bad_source_side_effects_cycles_and_libraries_are_reported(self) -> None:
        findings = self.queries.diagnostics("sample")
        rules = {item["rule_id"] for item in findings}
        self.assertIn("TB-CYCLE-001", rules)
        self.assertIn("TB-ANALYSIS-001", rules)
        self.assertFalse((self.root / "scan-should-not-execute").exists())
        libraries = self.queries.libraries("sample")
        self.assertIn(("requests", "third_party"), {(item["name"], item["category"]) for item in libraries})
        requests = next(item for item in libraries if item["name"] == "requests" and item["category"] == "third_party")
        self.assertIn("HTTP requests", requests["description"])
        self.assertEqual(requests["usage_files"], ["app/network.py"])
        self.assertEqual(self.queries.explain("sample", "requests")["kind"], "library")

    def test_cli_json_output_is_valid(self) -> None:
        output = StringIO()
        database = str(Path(self.temp.name) / "thread.sqlite3")
        with redirect_stdout(output):
            code = main(["functions", "sample", "--database", database, "--format", "json"])
        self.assertEqual(code, 0)
        import json
        result = json.loads(output.getvalue())
        self.assertTrue(any(row["qualified_name"] == "app.helpers.format_name" for row in result))

    def test_project_show_text_is_a_readable_report(self) -> None:
        output = StringIO()
        database = str(Path(self.temp.name) / "thread.sqlite3")
        with redirect_stdout(output):
            code = main(["project", "show", "sample", "--database", database])
        self.assertEqual(code, 0)
        report = output.getvalue()
        self.assertIn("Project: sample", report)
        self.assertIn("Location", report)
        self.assertIn("Scans (", report)
        self.assertNotIn("summary_json=", report)

    def test_reconstruction_detects_entry_points_resources_and_data_flow(self) -> None:
        overview = self.queries.overview("sample")
        self.assertTrue(overview["components"])
        entries = self.queries.entrypoints("sample")
        self.assertIn(("console", "sample"), {(item["kind"], item["name"]) for item in entries})
        self.assertIn(("route", "GET /items"), {(item["kind"], item["name"]) for item in entries})
        resources = self.queries.resources("sample", "env")
        self.assertEqual(resources[0]["operation"], "read")
        data = self.queries.dataflow("sample", "app.main.main")
        self.assertIn(("value", "value"), {(item["source_name"], item["target_name"]) for item in data["edges"]})
        path = self.queries.path("sample", "sample")
        self.assertTrue(any(step["target_name"] == "app.helpers.format_name" for step in path["steps"]))

    def test_beginner_guide_walk_file_map_and_friendly_findings(self) -> None:
        guide = self.queries.guide("sample")
        self.assertEqual(guide["starting_points"][0]["kind"], "route")
        self.assertNotIn("test", {item["kind"] for item in guide["starting_points"]})
        walkthrough = self.queries.walk("sample", "sample")
        self.assertTrue(any("Calls app.helpers.format_name" in step["explanation"] for step in walkthrough["steps"]))
        linked = self.queries.file_map("sample", "sample")
        self.assertIn("app/main.py", {item["relative_path"] for item in linked["files"]})
        project_map = self.queries.file_map("sample")
        self.assertEqual(project_map["target"], "entire project")
        self.assertIn("app/helpers.py", {item["relative_path"] for item in project_map["files"]})
        findings = self.queries.findings("sample")
        self.assertFalse(any(item["rule_id"] == "TB-CALL-001" for item in findings))
        self.assertTrue(all(item["plain_explanation"] for item in findings))

    def test_file_and_code_graphs_retain_link_reasons(self) -> None:
        files = self.queries.file_graph("sample")
        self.assertIn("app/main.py", {node["relative_path"] for node in files["nodes"]})
        self.assertTrue(any(
            edge["source"] == "app.main" and edge["target"] == "app.helpers" and edge["kind"] == "direct_call"
            for edge in files["edges"]
        ))
        code = self.queries.code_graph("sample")
        by_name = {node["qualified_name"]: node["id"] for node in code["nodes"]}
        self.assertTrue(any(
            edge["source"] == by_name["app.core.Service"]
            and edge["target"] == by_name["app.core.Service.process"]
            and edge["kind"] == "contains"
            for edge in code["edges"]
        ))
        self.assertTrue(any(
            edge["source"] == by_name["app.main.main"]
            and edge["target"] == by_name["app.helpers.format_name"]
            and edge["kind"] == "direct_call"
            for edge in code["edges"]
        ))

    def test_architecture_export_and_module_exit_codes(self) -> None:
        architecture = self.queries.architecture("sample")
        self.assertTrue(architecture["components"])
        from threadtrace.cli import _graph_export
        self.assertIn("flowchart LR", _graph_export(architecture, "mermaid"))
        self.assertIn("digraph thread", _graph_export(architecture, "dot"))

    def test_cli_reconstruction_output_and_scan_comparison(self) -> None:
        output = StringIO()
        database = str(Path(self.temp.name) / "thread.sqlite3")
        with redirect_stdout(output):
            code = main(["overview", "sample", "--database", database, "--format", "json"])
        self.assertEqual(code, 0)
        self.assertIn("entry_points", __import__("json").loads(output.getvalue()))
        first = self.engine.latest_scan("sample")["scan_id"]
        (self.root / "pyproject.toml").write_text(
            "[project]\nname = 'sample'\nversion = '0'\n[project.scripts]\n"
            "sample = 'app.main:main'\nsecond = 'app.main:main'\n"
        )
        second = self.engine.scan("sample")["scan_id"]
        comparison = self.queries.compare("sample", first, second)
        self.assertIn("console:second", comparison["changes"]["entry_points"]["added"])

    def test_incremental_scan_reuses_unchanged_parse_facts(self) -> None:
        scan = self.engine.scan("sample")
        summary = __import__("json").loads(scan["summary_json"])
        self.assertGreater(summary["reused_files"], 0)

    def test_explicit_runtime_trace_is_stored_separately(self) -> None:
        (self.root / "runner.py").write_text("def main():\n    return 'ok'\n\nmain()\n")
        result = self.engine.trace("sample", [sys.executable, "runner.py"], timeout=5, output_cap=1_000)
        self.assertEqual(result["exit_code"], 0)
        self.assertTrue(result["events"])
        traces = self.queries.traces("sample")
        self.assertEqual(traces[0]["trace_id"], result["trace_id"])

    def test_controlled_runtime_session_steps_and_summarises_values(self) -> None:
        from threadtrace.runtime.session import RuntimeSession

        (self.root / "runtime_runner.py").write_text(
            "def transform(value):\n"
            "    result = value + 1\n"
            "    return result\n\n"
            "answer = transform(4)\n"
            "print(answer)\n"
        )
        session = RuntimeSession(self.root, [sys.executable, "runtime_runner.py"], timeout=3, output_cap=10_000)
        try:
            session.step()
            for _ in range(40):
                session.poll()
                if session.events:
                    break
                time.sleep(0.025)
            self.assertTrue(session.events)
            self.assertTrue(all(event.get("protocol_version") == 1 for event in session.events))
            self.assertTrue(all("thread_id" in event and "call_depth" in event for event in session.events))
            session.continue_run()
            for _ in range(80):
                session.poll()
                if session.finished:
                    break
                time.sleep(0.025)
            result = session.result()
            self.assertTrue(session.finished)
            self.assertTrue(any(event["kind"] == "call" and event["name"] == "transform" for event in result["events"]))
            self.assertTrue(any(event.get("output", {}).get("value") == 5 for event in result["events"] if event["kind"] == "return"))
            self.assertIn("5", result["stdout"])
        finally:
            session.close()

    def test_controlled_runtime_session_preserves_environment_and_accepts_input(self) -> None:
        from threadtrace.runtime.session import RuntimeSession

        (self.root / "input_runner.py").write_text(
            "import os\n"
            "print('PROMPT>', flush=True)\n"
            "answer = input()\n"
            "print(f'ANSWER={answer}; VALUE={os.getenv(\"THREADTRACE_TEST_VALUE\")}', flush=True)\n"
        )
        session = RuntimeSession(
            self.root, [sys.executable, "input_runner.py"], timeout=3, output_cap=10_000,
            runtime_environment={"THREADTRACE_TEST_VALUE": "available"},
        )
        try:
            session.continue_run()
            for _ in range(200):
                if "PROMPT>" in session.result()["stdout"]:
                    break
                time.sleep(0.025)
            self.assertIn("PROMPT>", session.result()["stdout"])
            self.assertTrue(session.send_input("Ada"))
            for _ in range(200):
                if session.finished:
                    break
                time.sleep(0.025)
            self.assertTrue(session.finished)
            self.assertIn("ANSWER=Ada; VALUE=available", session.result()["stdout"])
        finally:
            session.close()

    def test_controlled_runtime_session_preserves_dotenv_caller_lookup(self) -> None:
        """A tracer must not make python-dotenv fall back to the project cwd."""
        from threadtrace.runtime.session import RuntimeSession

        dotenv = self.root / "dotenv"
        dotenv.mkdir()
        (dotenv / "__init__.py").write_text("from .main import load_dotenv\n")
        (dotenv / "main.py").write_text(
            "import os\n"
            "def find_dotenv(filename='.env', raise_error_if_not_found=False, usecwd=False):\n"
            "    return ''\n"
            "def load_dotenv():\n"
            "    path = find_dotenv()\n"
            "    if not path:\n"
            "        return False\n"
            "    for line in open(path, encoding='utf-8'):\n"
            "        key, value = line.strip().split('=', 1)\n"
            "        os.environ.setdefault(key, value)\n"
            "    return True\n"
        )
        backend = self.root / "backend"
        backend.mkdir()
        (backend / "__init__.py").write_text("")
        (backend / ".env").write_text("NESTED_VALUE=available\n")
        (backend / "settings.py").write_text(
            "import os\n"
            "from dotenv import load_dotenv\n"
            "load_dotenv()\n"
            "value = os.getenv('NESTED_VALUE')\n"
        )
        (self.root / "dotenv_runner.py").write_text(
            "from backend import settings\n"
            "print(settings.value, flush=True)\n"
        )
        session = RuntimeSession(
            self.root, [sys.executable, "dotenv_runner.py"], timeout=3, output_cap=10_000,
            # sitecustomize is imported before Python adds the script's
            # directory to sys.path; CIEL's real dotenv is installed in its
            # virtual environment and is therefore already importable here.
            runtime_environment={"PYTHONPATH": str(self.root)},
        )
        try:
            session.continue_run()
            for _ in range(80):
                if session.finished:
                    break
                time.sleep(0.025)
            self.assertTrue(session.finished)
            self.assertEqual(session.result()["stdout"].strip(), "available")
        finally:
            session.close()

    def test_runtime_breakpoint_pauses_a_continued_project_run(self) -> None:
        from threadtrace.runtime.session import RuntimeSession

        runner = self.root / "breakpoint_runner.py"
        runner.write_text(
            "def transform(value):\n"
            "    return value + 1\n\n"
            "answer = transform(4)\n"
            "print(answer)\n"
        )
        session = RuntimeSession(self.root, [sys.executable, runner.name], timeout=3, output_cap=10_000)
        try:
            self.assertTrue(session.toggle_breakpoint(runner, 1))
            self.assertIn((str(runner.resolve()), 1), session.breakpoints)
            session.continue_run()
            for _ in range(80):
                session.poll()
                if any(event["relative_path"] == runner.name and event["line"] == 1 for event in session.events):
                    break
                time.sleep(0.025)
            self.assertTrue(any(event["relative_path"] == runner.name and event["line"] == 1 for event in session.events))
            self.assertFalse(session.finished)
            self.assertFalse(session.toggle_breakpoint(runner, 1))
            session.continue_run()
            for _ in range(80):
                session.poll()
                if session.finished:
                    break
                time.sleep(0.025)
            self.assertTrue(session.finished)
            self.assertIn("5", session.result()["stdout"])
        finally:
            session.close()

    def test_process_discovery_lists_only_current_user_python_metadata(self) -> None:
        from threadtrace.processes import _redact_command, process_tree, public_process, running_python_processes

        processes = running_python_processes()
        current = next(item for item in processes if item["pid"] == __import__("os").getpid())
        self.assertIn("python", Path(current["executable"]).name.lower())
        self.assertTrue(current["command"])
        self.assertEqual(process_tree(processes, current["pid"])[0]["pid"], current["pid"])
        self.assertEqual(_redact_command(["tool", "--password", "value", "-AUTH_PASSWORD=also-secret"]), ["tool", "--password", "<redacted>", "-AUTH_PASSWORD=<redacted>"])
        self.assertNotIn("_restart_command", public_process(current))

    def test_runtime_host_explicitly_passes_the_interactive_terminal_to_child(self) -> None:
        from threadtrace.runtime.host import RuntimeHost

        sessions = self.root / "live-sessions"
        sessions.mkdir()
        stdin, stdout, stderr = (MagicMock(), MagicMock(), MagicMock())
        for stream in (stdin, stdout, stderr):
            stream.isatty.return_value = True
        process = MagicMock(pid=123)
        process.wait.return_value = 0
        with patch("threadtrace.runtime.host._sessions_root", return_value=sessions), \
             patch("threadtrace.runtime.host.sys.stdin", stdin), \
             patch("threadtrace.runtime.host.sys.stdout", stdout), \
             patch("threadtrace.runtime.host.sys.stderr", stderr), \
             patch("threadtrace.runtime.host.subprocess.Popen", return_value=process) as launch:
            host = RuntimeHost(self.root, "sample", [sys.executable, "-c", "pass"])
            self.assertEqual(host.run(), 0)
        self.assertEqual(launch.call_args.kwargs["stdin"], stdin)
        self.assertEqual(launch.call_args.kwargs["stdout"], stdout)
        self.assertEqual(launch.call_args.kwargs["stderr"], stderr)
        self.assertFalse(launch.call_args.kwargs["start_new_session"])

    def _run_host_in_terminal(self, script: str, marker: bytes, reply: bytes) -> tuple[bytes, list[dict]]:
        sessions = Path(self.temp.name) / "runtime"
        sessions.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = str(sessions)
        env["TERM"] = "xterm"
        source = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [source, env.get("PYTHONPATH", "")]))
        command = [sys.executable, "-m", "threadtrace.cli", "runtime-host", "sample", "--database",
                   str(Path(self.temp.name) / "thread.sqlite3"), "--", sys.executable, script]
        pid, master = pty.fork()
        if pid == 0:
            os.execve(sys.executable, command, env)
        output = bytearray()
        status = None
        sent = False
        try:
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    try:
                        output.extend(os.read(master, 65536))
                    except OSError as exc:
                        if exc.errno != errno.EIO:
                            raise
                if marker in output and not sent:
                    os.write(master, reply)
                    sent = True
                finished, child_status = os.waitpid(pid, os.WNOHANG)
                if finished:
                    status = child_status
                    break
            self.assertIsNotNone(status, f"Runtime Host timed out: {bytes(output)!r}")
            while select.select([master], [], [], 0)[0]:
                try:
                    output.extend(os.read(master, 65536))
                except OSError as exc:
                    if exc.errno != errno.EIO:
                        raise
                    break
            self.assertEqual(os.waitstatus_to_exitcode(status), 0, bytes(output).decode(errors="replace"))
            self.assertTrue(sent, bytes(output).decode(errors="replace"))
            logs = list(sessions.glob("threadtrace-live-*/*/events.jsonl"))
            self.assertEqual(len(logs), 1)
            return bytes(output), [json.loads(line) for line in logs[0].read_text().splitlines()]
        finally:
            if status is None:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.waitpid(pid, 0)
            os.close(master)

    @unittest.skipUnless(os.name == "posix", "requires a POSIX pseudo-terminal")
    def test_runtime_host_cli_prompt_and_dependency_callback(self) -> None:
        dependency = Path(self.temp.name) / "dependency.py"
        dependency.write_text(
            "def run(callback):\n"
            "    for i in range(300000):\n"
            "        value = i + 1\n"
            "    return callback()\n"
        )
        runner = self.root / "prompt_runner.py"
        runner.write_text(
            "import sys\n"
            f"sys.path.insert(0, {str(dependency.parent)!r})\n"
            "from dependency import run\n"
            "def prompt():\n"
            "    print('TTY:', sys.stdin.isatty(), sys.stdout.isatty(), sys.stderr.isatty(), flush=True)\n"
            "    answer = input('PROMPT> ')\n"
            "    print('ANSWER:', answer, flush=True)\n"
            "run(prompt)\n"
        )
        output, events = self._run_host_in_terminal(runner.name, b"PROMPT> ", b"Alice\n")
        self.assertIn(b"TTY: True True True", output)
        self.assertIn(b"ANSWER: Alice", output)
        self.assertTrue(any(event["name"] == "prompt" and event["kind"] == "call" for event in events))
        self.assertTrue(all(Path(event["path"]).resolve().is_relative_to(self.root.resolve()) for event in events))

    @unittest.skipUnless(os.name == "posix", "requires a POSIX pseudo-terminal")
    def test_runtime_host_full_screen_tui_receives_keys(self) -> None:
        runner = self.root / "tui_runner.py"
        runner.write_text(
            "import curses\n"
            "def main(screen):\n"
            "    screen.addstr(0, 0, 'SCREEN READY')\n"
            "    screen.refresh()\n"
            "    return screen.getch()\n"
            "key = curses.wrapper(main)\n"
            "print('KEY:', key, flush=True)\n"
        )
        output, events = self._run_host_in_terminal(runner.name, b"SCREEN READY", b"x")
        self.assertIn(b"\x1b[?1049h", output)
        self.assertIn(b"\x1b[?1049l", output)
        self.assertIn(b"KEY: 120", output)
        self.assertTrue(any(event["name"] == "main" and event["kind"] == "call" for event in events))

    @unittest.skipUnless(os.name == "posix", "requires a POSIX pseudo-terminal")
    def test_live_listener_shows_frame_variables_and_supports_history(self) -> None:
        import asyncio
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from threadtrace.live_tui import LiveRuntimeApp

        runner = self.root / "live_runner.py"
        runner.write_text(
            "def work():\n"
            "    value = 1\n"
            "    value = 2\n"
            "    del value\n"
            "    print('READY', flush=True)\n"
            "    input()\n"
            "work()\n"
        )
        self._run_host_in_terminal(runner.name, b"READY", b"\n")
        sessions = Path(self.temp.name) / "runtime"
        metadata = list(sessions.glob("threadtrace-live-*/*/session.json"))[0]
        session = json.loads(metadata.read_text())

        async def exercise() -> None:
            with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
                with patch("threadtrace.live_tui.live_sessions", return_value=[session]):
                    app = LiveRuntimeApp(self.engine, "sample")
                    task = asyncio.create_task(app.app.run_async())
                    try:
                        for _ in range(100):
                            await asyncio.sleep(0.02)
                            if app.events:
                                break
                        self.assertTrue(app.events)
                        self.assertTrue(app.following)
                        self.assertEqual(app.selected, len(app.events) - 1)
                        created = next(i for i, event in enumerate(app.events)
                                       if event.get("variables", {}).get("created", {}).get("value", {}).get("value") == 1)
                        changed = next(i for i, event in enumerate(app.events)
                                       if event.get("variables", {}).get("changed", {}).get("value", {}).get("value") == 2)
                        removed = next(i for i, event in enumerate(app.events)
                                       if "value" in event.get("variables", {}).get("removed", []))
                        app._select_event(created)
                        self.assertIn("value = 1 (int)", app.inspector.text)
                        self.assertIn("Source context", app.inspector.text)
                        self.assertNotIn('"created":', app.inspector.text)
                        app._select_event(changed)
                        self.assertIn("Changed\n  value = 2 (int)", app.inspector.text)
                        self.assertEqual(app._variables_at(app.events[changed])["value"]["value"], 2)
                        app._select_event(removed)
                        self.assertIn("Removed\n  value", app.inspector.text)
                        self.assertNotIn("value", app._variables_at(app.events[removed]))
                        pipe.send_text("k")
                        await asyncio.sleep(0.1)
                        self.assertEqual(app.selected, removed - 1)
                        self.assertFalse(app.following)
                        extra = dict(app.events[changed])
                        extra["variables"] = {"created": {"password": {"type": "str", "value": "<redacted>"}}, "changed": {}, "removed": []}
                        encoded = json.dumps(extra)
                        count = len(app.events)
                        with app.log_path.open("a") as log:
                            log.write(encoded[:20])
                        app._tick()
                        self.assertEqual(len(app.events), count)
                        with app.log_path.open("a") as log:
                            log.write(encoded[20:] + "\n")
                        app._tick()
                        self.assertEqual(len(app.events), count + 1)
                        self.assertEqual(app.selected, removed - 1)
                        pipe.send_text("l")
                        await asyncio.sleep(0.1)
                        self.assertTrue(app.following)
                        self.assertEqual(app.selected, len(app.events) - 1)
                        self.assertIn("password = <redacted> (str)", app.inspector.text)
                        pipe.send_text("q")
                        await asyncio.wait_for(task, 2)
                    finally:
                        if not task.done():
                            app.app.exit()
                            await task
        asyncio.run(exercise())

    def test_scan_groups_module_file_and_package_name_collisions(self) -> None:
        (self.root / "app" / "tools.py").write_text("def legacy_tool():\n    return 'legacy'\n")
        (self.root / "app" / "tools").mkdir()
        (self.root / "app" / "tools" / "__init__.py").write_text("def package_tool():\n    return 'package'\n")
        self.engine.scan("sample")
        components = self.queries.architecture("sample")["components"]
        matching = [component for component in components if component["name"] == "app.tools"]
        self.assertEqual(len(matching), 1)
        self.assertIn("across 2 source files", matching[0]["summary"])

    def test_lesson_explains_local_patterns_without_an_llm(self) -> None:
        (self.root / "app" / "patterns.py").write_text(
            "def normalise_and_sort(values):\n"
            "    cleaned = [value.strip() for value in values if value]\n"
            "    return sorted(cleaned)\n"
        )
        self.engine.scan("sample")
        lesson = self.queries.lesson("sample", "app.patterns.normalise_and_sort")
        self.assertEqual(lesson["source"]["path"], "app/patterns.py")
        self.assertIn("Collection transformation", {item["name"] for item in lesson["patterns"]})
        self.assertIn("Sorting", {item["name"] for item in lesson["patterns"]})
        self.assertTrue(any(item["name"] == "sorted" for item in lesson["calls"]))

    def test_ai_prompt_is_bounded_and_evidence_only(self) -> None:
        from threadtrace.ai import _prompt

        prompt = _prompt({"evidence": {"path": "app/main.py", "line": 4}, "large": "x" * 2_000})
        self.assertIn("app/main.py", prompt)
        self.assertNotIn("x" * 1_001, prompt)


if __name__ == "__main__":
    unittest.main()
