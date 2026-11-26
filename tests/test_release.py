"""Release gate: clean upgrade paths and executable quick-start commands."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

from threadtrace.cli import main
from threadtrace.engine import Engine
from threadtrace.formats import EXPORT_SCHEMA_VERSION, TRACE_PROTOCOL_VERSION
from threadtrace.runtime.schema import EVENT_SCHEMA_VERSION
from threadtrace.store import INITIAL_SCHEMA, SCHEMA_VERSION, Store

ROOT = Path(__file__).resolve().parents[1]
FILES = {
    "app/__init__.py": "",
    "app/routes.py": (
        "from flask import Flask\n\n"
        "app = Flask(__name__)\n\n\n"
        "@app.get('/users')\n"
        "def list_users():\n"
        "    return []\n"
    ),
    "app/core.py": (
        "def process(token: str) -> str:\n"
        "    cleaned = token.strip()\n"
        "    return cleaned\n"
    ),
    "run_me.py": "print('release gate')\n",
}


class ReleaseGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.database = Path(self.temp.name) / "thread.sqlite3"
        self.root = Path(self.temp.name) / "project"
        for relative, text in FILES.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str) -> tuple[int, str, str]:
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([*args, "--database", str(self.database)])
        return code, output.getvalue(), errors.getvalue()

    def test_version_one_database_migrates_through_the_full_chain(self):
        database = Path(self.temp.name) / "version-one.sqlite3"
        connection = sqlite3.connect(database)
        connection.executescript(INITIAL_SCHEMA)
        connection.execute(
            "INSERT INTO projects (project_id,name,root_path,created_at) VALUES (?,?,?,?)",
            ("project", "legacy", str(self.root), "2026-10-26T00:00:00+00:00"),
        )
        connection.execute(
            "INSERT INTO scans (scan_id,project_id,started_at,completed_at,status,tool_version)"
            " VALUES (?,?,?,?,?,?)",
            ("scan", "project", "2026-10-26T00:00:00+00:00", "2026-10-26T00:00:01+00:00",
             "completed", "0.7.0a1"),
        )
        connection.commit()
        connection.close()

        store = Store(database)
        try:
            self.assertEqual(store.schema_version, SCHEMA_VERSION)
            self.assertEqual(store.one("SELECT name FROM projects")["name"], "legacy")
            scan = store.one("SELECT status,evidence_version FROM scans")
            self.assertEqual(scan["status"], "completed")
            self.assertEqual(scan["evidence_version"], 1)
            backup = sqlite3.connect(Path(str(database) + ".bak"))
            try:
                self.assertEqual(backup.execute("PRAGMA user_version").fetchone()[0], 1)
            finally:
                backup.close()
        finally:
            store.close()

    def test_version_one_export_still_imports(self):
        store = Store(self.database)
        try:
            engine = Engine(store)
            engine.add_project(str(self.root), "example")
            engine.scan("example")
            engine.save_investigation("example", "flow", {"notes": "gate"})
            exported = Path(self.temp.name) / "flow.json"
            engine.export_investigation("example", "flow", output=str(exported))
            bundle = json.loads(exported.read_text(encoding="utf-8"))
            bundle["export_version"] = 1
            exported.write_text(json.dumps(bundle), encoding="utf-8")
            imported = engine.import_investigation("example", str(exported), name="flow-v1")
            self.assertEqual(imported["export_version"], 1)
            self.assertEqual(imported["name"], "flow-v1")
        finally:
            store.close()
        self.assertEqual(EXPORT_SCHEMA_VERSION, 2)
        self.assertEqual(TRACE_PROTOCOL_VERSION, 1)
        self.assertEqual(EVENT_SCHEMA_VERSION, 2)

    def test_readme_quick_start_commands_execute(self):
        commands = [
            ("project", "add", str(self.root), "--name", "example"),
            ("scan", "example"),
            ("modules", "example"),
            ("functions", "example"),
            ("deps", "example", "app.core", "--depth", "2"),
            ("diagnostics", "example"),
            ("overview", "example", "--format", "markdown"),
            ("entrypoints", "example"),
            ("architecture", "example", "--format", "mermaid"),
            ("path", "example", "GET /users"),
            ("resources", "example", "--kind", "database"),
            ("impact", "example", "app.core.process", "--direction", "both"),
            ("dataflow", "example", "app.core.process", "--to", "database"),
            ("slice", "example", "app.core.process", "token", "--direction", "backward"),
            ("control-flow", "example", "app.core.process"),
            ("complexity", "example", "app.core.process"),
            ("advanced", "example", "app.core.process"),
            ("map", "example", "GET /users"),
            ("references", "example", "app.core.process"),
            ("usage", "example", "app.core.process"),
            ("boundaries", "example"),
            ("metadata", "example"),
            ("staleness", "example"),
            ("findings", "example"),
            ("capabilities", "example"),
        ]
        for command in commands:
            with self.subTest(command=" ".join(command)):
                code, _, errors = self.run_cli(*command)
                self.assertEqual(code, 0, errors)
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main([
                "trace", "example", "--yes-run", "--timeout", "20",
                "--database", str(self.database), "--",
                sys.executable, str(self.root / "run_me.py"),
            ])
        self.assertEqual(code, 0, errors.getvalue())
        code, output, errors = self.run_cli("traces", "example", "--format", "json")
        self.assertEqual(code, 0, errors)
        traces = json.loads(output)
        self.assertTrue(traces)
        trace_id = traces[0]["trace_id"]
        for command in (
            ("trace-profile", "example", trace_id),
            ("trace-concurrency", "example", trace_id),
            ("trace-events", "example", trace_id, "--kind", "line", "--path", "app/"),
        ):
            with self.subTest(command=" ".join(command)):
                code, _, errors = self.run_cli(*command)
                self.assertEqual(code, 0, errors)


if __name__ == "__main__":
    unittest.main()
