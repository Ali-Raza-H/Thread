from __future__ import annotations

import errno
import json
import os
import platform
import select
import shutil
import signal
import socket
import sqlite3
import stat
import subprocess
import tempfile
import sys
import threading
import time
import unittest
from datetime import UTC, datetime

if os.name == "posix":
    import fcntl
    import pty
    import struct
    import termios
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from threadtrace.cli import main
from threadtrace.engine import Engine, ThreadError
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
            "from flask import Flask\n\n"
            "app = Flask(__name__)\n\n"
            "@app.get('/items')\n"
            "def list_items(limit: int = 10):\n"
            "    return limit\n"
        )
        (self.root / "app" / "resources.py").write_text(
            "import os\nfrom pathlib import Path\n\n"
            "def load_token():\n"
            "    Path('settings.json')\n"
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

    def test_call_resolution_requires_python_binding_evidence(self) -> None:
        (self.root / "app" / "alpha.py").write_text("def shared():\n    return 'alpha'\n")
        (self.root / "app" / "beta.py").write_text("def shared():\n    return 'beta'\n")
        (self.root / "app" / "nested.py").write_text(
            "def outer():\n"
            "    def inner():\n"
            "        return helper()\n"
            "    def helper():\n"
            "        return 'nested'\n"
            "    return inner\n"
        )
        (self.root / "app" / "exported.py").write_text("def original():\n    return 'exported'\n")
        (self.root / "app" / "__init__.py").write_text("from .exported import original as public\n")
        (self.root / "app" / "consumer.py").write_text(
            "from . import public\n\n"
            "def use():\n"
            "    return public()\n"
        )
        (self.root / "app" / "caller.py").write_text(
            "from .helpers import format_name\n\n"
            "def run(format_name):\n"
            "    return format_name('shadowed')\n\n"
            "def unresolved():\n"
            "    return shared()\n\n"
            "def imported():\n"
            "    return format_name('ok')\n"
        )
        (self.root / "app" / "local_scope.py").write_text(
            "def local_import():\n"
            "    from .helpers import format_name\n"
            "    return format_name('local')\n\n"
            "def outside():\n"
            "    return format_name('outside')\n"
        )
        self.engine.scan("sample")

        def calls(name: str) -> list[dict[str, object]]:
            return self.queries.callees("sample", name)["calls"]

        shadowed = calls("app.caller.run")[0]
        self.assertEqual((shadowed["resolution"], shadowed["callee"]), ("unresolved", None))
        self.assertEqual(shadowed["basis"], "shadowed")

        imported = calls("app.caller.imported")[0]
        self.assertEqual(imported["callee"], "app.helpers.format_name")
        self.assertEqual(imported["basis"], "import")

        unresolved = calls("app.caller.unresolved")[0]
        self.assertEqual((unresolved["resolution"], unresolved["callee"]), ("unresolved", None))
        self.assertEqual(unresolved["basis"], "heuristic")
        self.assertEqual(len(json.loads(unresolved["candidate_json"])), 2)

        nested = calls("app.nested.outer.inner")[0]
        self.assertEqual(nested["callee"], "app.nested.outer.helper")
        self.assertEqual(nested["basis"], "lexical")

        reexported = calls("app.consumer.use")[0]
        self.assertEqual(reexported["callee"], "app.exported.original")
        self.assertEqual(reexported["basis"], "re-export")

        local_import = calls("app.local_scope.local_import")[0]
        self.assertEqual(local_import["callee"], "app.helpers.format_name")
        self.assertEqual(local_import["basis"], "import")
        outside = calls("app.local_scope.outside")[0]
        self.assertEqual(outside["resolution"], "unresolved")
        self.assertEqual(outside["basis"], "heuristic")

        shared_callers = self.queries.callers("sample", "app.alpha.shared")
        self.assertEqual(shared_callers, [])
        candidates = self.queries.impact("sample", "app.alpha.shared")["downstream"]
        self.assertEqual(candidates, [])
        mentioned = [
            edge for edge in self.queries.file_graph("sample")["edges"]
            if edge["kind"] == "candidate_call" and "app.caller" in edge["source"]
        ]
        self.assertTrue(mentioned)
        self.assertTrue(all(edge["confidence"] == "possible" for edge in mentioned))
        self.assertTrue(all(edge["evidence"][0]["reason"] for edge in mentioned))

    def test_scan_reads_common_dependency_metadata_without_executing_it(self) -> None:
        (self.root / "setup.cfg").write_text("[options]\ninstall_requires =\n    rich>=13\n")
        (self.root / "setup.py").write_text("setup(install_requires=['httpx>=0.1'])\n")
        (self.root / "Pipfile").write_text("[packages]\nclick = '*'\n")
        self.engine.scan("sample")
        declarations = {(item["name"], item["declared_in"]) for item in self.queries.libraries("sample")}
        self.assertIn(("rich", "setup.cfg"), declarations)
        self.assertIn(("httpx", "setup.py"), declarations)
        self.assertIn(("click", "Pipfile"), declarations)

    def test_experiment_uses_a_copy_and_preserves_registered_source(self) -> None:
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            experiment = self.engine.create_experiment("sample", "change-helper")
            self.engine.patch_experiment("sample", "change-helper", "app/helpers.py", 2, 2, "    return value.upper()")
            self.assertIn("value.upper()", self.engine.experiment_diff("sample", "change-helper"))
            self.assertIn("value.strip()", (self.root / "app/helpers.py").read_text())
            result = self.engine.run_experiment("sample", "change-helper", [sys.executable, "-c", "print('ok')"], 5, 1_000)
            removed = self.engine.remove_experiment("sample", "change-helper")
        self.assertEqual(result["stdout"], "ok\n")
        self.assertEqual(experiment["status"], "ready")
        self.assertTrue(removed["removed"])
        self.assertFalse(Path(removed["workspace_path"]).exists())
        self.assertTrue(self.root.exists())

    def test_experiment_comparison_uses_pristine_baseline_and_records_revisions(self) -> None:
        target = self.root / "app" / "comparison_patch.py"
        target.write_text("def result():\n    return 'baseline'\n")
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            experiment = self.engine.create_experiment("sample", "file-patch-comparison")
            self.engine.patch_experiment(
                "sample", "file-patch-comparison", "app/comparison_patch.py", 2, 2,
                "    return 'modified'",
            )
            comparison = self.engine.compare_experiment(
                "sample", "file-patch-comparison", "app.comparison_patch:result",
            )

            self.assertEqual(comparison["baseline"]["return_value"], "baseline")
            self.assertEqual(comparison["modified"]["return_value"], "modified")
            self.assertEqual(comparison["baseline"]["workspace_role"], "baseline")
            self.assertEqual(comparison["modified"]["workspace_role"], "modified")
            self.assertEqual(comparison["baseline"]["configuration"], {})
            self.assertNotEqual(
                comparison["baseline"]["source_revision"]["tree_hash"],
                comparison["modified"]["source_revision"]["tree_hash"],
            )
            self.assertEqual(
                comparison["baseline"]["interpreter"],
                comparison["modified"]["interpreter"],
            )
            self.assertEqual(
                experiment["baseline_source_hash"],
                comparison["baseline"]["source_revision"]["tree_hash"],
            )
            output = StringIO()
            with redirect_stdout(output), redirect_stderr(StringIO()):
                code = main([
                    "experiment", "compare", "sample", "file-patch-comparison",
                    "app.comparison_patch:result", "--yes-run", "--database", str(self.store.path),
                    "--format", "json",
                ])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["baseline"]["workspace_role"], "baseline")

            target.write_text("def result():\n    return 'changed after creation'\n")
            with self.assertRaisesRegex(ThreadError, "Registered source changed after this experiment was created"):
                self.engine.compare_experiment(
                    "sample", "file-patch-comparison", "app.comparison_patch:result",
                )

    def test_experiment_comparison_covers_configuration_files_exceptions_side_effects_and_unknown_values(self) -> None:
        (self.root / "app" / "comparison_types.py").write_text(
            "import os\n"
            "from pathlib import Path\n\n"
            "SETTING = 'baseline'\n\n"
            "def configured():\n"
            "    return SETTING\n\n"
            "def raises():\n"
            "    return 'no exception'\n\n"
            "def side_effect():\n"
            "    Path('comparison-effect.txt').write_text(SETTING)\n"
            "    return SETTING\n\n"
            "def opaque():\n"
            "    return object()\n\n"
            "def process_id():\n"
            "    return os.getpid()\n"
        )
        (self.root / "app" / "removed_target.py").write_text(
            "def result():\n    return 'present'\n"
        )
        config = {
            "overrides": {"app.comparison_types:SETTING": "modified"},
            "patches": [{
                "target": "app.comparison_types:raises",
                "code": "def replacement():\n    raise ValueError('modified failure')",
            }],
        }
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            experiment = self.engine.create_experiment("sample", "comparison-types")
            workspace = Path(experiment["workspace_path"])
            (workspace / "app" / "created_target.py").write_text(
                "def result():\n    return 'created'\n"
            )
            (workspace / "app" / "removed_target.py").unlink()
            self.engine.configure_experiment("sample", "comparison-types", config)

            configured = self.engine.compare_experiment(
                "sample", "comparison-types", "app.comparison_types:configured",
            )
            created = self.engine.compare_experiment(
                "sample", "comparison-types", "app.created_target:result",
            )
            deleted = self.engine.compare_experiment(
                "sample", "comparison-types", "app.removed_target:result",
            )
            exception = self.engine.compare_experiment(
                "sample", "comparison-types", "app.comparison_types:raises",
            )
            side_effect = self.engine.compare_experiment(
                "sample", "comparison-types", "app.comparison_types:side_effect",
            )
            unknown = self.engine.compare_experiment(
                "sample", "comparison-types", "app.comparison_types:opaque",
            )
            processes = self.engine.compare_experiment(
                "sample", "comparison-types", "app.comparison_types:process_id",
            )

        self.assertEqual(configured["baseline"]["return_value"], "baseline")
        self.assertEqual(configured["modified"]["return_value"], "modified")
        self.assertEqual(configured["modified"]["configuration"], config)
        self.assertEqual(created["baseline"]["exception"]["type"], "ModuleNotFoundError")
        self.assertEqual(created["modified"]["return_value"], "created")
        self.assertEqual(deleted["baseline"]["return_value"], "present")
        self.assertEqual(deleted["modified"]["exception"]["type"], "ModuleNotFoundError")
        self.assertIsNone(exception["baseline"]["exception"])
        self.assertEqual(exception["modified"]["exception"]["type"], "ValueError")
        self.assertIn("side_effects", {item["field"] for item in side_effect["differences"]})
        self.assertIn("return_value", {item["field"] for item in unknown["unknown_comparisons"]})
        self.assertNotEqual(processes["baseline"]["return_value"], processes["modified"]["return_value"])

    def test_callable_experiments_support_rules_instances_replay_exploration_and_comparison(self) -> None:
        (self.root / "app" / "hybrid_target.py").write_text(
            "FLAG = 'normal'\n\n"
            "def dependency(value):\n"
            "    return value.strip()\n\n"
            "def run(value):\n"
            "    return dependency(value) + '-' + FLAG\n\n"
            "class Counter:\n"
            "    def __init__(self, value):\n"
            "        self.value = value\n\n"
            "    def add(self, amount):\n"
            "        self.value += amount\n"
            "        return self.value\n"
        )
        (self.root / "app" / "external_target.py").write_text(
            "from fake_service import fetch\n\n"
            "def run():\n"
            "    return fetch()\n"
        )
        (self.root / "app" / "time_target.py").write_text(
            "import time\n\n"
            "def now():\n"
            "    return time.time()\n"
        )
        (self.root / "app" / "missing_target.py").write_text(
            "import available_service\n\n"
            "def run():\n"
            "    return available_service.value\n"
        )
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            self.engine.create_experiment("sample", "callable")
            self.engine.configure_experiment("sample", "callable", {
                "substitutions": [{"target": "app.hybrid_target:dependency", "mode": "return", "value": "mock"}],
                "overrides": {"app.hybrid_target:FLAG": "changed"},
                "seed": 7,
            })
            changed = self.engine.callable_experiment("sample", "callable", "app.hybrid_target:run", [" value "])
            self.assertEqual(changed["return_value"], "mock-changed")
            self.assertIn("substitution", {item["kind"] for item in changed["interactions"]})
            self.assertIn("intercepted_call", {item["kind"] for item in changed["interactions"]})
            output = StringIO()
            with redirect_stdout(output):
                code = main(["experiment", "call", "sample", "callable", "app.hybrid_target:run", "--args", '["value"]',
                             "--yes-run", "--database", str(self.store.path), "--format", "json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["return_value"], "mock-changed")
            operations = self.engine.callable_experiment("sample", "callable", "", operations=[
                {"kind": "construct", "name": "counter", "target": "app.hybrid_target:Counter", "args": [1]},
                {"kind": "call_instance", "instance": "counter", "method": "add", "args": [4]},
            ])
            self.assertEqual(operations["operations"][-1]["return_value"], 5)
            exploration = self.engine.explore_experiment("sample", "callable", "app.hybrid_target:run", [
                {"label": "one", "args": ["one"]}, {"label": "two", "args": ["two"]},
            ])
            self.assertEqual(len(exploration["results"]), 2)
            runs = self.engine.experiment_runs("sample", "callable")
            harness_run = next(item for item in runs if item["kind"] == "harness" and item["request"].get("target"))
            replay = self.engine.replay_experiment("sample", "callable", harness_run["run_id"])
            self.assertEqual(replay["result"]["return_value"], "mock-changed")
            comparison = self.engine.compare_experiment("sample", "callable", "app.hybrid_target:run", [" value "])
            self.engine.configure_experiment("sample", "callable", {
                "mocks": {"fake_service": {"fetch": {"mode": "return", "value": "mocked"}}},
            })
            mocked = self.engine.callable_experiment("sample", "callable", "app.external_target:run")
            self.assertEqual(mocked["return_value"], "mocked")
            self.engine.configure_experiment("sample", "callable", {"freeze_time": 123.5})
            frozen = self.engine.callable_experiment("sample", "callable", "app.time_target:now")
            self.assertEqual(frozen["return_value"], 123.5)
            self.engine.configure_experiment("sample", "callable", {"disabled_modules": ["available_service"]})
            unavailable = self.engine.callable_experiment("sample", "callable", "app.missing_target:run")
            self.assertEqual(unavailable["exception"]["type"], "ModuleNotFoundError")
            self.engine.configure_experiment("sample", "callable", {
                "patches": [{"target": "app.hybrid_target:run", "code": "def replacement(value):\n    return 'patched'"}],
            })
            patched = self.engine.callable_experiment("sample", "callable", "app.hybrid_target:run", ["value"])
            self.assertEqual(patched["return_value"], "patched")
        self.assertFalse(comparison["same_behaviour"])
        self.assertIn("return_value", {item["field"] for item in comparison["differences"]})
        self.assertEqual(
            comparison["baseline"]["source_revision"]["tree_hash"],
            comparison["modified"]["source_revision"]["tree_hash"],
        )
        self.assertEqual(comparison["baseline"]["configuration"], {})
        self.assertTrue(comparison["modified"]["configuration"])
        self.assertIn("return value", (self.root / "app" / "hybrid_target.py").read_text())

    def test_full_hybrid_harness_supports_live_overrides_async_generators_typed_values_and_alias_patches(self) -> None:
        (self.root / "app" / "live_target.py").write_text(
            "def calculate(value):\n"
            "    current = value + 1\n"
            "    return current * 2\n\n"
            "async def async_value(value):\n"
            "    return value + 1\n\n"
            "def generated(limit):\n"
            "    for value in range(limit):\n"
            "        yield value\n\n"
            "def value_type(value):\n"
            "    return type(value).__name__\n\n"
            "def dependency():\n"
            "    return 2\n\n"
            "def first():\n"
            "    return 9\n\n"
            "def use_dependency():\n"
            "    return dependency()\n\n"
            "class Holder:\n"
            "    @property\n"
            "    def score(self):\n"
            "        return 1\n"
        )
        (self.root / "app" / "alias_target.py").write_text(
            "from .live_target import dependency\n\n"
            "def call_alias():\n"
            "    return dependency()\n"
        )
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            experiment = self.engine.create_experiment("sample", "full-harness")
            self.engine.configure_experiment("sample", "full-harness", {
                "line_overrides": [{"path": "app/live_target.py", "line": 3, "locals": {"current": 9}}],
            })
            overridden = self.engine.callable_experiment("sample", "full-harness", "app.live_target:calculate", [1])
            self.assertEqual(overridden["return_value"], 18)
            self.assertTrue(any(item["kind"] == "line_override" for item in overridden["interactions"]))
            async_result = self.engine.callable_experiment("sample", "full-harness", "app.live_target:async_value", [3])
            generated = self.engine.callable_experiment("sample", "full-harness", "app.live_target:generated", [3])
            typed = self.engine.callable_experiment("sample", "full-harness", "app.live_target:value_type",
                                                    [{"$type": "tuple", "value": [1, 2]}])
            self.assertEqual(async_result["return_value"], 4)
            self.assertEqual(generated["return_value"], [0, 1, 2])
            self.assertEqual(typed["return_value"], "tuple")
            self.engine.configure_experiment("sample", "full-harness", {
                "setup_code": "import app.alias_target",
                "patches": [{"target": "app.live_target:dependency", "code": "def replacement():\n    return 41"}],
            })
            aliased = self.engine.callable_experiment("sample", "full-harness", "app.alias_target:call_alias")
            self.assertEqual(aliased["return_value"], 41)
            patch_event = next(item for item in aliased["interactions"] if item["kind"] == "patch")
            self.assertIn("app.alias_target:dependency", patch_event["aliases"])
            per_call = self.engine.callable_experiment(
                "sample", "full-harness", "app.alias_target:call_alias", per_call={
                    "substitutions": [{"target": "app.live_target:dependency", "mode": "return", "value": 5}],
                },
            )
            self.assertEqual(per_call["return_value"], 5)
            with self.assertRaisesRegex(RuntimeError, "Conflicting patches"):
                self.engine.configure_experiment("sample", "full-harness", {
                    "patches": [{"target": "app.live_target:dependency", "code": "def replacement(): return 1"},
                                {"target": "app.live_target:dependency", "code": "def replacement(): return 2"}],
                })
            self.engine.configure_experiment("sample", "full-harness", {
                "patches": [{"target": "app.live_target:Holder.score",
                             "code": "def replacement(self):\n    return 6"}],
            })
            descriptor = self.engine.callable_experiment("sample", "full-harness", "", operations=[
                {"kind": "construct", "name": "holder", "target": "app.live_target:Holder"},
                {"kind": "get_instance", "instance": "holder", "attribute": "score"},
            ])
            self.assertEqual(descriptor["operations"][1]["value"], 6)
            self.engine.configure_experiment("sample", "full-harness", {
                "substitutions": [{"target": "app.live_target:dependency", "mode": "recorded", "recording": "0"}],
            })
            recorded = self.engine.callable_experiment("sample", "full-harness", "", operations=[
                {"kind": "call", "target": "app.live_target:first"},
                {"kind": "call", "target": "app.live_target:use_dependency"},
            ])
            self.assertEqual(recorded["operations"][1]["return_value"], 9)
            capability = self.engine.experiment_capability("sample", "full-harness", "app.live_target:async_value")
            self.assertEqual(capability["capability"]["status"], "Full")
            self.assertEqual(capability["capability"]["kind"], "coroutine_function")
            from threadtrace.hybrid import HybridHarness
            pauses = []

            def pause_handler(state: dict) -> dict | None:
                pauses.append(state)
                return {"current": 7} if state["path"] == "app/live_target.py" and state["line"] == 3 else None

            native = HybridHarness(Path(experiment["workspace_path"]), pause_handler=pause_handler)
            self.assertEqual(native.call("app.live_target:calculate", [1])["return_value"], 14)
            self.assertTrue(pauses)

    def test_full_hybrid_adapters_exploration_side_effects_and_deterministic_replay(self) -> None:
        (self.root / "app" / "adapter_target.py").write_text(
            "import random\n"
            "import socket\n"
            "import sqlite3\n"
            "import subprocess\n"
            "import time\n"
            "import urllib.request\n"
            "import uuid\n"
            "from pathlib import Path\n\n"
            "def external():\n"
            "    response = urllib.request.urlopen('https://service.test/items')\n"
            "    rows = sqlite3.connect(':memory:').execute('SELECT value').fetchall()\n"
            "    Path('adapter-output.txt').write_text(response.read().decode())\n"
            "    return rows\n\n"
            "def controlled():\n"
            "    return [random.random(), time.time(), str(uuid.uuid4())]\n\n"
            "def choose(value: int):\n"
            "    if value < 0:\n"
            "        raise ValueError('negative')\n"
            "    return 'zero' if value == 0 else 'positive'\n\n"
            "def read_selected_file():\n"
            "    return Path('selected.txt').read_text()\n\n"
            "def connect():\n"
            "    return socket.create_connection(('service.test', 443))\n\n"
            "def start_process():\n"
            "    return subprocess.run(['tool'], check=False).returncode\n"
        )
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            self.engine.create_experiment("sample", "adapters")
            self.engine.configure_experiment("sample", "adapters", {
                "adapters": {
                    "http": [{"url_contains": "service.test", "status": 200, "body": "mock body"}],
                    "database": [{"query_contains": "SELECT value", "rows": [[7], [8]]}],
                },
                "seed": 11, "freeze_time": 123.5, "uuid": "12345678-1234-5678-1234-567812345678",
            })
            external = self.engine.callable_experiment("sample", "adapters", "app.adapter_target:external")
            self.assertEqual(external["return_value"], [[7], [8]])
            operations = external["side_effects"]["external_operations"]
            self.assertTrue(any(item["adapter"] == "http" and item["mocked"] for item in operations))
            self.assertTrue(any(item["adapter"] == "database" and item["operation"] == "execute" for item in operations))
            self.assertIn("adapter-output.txt", external["side_effects"]["filesystem"]["created"])
            controlled = self.engine.callable_experiment("sample", "adapters", "app.adapter_target:controlled")
            replay = self.engine.replay_experiment("sample", "adapters", controlled["run_id"])
            self.assertTrue(replay["deterministic"])
            self.assertEqual(replay["result"]["return_value"], controlled["return_value"])
            exploration = self.engine.explore_experiment("sample", "adapters", "app.adapter_target:choose")
            labels = {item["case"]["label"] for item in exploration["results"]}
            self.assertIn("value: negative", labels)
            self.assertTrue(exploration["crashes"])
            self.assertGreaterEqual(len(exploration["distinct_cases"]), 3)
            self.engine.configure_experiment("sample", "adapters", {
                "adapters": {"filesystem": [{"path_contains": "selected.txt", "operation": "read", "content": "selected"}]},
            })
            selected = self.engine.callable_experiment("sample", "adapters", "app.adapter_target:read_selected_file")
            self.assertEqual(selected["return_value"], "selected")
            self.engine.configure_experiment("sample", "adapters", {
                "adapters": {"http": [{"url_contains": "service.test", "error": "timeout", "message": "slow"}]},
            })
            timed_out = self.engine.callable_experiment("sample", "adapters", "app.adapter_target:external")
            self.assertEqual(timed_out["exception"]["type"], "TimeoutError")
            self.engine.configure_experiment("sample", "adapters", {
                "adapters": {"network": [{"address_contains": "service.test", "error": "connection"}]},
            })
            connection = self.engine.callable_experiment("sample", "adapters", "app.adapter_target:connect")
            self.assertEqual(connection["exception"]["type"], "ConnectionError")
            self.engine.configure_experiment("sample", "adapters", {
                "adapters": {"process": [{"command_contains": "tool", "block": True}]},
            })
            process = self.engine.callable_experiment("sample", "adapters", "app.adapter_target:start_process")
            self.assertEqual(process["exception"]["type"], "PermissionError")
            export_path = Path(self.temp.name) / "adapter-experiment.json"
            exported = self.engine.export_experiment("sample", "adapters", str(export_path))
            self.assertEqual(exported["payload_type"], "experiment")
            self.assertEqual(exported["database_schema_version"], 17)
            self.assertEqual(exported["trace_protocol_version"], 1)
            self.assertTrue(exported["application_version"])
            self.assertEqual(stat.S_IMODE(export_path.stat().st_mode), 0o600)
            self.assertTrue(exported["payload"]["runs"])
            self.assertEqual(exported["run_ids"], [run["run_id"] for run in exported["payload"]["runs"]])
            self.assertEqual(json.loads(export_path.read_text())["payload"]["experiment"], "adapters")

    def test_experiment_preflight_precedence_replay_controls_and_honest_truncation(self) -> None:
        (self.root / "app" / "preflight_target.py").write_text(
            "import urllib.request\n"
            "from time import time as clock\n\n"
            "def now():\n"
            "    return clock()\n\n"
            "def download():\n"
            "    return urllib.request.urlopen('https://service.test/data').read()\n\n"
            "def large():\n"
            "    return list(range(150))\n\n"
            "def streamed():\n"
            "    for value in range(5_000):\n"
            "        yield value\n"
        )
        (self.root / "app" / "preflight_user.py").write_text(
            "from app.preflight_target import now\n\n"
            "def current():\n"
            "    return now()\n"
        )
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            self.engine.create_experiment("sample", "preflight")
            self.engine.configure_experiment("sample", "preflight", {
                "substitutions": [{"target": "app.preflight_target:clock", "mode": "return", "value": 1}],
                "patches": [{"target": "app.preflight_target:now", "code": "def replacement():\n    return 2"}],
                "overrides": {"app.preflight_target:now": None},
                "line_overrides": [{"path": "app/preflight_target.py", "line": 5, "locals": {"value": 1}}],
                "adapters": {"http": [{"url_contains": "service.test", "body": "mock"}]},
                "seed": 3, "freeze_time": 10.0, "uuid": "12345678-1234-5678-1234-567812345678",
                "environment": {"TT_MODE": "controlled"}, "interpreter": sys.executable,
            })
            report = self.engine.experiment_preflight("sample", "preflight")
            self.assertEqual(report["status"], "ready")
            self.assertEqual([item["rule"] for item in report["precedence"]], [
                "explicit per-call/per-experiment override", "named patch/substitution",
                "adapter mock/replay", "original behaviour",
            ])
            self.assertEqual(report["shadowed"], [{"target": "app.preflight_target:now", "winner": "override",
                                                   "shadowed_rule": "patch"}])
            self.assertIn({"target": "app.preflight_target:now", "module": "app.preflight_target",
                           "path": "app/preflight_user.py", "line": 1, "bound_as": "now"},
                          report["prebound_aliases"])
            self.assertEqual({item["input"] for item in report["replay"]["controlled"]},
                             {"random", "time", "uuid", "environment"})
            self.assertIn("os.urandom, secrets, and hash randomisation",
                          {item["input"] for item in report["replay"]["uncontrolled"]})
            self.assertFalse(report["replay"]["fully_controlled"])
            self.assertTrue(report["interpreter"]["available"])
            output = StringIO()
            with redirect_stdout(output):
                code = main(["experiment", "preflight", "sample", "preflight", "--database", str(self.store.path),
                             "--format", "json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["rules"]["patches"], ["app.preflight_target:now"])
            with self.assertRaisesRegex(ThreadError, "Patches and substitutions"):
                self.engine.configure_experiment("sample", "preflight", {
                    "patches": [{"target": "app.preflight_target:now", "code": "def replacement():\n    return 2"}],
                    "substitutions": [{"target": "app.preflight_target:now", "mode": "return", "value": 3}],
                })
            with self.assertRaisesRegex(ThreadError, "Conflicting line overrides"):
                self.engine.configure_experiment("sample", "preflight", {
                    "line_overrides": [{"path": "app/preflight_target.py", "line": 5, "locals": {"value": 1}},
                                       {"path": "app/preflight_target.py", "line": 5, "locals": {"value": 2}}],
                })
            with self.assertRaisesRegex(ThreadError, "Conflicting http adapter"):
                self.engine.configure_experiment("sample", "preflight", {
                    "adapters": {"http": [{"url_contains": "service.test", "body": "one"},
                                          {"url_contains": "service.test", "body": "two"}]},
                })
            self.engine.configure_experiment("sample", "preflight", {"seed": 3, "freeze_time": 10.0,
                                                                      "uuid": "12345678-1234-5678-1234-567812345678"})
            frozen = self.engine.callable_experiment("sample", "preflight", "app.preflight_target:now")
            self.assertEqual(frozen["return_value"], 10.0)
            self.assertIn("time", {item["input"] for item in frozen["replay"]["controlled"]})
            self.assertIn("preflight", frozen)
            self.engine.configure_experiment("sample", "preflight", {
                "adapters": {"http": [{"url_contains": "service.test", "body": "mock"}]},
                "patches": [{"target": "urllib.request:urlopen", "code": (
                    "def replacement(*args, **kwargs):\n"
                    "    class Response:\n"
                    "        def read(self):\n"
                    "            return b'patched'\n"
                    "    return Response()")}],
            })
            downloaded = self.engine.callable_experiment("sample", "preflight", "app.preflight_target:download")
            self.assertEqual(downloaded["return_value"]["preview"], "patched")
            self.assertFalse([item for item in downloaded["interactions"] if item.get("adapter") == "http"])
            self.engine.configure_experiment("sample", "preflight", {"yield_cap": 1_000})
            comparison = self.engine.compare_experiment("sample", "preflight", "app.preflight_target:large")
            self.assertFalse(comparison["same_behaviour"])
            self.assertNotIn("return_value", {item["field"] for item in comparison["differences"]})
            self.assertIn("return_value", {item["field"] for item in comparison["unknown_comparisons"]})
            self.engine.configure_experiment("sample", "preflight", {"yield_cap": 10})
            streamed = self.engine.callable_experiment("sample", "preflight", "app.preflight_target:streamed")
            self.assertTrue(streamed["collection_truncated"])
            self.assertEqual(len(streamed["return_value"]), 10)
            missing = str(Path(self.temp.name) / "missing-python")
            self.engine.configure_experiment("sample", "preflight", {"interpreter": missing})
            self.assertFalse(self.engine.experiment_preflight("sample", "preflight")["interpreter"]["available"])
            with self.assertRaisesRegex(ThreadError, "Could not run callable experiment"):
                self.engine.callable_experiment("sample", "preflight", "app.preflight_target:now")
        if os.name != "posix":
            return
        from threadtrace.hybrid import run_workspace
        marker = Path(self.temp.name) / "grandchild-survived.txt"
        grandchild = f"import pathlib,time; time.sleep(2); pathlib.Path({str(marker)!r}).write_text('alive')"
        parent = f"import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',{grandchild!r}]); time.sleep(30)"
        result = run_workspace(Path(self.temp.name), [sys.executable, "-c", parent], timeout=1, output_cap=1_000)
        self.assertTrue(result["timed_out"])
        time.sleep(2.5)
        self.assertFalse(marker.exists())

    def test_hybrid_adapter_replay_denies_unreplaced_operations_and_budgets_exploration(self) -> None:
        (self.root / "app" / "adapter_guard.py").write_text(
            "import socket\n"
            "import sqlite3\n"
            "import subprocess\n"
            "import urllib.request\n"
            "from pathlib import Path\n\n"
            "def read_selected():\n"
            "    return Path('selected.txt').read_text()\n\n"
            "def download():\n"
            "    return urllib.request.urlopen('https://service.test/item').read().decode()\n\n"
            "def connect():\n"
            "    return socket.create_connection(('service.test', 443))\n\n"
            "def start_process():\n"
            "    return subprocess.run(['tool'], check=False).returncode\n\n"
            "def database():\n"
            "    connection = sqlite3.connect(':memory:')\n"
            "    connection.execute('SELECT value')\n"
            "    connection.commit()\n"
            "    connection.close()\n"
            "    return 'done'\n\n"
            "def choose(value: int):\n"
            "    if value < 0:\n"
            "        raise ValueError('negative')\n"
            "    return 'zero' if value == 0 else 'positive'\n\n"
            "def accept(value):\n"
            "    return value\n"
        )
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            experiment = self.engine.create_experiment("sample", "budgets")
            (Path(experiment["workspace_path"]) / "selected.txt").write_text("real")
            self.engine.configure_experiment("sample", "budgets", {
                "adapters": {
                    "filesystem": [{"path_contains": "selected.txt", "operation": "read"}],
                    "network": [{"address_contains": "service.test"}],
                    "process": [{"command_contains": "tool"}],
                },
            })
            for target in ("app.adapter_guard:read_selected", "app.adapter_guard:connect", "app.adapter_guard:start_process"):
                denied = self.engine.callable_experiment("sample", "budgets", target)
                self.assertEqual(denied["exception"]["type"], "PermissionError")
                entry = next(item for item in denied["interactions"] if item.get("kind") == "adapter")
                self.assertEqual(entry["status"], "failed")
                self.assertIn("error", entry)
                self.assertTrue(entry["adapter_version"])
            self.engine.configure_experiment("sample", "budgets", {
                "adapters": {"filesystem": [{"path_contains": "selected.txt", "operation": "read",
                                              "pass_through": True}]},
            })
            passed = self.engine.callable_experiment("sample", "budgets", "app.adapter_guard:read_selected")
            self.assertEqual(passed["return_value"], "real")
            self.assertEqual(next(item for item in passed["interactions"] if item.get("kind") == "adapter")["status"],
                             "pass_through")
            self.engine.configure_experiment("sample", "budgets", {
                "adapters": {
                    "http": [{"url_contains": "service.test", "body": "ok", "latency": 0.01}],
                    "database": [{"query_contains": "SELECT value", "rows": [[1]]}],
                },
            })
            http_result = self.engine.callable_experiment("sample", "budgets", "app.adapter_guard:download")
            self.assertEqual(http_result["return_value"], "ok")
            http_entry = next(item for item in http_result["interactions"] if item.get("adapter") == "http")
            self.assertEqual(http_entry["status"], "mocked")
            self.assertGreaterEqual(http_entry["duration_seconds"], 0.01)
            self.assertTrue(http_entry["adapter_version"])
            database_result = self.engine.callable_experiment("sample", "budgets", "app.adapter_guard:database")
            self.assertTrue(any(item["operation"] == "commit" and item["status"] == "completed"
                                for item in database_result["side_effects"]["external_operations"]))
            with self.assertRaisesRegex(ThreadError, "cannot pass through and fail"):
                self.engine.configure_experiment("sample", "budgets", {
                    "adapters": {"http": [{"url_contains": "service.test", "pass_through": True, "error": "timeout"}]},
                })
            with self.assertRaisesRegex(ThreadError, "budgets.max_cases"):
                self.engine.configure_experiment("sample", "budgets", {"budgets": {"max_cases": 0}})
            self.engine.configure_experiment("sample", "budgets", {"budgets": {"max_cases": 3}})
            cases = [{"label": f"case {index}", "args": [index]} for index in range(10)]
            limited = self.engine.explore_experiment("sample", "budgets", "app.adapter_guard:choose", cases)
            self.assertEqual(len(limited["results"]), 3)
            self.assertEqual(limited["budgets"]["requested_cases"], 10)
            self.assertTrue(limited["budgets"]["cases_truncated"])
            self.assertEqual(limited["budgets"]["stopped_reason"], "case_limit")
            retained = self.engine.explore_experiment("sample", "budgets", "app.adapter_guard:choose", [
                {"label": "failing", "args": [-1]}, {"label": "working", "args": [1]},
            ])
            self.assertTrue(any("exception" in item["reasons"] and item["case"]["args"] == [-1]
                                for item in retained["retained"]))
            self.assertTrue(any("distinct behaviour" in item["reasons"] for item in retained["retained"]))
            with self.assertRaisesRegex(ThreadError, "Input generator syntax error"):
                self.engine.configure_experiment("sample", "budgets", {"input_generators": [{"expression": "("}]})
            self.engine.configure_experiment("sample", "budgets", {
                "input_generators": [{"expression": "[{'args': ['first']}, {'args': ['second']}]"}],
            })
            generated = self.engine.explore_experiment("sample", "budgets", "app.adapter_guard:accept")
            generator_case = next(item["case"] for item in generated["results"] if item["case"]["label"] == "generator 1")
            self.assertEqual(generator_case["execution"], "explicit experiment execution")
            self.assertGreater(generated["budgets"]["memory_peak_bytes"], 0)
            self.assertIn("retained", generated)

    def test_profiles_and_investigations_are_project_scoped(self) -> None:
        profile = self.engine.save_launch_profile("sample", "check", [sys.executable, "-c", "print('ok')"])
        self.assertEqual(profile["command"][-1], "print('ok')")
        investigation = self.engine.save_investigation("sample", "first look", {"notes": "follow the route"})
        self.assertEqual(investigation["content"]["notes"], [{"text": "follow the route"}])
        self.assertEqual(self.engine.launch_profiles("sample")[0]["name"], "check")
        self.assertEqual(self.engine.investigations("sample")[0]["name"], "first look")

    def test_exports_share_one_version_envelope_across_formats(self) -> None:
        from threadtrace.exports import report_bundle
        from threadtrace.formats import EXPORT_FORMAT, EXPORT_SCHEMA_VERSION

        bundle = report_bundle(self.engine, self.queries, "sample")
        scan = self.engine.latest_scan("sample")
        self.assertEqual(bundle["format"], EXPORT_FORMAT)
        self.assertEqual(bundle["export_version"], EXPORT_SCHEMA_VERSION)
        self.assertEqual(bundle["payload_type"], "report")
        self.assertEqual(bundle["scan_id"], scan["scan_id"])
        self.assertTrue(bundle["source"]["files"]["app/main.py"])
        self.assertTrue(bundle["source"]["digest"])
        self.assertTrue(bundle["capabilities"])
        self.assertIn("overview", bundle["payload"])
        self.assertIn("staleness", bundle["payload"])

        database = str(self.store.path)
        for output_format in ("json", "markdown", "csv", "html", "mermaid", "dot"):
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["export", "sample", "--database", database, "--format", output_format])
            self.assertEqual(code, 0, errors.getvalue())
            self.assertIn("threadtrace-export", output.getvalue().lower())
            self.assertNotIn("<script>", output.getvalue())
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            main(["export", "sample", "--database", database])
        self.assertEqual(json.loads(output.getvalue())["payload"]["overview"]["scan_id"], scan["scan_id"])

        destination = Path(self.temp.name) / "bundle.html"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = main(["export", "sample", "--database", database, "--format", "html", "--output", str(destination)])
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertIn("&quot;", destination.read_text())

    def test_investigations_capture_resume_export_and_import_references(self) -> None:
        from threadtrace.formats import EXPORT_SCHEMA_VERSION

        investigation = self.engine.save_investigation("sample", "route", {
            "notes": "start at the route",
            "files": [{"path": "app/api.py"}],
            "symbols": [{"path": "app/api.py", "line": 6, "qualified_name": "app.api.list_items"}],
            "hypotheses": [{"text": "limit is unused"}],
        })
        content = investigation["content"]
        self.assertEqual(content["notes"], [{"text": "start at the route"}])
        self.assertEqual(content["bookmarks"], [])
        symbol_snapshot = next(item for item in content["source"] if item["line"] == 6)
        self.assertIn("def list_items", symbol_snapshot["snippet"])
        self.assertEqual(len(symbol_snapshot["sha256"]), 64)

        resumed = self.engine.resume_investigation("sample", "route")
        self.assertEqual(resumed["warnings"], [])
        self.assertEqual({reference["status"] for reference in resumed["references"]}, {"unchanged"})

        export_path = Path(self.temp.name) / "route-investigation.json"
        exported = self.engine.export_investigation("sample", "route", str(export_path))
        self.assertEqual(exported["payload_type"], "investigation")
        self.assertEqual(exported["payload"]["name"], "route")
        self.assertTrue(exported["source"]["files"]["app/api.py"])
        self.assertTrue(exported["limitations"])
        self.assertEqual(stat.S_IMODE(export_path.stat().st_mode), 0o600)
        self.assertEqual(json.loads(export_path.read_text())["payload"]["content"]["hypotheses"],
                         [{"text": "limit is unused"}])

        newer = Path(self.temp.name) / "newer-investigation.json"
        newer.write_text(json.dumps({**json.loads(export_path.read_text()), "export_version": EXPORT_SCHEMA_VERSION + 1}))
        with self.assertRaisesRegex(ThreadError, "newer"):
            self.engine.import_investigation("sample", str(newer))
        with self.assertRaisesRegex(ThreadError, "not a ThreadTrace export"):
            self.engine.import_investigation("sample", str(self.root / "app" / "main.py"))

        (self.root / "app" / "api.py").write_text("def list_items(limit: int = 10):\n    return limit\n")
        self.engine.scan("sample")
        imported = self.engine.import_investigation("sample", str(export_path), "route imported")
        self.assertEqual(imported["name"], "route imported")
        self.assertTrue(any("changed since" in warning for warning in imported["warnings"]))
        kept = [item for item in imported["investigation"]["content"]["source"] if item["line"] == 6]
        self.assertEqual([item["sha256"] for item in kept], [symbol_snapshot["sha256"]])
        for section in ("notes", "symbols", "hypotheses"):
            self.assertEqual(imported["investigation"]["content"][section], content[section])

    def test_investigation_cli_and_read_only_python_api(self) -> None:
        from threadtrace import HybridHarness, ThreadTrace

        database = str(self.store.path)
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["investigation", "save", "sample", "cli notes", "--database", database,
                         "--content", '{"notes": "follow the route"}'])
        self.assertEqual(code, 0, errors.getvalue())
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(["investigation", "resume", "sample", "cli notes", "--database", database]), 0)
        path = Path(self.temp.name) / "cli-investigation.json"
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            self.assertEqual(main(["investigation", "export", "sample", "cli notes", "--database", database,
                                   "--output", str(path)]), 0)
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            self.assertEqual(main(["investigation", "import", "sample", str(path), "--name", "imported notes",
                                   "--database", database, "--format", "json"]), 0)
        self.assertEqual(json.loads(output.getvalue())["name"], "imported notes")

        with ThreadTrace(self.store.path) as api:
            self.assertEqual(api.project("sample")["name"], "sample")
            self.assertTrue(api.scans("sample"))
            self.assertEqual(api.latest_scan("sample")["scan_id"], self.engine.latest_scan("sample")["scan_id"])
            self.assertTrue(api.query("functions", "sample"))
            self.assertEqual(api.investigation("sample", "cli notes")["name"], "cli notes")
            self.assertEqual(api.export("sample")["payload_type"], "report")
            with self.assertRaisesRegex(ThreadError, "not a read-only query"):
                api.query("scan", "sample")
            self.assertFalse(hasattr(api, "run_experiment"))
        self.assertEqual(HybridHarness.__name__, "HybridHarness")

    def test_saved_profile_executes_interpreter_environment_and_working_directory(self) -> None:
        environment_dir = Path(self.temp.name) / "runtime env with spaces"
        created = subprocess.run(
            [sys.executable, "-m", "venv", str(environment_dir)], capture_output=True, text=True, timeout=120,
        )
        if created.returncode != 0:
            self.skipTest(f"Could not create a virtual environment: {created.stderr}")
        interpreter = environment_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not interpreter.exists():
            self.skipTest("The created virtual environment has no Python executable.")
        (self.root / "app" / "profile_runner.py").write_text(
            "import json\n"
            "import os\n"
            "import sys\n\n"
            "print(json.dumps({\n"
            "    'prefix': sys.prefix,\n"
            "    'cwd': os.getcwd(),\n"
            "    'set': os.environ.get('PROFILE_SET'),\n"
            "    'removed': os.environ.get('PROFILE_REMOVE', 'missing'),\n"
            "}))\n"
        )
        with patch.dict(os.environ, {"PROFILE_REMOVE": "inherited"}):
            profile = self.engine.save_launch_profile(
                "sample", "venv", ["python", "profile_runner.py"], interpreter=str(interpreter),
                cwd_path="app", environment={"PROFILE_SET": "visible", "PROFILE_REMOVE": None},
            )
            self.assertIsNone(profile["environment"]["PROFILE_REMOVE"])
            result = self.engine.run_launch_profile("sample", "venv", timeout=10, output_cap=10_000)

        self.assertEqual(result["exit_code"], 0, result["stderr"])
        observed = json.loads(result["stdout"])
        self.assertEqual(Path(observed["prefix"]).resolve(), environment_dir.resolve())
        self.assertEqual(Path(observed["cwd"]).resolve(), (self.root / "app").resolve())
        self.assertEqual(observed["set"], "visible")
        self.assertEqual(observed["removed"], "missing")

        launch = result["launch"]
        self.assertEqual(launch["requested_command"][0], str(interpreter))
        self.assertEqual(Path(launch["resolved_executable"]), interpreter)
        self.assertEqual(launch["arguments"], ["profile_runner.py"])
        self.assertEqual(launch["cwd"], str((self.root / "app").resolve()))
        self.assertRegex(launch["interpreter_version"], r"^\d+\.\d+")
        self.assertEqual(launch["environment_overridden"], ["PROFILE_SET"])
        self.assertEqual(launch["environment_removed"], ["PROFILE_REMOVE"])
        self.assertIn("PATH", launch["environment_inherited"])
        self.assertEqual(launch["instrumentation_collisions"], [])

        stored = next(row for row in self.queries.traces("sample") if row["trace_id"] == result["trace_id"])
        self.assertEqual(stored["launch"]["resolved_executable"], launch["resolved_executable"])

    def test_profile_launch_rejects_ambiguous_commands_secrets_and_missing_executables(self) -> None:
        with self.assertRaisesRegex(ThreadError, "Python command"):
            self.engine.save_launch_profile("sample", "ambiguous", ["bash", "run.sh"], interpreter=sys.executable)
        with self.assertRaisesRegex(ThreadError, "Secret"):
            self.engine.save_launch_profile(
                "sample", "secret", [sys.executable, "-c", "pass"], environment={"API_TOKEN": "hidden"}
            )

        missing = Path(self.temp.name) / "missing" / "python"
        self.engine.save_launch_profile("sample", "missing", ["python", "-c", "pass"], interpreter=str(missing))
        with self.assertRaisesRegex(ThreadError, "not found|does not exist"):
            self.engine.run_launch_profile("sample", "missing", timeout=5, output_cap=1_000)

        output = StringIO()
        with redirect_stdout(output):
            code = main([
                "profile", "save", "sample", "cli", "--env", "PROFILE_SET=visible",
                "--unset-env", "PROFILE_REMOVE", "--database", str(self.store.path), "--format", "json",
                "--", "python", "-c", "pass",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(output.getvalue())["environment"],
            {"PROFILE_SET": "visible", "PROFILE_REMOVE": None},
        )
        with redirect_stderr(StringIO()):
            code = main([
                "profile", "save", "sample", "conflict", "--env", "CONFLICT=1",
                "--unset-env", "CONFLICT", "--database", str(self.store.path),
                "--", "python", "-c", "pass",
            ])
        self.assertEqual(code, 2)

        if os.name != "posix":
            return
        first = Path(self.temp.name) / "first-path"
        first.mkdir()
        runner = first / "runner"
        runner.write_text("#!/bin/sh\necho first\n")
        runner.chmod(0o755)
        self.engine.save_launch_profile("sample", "path", ["runner"], environment={"PATH": str(first)})
        result = self.engine.run_launch_profile("sample", "path", timeout=10, output_cap=1_000)
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertEqual(result["stdout"], "first\n")
        self.assertEqual(result["launch"]["resolved_executable"], str(runner))

    def test_experiment_diff_reports_every_workspace_change(self) -> None:
        (self.root / "app" / "obsolete.py").write_text("def obsolete():\n    return 0\n")
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            experiment = self.engine.create_experiment("sample", "changes")
            workspace = Path(experiment["workspace_path"])
            self.engine.patch_experiment("sample", "changes", "app/helpers.py", 2, 2, "    return value.upper()")
            (workspace / "app" / "created.py").write_text("def added():\n    return 1\n")
            (workspace / "app" / "obsolete.py").unlink()
            moved = (workspace / "app" / "api.py").read_bytes()
            (workspace / "app" / "api.py").unlink()
            (workspace / "app" / "moved_api.py").write_bytes(moved)
            (workspace / "data.bin").write_bytes(b"\x00\x01binary")
            report = self.engine.experiment_changes("sample", "changes")
            text = self.engine.experiment_diff("sample", "changes")
            exported = self.engine.export_experiment("sample", "changes")
            output = StringIO()
            with redirect_stdout(output):
                code = main([
                    "experiment", "diff", "sample", "changes", "--database", str(self.store.path), "--format", "json",
                ])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["summary"]["renamed"], 1)
        self.assertEqual(exported["payload"]["workspace_changes"]["summary"]["renamed"], 1)
        self.assertEqual(len(exported["payload"]["patches"]), 1)
        by_kind = {change["kind"]: change for change in report["changes"]}
        self.assertEqual(
            {"modified", "created", "deleted", "renamed", "binary"}.issubset(by_kind),
            True,
            sorted(by_kind),
        )
        self.assertEqual(by_kind["modified"]["path"], "app/helpers.py")
        self.assertNotEqual(by_kind["modified"]["before"]["sha256"], by_kind["modified"]["after"]["sha256"])
        self.assertIn("value.upper()", by_kind["modified"]["preview"])
        self.assertEqual(by_kind["created"]["path"], "app/created.py")
        self.assertEqual(by_kind["deleted"]["path"], "app/obsolete.py")
        self.assertEqual(by_kind["renamed"]["old_path"], "app/api.py")
        self.assertEqual(by_kind["renamed"]["path"], "app/moved_api.py")
        self.assertEqual(by_kind["renamed"]["before"]["sha256"], by_kind["renamed"]["after"]["sha256"])
        self.assertEqual(by_kind["binary"]["path"], "data.bin")
        self.assertTrue(all(len(change.get("preview") or "") <= 20_000 for change in report["changes"]))
        for marker in ("modified", "created", "deleted", "renamed", "binary"):
            self.assertIn(marker, text)

    def test_experiment_patch_rejects_invalid_python_and_records_hashes(self) -> None:
        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            experiment = self.engine.create_experiment("sample", "invalid-patch")
            workspace = Path(experiment["workspace_path"])
            target = workspace / "app" / "helpers.py"
            before = target.read_bytes()
            with self.assertRaisesRegex(ThreadError, "invalid Python"):
                self.engine.patch_experiment("sample", "invalid-patch", "app/helpers.py", 1, 2, "def broken(")
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(self.engine.experiment_patches("sample", "invalid-patch"), [])
            with patch("threadtrace.hybrid.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaisesRegex(ThreadError, "interrupted"):
                    self.engine.patch_experiment("sample", "invalid-patch", "app/helpers.py", 2, 2, "    return value.upper()")
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(list(target.parent.glob(".threadtrace-patch-*")), [])
            self.assertEqual(self.engine.experiment_patches("sample", "invalid-patch"), [])

            patched = self.engine.patch_experiment("sample", "invalid-patch", "app/helpers.py", 2, 2, "    return value.upper()")
            history = self.engine.experiment_patches("sample", "invalid-patch")

        self.assertNotEqual(patched["patch"]["before_sha256"], patched["patch"]["after_sha256"])
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["relative_path"], "app/helpers.py")
        self.assertEqual(history[0]["before_sha256"], patched["patch"]["before_sha256"])
        self.assertEqual(history[0]["after_sha256"], patched["patch"]["after_sha256"])

    def test_project_removal_cleans_or_preserves_experiment_workspaces_safely(self) -> None:
        application = Path(self.temp.name) / "application"
        with patch("threadtrace.engine.application_directory", return_value=application):
            first = self.engine.create_experiment("sample", "kept")
            preserved = self.engine.remove_project("sample", keep_experiments=True)
            self.assertTrue(Path(first["workspace_path"]).exists())
            self.assertEqual(preserved["experiments_preserved"][0]["name"], "kept")

            self.engine.add_project(str(self.root), "sample")
            retried = self.engine.create_experiment("sample", "retry")
            with patch("threadtrace.engine.shutil.rmtree", side_effect=OSError("busy")):
                with self.assertRaisesRegex(ThreadError, "busy|remain"):
                    self.engine.remove_project("sample")
            self.engine.get_project("sample")
            self.engine._experiment("sample", "retry")
            removed = self.engine.remove_project("sample")
            self.assertFalse(Path(retried["workspace_path"]).exists())
            self.assertEqual(removed["experiments_removed"][0]["name"], "retry")

            self.engine.add_project(str(self.root), "sample")
            linked = self.engine.create_experiment("sample", "linked")
            workspace = Path(linked["workspace_path"])
            outside = Path(self.temp.name) / "outside"
            outside.mkdir()
            marker = outside / "keep.txt"
            marker.write_text("untouched")
            shutil.rmtree(workspace)
            workspace.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(ThreadError, "remain|workspace"):
                self.engine.remove_project("sample")
            self.assertTrue(marker.exists())
            self.engine.get_project("sample")

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
        self.assertEqual(resources[0]["name"], "TOKEN")
        self.assertEqual(resources[0]["evidence_class"], "statically resolved")
        files = self.queries.resources("sample", "file")
        self.assertIn("settings.json", {item["name"] for item in files})
        data = self.queries.dataflow("sample", "app.main.main")
        self.assertIn(("value", "value"), {(item["source_name"], item["target_name"]) for item in data["edges"]})
        path = self.queries.path("sample", "sample")
        self.assertTrue(any(step["target_name"] == "app.helpers.format_name" for step in path["steps"]))
        sliced = self.queries.slice("sample", "app.main.main", "value")
        self.assertTrue(sliced["events"])
        graph = self.queries.control_flow("sample", "app.main.main")
        self.assertEqual(graph["nodes"][0]["kind"], "start")

    def test_complexity_separates_cyclomatic_count_from_bounded_costs(self) -> None:
        (self.root / "app" / "metrics.py").write_text(
            "def constant(items):\n"
            "    return items[0]\n\n"
            "def linear(items):\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        total += item\n"
            "    return total\n\n"
            "def quadratic(rows):\n"
            "    for row in rows:\n"
            "        for cell in row:\n"
            "            print(cell)\n\n"
            "def sorted_rows(rows):\n"
            "    return sorted(rows)\n\n"
            "def recursive(n):\n"
            "    return recursive(n - 1) if n else 0\n\n"
            "def caller(items):\n"
            "    return linear(items)\n"
        )
        self.engine.scan("sample")
        constant = self.queries.complexity("sample", "app.metrics.constant")
        linear = self.queries.complexity("sample", "app.metrics.linear")
        quadratic = self.queries.complexity("sample", "app.metrics.quadratic")
        sorted_rows = self.queries.complexity("sample", "app.metrics.sorted_rows")
        recursive = self.queries.complexity("sample", "app.metrics.recursive")
        caller = self.queries.complexity("sample", "app.metrics.caller")
        self.assertEqual(constant["time"]["bound"], "O(1)")
        self.assertEqual(linear["time"]["bound"], "O(n)")
        self.assertEqual(quadratic["time"]["bound"], "O(n^2)")
        self.assertEqual(sorted_rows["time"]["bound"], "O(n log n)")
        self.assertEqual(sorted_rows["space"]["bound"], "O(n)")
        self.assertEqual(recursive["time"]["bound"], "unknown")
        self.assertEqual(caller["time"]["bound"], "O(n)")
        self.assertTrue(caller["called_functions"])
        self.assertEqual(linear["cyclomatic"], self.queries.symbol("sample", "app.metrics.linear")[0]["complexity"])
        self.assertTrue(linear["time"]["assumptions"])

    def test_advanced_facts_and_expanded_resource_classes_are_evidence_backed(self) -> None:
        (self.root / "app" / "advanced_facts.py").write_text(
            "import asyncio\nimport ctypes\nimport json\nimport sys\nfrom pathlib import Path\nimport requests\n\n"
            "class Descriptor:\n"
            "    def __get__(self, instance, owner):\n"
            "        return 1\n\n"
            "class Meta(type):\n"
            "    pass\n\n"
            "class Model(metaclass=Meta):\n"
            "    value = Descriptor()\n"
            "    __slots__ = ('name',)\n\n"
            "    @property\n"
            "    def name(self):\n"
            "        return self._name\n\n"
            "    @name.setter\n"
            "    def name(self, value):\n"
            "        self._name = value\n\n"
            "def outer(limit):\n"
            "    def inner(value):\n"
            "        return value + limit\n"
            "    return inner\n\n"
            "async def fetch(lock):\n"
            "    async with lock:\n"
            "        return 1\n\n"
            "async def stream(items):\n"
            "    for item in items:\n"
            "        yield item\n\n"
            "def schedule(work):\n"
            "    asyncio.create_task(work())\n\n"
            "def dynamic(obj):\n"
            "    setattr(obj, 'value', 1)\n\n"
            "def native():\n"
            "    ctypes.CDLL('native.so')\n\n"
            "def pattern(rows):\n"
            "    selected = [row for row in rows if row]\n"
            "    cleaned = []\n"
            "    for row in selected:\n"
            "        cleaned.append(row)\n"
            "    cleaned.sort()\n"
            "    return cleaned\n\n"
            "def load_resources():\n"
            "    Path('config/settings.toml').read_text()\n"
            "    Path('data/records.csv').read_text()\n"
            "    Path('notes.txt').read_text()\n"
            "    Path('image.png').read_bytes()\n"
            "    Path('templates/index.html').read_text()\n"
            "    json.loads('{}')\n"
            "    requests.get('https://example.test')\n"
            "    return sys.version\n"
        )
        self.engine.scan("sample")
        model = self.queries.advanced("sample", "app.advanced_facts.Model")
        outer = self.queries.advanced("sample", "app.advanced_facts.outer")
        fetch = self.queries.advanced("sample", "app.advanced_facts.fetch")
        stream = self.queries.advanced("sample", "app.advanced_facts.stream")
        schedule = self.queries.advanced("sample", "app.advanced_facts.schedule")
        dynamic = self.queries.advanced("sample", "app.advanced_facts.dynamic")
        native = self.queries.advanced("sample", "app.advanced_facts.native")
        descriptor = self.queries.advanced("sample", "app.advanced_facts.Descriptor")
        pattern = self.queries.advanced("sample", "app.advanced_facts.pattern")
        self.assertEqual(model["metaclass"], "Meta")
        self.assertIn("name", model["properties"])
        self.assertIn("__slots__", model["dynamic_members"])
        self.assertEqual(outer["closures"][0]["free_names"], ["limit"])
        self.assertTrue(fetch["async"])
        self.assertTrue(fetch["context_managers"])
        self.assertTrue(stream["async_generator"])
        self.assertTrue(schedule["concurrency"])
        self.assertIn("setattr", dynamic["dynamic_attributes"])
        self.assertTrue(native["native_boundaries"])
        self.assertIn("__get__", descriptor["descriptors"])
        self.assertTrue({"Collection transformation", "Accumulation loop", "Sorting"}.issubset(
            {item["name"] for item in pattern["algorithms"]}
        ))
        self.assertTrue(all(item["confidence"] == "heuristic" for item in pattern["algorithms"]))
        self.assertTrue(all(item["evidence_class"] == "heuristically predicted" for item in pattern["algorithms"]))
        self.assertTrue(all(item["evidence_record"] for item in outer["closures"]))

        resources = self.queries.resources("sample")
        kinds = {item["kind"] for item in resources}
        self.assertTrue({"configuration", "structured-data", "text", "template", "media", "serialization", "service", "interpreter"}.issubset(kinds))
        self.assertIn("config/settings.toml", {item["name"] for item in self.queries.resources("sample", "configuration")})
        self.assertIn("settings.json", {item["name"] for item in self.queries.resources("sample", "file")})
        self.assertTrue(any(item["evidence"].get("path_kind") for item in resources))

    def test_impact_traverses_upstream_downstream_imports_inheritance_and_references(self) -> None:
        (self.root / "app" / "impact.py").write_text(
            "from .helpers import format_name\n\n"
            "class Base:\n"
            "    pass\n\n"
            "class Child(Base):\n"
            "    pass\n\n"
            "def target(value):\n"
            "    return format_name(value)\n\n"
            "def caller():\n"
            "    return target('x')\n\n"
            "def observe():\n"
            "    return target\n"
        )
        self.engine.scan("sample")

        impact = self.queries.impact("sample", "app.impact.target")
        self.assertEqual(impact["direction"], "both")
        self.assertTrue(any(edge["source_name"] == "app.impact.caller" for edge in impact["upstream"]))
        self.assertTrue(all(edge["direction"] == "upstream" for edge in impact["upstream"]))
        self.assertTrue(all(edge["direction"] == "downstream" for edge in impact["downstream"]))
        self.assertIn("app.impact.observe", {item["scope_name"] for item in impact["references"]})

        base = self.queries.impact("sample", "app.impact.Base")
        self.assertIn("app.impact.Child", {item["qualified_name"] for item in base["inheritance_users"]})

        helper = self.queries.impact("sample", "app.helpers.format_name")
        self.assertIn("app.main", helper["import_dependants"])
        self.assertIn("app.impact", helper["import_dependants"])

        entry = self.queries.impact("sample", "app.main.main")
        self.assertIn("sample", {item["name"] for item in entry["entry_points"]})
        resources = self.queries.impact("sample", "app.resources.load_token")["resources"]
        self.assertTrue({"TOKEN", "settings.json"}.issubset({item["name"] for item in resources}))
        self.assertEqual(self.queries.impact("sample", "app.impact.target", direction="downstream")["upstream"], [])
        self.assertTrue(impact["budget"])

    def test_control_flow_models_loops_exceptions_finally_match_and_generators(self) -> None:
        (self.root / "app" / "branches.py").write_text(
            "def pipeline(items, key):\n"
            "    total = 0\n"
            "    for item in items:\n"
            "        if not item:\n"
            "            continue\n"
            "        try:\n"
            "            total += int(item)\n"
            "        except ValueError:\n"
            "            total = 0\n"
            "            raise\n"
            "        else:\n"
            "            total += 1\n"
            "        finally:\n"
            "            key = str(key)\n"
            "        if total > 10:\n"
            "            break\n"
            "    else:\n"
            "        total = -1\n"
            "    match total:\n"
            "        case 1:\n"
            "            return total\n"
            "        case _:\n"
            "            pass\n"
            "    yield total\n\n"
            "async def fetch(lock):\n"
            "    async with lock:\n"
            "        return 1\n"
        )
        self.engine.scan("sample")
        graph = self.queries.control_flow("sample", "app.branches.pipeline")
        kinds = {edge["kind"] for edge in graph["edges"]}
        self.assertTrue({"break", "continue", "exception", "finally", "loop-else", "return", "raise", "case", "exit"}.issubset(kinds))
        self.assertTrue({"start", "end", "yield"}.issubset({node["kind"] for node in graph["nodes"]}))
        self.assertFalse(graph["truncated"])
        self.assertEqual(graph["budget"], {"nodes": 400, "edges": 1200})
        async_graph = self.queries.control_flow("sample", "app.branches.fetch")
        self.assertIn("with", {node["kind"] for node in async_graph["nodes"]})
        self.assertIn("return", {edge["kind"] for edge in async_graph["edges"]})

        (self.root / "app" / "wide.py").write_text(
            "def wide():\n" + "".join(f"    value_{index} = {index}\n" for index in range(450))
        )
        self.engine.scan("sample")
        bounded = self.queries.control_flow("sample", "app.wide.wide")
        self.assertTrue(bounded["truncated"])
        self.assertLessEqual(len(bounded["nodes"]), bounded["budget"]["nodes"] + 1)
        self.assertIn("end", {node["kind"] for node in bounded["nodes"]})

    def test_slices_use_scope_boundaries_mutation_and_resolved_interprocedural_edges(self) -> None:
        (self.root / "app" / "slices.py").write_text(
            "from pathlib import Path\n\n"
            "def source(value):\n"
            "    token = value.strip()\n"
            "    return token\n\n"
            "def send(value):\n"
            "    token = source(value)\n"
            "    payload = {'token': token}\n"
            "    Path('out.txt').write_text(token)\n"
            "    return payload\n\n"
            "def shadow():\n"
            "    token = 'unrelated'\n"
            "    return token\n\n"
            "def mutate(record, values):\n"
            "    record.name = values[0]\n"
            "    values[0] = record.name\n"
            "    values.append(record.name)\n"
            "    return record.name\n"
        )
        self.engine.scan("sample")
        backward = self.queries.slice("sample", "app.slices.send", "token", "backward")
        self.assertEqual(backward["direction"], "backward")
        self.assertTrue(backward["definitions"])
        self.assertEqual({item["scope"] for item in backward["definitions"]}, {"app.slices.send"})
        self.assertNotIn("shadow", " ".join(item["scope"] for item in backward["definitions"]))
        self.assertTrue(any(edge["kind"] == "return_assignment" for edge in backward["data_edges"]))
        forward = self.queries.slice("sample", "app.slices.send", "token", "forward")
        self.assertTrue(forward["uses"])
        self.assertTrue(all(item["scope"] == "app.slices.send" for item in forward["uses"]))

        values = self.queries.slice("sample", "app.slices.mutate", "values")
        self.assertIn("mutation", {item["kind"] for item in values["definitions"]})
        self.assertIn("mutation", {item["kind"] for item in values["boundaries"]})
        attribute = self.queries.slice("sample", "app.slices.mutate", "record.name")
        self.assertTrue(attribute["definitions"])
        self.assertTrue(any("record.name" in item["name"] for item in attribute["definitions"]))

        dataflow = self.queries.dataflow("sample", "app.slices.send", "file")
        self.assertTrue(dataflow["resource_flows"])
        self.assertTrue(any("token" in flow["source_names"] for flow in dataflow["resource_flows"]))
        self.assertTrue(any(flow["data_edges"] for flow in dataflow["resource_flows"]))

    def test_file_map_and_friendly_findings(self) -> None:
        self.assertIn("route", {item["kind"] for item in self.queries.entrypoints("sample")})
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
        from threadtrace.cli import _csv, _graph_export, _html
        self.assertIn("flowchart LR", _graph_export(architecture, "mermaid"))
        self.assertIn("digraph thread", _graph_export(architecture, "dot"))
        self.assertIn("qualified_name", _csv(self.queries.functions("sample")))
        self.assertIn("<!doctype html>", _html(architecture))

    def test_cli_format_capabilities_reject_unsupported_output(self) -> None:
        database = str(self.store.path)
        for output_format in ("json", "markdown", "csv", "html"):
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["overview", "sample", "--database", database, "--format", output_format])
            self.assertEqual(code, 0, errors.getvalue())
            self.assertTrue(output.getvalue().strip())

        def rejected(arguments: list[str]) -> str:
            output, errors = StringIO(), StringIO()
            with self.assertRaises(SystemExit) as raised:
                with redirect_stdout(output), redirect_stderr(errors):
                    main(arguments)
            self.assertEqual(raised.exception.code, 2)
            self.assertEqual(output.getvalue(), "")
            return errors.getvalue()

        for command in (["functions", "sample"], ["diagnostics", "sample"], ["map", "sample"]):
            errors = rejected(command + ["--database", database, "--format", "mermaid"])
            self.assertIn("mermaid", errors)

        for output_format in ("mermaid", "dot"):
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                code = main(["architecture", "sample", "--database", database, "--format", output_format])
            self.assertEqual(code, 0, errors.getvalue())
            self.assertIn("flowchart" if output_format == "mermaid" else "digraph", output.getvalue())

        errors = rejected(["runtime", "sample", "--database", database, "--format", "json", "--", "python", "-c", "pass"])
        self.assertNotIn("prompt_toolkit", errors)

        with patch("threadtrace.engine.application_directory", return_value=Path(self.temp.name) / "application"):
            self.engine.create_experiment("sample", "format-check")
            rejected(["experiment", "diff", "sample", "format-check", "--database", database, "--format", "markdown"])

    def test_every_cli_format_option_is_capability_checked(self) -> None:
        import argparse

        from threadtrace.cli import STANDARD_FORMATS, _validate_format, build_parser

        def format_commands(parser: argparse.ArgumentParser, path: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
            found = [path] if any("--format" in action.option_strings for action in parser._actions) else []
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    for name, child in action.choices.items():
                        found.extend(format_commands(child, path + (name,)))
            return found

        unrestricted = {"architecture"}
        text_only = {"runtime", "runtime-host", "runtime-listen", "process", "tui", "browser"}
        commands = format_commands(build_parser())
        self.assertTrue(commands)
        for path in commands:
            args = argparse.Namespace(command=path[0], format="text")
            if len(path) > 1:
                setattr(args, f"{path[0]}_command", path[1])
            allowed = STANDARD_FORMATS | {"mermaid", "dot"} if path[0] in unrestricted else STANDARD_FORMATS
            if path[0] == "export":
                allowed = {"json", "markdown", "csv", "html", "mermaid", "dot"}
            elif path == ("experiment", "diff"):
                allowed = {"text", "json"}
            elif path[0] in text_only:
                allowed = {"text"}
            for output_format in STANDARD_FORMATS | {"mermaid", "dot"}:
                args.format = output_format
                with self.subTest(command=" ".join(path), output_format=output_format):
                    if output_format in allowed:
                        _validate_format(args)
                    else:
                        with self.assertRaisesRegex(ThreadError, "does not support --format"):
                            _validate_format(args)

    def test_graph_exports_escape_project_controlled_labels(self) -> None:
        from threadtrace.cli import _graph_export

        name = 'evil "name"\n<tag>|pipe\\slash'
        value = {
            "components": [{"name": name, "role": 'role "quoted"'}, {"name": "plain", "role": "role"}],
            "relationships": [{"source_module": name, "target_module": "plain", "kind": 'call" | evil'},
                              {"source_module": "missing", "target_module": "plain", "kind": "call"}],
        }
        mermaid = _graph_export(value, "mermaid")
        dot = _graph_export(value, "dot")
        self.assertIn("#quot;", mermaid)
        self.assertIn("#124;", mermaid)
        self.assertIn("#lt;", mermaid)
        self.assertNotIn('evil "name"', mermaid)
        self.assertNotIn("missing", mermaid)
        self.assertIn('\\"', dot)
        self.assertIn("\\n<tag>", dot)
        self.assertNotIn("\n<tag>", dot)
        self.assertIn("\\\\slash", dot)
        self.assertNotIn('evil "name"', dot)
        self.assertNotIn("missing", dot)

    def test_cli_database_failures_exit_cleanly_without_traceback(self) -> None:
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["project", "list", "--database", str(self.root)])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("Error:", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

        newer = Path(self.temp.name) / "newer.sqlite3"
        connection = sqlite3.connect(newer)
        connection.execute("PRAGMA user_version = 999")
        connection.commit()
        connection.close()
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["project", "list", "--database", str(newer)])
        self.assertEqual(code, 2)
        self.assertIn("newer version", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

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
        self.assertEqual(comparison["evidence_record"]["class"], "statically resolved")

    def test_incremental_scan_reuses_unchanged_parse_facts(self) -> None:
        scan = self.engine.scan("sample")
        summary = __import__("json").loads(scan["summary_json"])
        self.assertGreater(summary["reused_files"], 0)

    def test_runtime_event_schema_and_instrumentation_selection_are_bounded(self) -> None:
        from threadtrace.runtime.schema import EVENT_SCHEMA_VERSION, instrumentation_plan

        (self.root / "schema_runner.py").write_text(
            "def work(value):\n"
            "    return value + 1\n\n"
            "print(work(1))\n"
        )
        result = self.engine.trace("sample", [sys.executable, "schema_runner.py"], timeout=5, output_cap=10_000,
                                   instrumentation="auto")
        self.assertEqual(result["instrumentation"]["selected"], ["settrace"])
        self.assertEqual(result["recording_scope"], "project")
        self.assertTrue(result["events"])
        self.assertTrue(all(event["event_schema_version"] == EVENT_SCHEMA_VERSION for event in result["events"]))
        self.assertTrue(all(event["kind"] in {"call", "line", "return", "exception", "thread", "audit", "process"}
                            for event in result["events"]))
        self.assertIn("profile", instrumentation_plan("monitoring")["mechanisms"])
        self.assertTrue(instrumentation_plan("monitoring")["fallback"])
        self.assertEqual(instrumentation_plan("profile")["selected"], ["profile"])

        profile = self.engine.trace("sample", [sys.executable, "schema_runner.py"], timeout=5, output_cap=10_000,
                                    instrumentation="profile")
        self.assertEqual(profile["instrumentation"]["selected"], ["profile"])
        self.assertTrue(profile["events"])
        self.assertNotIn("line", {event["kind"] for event in profile["events"]})

    def test_trace_profile_and_coverage_py_import_keep_provenance(self) -> None:
        (self.root / "profile_runner.py").write_text(
            "def transform(value):\n"
            "    result = value + 1\n"
            "    return result\n\n"
            "transform(4)\n"
        )
        result = self.engine.trace("sample", [sys.executable, "profile_runner.py"], timeout=5, output_cap=10_000)
        profile = self.queries.trace_profile("sample", result["trace_id"])
        self.assertTrue(profile["functions"])
        self.assertTrue(any(item["name"] == "transform" and item["calls"] >= 1 for item in profile["functions"]))
        self.assertTrue(all("average_seconds" in item for item in profile["functions"]))
        self.assertEqual(profile["memory"]["status"], "observed")
        self.assertIn("tracer overhead", " ".join(profile["limitations"]))
        filtered = self.queries.trace_events("sample", result["trace_id"], kind="line", path_prefix="profile_runner")
        self.assertTrue(filtered)
        self.assertTrue(all(event["event_kind"] == "line" for event in filtered))
        searched = self.queries.trace_events("sample", result["trace_id"], search="transform")
        self.assertTrue(searched)
        self.assertTrue(all("transform" in json.dumps(event) for event in searched))

        database = self.root / ".coverage"
        connection = sqlite3.connect(database)
        connection.executescript(
            "CREATE TABLE file (id INTEGER PRIMARY KEY, path TEXT);"
            "CREATE TABLE line_bits (file_id INTEGER, context_id INTEGER, numbits BLOB);"
        )
        connection.execute("INSERT INTO file VALUES(1, ?)", (str(self.root / "profile_runner.py"),))
        connection.execute("INSERT INTO line_bits VALUES(1, 0, ?)", (bytes([0b110]),))
        connection.commit()
        connection.close()
        coverage = self.queries.coverage("sample", result["trace_id"])
        self.assertEqual(coverage["coverage_py_import"]["status"], "imported")
        self.assertEqual(coverage["coverage_py_import"]["granularity"], "line")
        self.assertEqual(coverage["coverage_py_import"]["files"]["profile_runner.py"], [2, 3])
        self.assertIn("coverage.py", coverage["provenance"])

    def test_runtime_timeout_can_be_extended_or_gracefully_stopped(self) -> None:
        from threadtrace.runtime.session import RuntimeSession

        (self.root / "timeout_runner.py").write_text("while True:\n    pass\n")
        session = RuntimeSession(self.root, [sys.executable, "timeout_runner.py"], timeout=1, output_cap=1_000)
        try:
            session.step()
            for _ in range(40):
                session.poll()
                if session.events:
                    break
                time.sleep(0.025)
            session.pause()
            session.extend_timeout(5)
            self.assertEqual(session.timeout, 5)
            session.graceful_stop()
            for _ in range(80):
                if session.finished:
                    break
                time.sleep(0.025)
            if not session.finished:
                session.force_stop()
            self.assertIn(session.mode, {"stopping", "stopped"})
        finally:
            session.close()

    def test_explicit_runtime_trace_is_stored_separately(self) -> None:
        (self.root / "runner.py").write_text("def main():\n    return 'ok'\n\nmain()\n")
        result = self.engine.trace("sample", [sys.executable, "runner.py"], timeout=5, output_cap=1_000)
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertTrue(result["events"])
        traces = self.queries.traces("sample")
        self.assertEqual(traces[0]["trace_id"], result["trace_id"])
        self.assertLessEqual(traces[0]["started_at"], traces[0]["finished_at"])
        self.assertEqual(traces[0]["evidence_record"]["class"], "observed at runtime")
        events = self.queries.trace_events("sample", result["trace_id"])
        self.assertEqual(events[0]["evidence_record"]["class"], "observed at runtime")
        self.assertEqual(events[0]["evidence_record"]["run"], result["trace_id"])
        timing = events[0]["evidence"]["details"]
        self.assertIsInstance(timing["wall_time"], float)
        self.assertGreaterEqual(timing["elapsed_seconds"], 0)
        self.assertGreaterEqual(timing["cpu_seconds"], 0)
        bookmark = self.engine.bookmark_trace("sample", result["trace_id"], events[0]["ordinal"], "first call")
        self.assertEqual(bookmark["label"], "first call")
        self.assertEqual(self.engine.trace_bookmarks("sample", result["trace_id"])[0]["ordinal"], events[0]["ordinal"])

    def test_runtime_failure_report_preserves_traceback_evidence(self) -> None:
        (self.root / "failure_runner.py").write_text(
            "def explode():\n"
            "    raise ValueError('broken input')\n\n"
            "explode()\n"
        )
        self.engine.scan("sample")
        result = self.engine.trace("sample", [sys.executable, "failure_runner.py"], timeout=5, output_cap=10_000)
        self.assertEqual(result["exit_code"], 1)
        self.assertEqual(result["failure_report"]["exception"], "ValueError")
        self.assertEqual(result["failure_report"]["terminating_exception"]["type"], "ValueError")
        self.assertEqual(result["failure_report"]["terminating_exception"]["correlation"], "traceback_and_trace")
        self.assertTrue(result["failure_report"]["observed_exceptions"])
        self.assertEqual(result["failure_report"]["location"]["path"], "failure_runner.py")
        self.assertTrue(result["failure_report"]["source_hash"])
        report = self.queries.trace_failure("sample", result["trace_id"])["report"]
        self.assertEqual(report["reason"], "nonzero_exit")
        self.assertIn("ValueError: broken input", report["traceback_excerpt"])
        self.assertEqual(report["call_chain"][-1]["function"], "explode")
        output = StringIO()
        with redirect_stdout(output):
            code = main(["trace-failure", "sample", result["trace_id"], "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["report"]["exception"], "ValueError")
        self.assertEqual(
            json.loads(output.getvalue())["report"]["terminating_exception"]["type"],
            "ValueError",
        )

    def test_runtime_failure_selects_fatal_exception_after_handled_exception(self) -> None:
        (self.root / "handled_then_fatal.py").write_text(
            "def handled():\n"
            "    try:\n"
            "        raise KeyError('handled')\n"
            "    except KeyError:\n"
            "        return\n\n"
            "def fatal():\n"
            "    handled()\n"
            "    raise RuntimeError('terminal failure')\n\n"
            "fatal()\n"
        )
        result = self.engine.trace(
            "sample", [sys.executable, "handled_then_fatal.py"], timeout=5, output_cap=10_000,
        )
        report = result["failure_report"]

        self.assertEqual(report["terminating_exception"]["type"], "RuntimeError")
        self.assertEqual(report["terminating_exception"]["message"], "terminal failure")
        self.assertEqual(report["terminating_exception"]["location"]["function"], "fatal")
        self.assertEqual(report["call_chain"][-1]["function"], "fatal")
        self.assertIn("RuntimeError: terminal failure", report["traceback_excerpt"])
        observed = [item["type"] for item in report["observed_exceptions"]]
        self.assertIn("KeyError", observed)
        self.assertIn("RuntimeError", observed)
        self.assertLess(observed.index("KeyError"), observed.index("RuntimeError"))

    def test_runtime_failure_correlates_nested_propagation_and_reraise(self) -> None:
        fixtures = {
            "nested_failure.py": (
                "def inner():\n"
                "    raise IndexError('deep failure')\n\n"
                "def middle():\n"
                "    inner()\n\n"
                "def outer():\n"
                "    middle()\n\n"
                "outer()\n",
                "IndexError",
                "inner",
            ),
            "reraised_failure.py": (
                "def reraised():\n"
                "    try:\n"
                "        raise LookupError('original failure')\n"
                "    except LookupError:\n"
                "        raise\n\n"
                "reraised()\n",
                "LookupError",
                "reraised",
            ),
        }
        for filename, (source, exception_name, function) in fixtures.items():
            with self.subTest(filename=filename):
                (self.root / filename).write_text(source)
                result = self.engine.trace(
                    "sample", [sys.executable, filename], timeout=5, output_cap=10_000,
                )
                report = result["failure_report"]
                self.assertEqual(report["terminating_exception"]["type"], exception_name)
                self.assertEqual(report["terminating_exception"]["correlation"], "traceback_and_trace")
                self.assertEqual(report["location"]["function"], function)
                self.assertEqual(report["call_chain"][-1]["function"], function)
                self.assertIn(exception_name, report["traceback_excerpt"])

    def test_runtime_failure_reports_control_flow_exceptions_and_timeout(self) -> None:
        fixtures = {
            "system_exit_failure.py": (
                "def stop():\n"
                "    raise SystemExit('planned exit')\n\n"
                "stop()\n",
                "SystemExit",
                "stop",
                "planned exit",
            ),
            "keyboard_failure.py": (
                "def stop():\n"
                "    raise KeyboardInterrupt('manual interrupt')\n\n"
                "stop()\n",
                "KeyboardInterrupt",
                "stop",
                "manual interrupt",
            ),
        }
        for filename, (source, exception_name, function, message) in fixtures.items():
            with self.subTest(filename=filename):
                (self.root / filename).write_text(source)
                result = self.engine.trace(
                    "sample", [sys.executable, filename], timeout=5, output_cap=10_000,
                )
                report = result["failure_report"]
                self.assertEqual(report["terminating_exception"]["type"], exception_name)
                self.assertEqual(report["terminating_exception"]["message"], message)
                self.assertEqual(report["location"]["function"], function)
                self.assertEqual(report["call_chain"][-1]["function"], function)
                self.assertTrue(report["source_hash"])

        (self.root / "timeout_failure.py").write_text("import time\ntime.sleep(5)\n")
        timed_out = self.engine.trace(
            "sample", [sys.executable, "timeout_failure.py"], timeout=1, output_cap=10_000,
        )["failure_report"]
        self.assertEqual(timed_out["reason"], "timeout")
        self.assertEqual(timed_out["terminating_exception"]["type"], "unknown")
        self.assertEqual(timed_out["terminating_exception"]["correlation"], "unknown")
        self.assertIsNone(timed_out["location"])

    def test_runtime_failure_preserves_unknown_when_evidence_cannot_be_reconciled(self) -> None:
        from threadtrace.runtime.failure import failure_report

        path = self.root / "unreconciled.py"
        path.write_text("raise ValueError('fatal')\n")
        report = failure_report(self.root, {
            "exit_code": 1,
            "timed_out": False,
            "stderr": (
                "Traceback (most recent call last):\n"
                f"  File \"{path}\", line 1, in <module>\n"
                "    raise ValueError('fatal')\n"
                "ValueError: fatal\n"
            ),
            "events": [{
                "kind": "exception", "exception": "TypeError", "path": str(path),
                "relative_path": "unreconciled.py", "line": 1, "name": "<module>",
            }],
        })
        assert report is not None
        self.assertEqual(report["terminating_exception"]["type"], "ValueError")
        self.assertEqual(report["terminating_exception"]["correlation"], "unknown")
        self.assertEqual(report["observed_exceptions"][0]["type"], "TypeError")
        self.assertFalse(report["observed_exceptions"][0]["terminating"])

    def test_runtime_failure_uses_bounded_stderr_tail_after_output_truncation(self) -> None:
        (self.root / "truncated_failure.py").write_text(
            "import sys\n"
            "sys.stderr.write('x' * 5000)\n"
            "raise ValueError('visible terminal failure')\n"
        )
        result = self.engine.trace(
            "sample", [sys.executable, "truncated_failure.py"], timeout=5, output_cap=100,
        )

        self.assertTrue(result["output_truncated"])
        self.assertNotIn("ValueError", result["stderr"])
        self.assertEqual(result["failure_report"]["terminating_exception"]["type"], "ValueError")
        self.assertEqual(
            result["failure_report"]["terminating_exception"]["message"],
            "visible terminal failure",
        )

    def test_runtime_resource_adapters_capture_redacted_side_effect_timeline(self) -> None:
        (self.root / "resource_runner.py").write_text(
            "import json\n"
            "import os\n"
            "import sqlite3\n"
            "import urllib.request\n\n"
            "with urllib.request.urlopen('data:text/plain,ok') as response:\n"
            "    assert response.read() == b'ok'\n"
            "json.dumps({'token': 'top-secret', 'name': 'Ada'})\n"
            "with open('note.txt', 'w', encoding='utf-8') as handle:\n"
            "    handle.write('bounded content')\n"
            "with open('note.txt', encoding='utf-8') as handle:\n"
            "    handle.read()\n"
            "os.rename('note.txt', 'renamed.txt')\n"
            "os.stat('renamed.txt')\n"
            "os.chmod('renamed.txt', 0o600)\n"
            "os.remove('renamed.txt')\n"
            "descriptor = os.open('descriptor.txt', os.O_CREAT | os.O_RDWR, 0o600)\n"
            "os.write(descriptor, b'bytes')\n"
            "os.close(descriptor)\n"
            "os.remove('descriptor.txt')\n"
            "database = sqlite3.connect('runtime.sqlite3')\n"
            "database.execute('create table credentials (token text)')\n"
            "database.execute('insert into credentials values (?)', ('top-secret',))\n"
            "database.commit()\n"
            "assert database.execute('select token from credentials').fetchone() == ('top-secret',)\n"
            "try:\n"
            "    urllib.request.urlopen('http://127.0.0.1:1/?token=top-secret', timeout=0.1)\n"
            "except OSError:\n"
            "    pass\n"
        )
        result = self.engine.trace(
            "sample", [sys.executable, "resource_runner.py"], timeout=5,
            output_cap=10_000, capture_level="content", capture_limit=64,
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        report = self.queries.trace_resources("sample", result["trace_id"])
        self.assertEqual(
            set(report["by_resource"]),
            {"database", "filesystem", "http", "network", "serialization"},
        )
        self.assertGreaterEqual(report["failure_count"], 1)
        self.assertNotIn("top-secret", json.dumps(report))
        http_events = [
            event["evidence"]["details"] for event in report["timeline"]
            if event["evidence"]["details"]["resource_kind"] == "http"
        ]
        self.assertTrue(any(event["status"] == "completed" for event in http_events))
        self.assertTrue(all("response_body" not in event["details"] for event in http_events))
        filesystem_operations = {
            event["evidence"]["details"]["operation"] for event in report["timeline"]
            if event["evidence"]["details"]["resource_kind"] == "filesystem"
        }
        self.assertTrue({"create", "open", "read", "write", "rename", "stat", "permission", "delete"}
                        <= filesystem_operations)
        database_events = [
            event["evidence"]["details"] for event in report["timeline"]
            if event["evidence"]["details"]["resource_kind"] == "database"
        ]
        self.assertTrue(any(event["operation"] == "commit" for event in database_events))
        self.assertTrue(any(event["operation"] == "fetchone" for event in database_events))
        self.assertTrue(all(event["capture"]["level"] == "content" for event in database_events))
        stored = self.queries.traces("sample")[0]
        self.assertEqual(stored["capture_level"], "content")
        self.assertTrue(stored["redaction_enabled"])
        output = StringIO()
        with redirect_stdout(output):
            code = main([
                "trace-resources", "sample", result["trace_id"],
                "--database", str(self.store.path), "--format", "json",
            ])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["event_count"], report["event_count"])

    def test_runtime_resource_failures_never_appear_completed(self) -> None:
        (self.root / "existing-directory").mkdir()
        (self.root / "resource_failures.py").write_text(
            "import json\n"
            "import os\n"
            "import pickle\n"
            "import sqlite3\n"
            "import urllib.request\n\n"
            "def attempt(function):\n"
            "    try:\n"
            "        function()\n"
            "    except BaseException:\n"
            "        pass\n\n"
            "attempt(lambda: os.remove('missing-delete'))\n"
            "attempt(lambda: os.rename('missing-rename', 'renamed'))\n"
            "attempt(lambda: os.chmod('missing-chmod', 0o600))\n"
            "attempt(lambda: os.mkdir('existing-directory'))\n"
            "attempt(lambda: os.link('missing-link-source', 'linked'))\n"
            "attempt(lambda: os.symlink('target', 'missing-parent/symlink'))\n"
            "attempt(lambda: os.truncate('missing-truncate', 0))\n"
            "attempt(lambda: __import__('missing_threadtrace_dependency'))\n"
            "attempt(lambda: urllib.request.urlopen('http://127.0.0.1:1/', timeout=0.1))\n"
            "database = sqlite3.connect(':memory:')\n"
            "attempt(lambda: database.execute('select * from missing_table'))\n"
            "attempt(lambda: json.dumps({1, 2}))\n"
            "attempt(lambda: pickle.loads(b'not-a-pickle'))\n"
            "attempt(lambda: os.open('missing-descriptor', os.O_RDONLY))\n"
            "attempt(lambda: os.read(-1, 1))\n"
            "attempt(lambda: os.write(-1, b'x'))\n"
        )
        result = self.engine.trace(
            "sample", [sys.executable, "resource_failures.py"], timeout=5, output_cap=10_000,
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        report = self.queries.trace_resources("sample", result["trace_id"])
        timeline = [event["evidence"]["details"] for event in report["timeline"]]
        failed = {(event["resource_kind"], event["operation"]) for event in timeline if event["status"] == "failed"}
        self.assertTrue({
            ("filesystem", "delete"), ("filesystem", "rename"),
            ("filesystem", "permission"), ("filesystem", "create"),
            ("filesystem", "truncate"), ("filesystem", "import"),
            ("filesystem", "open"), ("filesystem", "read"), ("filesystem", "write"),
            ("http", "request"), ("database", "execute"),
            ("serialization", "serialize"), ("serialization", "deserialize"),
        }.issubset(failed))
        self.assertGreater(report["by_status"].get("attempted", 0), 0)
        by_correlation = {}
        for event in timeline:
            if event.get("correlation_id"):
                by_correlation.setdefault(event["correlation_id"], []).append(event["status"])
        for statuses in by_correlation.values():
            if "failed" in statuses:
                self.assertNotIn("completed", statuses)

    def test_runtime_tcp_ftp_savepoints_status_and_static_preflight(self) -> None:
        (self.root / "network_ftp_runner.py").write_text(
            "import ftplib\n"
            "import socket\n"
            "import sqlite3\n"
            "import threading\n\n"
            "server = socket.socket()\n"
            "server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "server.bind(('127.0.0.1', 0))\n"
            "server.listen(1)\n"
            "port = server.getsockname()[1]\n"
            "def serve():\n"
            "    connection, _ = server.accept()\n"
            "    connection.sendall(b'220 fixture ready\\r\\n')\n"
            "    with connection.makefile('rwb') as stream:\n"
            "        for line in stream:\n"
            "            command = line.split(b' ', 1)[0].upper()\n"
            "            if command == b'USER':\n"
            "                connection.sendall(b'331 password\\r\\n')\n"
            "            elif command == b'PASS':\n"
            "                connection.sendall(b'230 ok\\r\\n')\n"
            "            elif command == b'QUIT':\n"
            "                connection.sendall(b'221 bye\\r\\n')\n"
            "                break\n"
            "            else:\n"
            "                connection.sendall(b'200 ok\\r\\n')\n"
            "    connection.close()\n"
            "threading.Thread(target=serve, daemon=True).start()\n"
            "client = ftplib.FTP()\n"
            "client.connect('127.0.0.1', port, timeout=3)\n"
            "client.login('fixture', 'password')\n"
            "client.quit()\n"
            "server.close()\n"
            "database = sqlite3.connect(':memory:')\n"
            "database.execute('create table values_table (value text)')\n"
            "database.execute('savepoint outer')\n"
            "database.execute(\"insert into values_table values ('one')\")\n"
            "database.execute('release outer')\n"
            "cursor = database.execute('select value from values_table')\n"
            "assert cursor.fetchone() == ('one',)\n"
            "assert cursor.fetchone() is None\n"
            "database.rollback()\n"
        )
        result = self.engine.trace(
            "sample", [sys.executable, "network_ftp_runner.py"], timeout=8, output_cap=10_000,
        )
        self.assertEqual(result["exit_code"], 0, result["stderr"])
        self.assertEqual(result["preflight"]["status"], "static-estimate")
        self.assertFalse(result["preflight"]["guarantee"])
        report = self.queries.trace_resources("sample", result["trace_id"])
        timeline = [event["evidence"]["details"] for event in report["timeline"]]
        network = {((event.get("details") or {}).get("protocol"), event.get("operation")) for event in timeline
                   if event.get("resource_kind") == "network"}
        self.assertIn(("ftp", "connect"), network)
        self.assertIn(("ftp", "login"), network)
        self.assertIn(("ftp", "quit"), network)
        self.assertIn(("tcp", "connect"), network)
        database = {event.get("operation") for event in timeline
                    if event.get("resource_kind") == "database"}
        self.assertTrue({"savepoint", "release", "fetchone"}.issubset(database))
        self.assertTrue(any(item["adapter"] == "sqlite3" and item["status"] == "supported"
                            for item in report["adapter_support"]))
        self.assertIn("sqlite3", report["adapter_performance"])
        self.assertTrue(all("tls_intercepted" not in event or not event["tls_intercepted"]
                            for event in timeline if event.get("resource_kind") == "network"))

    def test_runtime_redacts_every_documented_credential_location_and_marks_truncation(self) -> None:
        secrets = {
            "environment-secret-123", "encoded%2Duser", "encoded%2Dpassword", "query-secret",
            "header-secret", "cookie-secret", "database-secret", "nested-secret", "malformed-secret",
            "bytes-secret",
        }
        (self.root / "redaction_runner.py").write_text(
            "import json\n"
            "import os\n"
            "import pickle\n"
            "import sqlite3\n"
            "import sys\n"
            "import urllib.request\n\n"
            "secret = os.environ['SERVICE_TOKEN']\n"
            "print(secret)\n"
            "sys.stderr.write(secret + '\\n')\n"
            "request = urllib.request.Request(\n"
            "    'http://encoded%2Duser:encoded%2Dpassword@127.0.0.1:1/path?ordinary=query-secret',\n"
            "    headers={'aUtHoRiZaTiOn': 'Bearer header-secret', 'CoOkIe': 'session=cookie-secret'},\n"
            ")\n"
            "for target in (request, 'http://[broken?token=malformed-secret',\n"
            "               'unknown://host/path?ordinary=query-secret'):\n"
            "    try:\n"
            "        urllib.request.urlopen(target, timeout=0.1)\n"
            "    except BaseException:\n"
            "        pass\n"
            "descriptor = os.open('bytes.txt', os.O_CREAT | os.O_WRONLY, 0o600)\n"
            "os.write(descriptor, b'password=bytes-secret')\n"
            "os.close(descriptor)\n"
            "os.remove('bytes.txt')\n"
            "database = sqlite3.connect(':memory:')\n"
            "database.execute('create table values_table (value text)')\n"
            "database.execute('insert into values_table values (?)', ('database-secret',))\n"
            "json.dumps({'outer': {'PaSsWoRd': 'nested-secret'}, 'long': 'x' * 200})\n"
            "pickle.dumps({'data': secret.encode(), 'long_bytes': b'x' * 200})\n"
        )
        with patch.dict(os.environ, {"SERVICE_TOKEN": "environment-secret-123"}):
            result = self.engine.trace(
                "sample", [sys.executable, "redaction_runner.py"], timeout=5, output_cap=10_000,
                capture_level="content", capture_limit=32,
            )
        report = self.queries.trace_resources("sample", result["trace_id"])
        stored = json.dumps({"stdout": result["stdout"], "stderr": result["stderr"], "resources": report})
        for secret in secrets:
            self.assertNotIn(secret, stored)
        self.assertIn("<redacted>", stored)
        self.assertIn('"truncated": true', stored)

        (self.root / "redacted_failure.py").write_text(
            "import os\n"
            "raise RuntimeError('token=' + os.environ['SERVICE_TOKEN'] +\n"
            "                   ' http://host/path?ordinary=query-secret')\n"
        )
        with patch.dict(os.environ, {"SERVICE_TOKEN": "environment-secret-123"}):
            failed = self.engine.trace(
                "sample", [sys.executable, "redacted_failure.py"], timeout=5, output_cap=10_000,
            )
        self.assertNotIn("environment-secret-123", json.dumps(failed["failure_report"]))
        self.assertNotIn("query-secret", json.dumps(failed["failure_report"]))
        self.assertIn("<redacted>", failed["failure_report"]["terminating_exception"]["message"])

    def test_hybrid_adapter_records_redact_nested_credentials(self) -> None:
        from threadtrace.hybrid_adapters import AdapterController

        interactions = []
        controller = AdapterController(self.root, {}, interactions)
        controller.record(
            "http", "GET",
            url="https://encoded%2Duser:encoded%2Dpassword@example.test/path?ordinary=query-secret",
            request={"headers": {"aUtHoRiZaTiOn": "Bearer header-secret", "CoOkIe": "cookie-secret"}},
            payload={"nested": {"Api-Key": "nested-secret"}, "long": "x" * 5000,
                     "body": "password=body-secret"},
        )
        controller.record("database", "execute", query="select ?", parameters=["database-secret"])

        stored = json.dumps(interactions)
        for secret in (
            "encoded%2Duser", "encoded%2Dpassword", "query-secret", "header-secret",
            "cookie-secret", "nested-secret", "database-secret", "body-secret",
        ):
            self.assertNotIn(secret, stored)
        self.assertIn("<redacted>", stored)
        self.assertIn('"truncated": true', stored)

    def test_runtime_failure_includes_observed_side_effects(self) -> None:
        (self.root / "side_effect_failure.py").write_text(
            "with open('attempt.txt', 'w', encoding='utf-8') as handle:\n"
            "    handle.write('started')\n"
            "raise RuntimeError('stopped')\n"
        )
        result = self.engine.trace(
            "sample", [sys.executable, "side_effect_failure.py"], timeout=5,
            output_cap=10_000,
        )
        self.assertEqual(result["exit_code"], 1)
        self.assertTrue(result["failure_report"]["side_effects"])
        self.assertEqual(result["failure_report"]["side_effects"][0]["resource_kind"], "filesystem")

    def test_runtime_capture_levels_and_redaction_controls(self) -> None:
        (self.root / "capture_runner.py").write_text(
            "import json\n"
            "json.dumps({'token': 'private-data'})\n"
            "with open('capture.txt', 'w', encoding='utf-8') as handle:\n"
            "    handle.write('ordinary content')\n"
        )
        metadata = self.engine.trace(
            "sample", [sys.executable, "capture_runner.py"], timeout=5,
            output_cap=10_000,
        )
        metadata_report = self.queries.trace_resources("sample", metadata["trace_id"])
        self.assertGreater(metadata_report["event_count"], 0)
        self.assertNotIn("private-data", json.dumps(metadata_report))
        self.assertNotIn("ordinary content", json.dumps(metadata_report))

        disabled = self.engine.trace(
            "sample", [sys.executable, "capture_runner.py"], timeout=5,
            output_cap=10_000, capture_level="off",
        )
        self.assertEqual(self.queries.trace_resources("sample", disabled["trace_id"])["event_count"], 0)

        unredacted = self.engine.trace(
            "sample", [sys.executable, "capture_runner.py"], timeout=5,
            output_cap=10_000, capture_level="content", redact=False,
        )
        unredacted_report = self.queries.trace_resources("sample", unredacted["trace_id"])
        self.assertIn("private-data", json.dumps(unredacted_report))

        errors = StringIO()
        with redirect_stderr(errors):
            code = main([
                "trace", "sample", "--yes-run", "--no-redact",
                "--database", str(self.store.path), "--", sys.executable, "capture_runner.py",
            ])
        self.assertEqual(code, 2)
        self.assertIn("--yes-sensitive", errors.getvalue())
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            code = main([
                "trace", "sample", "--yes-run", "--no-redact", "--yes-sensitive",
                "--capture-level", "content", "--database", str(self.store.path), "--format", "json",
                "--", sys.executable, "capture_runner.py",
            ])
        self.assertEqual(code, 0)
        self.assertIn("private-data", output.getvalue())

    def test_runtime_trace_coverage_marks_observed_source_lines(self) -> None:
        (self.root / "coverage_runner.py").write_text(
            "def choose(value):\n"
            "    if value:\n"
            "        return 'yes'\n"
            "    else:\n"
            "        return 'no'\n\n"
            "choose(True)\n"
        )
        self.engine.scan("sample")
        result = self.engine.trace("sample", [sys.executable, "coverage_runner.py"], timeout=5, output_cap=10_000)
        self.assertEqual(result["exit_code"], 0)
        report = self.queries.coverage("sample", result["trace_id"])
        file_report = next(item for item in report["files"] if item["path"] == "coverage_runner.py")
        self.assertTrue(file_report["source_matches_scan"])
        self.assertIn(3, file_report["executed_lines"])
        self.assertIn(5, file_report["unexecuted_lines"])
        self.assertTrue(any(item["symbol"] == "coverage_runner.choose" and item["executed"] for item in report["functions"]))
        conditional = next(item for item in file_report["branches"] if item["kind"] == "if")
        self.assertTrue(conditional["outcomes"][0]["observed"])
        self.assertFalse(conditional["outcomes"][1]["observed"])
        self.assertEqual(report["evidence_record"]["class"], "observed at runtime")
        self.assertEqual(file_report["evidence_record"]["class"], "observed at runtime")
        output = StringIO()
        with redirect_stdout(output):
            code = main(["coverage", "sample", result["trace_id"], "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["evidence_class"], "observed at runtime")

    def test_api_discovery_reports_route_controls_without_importing_frameworks(self) -> None:
        (self.root / "app" / "service.py").write_text(
            "from fastapi import Depends, FastAPI, Query\n\n"
            "app = FastAPI()\n\n"
            "@app.middleware('http')\n"
            "async def audit(request, call_next):\n"
            "    return await call_next(request)\n\n"
            "def require_user():\n"
            "    return 'user'\n\n"
            "@app.get('/accounts/{account_id}', dependencies=[Depends(require_user)], response_model=dict)\n"
            "@limiter.limit('10/minute')\n"
            "def account(account_id: int = Query(gt=0)):\n"
            "    return {'id': account_id}\n"
        )
        self.engine.scan("sample")
        report = self.queries.api("sample")
        endpoint = next(item for item in report["endpoints"] if item["route"] == "/accounts/{account_id}")
        self.assertEqual(endpoint["method"], "GET")
        self.assertIn("fastapi", endpoint["frameworks"])
        self.assertEqual(endpoint["framework"], "fastapi")
        self.assertEqual(endpoint["variant"], "application")
        self.assertEqual({item["kind"] for item in endpoint["checks"]},
                         {"authentication", "authorisation", "validation", "rate_limiting", "serialisation"})
        self.assertTrue(any(item["name"] == "app.service.audit" for item in report["middleware"]))
        output = StringIO()
        with redirect_stdout(output):
            code = main(["api", "sample", "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["endpoints"][0]["method"], "GET")

    def test_framework_analysers_require_registration_evidence(self) -> None:
        web = self.root / "web"
        web.mkdir()
        (web / "__init__.py").write_text("")
        (web / "flask_app.py").write_text(
            "from flask import Blueprint, Flask\n\n"
            "app = Flask(__name__)\n"
            "blueprint = Blueprint('users', __name__)\n\n"
            "@blueprint.route('/users/<int:user_id>', methods=['GET', 'PATCH'])\n"
            "def user(user_id):\n"
            "    return user_id\n\n"
            "@app.before_request\n"
            "def prepare():\n"
            "    return None\n"
        )
        (web / "django_views.py").write_text("def health(request):\n    return 'ok'\n")
        (web / "django_urls.py").write_text(
            "from django.urls import path\nfrom . import django_views\n\n"
            "urlpatterns = [path('health/', django_views.health, name='health')]\n"
        )
        (web / "aiohttp_app.py").write_text(
            "from aiohttp import web\n\n"
            "app = web.Application()\n\n"
            "async def status(request):\n"
            "    return web.json_response({'ok': True})\n\n"
            "app.router.add_get('/status', status)\n"
        )
        (web / "starlette_app.py").write_text(
            "from starlette.routing import Route\n\n"
            "async def home(request):\n"
            "    return 'home'\n\n"
            "routes = [Route('/home', home, methods=['GET'])]\n"
        )
        (web / "django_middleware.py").write_text(
            "from django.middleware.common import MiddlewareMixin\n\n"
            "class AuditMiddleware(MiddlewareMixin):\n"
            "    def process_request(self, request):\n"
            "        return None\n"
        )
        (web / "quart_app.py").write_text(
            "from quart import Quart\n\n"
            "app = Quart(__name__)\n\n"
            "@app.get('/quart')\n"
            "async def quart_home():\n"
            "    return 'quart'\n"
        )
        (self.root / "cli_tools.py").write_text(
            "import argparse\nimport click\nimport optparse\nimport typer\n\n"
            "parser = argparse.ArgumentParser()\n"
            "parser.add_argument('--count', type=int, default=3)\n"
            "subparsers = parser.add_subparsers()\n"
            "subparsers.add_parser('run')\n\n"
            "def parse():\n"
            "    return parser.parse_args()\n\n"
            "option_parser = optparse.OptionParser()\n"
            "option_parser.add_option('-n', '--name', default='demo')\n\n"
            "@click.group()\n"
            "def cli():\n"
            "    pass\n\n"
            "@cli.command()\n"
            "@click.option('--name', default='demo')\n"
            "def hello(name):\n"
            "    return name\n\n"
            "app = typer.Typer()\n\n"
            "@app.command()\n"
            "def serve(port: int = 8000):\n"
            "    return port\n"
        )
        (self.root / "jobs.py").write_text(
            "from celery import Celery, shared_task\n"
            "from apscheduler.schedulers.background import BackgroundScheduler\n"
            "from pluggy import hookimpl\n"
            "from rq import job\n"
            "from dramatiq import actor\n"
            "from huey import task\n\n"
            "app = Celery('jobs')\n"
            "scheduler = BackgroundScheduler()\n\n"
            "@shared_task\n"
            "def refresh():\n"
            "    return 'refresh'\n\n"
            "@app.task\n"
            "def cleanup():\n"
            "    return 'cleanup'\n\n"
            "def tick():\n"
            "    return 'tick'\n\n"
            "scheduler.add_job(tick, 'interval', seconds=10)\n\n"
            "@hookimpl\n"
            "def attach():\n"
            "    pass\n\n"
            "@job\n"
            "def rq_work():\n"
            "    pass\n\n"
            "@actor\n"
            "def dramatiq_work():\n"
            "    pass\n\n"
            "@task\n"
            "def huey_work():\n"
            "    pass\n"
        )
        (self.root / "noise.py").write_text(
            "@app.get('/not-a-route')\n"
            "def unrelated():\n"
            "    return 'noise'\n\n"
            "@task\n"
            "def also_unrelated():\n"
            "    return 'noise'\n"
        )
        self.engine.scan("sample")
        entries = self.queries.entrypoints("sample")
        self.assertNotIn(("route", "GET /not-a-route"), {(item["kind"], item["name"]) for item in entries})
        self.assertNotIn(("worker", "also_unrelated"), {(item["kind"], item["name"]) for item in entries})
        routes = {(item["input"].get("framework"), item["name"]) for item in entries if item["kind"] == "route"}
        self.assertIn(("flask", "GET /users/<int:user_id>"), routes)
        self.assertIn(("django", "DJANGO health/"), routes)
        self.assertIn(("aiohttp", "GET /status"), routes)
        self.assertIn(("starlette", "GET /home"), routes)
        self.assertIn(("quart", "GET /quart"), routes)
        frameworks = {item["input"].get("framework") for item in entries if item["kind"] == "route"}
        self.assertEqual(frameworks, {"flask", "django", "aiohttp", "starlette", "quart"})
        cli = {(item["input"].get("framework"), item["kind"]) for item in entries if item["kind"] == "cli"}
        self.assertTrue({("argparse", "cli"), ("click", "cli"), ("typer", "cli"), ("optparse", "cli")}.issubset(cli))
        workers = {item["input"].get("framework") for item in entries if item["kind"] == "worker"}
        self.assertTrue({"celery", "rq", "dramatiq", "huey"}.issubset(workers))
        self.assertTrue(any(item["kind"] == "scheduled" and item["input"].get("framework") == "apscheduler" for item in entries))
        self.assertTrue(any(item["kind"] == "plugin" and item["input"].get("framework") == "pluggy" for item in entries))
        self.assertTrue(any(item["kind"] == "middleware" and item["input"].get("framework") == "django" for item in entries))
        flask = next(item for item in entries if item["name"] == "GET /users/<int:user_id>")
        self.assertEqual(flask["input"]["methods"], ["GET", "PATCH"])
        self.assertEqual(flask["input"]["path_parameters"], ["user_id"])
        self.assertEqual(flask["evidence"]["variant"], "blueprint")
        self.assertTrue(flask["input"]["version"] is None or isinstance(flask["input"]["version"], str))
        api = self.queries.api("sample")
        self.assertTrue(any(item["route"] == "/status" and "aiohttp" in item["frameworks"] for item in api["endpoints"]))

    def test_static_data_access_recognises_orm_and_dbapi_calls(self) -> None:
        (self.root / "data_access.py").write_text(
            "import sqlite3\n"
            "from sqlalchemy import create_engine, select\n"
            "from django.db import models\n\n"
            "class Account(models.Model):\n"
            "    name = models.CharField(max_length=20)\n\n"
            "engine = create_engine('sqlite:///app.db')\n"
            "connection = sqlite3.connect('app.db')\n"
            "connection.execute('select * from accounts')\n"
            "accounts = select(Account)\n"
            "Account.objects.filter(name='demo')\n"
        )
        self.engine.scan("sample")
        resources = self.queries.resources("sample", "database")
        names = {item["name"] for item in resources}
        self.assertIn("select * from accounts", names)
        self.assertTrue(any("Account.objects.filter" in name for name in names))
        details = {item["evidence"].get("framework") for item in resources}
        self.assertTrue({"sqlalchemy", "django-orm"} & details)

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
            self.assertTrue(all(isinstance(event.get("wall_time"), float) for event in session.events))
            self.assertTrue(all(event.get("elapsed_seconds", -1) >= 0 and event.get("cpu_seconds", -1) >= 0 for event in session.events))
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

    def test_runtime_values_are_cycle_safe_and_debugger_expressions_are_read_only(self) -> None:
        from threadtrace.runtime.session import RuntimeSession
        from threadtrace.runtime.values import (
            UnsafeExpression, binding_changes, condition_result, configure_budgets, evaluate_expression, safe_value,
        )

        configure_budgets(depth=1, items=1, byte_limit=16)
        self.assertTrue(safe_value([1, 2])["truncated"])
        self.assertEqual(safe_value("x" * 40)["length"], 40)
        self.assertTrue(safe_value("x" * 40)["truncated"])
        configure_budgets(depth=3, items=20, byte_limit=160)

        loop = {}
        loop["self"] = loop
        summary = safe_value(loop, "loop")
        self.assertEqual(summary["object_id"], summary["items"][0]["value"]["reference"])
        self.assertTrue(summary["items"][0]["value"]["cycle"])
        before = {"items": safe_value([])}
        raw = []
        after = {"items": safe_value(raw)}
        self.assertIn("created", binding_changes(after, before))
        raw.append(1)
        self.assertIn("items", binding_changes({"items": safe_value(raw)}, after)["mutated"])
        self.assertTrue(evaluate_expression("value >= 3 and value not in (1, 2)", {"value": 3}))
        for expression in ("__import__('os')", "items.append(1)", "[x for x in items]", "obj.__class__"):
            with self.assertRaises(UnsafeExpression):
                evaluate_expression(expression, {"items": [], "obj": object()})
        self.assertFalse(condition_result("value = 3", {"value": 3}))

        runner = self.root / "condition_runner.py"
        runner.write_text(
            "def loop():\n"
            "    items = []\n"
            "    for value in range(5):\n"
            "        items.append(value)\n"
            "    return items\n"
            "\n"
            "print(len(loop()))\n"
        )
        session = RuntimeSession(self.root, [sys.executable, runner.name], timeout=3, output_cap=10_000)
        try:
            with self.assertRaises(UnsafeExpression):
                session.add_breakpoint(runner, 4, condition="items.append(1)")
            session.add_breakpoint(runner, 2, kind="tracepoint")
            session.add_breakpoint(runner, 4, condition="value == 3")
            session.continue_run()
            observed_line_four = False
            for _ in range(250):
                session.poll()
                if session.finished:
                    break
                if any(event.get("kind") == "tracepoint" for event in session.events):
                    observed_line_four = observed_line_four or any(
                        event.get("line") == 4 and event.get("name") == "loop" for event in session.events
                    )
                    # The conditional breakpoint may be holding the worker until the
                    # next command arrives, so keep the command channel drained.
                    session.continue_run()
                time.sleep(0.02)
            self.assertTrue(observed_line_four)
            self.assertTrue(session.finished)
            self.assertEqual(session.result()["stdout"].strip(), "5")

            changed = self.root / "changed_breakpoint.py"
            changed.write_text("value = 1\n")
            session.add_breakpoint(changed, 1)
            changed.write_text("value = 2\n")
            self.assertIn((str(changed.resolve()), 1), session.revalidate_breakpoints())
        finally:
            session.close()

    def test_runtime_concurrency_correlates_processes_threads_and_waits(self) -> None:
        (self.root / "concurrency_runner.py").write_text(
            "import queue\n"
            "import threading\n"
            "q = queue.Queue()\n"
            "def worker():\n"
            "    q.put(1)\n"
            "thread = threading.Thread(target=worker)\n"
            "thread.start()\n"
            "q.get(timeout=1)\n"
            "thread.join()\n"
        )
        result = self.engine.trace("sample", [sys.executable, "concurrency_runner.py"], timeout=5, output_cap=10_000)
        correlation = self.queries.trace_concurrency("sample", result["trace_id"])
        self.assertTrue(correlation["run_id"])
        self.assertTrue(correlation["processes"])
        self.assertTrue(correlation["threads"])
        self.assertTrue(any(wait["resource_kind"] in {"queue", "thread"} for wait in correlation["waits"]))
        self.assertTrue(any(wait["status"] in {"completed", "timeout"} for wait in correlation["waits"]))
        self.assertIn("Native locks", " ".join(correlation["limitations"]))

        (self.root / "child_runner.py").write_text(
            "import subprocess, sys\n"
            "subprocess.run([sys.executable, '-c', 'import time; time.sleep(0.05)'], check=True)\n"
        )
        child_trace = self.engine.trace("sample", [sys.executable, "child_runner.py"], timeout=5, output_cap=10_000)
        child = self.queries.trace_concurrency("sample", child_trace["trace_id"])
        self.assertGreaterEqual(len(child["processes"]), 2)
        root_process = next(item for item in child["processes"] if not item["parent_process_id"])
        self.assertTrue(any(item["parent_process_id"] == root_process["process_id"] for item in child["processes"]))

    def test_runtime_asyncio_tasks_record_creation_and_lifecycle(self) -> None:
        from threadtrace.runtime.session import RuntimeSession

        (self.root / "async_runner.py").write_text(
            "import asyncio\n"
            "async def child():\n"
            "    await asyncio.sleep(0.01)\n"
            "async def main():\n"
            "    await asyncio.gather(child(), child())\n"
            "asyncio.run(main())\n"
        )
        session = RuntimeSession(self.root, [sys.executable, "async_runner.py"], timeout=5, output_cap=10_000)
        try:
            session.start()
            session.continue_run()
            for _ in range(150):
                session.poll()
                if session.finished:
                    break
                time.sleep(0.02)
            self.assertTrue(session.finished)
            kinds = {event.get("kind") for event in session.events}
            self.assertIn("task", kinds)
            self.assertIn("process", kinds)
            self.assertTrue(any(event.get("lifecycle") == "created" for event in session.events if event.get("kind") == "task"))
            self.assertTrue(any(event.get("kind") in {"yield", "resume"} for event in session.events))
            self.assertTrue(session.result()["run_id"])
        finally:
            session.close()

    def test_runtime_step_controls_are_thread_specific(self) -> None:
        from threadtrace.runtime.session import RuntimeSession

        runner = self.root / "thread_runner.py"
        runner.write_text(
            "import threading\n"
            "def worker(name):\n"
            "    first = 1\n"
            "    second = 2\n"
            "    print(name, first, second, flush=True)\n"
            "threads = [threading.Thread(target=worker, args=(name,)) for name in ('a', 'b')]\n"
            "for thread in threads: thread.start()\n"
            "for thread in threads: thread.join()\n"
        )
        session = RuntimeSession(self.root, [sys.executable, "thread_runner.py"], timeout=3, output_cap=10_000)
        try:
            session.add_breakpoint(runner, 3)
            session.start()
            session.continue_run()
            for _ in range(120):
                session.poll()
                workers = {event["thread_id"] for event in session.events
                           if event.get("name") == "worker" and event.get("kind") == "call"}
                if len(workers) >= 2:
                    break
                time.sleep(0.02)
            self.assertGreaterEqual(len(workers), 2)
            first, second = sorted(workers)[:2]
            before_first = sum(event.get("thread_id") == first for event in session.events)
            before_second = sum(event.get("thread_id") == second for event in session.events)
            for _ in range(3):
                session.step(first)
                for _ in range(60):
                    session.poll()
                    if sum(event.get("thread_id") == first for event in session.events) > before_first:
                        break
                    time.sleep(0.02)
                if sum(event.get("thread_id") == first for event in session.events) > before_first:
                    break
            self.assertGreater(sum(event.get("thread_id") == first for event in session.events), before_first)
            self.assertEqual(sum(event.get("thread_id") == second for event in session.events), before_second)
            session.continue_run()
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
        if os.name == "posix":
            self.assertEqual(launch.call_args.kwargs["process_group"], 0)

    def test_runtime_host_requires_execution_confirmation(self) -> None:
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["runtime-host", "sample", "--database", str(self.store.path), "--", sys.executable, "-c", "pass"])
        self.assertEqual(code, 2)
        self.assertEqual(output.getvalue(), "")
        self.assertIn("--yes-run", errors.getvalue())

    def test_runtime_host_paginates_and_reports_dropped_events(self) -> None:
        from threadtrace.runtime.host import _LiveSocketServer, _MAX_PAGE_EVENTS, receive_live_events

        directory = Path(self.temp.name) / "paging"
        directory.mkdir(mode=0o700)
        server = _LiveSocketServer("page-session", directory / "listener.sock", directory / "events.jsonl", 450)
        try:
            server.start()
        except PermissionError:
            self.skipTest("Unix sockets are prohibited by this environment")
        metadata = {"session_id": "page-session", "socket_path": str(directory / "listener.sock")}
        try:
            for index in range(450):
                server._record({"path": str(self.root / "app" / "main.py"), "name": f"event-{index}",
                                "payload": "x" * 3000})
            self.assertGreater((directory / "events.jsonl").stat().st_size, 1_000_000)
            events, latest, report = receive_live_events(metadata, 0, "pager")
            self.assertEqual(latest, 450)
            self.assertLess(len(events), _MAX_PAGE_EVENTS)
            self.assertGreater(len(events), 0)
            self.assertEqual(report["next"], len(events))
            self.assertGreater(report["lag"], 0)
            self.assertEqual([event["ordinal"] for event in events], list(range(1, len(events) + 1)))
            received = list(events)
            while report["next"] < latest:
                page, latest, report = receive_live_events(metadata, report["next"], "pager")
                self.assertLessEqual(len(page), _MAX_PAGE_EVENTS)
                received.extend(page)
            self.assertEqual(len(received), 450)
            self.assertEqual(report["lag"], 0)
        finally:
            server.stop()

    def test_runtime_host_limits_pages_and_reports_journal_truncation(self) -> None:
        from threadtrace.runtime.host import _LiveSocketServer, _MAX_EVENT_BYTES, receive_live_events

        directory = Path(self.temp.name) / "limits"
        directory.mkdir(mode=0o700)
        server = _LiveSocketServer("limit-session", directory / "listener.sock", directory / "events.jsonl", 5)
        try:
            server.start()
        except PermissionError:
            self.skipTest("Unix sockets are prohibited by this environment")
        metadata = {"session_id": "limit-session", "socket_path": str(directory / "listener.sock")}
        try:
            for index in range(20):
                server._record({"path": str(self.root / "app" / "main.py"), "name": str(index), "payload": "x" * 30_000})
            first, latest, report = receive_live_events(metadata, 0, "limited", max_bytes=32_000)
            self.assertLessEqual(len(first), 2)
            self.assertEqual(latest, 5)
            self.assertGreater(report["lag"], 0)
            self.assertTrue(report["journal_truncated"])
            self.assertEqual(report["dropped"], 15)
            self.assertEqual(len((directory / "events.jsonl").read_text().splitlines()), 5)
            server._record({"path": str(self.root / "app" / "main.py"), "name": "too-large",
                            "payload": "x" * (_MAX_EVENT_BYTES + 1)})
            self.assertEqual(server.oversized, 1)
        finally:
            server.stop()

    def test_runtime_host_takeover_expiry_and_protocol_errors(self) -> None:
        import struct
        from threadtrace.runtime.host import _LiveSocketServer, _PROTOCOL_VERSION, _request_socket, LiveTransportError, receive_live_events

        directory = Path(self.temp.name) / "protocol"
        directory.mkdir(mode=0o700)
        server = _LiveSocketServer("protocol-session", directory / "listener.sock", directory / "events.jsonl", 10)
        try:
            server.start()
        except PermissionError:
            self.skipTest("Unix sockets are prohibited by this environment")
        metadata = {"session_id": "protocol-session", "socket_path": str(directory / "listener.sock")}
        try:
            self.assertEqual(stat.S_IMODE((directory / "listener.sock").stat().st_mode), 0o600)
            first = receive_live_events(metadata, 0, "first")
            self.assertEqual(first[1], 0)
            with self.assertRaises(LiveTransportError):
                receive_live_events(metadata, 0, "second")
            server._listener_seen_at -= 3
            self.assertEqual(receive_live_events(metadata, 0, "second")[1], 0)
            wrong = _request_socket(directory / "listener.sock", {
                "kind": "listen", "protocol_version": _PROTOCOL_VERSION,
                "session_id": "wrong", "listener_id": "x", "after": 0,
            })
            self.assertFalse(wrong["ok"])
            self.assertIn("identity", wrong["error"])
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(1)
                connection.connect(str(directory / "listener.sock"))
                connection.sendall(b"not json\n")
                reply = json.loads(connection.recv(4096))
            self.assertFalse(reply["ok"])
            self.assertIn("Invalid", reply["error"])
            if hasattr(socket, "SO_PEERCRED"):
                class Credentials:
                    def getsockopt(self, _level: int, _option: int, _size: int) -> bytes:
                        return struct.pack("3i", 1, os.getuid() + 1, 1)
                self.assertFalse(server._current_user(Credentials()))
        finally:
            server.stop()

    def test_live_session_retention_and_active_selection(self) -> None:
        from threadtrace.runtime.host import cleanup_live_sessions, live_sessions

        root = Path(self.temp.name) / "retention"
        root.mkdir(mode=0o700)
        now = time.time()
        old = datetime.fromtimestamp(now - 2 * 24 * 60 * 60, UTC).isoformat()
        recent = datetime.fromtimestamp(now, UTC).isoformat()
        stale = datetime.fromtimestamp(now - 120, UTC).isoformat()

        def session(name: str, status: str, updated: str) -> Path:
            directory = root / name
            directory.mkdir(mode=0o700)
            (directory / "session.json").write_text(json.dumps({
                "session_id": name, "project_root": str(self.root), "status": status, "updated_at": updated,
            }))
            return directory

        old_directory = session("a" * 12, "finished", old)
        finished = session("b" * 12, "finished", recent)
        active = session("c" * 12, "running", recent)
        stale_running = session("d" * 12, "running", stale)
        external = Path(self.temp.name) / "external"
        external.mkdir()
        link = root / ("e" * 12)
        try:
            link.symlink_to(external, target_is_directory=True)
        except OSError:
            link = None
        with patch("threadtrace.runtime.host._sessions_root", return_value=root):
            self.assertEqual(cleanup_live_sessions(now), ["a" * 12])
            self.assertEqual([item["session_id"] for item in live_sessions(self.root)], ["c" * 12])
            history = live_sessions(self.root, include_finished=True)
            self.assertEqual({item["session_id"] for item in history}, {"b" * 12, "c" * 12, "d" * 12})
            self.assertEqual(live_sessions(self.root, "d" * 12)[0]["session_id"], "d" * 12)
        self.assertFalse(old_directory.exists())
        self.assertTrue(finished.exists())
        self.assertTrue(active.exists())
        self.assertTrue(stale_running.exists())
        if link:
            self.assertTrue(link.is_symlink())

    def test_runtime_host_stops_descendant_processes(self) -> None:
        from threadtrace.runtime.host import RuntimeHost

        sessions = Path(self.temp.name) / "stop-sessions"
        sessions.mkdir()
        child_script = "import time; time.sleep(30)"
        parent_script = (
            "import subprocess, sys, time\n"
            f"child = subprocess.Popen([sys.executable, '-c', {child_script!r}])\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(30)\n"
        )
        with patch("threadtrace.runtime.host._sessions_root", return_value=sessions):
            host = RuntimeHost(self.root, "sample", [sys.executable, "-c", "pass"])
        process = subprocess.Popen([sys.executable, "-c", parent_script], stdout=subprocess.PIPE, text=True,
                                   **(({"process_group": 0}) if os.name == "posix" else {}))
        try:
            assert process.stdout is not None
            child_pid = int(process.stdout.readline())
            self.assertIsNotNone(host._stop_process(process))
            for _ in range(40):
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.05)
            else:
                self.fail("Runtime Host left a descendant process running")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if process.stdout:
                process.stdout.close()
            shutil.rmtree(host.directory, ignore_errors=True)

    def _run_host_in_terminal(self, script: str, marker: bytes, reply: bytes) -> tuple[bytes, list[dict]]:
        sessions = Path(self.temp.name) / "runtime"
        sessions.mkdir(exist_ok=True)
        env = os.environ.copy()
        env["XDG_RUNTIME_DIR"] = str(sessions)
        env["TERM"] = "xterm"
        source = str(Path(__file__).resolve().parents[1] / "src")
        env["PYTHONPATH"] = os.pathsep.join(filter(None, [source, env.get("PYTHONPATH", "")]))
        command = [sys.executable, "-m", "threadtrace.cli", "runtime-host", "sample", "--yes-run", "--database",
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
        source_events = [event for event in events if event.get("kind") in {"call", "line", "return", "exception"}]
        self.assertTrue(source_events)
        self.assertTrue(all(Path(event["path"]).resolve().is_relative_to(self.root.resolve()) for event in source_events))

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
    def test_host_listener_uses_one_current_user_socket(self) -> None:
        from threadtrace.runtime.host import (
            _LiveSocketServer,
            _PROTOCOL_VERSION,
            _request_socket,
            LiveTransportError,
            receive_live_events,
            release_live_listener,
        )

        directory = Path(self.temp.name) / "ipc"
        directory.mkdir(mode=0o700)
        server = _LiveSocketServer("session", directory / "listener.sock", directory / "events.jsonl", 10)
        try:
            server.start()
        except PermissionError:
            self.skipTest("Unix sockets are prohibited by this environment")
        metadata = {"session_id": "session", "socket_path": str(directory / "listener.sock")}
        try:
            reply = _request_socket(directory / "listener.sock", {
                "kind": "event", "protocol_version": _PROTOCOL_VERSION, "session_id": "session",
                "event": {"path": str(self.root / "app" / "main.py"), "name": "main"},
            })
            self.assertTrue(reply["ok"])
            events, latest, _report = receive_live_events(metadata, 0, "first")
            self.assertEqual(latest, 1)
            self.assertEqual(events[0]["name"], "main")
            with self.assertRaises(LiveTransportError):
                receive_live_events(metadata, 0, "second")
            release_live_listener(metadata, "first")
            events, latest, _report = receive_live_events(metadata, 1, "second")
            self.assertEqual((events, latest), ([], 1))
        finally:
            server.stop()

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
        events = [json.loads(line) for line in Path(session["log_path"]).read_text().splitlines()]

        def receive(_metadata: dict, after: int, _listener_id: str) -> tuple[list[dict], int, dict]:
            return events[after:], len(events), {}

        async def exercise() -> None:
            with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
                with patch("threadtrace.live_tui.live_sessions", return_value=[session]), \
                     patch("threadtrace.live_tui.receive_live_events", side_effect=receive):
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
                        self.assertIn("Rebound\n  value = 2 (int)", app.inspector.text)
                        self.assertEqual(app._variables_at(app.events[changed])["value"]["value"], 2)
                        app._select_event(removed)
                        self.assertIn("Removed\n  value", app.inspector.text)
                        self.assertNotIn("value", app._variables_at(app.events[removed]))
                        pipe.send_text("k")
                        await asyncio.sleep(0.1)
                        self.assertEqual(app.selected, removed - 1)
                        self.assertFalse(app.following)
                        # Drain pending valid events so the next tick adds exactly one.
                        for _ in range(20):
                            settled = len(app.events)
                            app._tick()
                            if len(app.events) == settled:
                                break
                        extra = dict(app.events[changed])
                        extra["variables"] = {"created": {"password": {"type": "str", "value": "<redacted>"}}, "changed": {}, "removed": []}
                        count = len(app.events)
                        events.append(extra)
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

    def test_capabilities_are_explicit_about_unimplemented_features(self) -> None:
        capabilities = self.queries.capabilities("sample")
        statuses = {(item["area"], item["status"]) for item in capabilities}
        self.assertIn(("Static analysis", "Partial"), statuses)
        self.assertIn(("Hybrid", "Full"), statuses)

    def test_public_results_share_versioned_evidence_records(self) -> None:
        from threadtrace.evidence import EVIDENCE_CLASSES, EVIDENCE_VERSION

        control = self.queries.control_flow("sample", "app.main.main")
        lesson = self.queries.lesson("sample", "app.core.Service")
        dataflow = self.queries.dataflow("sample", "app.core.Service.process")
        architecture = self.queries.architecture("sample")
        file_graph = self.queries.file_graph("sample")
        code_graph = self.queries.code_graph("sample")
        rows: list[dict[str, Any]] = [
            *self.queries.modules("sample"), *self.queries.entrypoints("sample"),
            *self.queries.functions("sample"), *self.queries.classes("sample"),
            *self.queries.diagnostics("sample"), *architecture["components"], *architecture["relationships"],
            *file_graph["nodes"], *file_graph["edges"], *code_graph["nodes"], *code_graph["edges"],
            *self.queries.symbol("sample", "app.main.main"),
            *self.queries.callers("sample", "app.helpers.format_name"),
            *self.queries.callees("sample", "app.main.main")["calls"],
            *control["nodes"], *control["edges"], *lesson["patterns"], *dataflow["edges"],
            *self.queries.overview("sample")["components"],
        ]
        self.assertTrue(rows)
        for row in rows:
            evidence = row["evidence_record"]
            self.assertEqual(evidence["version"], EVIDENCE_VERSION)
            self.assertIn(evidence["class"], EVIDENCE_CLASSES)
            self.assertEqual(evidence["class"], row["evidence_class"])
            self.assertTrue(evidence["mechanism"])
            self.assertIn("legacy_confidence", evidence)
            self.assertIsInstance(evidence["limitations"], list)
        for result in (control, lesson, dataflow, architecture, file_graph, code_graph):
            self.assertEqual(result["evidence_record"]["version"], EVIDENCE_VERSION)

    def test_capabilities_support_project_scan_run_and_feature_scope(self) -> None:
        scan_id = self.engine.latest_scan("sample")["scan_id"]
        capabilities = self.queries.capabilities(
            "sample", scan_id=scan_id, interpreter=sys.executable, feature="Hybrid",
            entity="app.main.main",
        )
        self.assertEqual(len(capabilities), 1)
        capability = capabilities[0]
        self.assertEqual(capability["status"], "Full")
        self.assertEqual(capability["scope"]["scan_id"], scan_id)
        self.assertEqual(capability["scope"]["entity"], "app.main.main")
        self.assertEqual(capability["scope"]["interpreter"], sys.executable)
        self.assertTrue(capability["limitations"])
        self.assertEqual(capability["evidence_record"]["version"], 1)
        with self.assertRaises(ThreadError):
            self.queries.capabilities("sample", scan_id="missing")
        output = StringIO()
        with redirect_stdout(output):
            code = main(["capabilities", "sample", "--feature", "Host", "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue()))

    def test_v12_database_migrates_legacy_confidence_metadata(self) -> None:
        from threadtrace.evidence import EVIDENCE_VERSION

        database = Path(self.temp.name) / "legacy.sqlite3"
        legacy = Store(database)
        try:
            Engine(legacy).add_project(str(self.root), "legacy")
            Engine(legacy).scan("legacy")
        finally:
            legacy.close()
        connection = sqlite3.connect(database)
        connection.execute("ALTER TABLE scans DROP COLUMN evidence_version")
        connection.execute("ALTER TABLE scans DROP COLUMN legacy_confidence_json")
        connection.execute("DROP TABLE project_preferences")
        for table in ("symbol_references", "boundaries", "exports", "definitions", "scopes",
                      "metadata_files", "dependencies", "scan_inputs"):
            connection.execute(f"DROP TABLE {table}")
        for column in ("visible_name", "binding_path_json", "reexport", "is_type_checking", "source_path"):
            connection.execute(f"ALTER TABLE imports DROP COLUMN {column}")
        connection.execute("ALTER TABLE calls DROP COLUMN is_type_checking")
        connection.execute("ALTER TABLE calls DROP COLUMN binding_path_json")
        for column in ("origin_kind", "original_path", "resolved_path", "parser", "parser_version",
                       "capability", "category", "line_offset", "origin_id"):
            connection.execute(f"ALTER TABLE files DROP COLUMN {column}")
        connection.execute("PRAGMA user_version = 12")
        connection.commit()
        connection.close()
        migrated = Store(database)
        try:
            row = migrated.one("SELECT evidence_version,legacy_confidence_json FROM scans")
            self.assertEqual(row["evidence_version"], EVIDENCE_VERSION)
            counts = json.loads(row["legacy_confidence_json"])
            self.assertGreater(sum(counts.values()), 0)
            self.assertTrue(set(counts) <= {"confirmed", "high", "possible", "unknown"})
            self.assertEqual(migrated.connection.execute("PRAGMA user_version").fetchone()[0], 17)
            backup = Path(str(database) + ".bak")
            self.assertTrue(backup.exists())
            previous = sqlite3.connect(backup)
            try:
                self.assertEqual(previous.execute("PRAGMA user_version").fetchone()[0], 12)
            finally:
                previous.close()
            entries = QueryService(Engine(migrated)).entrypoints("legacy")
            self.assertTrue(entries)
            self.assertEqual(entries[0]["evidence_record"]["version"], EVIDENCE_VERSION)
        finally:
            migrated.close()

    def test_configuration_is_nonsecret_and_loopback_only(self) -> None:
        from threadtrace.config import load, write_default

        path = Path(self.temp.name) / "config.yaml"
        self.assertEqual(load(path)["browser"]["host"], "127.0.0.1")
        write_default(path)
        configuration = load(path)
        self.assertEqual(configuration["schema_version"], 1)
        self.assertEqual(configuration["storage"]["retention_days"], 30)
        self.assertEqual(configuration["runtime"]["capture_level"], "metadata")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(load(path)["browser"], {"host": "127.0.0.1", "port": 0})
        path.write_text("browser:\n  host: 0.0.0.0\n")
        with self.assertRaises(ValueError):
            load(path)

    def test_configuration_rejects_ambiguous_secret_or_invalid_values(self) -> None:
        from threadtrace.config import load

        path = Path(self.temp.name) / "invalid.yaml"
        path.write_text("browser:\n  host: 127.0.0.1\n  host: ::1\n")
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            load(path)
        path.write_text("storage:\n  token: hidden\n")
        with self.assertRaises(ValueError):
            load(path)
        path.write_text("runtime:\n  timeout_seconds: soon\n")
        with self.assertRaisesRegex(ValueError, "timeout_seconds"):
            load(path)
        path.write_text("schema_version: 2\n")
        with self.assertRaisesRegex(ValueError, "schema_version"):
            load(path)
        path.write_text("database_path: /tmp/legacy.sqlite3\nbrowser:\n  host: ::1\n  port: 0\n")
        legacy = load(path)
        self.assertEqual(legacy["storage"]["database_path"], "/tmp/legacy.sqlite3")
        self.assertEqual(legacy["browser"]["host"], "::1")

    def test_project_preferences_and_discovery_overrides(self) -> None:
        from threadtrace.config import DEFAULTS, merge

        saved = self.engine.save_project_preferences(
            "sample", {"discovery": {"exclude": ["app/broken.py"]}, "tui": {"compact": True}},
        )
        self.assertEqual(saved["tui"]["compact"], True)
        self.assertEqual(self.engine.project_preferences("sample")["discovery"]["exclude"], ["app/broken.py"])
        with self.assertRaises(ThreadError):
            self.engine.save_project_preferences("sample", {"storage": {"database_path": "/tmp/other.sqlite3"}})
        output = StringIO()
        with redirect_stdout(output):
            code = main(["project", "preferences", "sample", "--content", '{"exports": {"default_format": "json"}}',
                         "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["preferences"]["exports"]["default_format"], "json")
        effective = merge(DEFAULTS, saved)
        self.assertEqual(effective["tui"]["compact"], True)
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            code = main(["scan", "sample", "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertNotIn("app/broken.py", {item["relative_path"] for item in self.queries.modules("sample")})
        scan = self.engine.scan("sample", includes=["app/helpers.py"])
        modules = self.queries.modules("sample")
        self.assertEqual([item["relative_path"] for item in modules], ["app/helpers.py"])
        self.assertTrue(scan["scan_id"])

    def test_storage_usage_pruning_and_explicit_deletion(self) -> None:
        first = self.engine.latest_scan("sample")["scan_id"]
        self.engine.scan("sample")
        self.store.execute("UPDATE scans SET completed_at='2000-01-01T00:00:00+00:00' WHERE scan_id=?", (first,))
        (self.root / "delete_runner.py").write_text("print('delete')\n")
        trace = self.engine.trace("sample", [sys.executable, "delete_runner.py"], timeout=5, output_cap=1_000)
        self.store.execute("UPDATE runtime_traces SET finished_at='2000-01-01T00:00:00+00:00' WHERE trace_id=?", (trace["trace_id"],))
        self.store.connection.commit()
        usage = self.engine.storage_usage()
        self.assertGreater(usage["database_bytes"], 0)
        pruned = self.engine.prune_storage(1)
        self.assertEqual(pruned["removed_scans"], 1)
        self.assertEqual(pruned["removed_traces"], 1)
        self.assertEqual(self.queries.traces("sample"), [])
        trace = self.engine.trace("sample", [sys.executable, "delete_runner.py"], timeout=5, output_cap=1_000)
        removed = self.engine.remove_trace("sample", trace["trace_id"])
        self.assertEqual(removed["removed"], 1)
        with self.assertRaises(ThreadError):
            self.engine.remove_trace("sample", trace["trace_id"])
        removed_report = self.engine.remove_report("sample", "overview")
        self.assertEqual(removed_report["removed"], 1)
        with self.assertRaises(ThreadError):
            self.queries.overview("sample")
        output = StringIO()
        with redirect_stdout(output):
            code = main(["storage", "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertIn("usage", json.loads(output.getvalue()))
        self.engine.save_project_preferences("sample", {"storage": {"trace_saving": "none"}})
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            code = main(["trace", "sample", "--yes-run", "--database", str(self.store.path), "--format", "json",
                         "--", sys.executable, "-c", "pass"])
        self.assertEqual(code, 0)
        trace_result = json.loads(output.getvalue())
        self.assertIsNone(trace_result["trace_id"])
        self.assertFalse(trace_result["persisted"])
        self.assertEqual(self.queries.traces("sample"), [])
        output = StringIO()
        with redirect_stdout(output):
            code = main(["delete", "all", "--yes", "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertEqual(self.engine.list_projects(), [])

    def test_discovers_namespace_external_archive_notebook_and_generated_sources(self) -> None:
        import zipfile

        (self.root / "namespace" / "pkg").mkdir(parents=True)
        (self.root / "namespace" / "pkg" / "mod.py").write_text("VALUE = 1\n")
        (self.root / "asset.json").write_text("{}")
        (self.root / "native").mkdir()
        (self.root / "native" / "helper.so").write_bytes(b"native")
        (self.root / "unknown.bin").write_bytes(b"unknown")
        outside_directory = Path(self.temp.name) / "outside"
        outside_directory.mkdir()
        outside = outside_directory / "explicit.py"
        outside.write_text("def external_value():\n    return 2\n")
        with zipfile.ZipFile(self.root / "bundle.zip", "w") as archive:
            archive.writestr("bundle/good.py", "def zipped():\n    return 3\n")
            archive.writestr("../escape.py", "ESCAPED = True\n")
        notebook = {
            "cells": [
                {"cell_type": "markdown", "source": "# notes"},
                {"cell_type": "code", "id": "a", "source": ["def cell_value():\n", "    return 4\n"]},
                {"cell_type": "code", "id": "bad", "source": "def broken(:\n"},
            ]
        }
        (self.root / "demo.ipynb").write_text(json.dumps(notebook))
        self.engine.scan(
            "sample", explicit_sources=["bundle.zip", "demo.ipynb", str(outside)],
            generated=[{"name": "dynamic", "text": "GENERATED = 5\n"}],
        )
        modules = {item["relative_path"]: item for item in self.queries.modules("sample")}
        self.assertEqual(modules["namespace/pkg/mod.py"]["module_name"], "namespace.pkg.mod")
        self.assertEqual(modules["external/explicit.py"]["origin_kind"], "external_file")
        self.assertEqual(modules["external/explicit.py"]["capability"], "Partial")
        self.assertEqual(modules["bundle.zip!/bundle/good.py"]["origin_kind"], "zip_member")
        self.assertEqual(modules["demo.ipynb#a"]["origin_kind"], "notebook_cell")
        self.assertEqual(modules["demo.ipynb#a"]["parse_status"], "parsed")
        self.assertEqual(modules["demo.ipynb#bad"]["parse_status"], "failed")
        self.assertEqual(modules["generated://dynamic"]["origin_kind"], "generated")
        self.assertEqual(modules["generated://dynamic"]["parse_status"], "parsed")
        self.assertTrue(all(item["parser"] == "ast" and item["parser_version"] for item in modules.values()))
        scan = self.engine.scan_history("sample")[0]
        warnings = json.loads(scan["summary_json"])["discovery_warnings"]
        self.assertTrue(any("unsafe archive member path" in warning for warning in warnings))
        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()):
            code = main(["scan", "sample", "--generated", "cli=CLI = 6",
                         "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        generated = {item["relative_path"] for item in self.queries.modules("sample")}
        self.assertIn("generated://cli", generated)
        categories = json.loads(self.engine.scan_history("sample")[0]["summary_json"])["file_categories"]
        self.assertGreaterEqual(categories["metadata"], 1)
        self.assertEqual(categories["resource"], 1)
        self.assertEqual(categories["native"], 1)
        self.assertEqual(categories["unclassified"], 1)
        self.assertGreaterEqual(categories["archive"], 1)
        self.assertGreaterEqual(categories["notebook"], 1)

    def test_discovery_rejects_directory_symlinks_and_classifies_files(self) -> None:
        from threadtrace.parser import discover_source_units, file_category

        outside_directory = Path(self.temp.name) / "outside-dir"
        outside_directory.mkdir()
        (outside_directory / "hidden.py").write_text("HIDDEN = True\n")
        (self.root / "linked").symlink_to(outside_directory, target_is_directory=True)
        outside_file = outside_directory / "linked.py"
        outside_file.write_text("LINKED = True\n")
        (self.root / "linked.py").symlink_to(outside_file)
        units, warnings = discover_source_units(self.root, explicit_sources=["linked"])
        self.assertFalse(any("hidden.py" in unit.relative_path for unit in units))
        self.assertFalse(any(unit.relative_path == "linked.py" for unit in units))
        self.assertTrue(any("directory symlink is not traversed" in warning for warning in warnings))
        self.assertTrue(any("symlink points outside project" in warning for warning in warnings))
        self.assertTrue(any("explicit directories are not traversed" in warning for warning in warnings))
        self.assertEqual(file_category("pyproject.toml"), "metadata")
        self.assertEqual(file_category("assets/logo.json"), "resource")
        self.assertEqual(file_category("native/helper.so"), "native")
        self.assertEqual(file_category("tests/test_api.py"), "test")
        self.assertEqual(file_category("generated/models.py"), "generated")
        self.assertEqual(file_category("unknown.bin"), "unclassified")

    def test_python_aware_bindings_keep_shadowing_reexports_and_uncertainty(self) -> None:
        (self.root / "app" / "__init__.py").write_text(
            "from .helpers import format_name\n__all__ = ['format_name']\n"
        )
        (self.root / "app" / "bindings.py").write_text(
            "from typing import TYPE_CHECKING, overload\n"
            "import importlib\nimport json\n"
            "from .helpers import format_name as fn\n"
            "if TYPE_CHECKING:\n"
            "    from .helpers import format_name as typed_only\n"
            "try:\n"
            "    from .helpers import format_name as conditional_name\n"
            "except ImportError:\n"
            "    conditional_name = None\n"
            "def alias(value):\n"
            "    return fn(value)\n"
            "def shadow(format_name):\n"
            "    return format_name(value='x')\n"
            "def typed(value):\n"
            "    return typed_only(value)\n"
            "def conditional(value):\n"
            "    return conditional_name(value)\n"
            "@overload\n"
            "def double(value: int) -> int: ...\n"
            "@overload\n"
            "def double(value: str) -> str: ...\n"
            "def double(value):\n"
            "    return value\n"
            "def overloaded():\n"
            "    return double(1)\n"
            "def dynamic(name, code):\n"
            "    importlib.import_module(name)\n"
            "    return eval(code)\n"
        )
        (self.root / "app" / "wildcard.py").write_text(
            "from .helpers import *\ndef wild(value):\n    return format_name(value)\n"
        )
        (self.root / "app" / "reexport_use.py").write_text(
            "from app import format_name\ndef use_reexport(value):\n    return format_name(value)\n"
        )
        (self.root / "app" / "uncertain.py").write_text(
            "from missing_package import *\ndef use_unknown():\n    return surprise\n"
        )
        (self.root / "app" / "inheritance.py").write_text(
            "class Base:\n"
            "    @property\n"
            "    def label(self):\n"
            "        return 'base'\n"
            "    def inherited(self):\n"
            "        return 1\n"
            "class Child(Base):\n"
            "    def use(self):\n"
            "        return self.inherited()\n"
            "    def property_use(self):\n"
            "        return self.label\n"
            "    def missing(self):\n"
            "        return self.absent\n"
        )
        self.engine.scan("sample")
        references = self.queries.references("sample", "app.helpers.format_name")
        by_expression = {row["expression"]: row for row in references}
        self.assertEqual(by_expression["fn"]["resolution"], "import")
        self.assertTrue(any(row["expression"] == "format_name" and row["resolution"] == "wildcard"
                            for row in references))
        self.assertEqual(by_expression["typed_only"]["resolution"], "type_checking")
        self.assertEqual(by_expression["conditional_name"]["resolution"], "conditional")
        self.assertTrue(all(row["binding_path"] for row in references))
        self.assertNotIn("shadow", {row["name"] for row in references})
        inherited = self.store.rows(
            "SELECT resolution,binding_path_json FROM symbol_references WHERE scan_id=? AND expression='self.inherited'",
            (self.engine.scan_history("sample")[0]["scan_id"],),
        )
        self.assertEqual(inherited[0]["resolution"], "inherited")
        self.assertTrue(json.loads(inherited[0]["binding_path_json"]))
        descriptor = self.store.rows(
            "SELECT resolution,binding_path_json FROM symbol_references WHERE scan_id=? AND expression='self.label'",
            (self.engine.scan_history("sample")[0]["scan_id"],),
        )
        self.assertEqual(descriptor[0]["resolution"], "inherited")
        self.assertTrue(json.loads(descriptor[0]["binding_path_json"]))
        usage = self.queries.usage("sample", "app.helpers.format_name")
        self.assertGreaterEqual(usage["references"], len(references))
        boundaries = self.queries.boundaries("sample")
        kinds = {row["kind"] for row in boundaries}
        self.assertIn("dynamic_import", kinds)
        self.assertIn("dynamic_code", kinds)
        self.assertIn("unresolved_attribute", kinds)
        wildcard = self.store.rows(
            "SELECT resolution FROM symbol_references WHERE scan_id=? AND expression='surprise'",
            (self.engine.scan_history("sample")[0]["scan_id"],),
        )
        self.assertEqual(wildcard[0]["resolution"], "wildcard_unknown")
        scan_id = self.engine.scan_history("sample")[0]["scan_id"]
        standard = self.store.rows("SELECT source_path FROM imports WHERE scan_id=? AND target_module='json'", (scan_id,))
        self.assertTrue(standard[0]["source_path"].endswith("json/__init__.py"))
        calls = self.store.rows(
            "SELECT target_expression,resolution,binding_path_json FROM calls WHERE scan_id=? AND target_expression IN ('fn','double')",
            (scan_id,),
        )
        self.assertTrue(calls)
        self.assertTrue(all(row["resolution"] == "resolved" and json.loads(row["binding_path_json"]) for row in calls))
        output = StringIO()
        with redirect_stdout(output):
            code = main(["references", "sample", "app.helpers.format_name", "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue()))

    def test_metadata_parsers_preserve_groups_markers_and_partial_failures(self) -> None:
        from threadtrace.metadata import parse_metadata

        root = Path(self.temp.name) / "metadata-project"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            "[project]\nname='demo'\ndependencies=['requests[socks]>=2; python_version >= 3.8']\n"
            "[project.optional-dependencies]\nweb=['flask>=3']\n"
            "[dependency-groups]\ndev=['pytest']\n"
            "[build-system]\nrequires=['setuptools>=68']\n"
        )
        (root / "requirements.txt").write_text(
            "-r requirements-dev.txt\n-c constraints.txt\n-e ./localpkg\n"
            "demo @ https://example.com/demo.whl\n"
            "urllib3[secure]>=2; python_version < '3.12'\n"
        )
        (root / "requirements-dev.txt").write_text("pytest>=8\n")
        (root / "constraints.txt").write_text("urllib3<3\n")
        (root / "setup.cfg").write_text("[options]\ninstall_requires =\n    click>=8\n[options.extras_require]\ntest =\n    coverage\n")
        (root / "setup.py").write_text(
            "from setuptools import setup\nsetup(name='demo', install_requires=['attrs'], extras_require={'dev': ['ruff']})\n"
        )
        (root / "Pipfile").write_text(
            "[packages]\nflask = {version='>=3', extras=['async'], markers='python_version > \\\"3.9\\\"'}\n"
            "[dev-packages]\nblack = '*'\n"
        )
        (root / "Pipfile.lock").write_text(json.dumps({
            "default": {"idna": {"version": "==3.6", "markers": "python_version >= '3.6'"}},
            "develop": {"pytest": {"version": "==8.0"}},
        }))
        (root / "poetry.lock").write_text(
            "[[package]]\nname = 'anyio'\nversion = '4.0'\nmarkers = \"python_version >= '3.8'\"\n"
        )
        (root / "uv.lock").write_text("[[package]]\nname = 'httpx'\nversion = '0.27'\n")
        (root / "environment.yml").write_text(
            "name: demo\ndependencies:\n  - python=3.11\n  - pip:\n    - requests==2.32\n"
        )
        (root / "custom.lock").write_text("unsupported metadata")
        snapshot = parse_metadata(root)
        by_name = {item.name: item for item in snapshot.dependencies}
        pyproject_requests = next(item for item in snapshot.dependencies
                                  if item.name == "requests" and item.source_file == "pyproject.toml")
        self.assertEqual(pyproject_requests.marker, "python_version >= 3.8")
        self.assertEqual(pyproject_requests.extras, ["socks"])
        self.assertTrue(any(item.name == "flask" and item.group == "extras:web" for item in snapshot.dependencies))
        self.assertTrue(any(item.name == "pytest" and item.group == "group:dev" for item in snapshot.dependencies))
        self.assertTrue(by_name["localpkg"].editable)
        self.assertEqual(by_name["demo"].direct_url, "https://example.com/demo.whl")
        self.assertTrue(any(item.name == "urllib3" and item.group == "constraint" for item in snapshot.dependencies))
        self.assertEqual(by_name["black"].group, "dev-packages")
        self.assertTrue(any(item.name == "anyio" for item in snapshot.dependencies))
        self.assertTrue(any(item.name == "httpx" for item in snapshot.dependencies))
        self.assertTrue(any(item.metadata_kind == "conda" for item in snapshot.dependencies))
        self.assertEqual(next(item for item in snapshot.files if item.relative_path == "custom.lock").status, "unsupported")
        (root / "pyproject.toml").write_text("[project\n")
        broken = parse_metadata(root)
        self.assertEqual(next(item for item in broken.files if item.relative_path == "pyproject.toml").status, "failed")
        self.assertTrue(any(item.name == "click" for item in broken.dependencies))
        self.engine.scan("sample")
        metadata = self.queries.metadata("sample")
        self.assertTrue(metadata["files"])
        self.assertTrue(any(item["name"] == "requests" for item in metadata["dependencies"]))
        output = StringIO()
        with redirect_stdout(output):
            code = main(["metadata", "sample", "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["files"])

    def test_staleness_reports_source_metadata_discovery_and_interpreter_causes(self) -> None:
        self.assertFalse(self.queries.staleness("sample")["stale"])
        (self.root / "app" / "helpers.py").write_text("def format_name(value):\n    return value.upper()\n")
        (self.root / "pyproject.toml").write_text(
            "[project]\nname='sample'\nversion='0'\ndependencies=['requests>=2','httpx>=0.27']\n"
        )
        self.engine.save_project_preferences("sample", {"discovery": {"exclude": ["generated/*"]}})
        scan_id = self.engine.scan_history("sample")[0]["scan_id"]
        self.store.execute(
            "UPDATE scan_inputs SET value=? WHERE scan_id=? AND input_key='interpreter'",
            ('{"executable":"/different/python","version":"3.11","implementation":"cpython"}', scan_id),
        )
        report = self.queries.staleness("sample")
        causes = {item["cause"] for item in report["causes"]}
        self.assertTrue(report["stale"])
        self.assertIn("source_changed", causes)
        self.assertIn("metadata_changed", causes)
        self.assertIn("discovery_changed", causes)
        self.assertIn("interpreter_changed", causes)
        for item in report["causes"]:
            self.assertTrue(item["capabilities"])
        output = StringIO()
        with redirect_stdout(output):
            code = main(["staleness", "sample", "--database", str(self.store.path), "--format", "json"])
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(output.getvalue())["stale"])

    def test_failed_migration_restores_the_previous_database(self) -> None:
        database = Path(self.temp.name) / "failed-migration.sqlite3"
        store = Store(database)
        store.close()
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA user_version = 12")
        connection.commit()
        connection.close()
        with self.assertRaises(sqlite3.OperationalError):
            Store(database)
        self.assertTrue(Path(str(database) + ".bak").exists())
        self.assertFalse(Path(str(database) + "-wal").exists())
        restored = sqlite3.connect(database)
        try:
            self.assertEqual(restored.execute("PRAGMA user_version").fetchone()[0], 12)
        finally:
            restored.close()

    def test_database_permissions_and_busy_timeout(self) -> None:
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        timeout = self.store.connection.execute("PRAGMA busy_timeout").fetchone()[0]
        self.assertEqual(timeout, 5000)

    def test_loopback_browser_prepares_read_only_analysis(self) -> None:
        from threadtrace.browser import _PAGE, _payload, is_loopback_host

        payload = _payload(self.queries, "sample", "overview")
        self.assertEqual(payload["project"]["name"], "sample")
        self.assertIn("Read-only local view", _PAGE)
        self.assertTrue(is_loopback_host("127.0.0.1"))
        self.assertTrue(is_loopback_host("::1"))
        self.assertFalse(is_loopback_host("0.0.0.0"))

    def test_loopback_browser_serves_json_endpoints(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from urllib.error import HTTPError
        from urllib.request import urlopen

        from threadtrace.browser import BrowserServer

        try:
            server = BrowserServer("127.0.0.1", 0, self.queries, "sample")
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM, errno.EADDRNOTAVAIL}:
                self.skipTest("Loopback sockets are prohibited by this environment")
            raise

        def fetch(path: str) -> tuple[int, str, bytes]:
            try:
                with urlopen(server.url.rstrip("/") + path, timeout=2) as response:
                    return response.status, response.headers.get_content_type(), response.read()
            except HTTPError as exc:
                return exc.code, exc.headers.get_content_type(), exc.read()

        server.timeout = 2
        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                responses = {}
                for endpoint in ("overview", "architecture", "capabilities"):
                    future = executor.submit(fetch, f"/api/{endpoint}")
                    server.handle_request()
                    responses[endpoint] = future.result(timeout=2)
                future = executor.submit(fetch, "/api/unknown")
                server.handle_request()
                unknown = future.result(timeout=2)
        finally:
            server.server_close()

        for status, content_type, body in responses.values():
            self.assertEqual(status, 200)
            self.assertEqual(content_type, "application/json")
            json.loads(body)
        self.assertEqual(json.loads(responses["overview"][2])["project"]["name"], "sample")
        self.assertIn("relationships", json.loads(responses["architecture"][2]))
        self.assertIsInstance(json.loads(responses["capabilities"][2]), list)
        self.assertEqual(unknown[:2], (404, "application/json"))
        self.assertLess(len(unknown[2]), 200)
        self.assertEqual(json.loads(unknown[2])["error"], "Unknown local analysis endpoint.")

    def test_browser_api_failure_is_bounded_json(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from urllib.error import HTTPError
        from urllib.request import urlopen

        from threadtrace.browser import BrowserServer

        try:
            server = BrowserServer("127.0.0.1", 0, self.queries, "sample")
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM, errno.EADDRNOTAVAIL}:
                self.skipTest("Loopback sockets are prohibited by this environment")
            raise

        def fetch() -> tuple[int, str, bytes]:
            try:
                with urlopen(server.url + "api/overview", timeout=2) as response:
                    return response.status, response.headers.get_content_type(), response.read()
            except HTTPError as exc:
                return exc.code, exc.headers.get_content_type(), exc.read()

        server.timeout = 2
        try:
            with patch("threadtrace.browser._payload", side_effect=RuntimeError("private failure detail")):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(fetch)
                    server.handle_request()
                    status, content_type, body = future.result(timeout=2)
        finally:
            server.server_close()

        self.assertEqual(status, 500)
        self.assertEqual(content_type, "application/json")
        self.assertLess(len(body), 200)
        self.assertEqual(json.loads(body)["error"], "Could not build local analysis response.")
        self.assertNotIn(b"private failure detail", body)

    def test_browser_graph_families_paginate_filter_and_export_mermaid(self) -> None:
        from threadtrace.browser import graph_mermaid, graph_payload

        targets = {
            "dependency": "app.core",
            "call": "app.core.Service.process",
            "class": "app.core.Service",
            "ast": "app.core",
            "dataflow": "app.core.Service.process",
            "control-flow": "app.core.Service.process",
            "execution-flow": "main",
        }
        for family, target in targets.items():
            with self.subTest(family=family):
                payload = graph_payload(self.queries, "sample", family=family, target=target, limit=5)
                self.assertEqual(payload["family"], family)
                self.assertTrue(payload["nodes"])
                identifiers = {node["id"] for node in payload["nodes"]}
                for edge in payload["edges"]:
                    self.assertIn(edge["source"], identifiers)
                    self.assertIn(edge["target"], identifiers)
                    self.assertIn(edge["evidence_class"],
                                  {"statically resolved", "heuristically predicted", "observed at runtime", "unknown"})
                self.assertEqual(payload["capability"]["status"], "Partial")
                self.assertIn("stale", payload)
                self.assertEqual(payload["page"]["invalid_edges"], 0)
                mermaid = graph_mermaid(payload)
                self.assertIn("flowchart LR", mermaid)
                if payload["edges"]:
                    self.assertIn("-->", mermaid)

        page = graph_payload(self.queries, "sample", family="dependency", limit=1)
        self.assertGreater(page["page"]["edge_total"], 1)
        self.assertEqual(page["page"]["returned_edges"], 1)
        self.assertEqual(page["page"]["next_offset"], 1)
        self.assertTrue(page["page"]["truncated"])
        filtered = graph_payload(self.queries, "sample", family="dependency", target="app.core",
                                 kinds={"import"}, evidence={"statically resolved"}, q="helpers")
        self.assertTrue(filtered["edges"])
        self.assertTrue(all(edge["kind"] == "import" for edge in filtered["edges"]))
        self.assertTrue(all("helpers" in edge["source"] + edge["target"] for edge in filtered["edges"]))

    def test_browser_mermaid_escapes_labels_and_drops_unknown_edges(self) -> None:
        from threadtrace.browser import graph_mermaid

        value = {
            "nodes": [{"id": "a", "label": 'evil "name"\n<tag>|pipe'}],
            "edges": [{"source": "a", "target": "missing", "kind": 'call"|evil', "label": "bad"}],
        }
        mermaid = graph_mermaid(value)
        self.assertIn("flowchart LR", mermaid)
        self.assertIn("#quot;", mermaid)
        self.assertIn("#lt;", mermaid)
        self.assertIn("#124;", mermaid)
        self.assertNotIn('evil "name"', mermaid)
        self.assertNotIn("missing", mermaid)
        self.assertIn("Dropped 1 edge", mermaid)

    def test_loopback_browser_serves_graph_source_search_and_read_only_controls(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from urllib.error import HTTPError
        from urllib.request import Request, urlopen

        from threadtrace.browser import BrowserServer

        try:
            server = BrowserServer("127.0.0.1", 0, self.queries, "sample")
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EPERM, errno.EADDRNOTAVAIL}:
                self.skipTest("Loopback sockets are prohibited by this environment")
            raise

        (Path(self.temp.name) / "outside.py").write_text("secret = 1\n")

        def fetch(path: str, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, Any, bytes]:
            request = Request(server.url.rstrip("/") + path, method=method,
                              data=b"{}" if method != "GET" else None, headers=headers or {})
            try:
                with urlopen(request, timeout=2) as response:
                    return response.status, response.headers, response.read()
            except HTTPError as exc:
                try:
                    return exc.code, exc.headers, exc.read()
                finally:
                    exc.close()

        def call(path: str, method: str = "GET", headers: dict[str, str] | None = None) -> tuple[int, Any, bytes]:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(fetch, path, method, headers)
                server.handle_request()
                return future.result(timeout=2)

        server.timeout = 2
        try:
            page = call("/")
            graph = call("/api/graph?family=dependency&target=app.core&limit=1")
            mermaid = call("/api/graph.mmd?family=dependency&target=app.core")
            source = call("/api/source?path=app/core.py&line=4&context=2")
            traversal = call("/api/source?path=../outside.py")
            search = call("/api/search?q=format_name")
            unknown = call("/api/graph?family=missing&target=app.core")
            posted = call("/api/graph", method="POST")
            wrong_host = call("/api/graph?family=ast", headers={"Host": "evil.example"})
            wrong_origin = call("/api/graph?family=ast", headers={"Origin": "http://evil.example"})
        finally:
            server.server_close()

        self.assertEqual(page[0], 200)
        self.assertEqual(page[1].get_content_type(), "text/html")
        self.assertIn(b'id="graph"', page[2])
        self.assertIn(b"graph.mmd", page[2])
        self.assertIn(b"Control-flow graph", page[2])

        self.assertEqual(graph[0], 200)
        payload = json.loads(graph[2])
        self.assertEqual(payload["page"]["limit"], 1)
        self.assertLessEqual(payload["page"]["returned_edges"], 1)
        self.assertTrue(payload["nodes"])

        self.assertEqual(mermaid[0], 200)
        self.assertEqual(mermaid[1].get_content_type(), "text/plain")
        self.assertIn(b"flowchart LR", mermaid[2])

        self.assertEqual(source[0], 200)
        snippet = json.loads(source[2])
        self.assertEqual(snippet["path"], "app/core.py")
        self.assertIn("class Service", "\n".join(line["text"] for line in snippet["lines"]))
        self.assertNotIn(str(self.root), source[2].decode())

        self.assertEqual(traversal[0], 400)
        self.assertIn("registered project", json.loads(traversal[2])["error"])
        self.assertNotIn(b"secret = 1", traversal[2])

        self.assertEqual(search[0], 200)
        results = json.loads(search[2])["results"]
        self.assertTrue(any(item["target"] == "app.helpers.format_name" and item["family"] == "call"
                            for item in results))

        self.assertEqual(unknown[0], 400)
        self.assertIn("Unknown graph family", json.loads(unknown[2])["error"])
        self.assertEqual(posted[0], 405)
        self.assertEqual(posted[1]["Allow"], "GET")
        self.assertEqual(json.loads(posted[2])["error"], "This local browser view is read-only.")
        self.assertEqual(wrong_host[0], 403)
        self.assertEqual(wrong_origin[0], 403)

    def test_tui_keybindings_modes_and_compact_threshold(self) -> None:
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from threadtrace.tui import ASCII_GLYPHS, COMMANDS, DEFAULT_KEYS, ThreadApp, compact_required, resolve_keys

        from threadtrace.config import load

        self.assertEqual(resolve_keys([]), DEFAULT_KEYS)
        self.assertEqual(resolve_keys(["overview=n"])["overview"], "n")
        with self.assertRaisesRegex(ValueError, "Unknown TUI keybinding"):
            resolve_keys(["missing=x"])
        with self.assertRaisesRegex(ValueError, "assigned to both"):
            resolve_keys(["overview=m"])
        with self.assertRaisesRegex(ValueError, "reserved"):
            resolve_keys(["overview=t"])
        with self.assertRaises(ValueError):
            resolve_keys(["overview="])
        self.assertFalse(compact_required(80, 20))
        self.assertTrue(compact_required(79, 40))
        self.assertTrue(compact_required(120, 19))
        self.assertTrue(compact_required(120, 40, True))

        path = Path(self.temp.name) / "keys.yaml"
        path.write_text("tui:\n  keybindings: [overview=n, quit=z]\n")
        self.assertEqual(load(path)["tui"]["keybindings"], ["overview=n", "quit=z"])
        path.write_text("tui:\n  keybindings: [overview=n, modules=n]\n")
        with self.assertRaisesRegex(ValueError, "more than once"):
            load(path)
        path.write_text("tui:\n  keybindings: [overview=n, overview=m]\n")
        with self.assertRaisesRegex(ValueError, "repeats action"):
            load(path)

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = ThreadApp(self.engine, self.queries, "sample", {"compact": True, "ascii": True, "colour": False})
            self.assertTrue(app.compact)
            self.assertEqual(app.glyphs, ASCII_GLYPHS)
            app._load_view("functions")
            rendered = "".join(text for _, text in app._results_text())
            self.assertTrue(rendered.startswith("> "))
            self.assertNotIn("▶", rendered)
            app._set_compact_pane("inspector")
            self.assertEqual(app.compact_pane, "inspector")
            self.assertEqual(app.results_frame.title, "Inspector")
            self.assertEqual(app.app.layout.current_control, app.inspector.control)
            app._set_compact_pane("results")
            self.assertEqual(app.app.layout.current_control, app.results)
            app._load_view("functions")
            app.selected = next(index for index, row in enumerate(app.rows) if row["qualified_name"] == "app.helpers.format_name")
            app._go_to_definition()
            self.assertEqual(app.current_view, "definitions")
            self.assertTrue(app.rows)
            app._open_references()
            self.assertEqual(app.current_view, "references")
            self.assertTrue(any(row["relative_path"] == "app/main.py" for row in app.rows))
            from prompt_toolkit.document import Document
            from threadtrace.tui import PythonLexer
            fragments = PythonLexer().lex_document(Document("def f():\n    # note\n    return 1\n"))(2)
            self.assertIn(("class:token.keyword", "return"), fragments)
            self.assertIn(("class:token.comment", "# note"), PythonLexer().lex_document(Document("# note"))(0))
            source = self.root / "app" / "helpers.py"
            app._open_fullscreen("value = 1\n", "Code", source, 1, full_source="value = 1\n")
            app._toggle_edit_mode()
            app.source_editor.text = "value = 2\n"
            app._close_fullscreen()
            self.assertTrue(app.source_mode)
            self.assertEqual(app.status_level, "warning")
            app.source_editor.text = app.source_original
            app._close_fullscreen()
            self.assertFalse(app.source_mode)
            app.rows, app.current_view, app.selected = app._command_entries(""), "commands", 0
            self.assertEqual({row["name"] for row in app.rows}, set(COMMANDS))
            self.assertIn(":overview", "".join(text for _, text in app._results_text()))
            app._run_command("ascii")
            self.assertFalse(app.ascii)
            app._run_command("colour")
            self.assertTrue(app.colour)
            app._run_command("compact")
            self.assertFalse(app.compact)
            app.compact_preference = False
            with patch("threadtrace.tui._terminal_size", return_value=(60, 15)):
                app._sync_terminal_size()
            self.assertTrue(app.compact)
            with patch("threadtrace.tui._terminal_size", return_value=(120, 40)):
                app._sync_terminal_size()
            self.assertFalse(app.compact)
            self.assertEqual(app.status_level, "info")
            app._run_command("missing")
            self.assertEqual(app.status_level, "error")

    def test_tui_source_editing_checks_syntax_diff_and_hash(self) -> None:
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from threadtrace.tui import ThreadApp

        source = self.root / "app" / "helpers.py"
        original = source.read_text(encoding="utf-8")
        edited = original.replace("value.strip()", "value.strip().lower()")
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = ThreadApp(self.engine, self.queries, "sample", {"compact": True})
            app._open_fullscreen(original, "Code", source, 1, full_source=original)
            app._toggle_edit_mode()
            self.assertTrue(app.edit_mode)
            app.source_editor.text = "def broken(:\n"
            app._save_source(rescan=False)
            self.assertFalse(app.confirm_mode)
            self.assertIn("syntax", app.status.lower())
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            app.source_editor.text = edited
            app._save_source(rescan=False)
            self.assertTrue(app.confirm_mode)
            self.assertIn("+    return value.strip().lower()", app.save_diff)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            app._cancel_save()
            self.assertFalse(app.confirm_mode)
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            app.source_editor.text = edited
            app._save_source(rescan=False)
            app._confirm_save()
            self.assertFalse(app.confirm_mode)
            self.assertEqual(source.read_text(encoding="utf-8"), edited)
            self.assertFalse(list(source.parent.glob(".threadtrace-*.tmp")))
            app._toggle_edit_mode()
            app.source_editor.text = edited + "\n# pending\n"
            source.write_text(edited + "\n# other writer\n", encoding="utf-8")
            app._save_source(rescan=False)
            self.assertIn("changed outside", app.status)
            self.assertEqual(source.read_text(encoding="utf-8"), edited + "\n# other writer\n")

    def test_tui_rescan_preserves_view_and_selected_row(self) -> None:
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input.defaults import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        from threadtrace.tui import ThreadApp

        with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
            app = ThreadApp(self.engine, self.queries, "sample", {"compact": True})
            app._load_view("functions")
            app.selected = min(2, len(app.rows) - 1)
            identity = app._row_identity(app.rows[app.selected])
            self.assertTrue(app._rescan())
            self.assertEqual(app.current_view, "functions")
            self.assertEqual(app._row_identity(app.rows[app.selected]), identity)
            self.assertEqual(app.status_level, "good")
            app._open_graph("file")
            node = app.rows[app.selected]["id"]
            self.assertTrue(app._rescan())
            self.assertEqual(app.graph_data["kind"], "file")
            self.assertEqual(app.rows[app.selected]["id"], node)

    @staticmethod
    def _fake_http(payload: dict[str, Any]) -> Any:
        class FakeResponse:
            def read(self) -> bytes:
                return json.dumps(payload).encode("utf-8")

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *exc: Any) -> bool:
                return False

        return FakeResponse()

    def test_ai_provider_redaction_approval_and_validation(self) -> None:
        from threadtrace import ai

        facts = {
            "target": {"qualified_name": "app.helpers.format_name", "relative_path": "app/helpers.py",
                       "line_start": 1, "purpose": "Format a name.", "password": "hunter2"},
            "configuration": {"note": "api_key=abc123"},
            "project_overview": {"purpose": "Sample", "components": []},
        }
        structured = {
            "summary": "Formats names.",
            "claims": [
                {"text": "It strips whitespace.", "evidence": "app/helpers.py:1"},
                {"text": "Runtime latency is unknown.", "evidence": "unknown"},
            ],
            "mermaid": 'graph TD\nA["app.helpers.format_name"]\nA --> A',
        }
        local = ai.settings_from({"provider": "ollama", "model": "test", "temperature": 0.1, "token_limit": 200})
        sent: dict[str, Any] = {}

        def fake_urlopen(request: Any, timeout: int) -> Any:
            sent.update(url=request.full_url, body=json.loads(request.data), timeout=timeout)
            return self._fake_http({"message": {"content": json.dumps(structured)}})

        with patch("threadtrace.ai.urlopen", side_effect=fake_urlopen):
            result = ai.explain(facts, local)
        self.assertEqual(sent["url"], "http://127.0.0.1:11434/api/chat")
        self.assertEqual(sent["body"]["model"], "test")
        self.assertEqual(sent["body"]["options"]["num_predict"], 200)
        self.assertNotIn("hunter2", json.dumps(sent["body"]))
        self.assertNotIn("abc123", json.dumps(sent["body"]))
        self.assertGreater(result["request"]["manifest"]["redactions"], 0)
        self.assertEqual(result["response"]["claims"][0]["evidence"], "app/helpers.py:1")
        self.assertIn("app/helpers.py:1", result["narrative"])

        remote = ai.settings_from({
            "provider": "openai", "endpoint": "http://10.0.0.5:9000/v1/chat/completions",
        })
        with self.assertRaisesRegex(ai.AIError, "requires fresh approval"):
            ai.explain(facts, remote)
        approved: dict[str, Any] = {}

        def approve(request: dict[str, Any]) -> bool:
            approved.update(request)
            return True

        with patch.dict(os.environ, {"THREADTRACE_AI_API_KEY": "top-secret-key"}), \
                patch("threadtrace.ai.urlopen", return_value=self._fake_http(
                    {"choices": [{"message": {"content": json.dumps(structured)}}]})) as mocked:
            result = ai.explain(facts, remote, approve)
        sent_request = mocked.call_args.args[0]
        self.assertEqual(sent_request.headers.get("Authorization"), "Bearer top-secret-key")
        self.assertNotIn("top-secret-key", sent_request.data.decode("utf-8"))
        self.assertEqual(approved["digest"], result["request"]["digest"])
        self.assertTrue(result["request"]["remote"])

        invented = dict(structured, claims=[{"text": "Imaginary", "evidence": "app/ghost.py:7"}])
        with patch("threadtrace.ai.urlopen", return_value=self._fake_http({"message": {"content": json.dumps(invented)}})):
            with self.assertRaisesRegex(ai.AIError, "not present"):
                ai.explain(facts, local)
        bad_diagram = dict(structured, mermaid='graph TD\nA["app.ghost.symbol"]\nA --> A')
        with patch("threadtrace.ai.urlopen", return_value=self._fake_http({"message": {"content": json.dumps(bad_diagram)}})):
            with self.assertRaisesRegex(ai.AIError, "not present"):
                ai.explain(facts, local)
        with patch("threadtrace.ai.urlopen", return_value=self._fake_http({"message": {"content": "not json"}})):
            with self.assertRaisesRegex(ai.AIError, "invalid JSON"):
                ai.explain(facts, local)

        template = Path(self.temp.name) / "prompt.txt"
        template.write_text("CUSTOM {facts}")
        with patch("threadtrace.ai.urlopen", side_effect=fake_urlopen):
            ai.explain(facts, ai.settings_from({"provider": "ollama", "template_path": str(template)}))
        self.assertTrue(sent["body"]["messages"][1]["content"].startswith("CUSTOM {"))
        template.write_text("no placeholder")
        with self.assertRaisesRegex(ai.AIError, "placeholder"):
            ai.explain(facts, ai.settings_from({"provider": "ollama", "template_path": str(template)}))

        with self.assertRaisesRegex(ai.AIError, "Unsupported ollama parameter"):
            ai.settings_from({"provider": "ollama", "parameters": ["bogus=1"]})
        with self.assertRaisesRegex(ai.AIError, "credentials"):
            ai.settings_from({"provider": "openai", "endpoint": "https://user:pass@example.com/v1"})
        with self.assertRaisesRegex(ai.AIError, "temperature"):
            ai.settings_from({"provider": "ollama", "temperature": 3})

    def test_ai_exchange_is_persisted_listed_and_deletable(self) -> None:
        content = json.dumps({"summary": "Format names.", "claims": [
            {"text": "Strips whitespace.", "evidence": "app/helpers.py:1"}], "mermaid": None})
        with patch("threadtrace.ai.urlopen", return_value=self._fake_http({"message": {"content": content}})):
            result = self.queries.ai_explain("sample", "app.helpers.format_name",
                                             {"provider": "ollama", "model": "fake"})
        self.assertEqual(result["provider"], "ollama")
        self.assertEqual(result["model"], "fake")
        self.assertIn("Format names.", result["narrative"])
        self.assertTrue(result["report_kind"].startswith("ai_explain:"))
        self.assertIn("redactions", result["request"]["manifest"])
        history = self.queries.ai_history("sample")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["target"], "app.helpers.format_name")
        self.assertEqual(history[0]["claims"], 1)
        self.assertIn("delete report", history[0]["delete_with"])
        self.engine.remove_report("sample", history[0]["report_kind"])
        self.assertEqual(self.queries.ai_history("sample"), [])

    def test_ai_cli_requires_digest_for_remote_and_shows_payload(self) -> None:
        content = json.dumps({"summary": "Format names.", "claims": [], "mermaid": None})
        base = ["explain", "sample", "app.helpers.format_name", "--ai", "--ai-provider", "openai",
                "--ai-endpoint", "http://10.0.0.5:9000/v1/chat/completions", "--database", str(self.store.path)]
        errors = StringIO()
        with redirect_stdout(StringIO()), redirect_stderr(errors), \
                patch.dict(os.environ, {"THREADTRACE_AI_API_KEY": "secret-value"}):
            self.assertEqual(main(base + ["--ai-show"]), 0)
        preview = json.loads(errors.getvalue())
        digest = preview["digest"]
        self.assertTrue(preview["remote"])
        self.assertIn("app.helpers.format_name", json.dumps(preview["payload"]))
        self.assertNotIn("secret-value", json.dumps(preview))

        with redirect_stdout(StringIO()), redirect_stderr(errors := StringIO()), \
                patch.dict(os.environ, {"THREADTRACE_AI_API_KEY": "secret-value"}):
            self.assertEqual(main(base), 2)
        self.assertIn(digest, errors.getvalue())
        self.assertIn("--ai-approve", errors.getvalue())

        output = StringIO()
        with redirect_stdout(output), redirect_stderr(StringIO()), patch.dict(os.environ, {"THREADTRACE_AI_API_KEY": "secret-value"}), \
                patch("threadtrace.ai.urlopen", return_value=self._fake_http(
                    {"choices": [{"message": {"content": content}}]})):
            self.assertEqual(main(base + ["--ai-approve", digest]), 0)
        self.assertIn("Format names.", output.getvalue())
        self.assertEqual(len(self.queries.ai_history("sample")), 1)

    def test_ai_configuration_validates_provider_settings(self) -> None:
        from threadtrace.ai import AIError, settings_from
        from threadtrace.config import load

        path = Path(self.temp.name) / "ai.yaml"
        path.write_text(
            "ai:\n  provider: openai\n  model: local-model\n  temperature: 0.5\n"
            "  generation_limit: 128\n  timeout_seconds: 5\n  key_env: MY_AI_KEY\n"
            "  parameters: [top_p=0.9, seed=7]\n"
        )
        configuration = load(path)
        self.assertEqual(configuration["ai"]["temperature"], 0.5)
        self.assertEqual(configuration["ai"]["generation_limit"], 128)
        settings = settings_from(configuration["ai"])
        self.assertEqual(settings.provider, "openai")
        self.assertEqual(settings.parameters, {"top_p": 0.9, "seed": 7})
        self.assertFalse(settings.remote)
        path.write_text("ai:\n  provider: unknown\n")
        with self.assertRaisesRegex(ValueError, "provider"):
            load(path)
        path.write_text("ai:\n  temperature: 3\n")
        with self.assertRaisesRegex(ValueError, "temperature"):
            load(path)
        path.write_text("ai:\n  parameters: [top_p=0.9, top_p=0.5]\n")
        with self.assertRaisesRegex(ValueError, "repeats parameter"):
            load(path)
        with self.assertRaisesRegex(AIError, "Unsupported openai parameter"):
            settings_from({"provider": "openai", "parameters": ["num_ctx=1"]})

    def test_ai_prompt_is_bounded_and_evidence_only(self) -> None:
        from threadtrace.ai import _prompt

        prompt = _prompt({"evidence": {"path": "app/main.py", "line": 4}, "large": "x" * 2_000})
        self.assertIn("app/main.py", prompt)
        self.assertNotIn("x" * 1_001, prompt)


if __name__ == "__main__":
    unittest.main()
