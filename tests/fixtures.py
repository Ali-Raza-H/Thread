"""Deterministic offline fixtures for compatibility, accuracy, and performance gates."""

from __future__ import annotations

from pathlib import Path

FIXTURES: dict[str, dict[str, str]] = {
    "script": {
        "fixture_script.py": (
            "import argparse\n\n\n"
            "def main(items: list[str]) -> int:\n"
            "    return len(items)\n\n\n"
            "if __name__ == '__main__':\n"
            "    parser = argparse.ArgumentParser()\n"
            "    parser.add_argument('items', nargs='*')\n"
            "    raise SystemExit(main(parser.parse_args().items))\n"
        ),
    },
    "package": {
        "fixture_pkg/__init__.py": "from .service import Service\n\n__all__ = [\"Service\"]\n",
        "fixture_pkg/service.py": (
            "from .helpers import normalise\n\n\n"
            "class Service:\n"
            "    def run(self, value: str) -> str:\n"
            "        return normalise(value)\n"
        ),
        "fixture_pkg/helpers.py": "def normalise(value: str) -> str:\n    return value.strip().lower()\n",
    },
    "namespace": {
        "fixture_ns/plugins/__init__.py": "",
        "fixture_ns/plugins/loader.py": "from .registry import register\n\n\ndef load() -> str:\n    return register('plugin')\n",
        "fixture_ns/plugins/registry.py": "def register(name: str) -> str:\n    return name\n",
    },
    "malformed": {
        "fixture_broken/__init__.py": "",
        "fixture_broken/valid.py": "def ok() -> int:\n    return 1\n",
        "fixture_broken/broken.py": "def broken(:\n    return 2\n",
    },
    "cli": {
        "fixture_cli/__init__.py": "",
        "fixture_cli/argparse_cli.py": (
            "import argparse\n\n\n"
            "def build() -> argparse.ArgumentParser:\n"
            "    parser = argparse.ArgumentParser(prog='tool')\n"
            "    parser.add_argument('--name')\n"
            "    return parser\n"
        ),
        "fixture_cli/click_cli.py": (
            "import click\n\n\n"
            "@click.command()\n"
            "@click.option('--count', default=1)\n"
            "def hello(count: int) -> None:\n"
            "    pass\n"
        ),
        "fixture_cli/typer_cli.py": (
            "import typer\n\n"
            "app = typer.Typer()\n\n\n"
            "@app.command()\n"
            "def run(value: str) -> None:\n"
            "    pass\n"
        ),
    },
    "web": {
        "fixture_web/__init__.py": "",
        "fixture_web/flask_app.py": (
            "from flask import Flask\n\n"
            "app = Flask(__name__)\n\n\n"
            "@app.get('/items')\n"
            "def items():\n"
            "    return []\n"
        ),
        "fixture_web/fastapi_app.py": (
            "from fastapi import FastAPI\n\n"
            "app = FastAPI()\n\n\n"
            "@app.get('/health')\n"
            "def health():\n"
            "    return {'ok': True}\n"
        ),
        "fixture_web/django_urls.py": (
            "from django.urls import path\n\n"
            "from . import views\n\n"
            "urlpatterns = [path('items/<int:pk>/', views.item)]\n"
        ),
        "fixture_web/views.py": "def item(request, pk: int):\n    return pk\n",
    },
    "database": {
        "fixture_data/__init__.py": "",
        "fixture_data/images.py": (
            "import sqlite3\n\n\n"
            "def load() -> list[tuple]:\n"
            "    connection = sqlite3.connect('images.db')\n"
            "    return connection.execute('SELECT id FROM images').fetchall()\n"
        ),
        "fixture_data/models.py": (
            "from sqlalchemy import select\n"
            "from sqlalchemy.orm import Session\n\n\n"
            "def ids(session: Session) -> list[int]:\n"
            "    return [row.id for row in session.execute(select('id'))]\n"
        ),
    },
    "concurrency": {
        "fixture_concurrent/__init__.py": "",
        "fixture_concurrent/workers.py": (
            "import asyncio\n"
            "import threading\n\n\n"
            "def work() -> None:\n"
            "    pass\n\n\n"
            "def start() -> threading.Thread:\n"
            "    worker = threading.Thread(target=work)\n"
            "    worker.start()\n"
            "    return worker\n\n\n"
            "async def gather() -> None:\n"
            "    await asyncio.gather(asyncio.sleep(0))\n"
        ),
        "fixture_concurrent/processes.py": (
            "import multiprocessing\n\n\n"
            "def start(target) -> multiprocessing.Process:\n"
            "    process = multiprocessing.Process(target=target)\n"
            "    process.start()\n"
            "    return process\n"
        ),
    },
    "imports": {
        "fixture_imports/__init__.py": "",
        "fixture_imports/base.py": (
            "def resolve(value: str) -> str:\n"
            "    return value\n\n\n"
            "def shadowed() -> int:\n"
            "    return 1\n"
        ),
        "fixture_imports/reexport.py": "from .base import resolve as resolve\n\n__all__ = [\"resolve\"]\n",
        "fixture_imports/consumer.py": (
            "from .reexport import resolve\n"
            "from . import base\n\n\n"
            "def shadowed() -> int:\n"
            "    return 2\n\n\n"
            "def use(value: str) -> str:\n"
            "    return resolve(value) + str(base.shadowed())\n"
        ),
    },
    "exceptions": {
        "fixture_errors/__init__.py": "",
        "fixture_errors/handling.py": (
            "from unittest.mock import patch\n\n\n"
            "class Failure(RuntimeError):\n"
            "    pass\n\n\n"
            "def risky(value: int) -> int:\n"
            "    try:\n"
            "        if value < 0:\n"
            "            raise Failure('negative')\n"
            "        return value\n"
            "    except Failure:\n"
            "        return 0\n"
            "    finally:\n"
            "        pass\n\n\n"
            "def patched() -> int:\n"
            "    with patch('fixture_errors.handling.risky', return_value=1):\n"
            "        return risky(2)\n"
        ),
    },
    "adapters": {
        "fixture_adapters/__init__.py": "",
        "fixture_adapters/client.py": (
            "import json\n"
            "import sqlite3\n"
            "import urllib.request\n"
            "from pathlib import Path\n\n\n"
            "def fetch() -> dict:\n"
            "    with urllib.request.urlopen('https://service.test/items') as response:\n"
            "        return json.loads(response.read())\n\n\n"
            "def read() -> str:\n"
            "    return Path('state.json').read_text(encoding='utf-8')\n\n\n"
            "def stored() -> None:\n"
            "    connection = sqlite3.connect('store.sqlite3')\n"
            "    connection.execute('CREATE TABLE IF NOT EXISTS items (id INTEGER)')\n"
            "    connection.commit()\n"
        ),
    },
}


def write_fixture(root: Path, name: str) -> Path:
    """Write one deterministic fixture tree beneath an empty directory."""
    for relative, text in FIXTURES[name].items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def write_large_package(root: Path, modules: int = 60) -> Path:
    """Write a generated package used for measured performance gates."""
    package = root / "largepkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "base.py").write_text("def value(item: int) -> int:\n    return item\n", encoding="utf-8")
    for index in range(modules):
        (package / f"module_{index:02d}.py").write_text(
            "from .base import value\n\n\n"
            f"def run_{index}(item: int = {index}) -> int:\n"
            f"    return value(item) + {index}\n",
            encoding="utf-8",
        )
    return root
