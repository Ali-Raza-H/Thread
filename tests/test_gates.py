"""Compatibility, accuracy, safety, and performance gates for v1 claims."""

from __future__ import annotations

import json
import sys
import tempfile
import time
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from fixtures import FIXTURES, write_fixture, write_large_package
from threadtrace.capabilities import CAPABILITIES, capability_report
from threadtrace.cli import main
from threadtrace.engine import Engine, ThreadError
from threadtrace.exports import report_bundle
from threadtrace.query import QueryService
from threadtrace.store import Store
from threadtrace.test_evidence import CAPABILITY_TESTS, VERIFIED_PYTHON

PROJECT_ROOT = Path(__file__).resolve().parents[1]

EXPECTATIONS: dict[str, dict[str, set[str]]] = {
    "script": {"modules": {"fixture_script"}, "entries": {"script"}},
    "package": {"modules": {"fixture_pkg.service"}, "functions": {"fixture_pkg.service.Service.run"}},
    "namespace": {"modules": {"fixture_ns.plugins.loader"}},
    "malformed": {"modules": {"fixture_broken.valid"}, "failed": {"fixture_broken.broken"}},
    "cli": {"entries": {"cli"}},
    "web": {"entries": {"route"}},
    "database": {"resources": {"images.db"}},
    "concurrency": {"functions": {"fixture_concurrent.workers.start", "fixture_concurrent.processes.start"}},
    "imports": {"modules": {"fixture_imports.reexport"}},
    "exceptions": {"functions": {"fixture_errors.handling.risky"}},
    "adapters": {"resources": {"state.json", "store.sqlite3"}},
}


class GateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "thread.sqlite3")
        self.engine = Engine(self.store)
        self.queries = QueryService(self.engine)

    def tearDown(self) -> None:
        self.store.close()
        self.temp.cleanup()

    def add_project(self, name: str, files: dict[str, str] | None = None) -> dict:
        root = Path(self.temp.name) / name
        root.mkdir()
        if files is None:
            write_fixture(root, name)
        else:
            for relative, text in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        self.engine.add_project(str(root), name)
        return self.engine.scan(name)

    def test_fixture_families_scan_offline_without_target_execution(self) -> None:
        self.assertEqual(set(EXPECTATIONS), set(FIXTURES))
        for name, expected in EXPECTATIONS.items():
            with self.subTest(fixture=name):
                scan = self.add_project(name)
                self.assertEqual(scan["status"], "completed")
                modules = {item["module_name"]: item for item in self.queries.modules(name)}
                self.assertTrue(expected.get("modules", set()).issubset(modules), sorted(modules))
                entries = {item["kind"] for item in self.queries.entrypoints(name)}
                self.assertTrue(expected.get("entries", set()).issubset(entries), entries)
                functions = {item["qualified_name"] for item in self.queries.functions(name)}
                self.assertTrue(expected.get("functions", set()).issubset(functions), sorted(functions))
                resources = {item["name"] for item in self.queries.resources(name)}
                self.assertTrue(expected.get("resources", set()).issubset(resources), sorted(resources))
                failed = {path for path, item in modules.items() if item["parse_status"] != "parsed"}
                self.assertTrue(expected.get("failed", set()).issubset(failed), failed)
                self.assertFalse([item for item in sys.modules if item.startswith("fixture_")])
                self.assertFalse(list((Path(self.temp.name) / name).rglob("__pycache__")))

    def test_heuristic_corpus_measures_precision_and_recall(self) -> None:
        self.add_project("accuracy", {
            "fixture_accuracy/__init__.py": "",
            "fixture_accuracy/targets.py": "def run(value: int) -> int:\n    return value\n",
            "fixture_accuracy/other.py": "def run(value: int) -> int:\n    return value + 1\n",
            "fixture_accuracy/confirmed.py": (
                "from .targets import run\n\n\ndef caller() -> int:\n    return run(1)\n"
            ),
            "fixture_accuracy/ambiguous.py": "def caller():\n    return run(2)\n",
        })
        rows = self.queries.callers("accuracy", "fixture_accuracy.targets.run")
        resolved = {row["caller"] for row in rows if row["resolution"] == "resolved"}
        expected = {"fixture_accuracy.confirmed.caller"}
        precision = len(resolved & expected) / len(resolved) if resolved else 1.0
        recall = len(resolved & expected) / len(expected)
        print(f"\naccuracy corpus: precision={precision:.2f} recall={recall:.2f}")
        self.assertEqual(precision, 1.0, sorted(resolved - expected))
        self.assertGreaterEqual(recall, 0.9)
        self.assertNotIn("fixture_accuracy.ambiguous.caller", resolved)
        self.assertNotIn("fixture_accuracy.other.run", resolved)

    def test_capability_manifest_names_existing_offline_tests(self) -> None:
        available: set[str] = set()

        def collect(suite: unittest.TestSuite) -> None:
            for test in suite:
                if isinstance(test, unittest.TestSuite):
                    collect(test)
                else:
                    available.add(test._testMethodName)

        collect(unittest.defaultTestLoader.discover(str(Path(__file__).parent)))
        self.assertEqual({item["feature"] for item in CAPABILITIES}, set(CAPABILITY_TESTS))
        for item in CAPABILITIES:
            tests = CAPABILITY_TESTS[item["feature"]]
            self.assertTrue(tests, item["feature"])
            self.assertTrue(set(tests).issubset(available), sorted(set(tests) - available))
        report = capability_report()
        for row in report:
            self.assertTrue(row["tests"], row["feature"])
            self.assertEqual(row["verified_on"], VERIFIED_PYTHON if row["status"] == "Full" else None)

    def test_advertised_metadata_matches_verified_interpreter(self) -> None:
        metadata = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
        self.assertEqual(metadata["requires-python"], ">=3.11")
        self.assertEqual(metadata["classifiers"], [
            "Programming Language :: Python :: 3",
            "Programming Language :: Python :: 3.11",
        ])
        self.assertTrue(any(item.startswith("pytest") for item in metadata["optional-dependencies"]["dev"]))
        self.assertEqual(VERIFIED_PYTHON, f"CPython {sys.version_info.major}.{sys.version_info.minor}")

    def test_scan_query_export_and_storage_gates_are_bounded(self) -> None:
        root = Path(self.temp.name) / "large"
        root.mkdir()
        write_large_package(root, modules=60)
        self.engine.add_project(str(root), "large")
        started = time.perf_counter()
        scan = self.engine.scan("large")
        scan_seconds = time.perf_counter() - started
        started = time.perf_counter()
        overview = self.queries.overview("large")
        query_seconds = time.perf_counter() - started
        started = time.perf_counter()
        bundle = report_bundle(self.engine, self.queries, "large")
        export_seconds = time.perf_counter() - started
        started = time.perf_counter()
        usage = self.engine.storage_usage()
        storage_seconds = time.perf_counter() - started
        print(f"gates: scan={scan_seconds:.2f}s overview={query_seconds:.2f}s "
              f"export={export_seconds:.2f}s storage={storage_seconds:.3f}s "
              f"bytes={usage['total_bytes']}")
        self.assertEqual(scan["status"], "completed")
        self.assertTrue(overview["components"])
        self.assertTrue(bundle["payload"]["architecture"]["components"])
        self.assertLess(scan_seconds, 20)
        self.assertLess(query_seconds, 5)
        self.assertLess(export_seconds, 5)
        self.assertLess(storage_seconds, 2)
        self.assertLess(usage["total_bytes"], 20 * 1024 * 1024)

    def test_corrupt_database_and_export_inputs_fail_cleanly(self) -> None:
        corrupt = Path(self.temp.name) / "corrupt.sqlite3"
        corrupt.write_bytes(b"not a database")
        output, errors = StringIO(), StringIO()
        with redirect_stdout(output), redirect_stderr(errors):
            code = main(["project", "list", "--database", str(corrupt)])
        self.assertEqual(code, 2)
        self.assertIn("Error:", errors.getvalue())
        self.assertNotIn("Traceback", errors.getvalue())

        self.add_project("safety", {"fixture_safety/__init__.py": "", "fixture_safety/app.py": "def run():\n    return 1\n"})
        export = Path(self.temp.name) / "export.json"
        export.write_text("{truncated", encoding="utf-8")
        with self.assertRaisesRegex(ThreadError, "not a ThreadTrace export"):
            self.engine.import_investigation("safety", str(export))
        export.write_text(json.dumps({"format": "threadtrace-export", "export_version": 2,
                                      "payload_type": "report", "payload": {}}), encoding="utf-8")
        with self.assertRaisesRegex(ThreadError, "does not contain an investigation"):
            self.engine.import_investigation("safety", str(export))

        (Path(self.temp.name) / "outside.py").write_text("SECRET = 1\n", encoding="utf-8")
        saved = self.engine.save_investigation("safety", "escape", {"files": [{"path": "../outside.py"}]})
        self.assertEqual(saved["content"]["source"], [])
        self.assertEqual(self.engine.resume_investigation("safety", "escape")["references"], [])
