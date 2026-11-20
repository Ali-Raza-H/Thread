"""Safe project-metadata parsing without executing the target project."""

from __future__ import annotations

import ast
import configparser
import hashlib
import json
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

MAX_METADATA_BYTES = 2 * 1024 * 1024
LOCK_FILES = {"Pipfile.lock", "poetry.lock", "uv.lock"}
CONDA_NAMES = {"environment.yml", "environment.yaml", "conda.yml", "conda.yaml"}


@dataclass
class MetadataFile:
    relative_path: str
    kind: str
    status: str
    content_hash: str | None
    error: str | None = None
    limitations: list[str] = field(default_factory=list)


@dataclass
class Dependency:
    name: str
    raw: str
    group: str
    source_file: str
    marker: str | None = None
    extras: list[str] = field(default_factory=list)
    direct_url: str | None = None
    path: str | None = None
    editable: bool = False
    metadata_kind: str = "requirements"
    limitations: list[str] = field(default_factory=list)


@dataclass
class MetadataSnapshot:
    files: list[MetadataFile] = field(default_factory=list)
    dependencies: list[Dependency] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def declarations(self) -> list[tuple[str, str]]:
        return [(item.name, item.source_file) for item in self.dependencies if item.name]


def _normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip().lower())


def _read(path: Path) -> tuple[str | None, str | None, str | None]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_METADATA_BYTES:
            return None, None, f"metadata file exceeds {MAX_METADATA_BYTES} bytes"
        return raw.decode("utf-8"), hashlib.sha256(raw).hexdigest(), None
    except (OSError, UnicodeDecodeError) as exc:
        return None, None, str(exc)


def _snapshot_file(snapshot: MetadataSnapshot, root: Path, path: Path, kind: str,
                   status: str = "parsed", error: str | None = None,
                   limitations: list[str] | None = None) -> tuple[str | None, str | None]:
    relative = path.relative_to(root).as_posix()
    text, digest, read_error = _read(path)
    if read_error and not error:
        status, error = "failed", read_error
    snapshot.files.append(MetadataFile(relative, kind, status, digest, error, limitations or []))
    return text, digest


def _split_requirement(raw: str, source: str, group: str, kind: str,
                       editable: bool = False, limitations: list[str] | None = None) -> Dependency:
    value = raw.strip()
    marker = None
    if ";" in value:
        value, marker = value.split(";", 1)
        marker = marker.strip() or None
    direct_url = None
    path = None
    if " @ " in value:
        name, direct_url = value.split(" @ ", 1)
        name = name.strip()
    elif value.startswith(("http://", "https://", "git+", "file://")):
        direct_url = value
        name = Path(value.rstrip("/").split("/")[-1]).stem or "unnamed"
    elif value.startswith(("./", "../", "/", "~")):
        path = value
        name = Path(value).name or "unnamed"
    else:
        name = value
    extras: list[str] = []
    match = re.match(r"([A-Za-z0-9_.-]+)\s*\[([^\]]+)\]", name)
    if match:
        name = match.group(1)
        extras = [item.strip() for item in match.group(2).split(",") if item.strip()]
    else:
        name = re.split(r"[<>=!~* ]", name.strip(), maxsplit=1)[0]
    if not name:
        limitations = [*(limitations or []), "requirement name could not be identified"]
        name = value.split()[0]
    return Dependency(name=_normalise(name), raw=raw.strip(), group=group, source_file=source, marker=marker,
                      extras=extras, direct_url=direct_url, path=path, editable=editable,
                      metadata_kind=kind, limitations=limitations or [])


def _add_requirements(snapshot: MetadataSnapshot, root: Path, path: Path, seen: set[str],
                      group: str = "runtime") -> None:
    relative = path.relative_to(root).as_posix() if path.is_relative_to(root) else str(path)
    if relative in seen:
        snapshot.warnings.append(f"{relative}: recursive requirements include skipped")
        return
    seen.add(relative)
    text, _ = _snapshot_file(snapshot, root, path, "requirements")
    if text is None:
        return
    limitations = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(("-r ", "--requirement ")):
            included = stripped.split(maxsplit=1)[1].strip()
            candidate = (path.parent / included).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                _add_requirements(snapshot, root, candidate, seen, group)
            else:
                snapshot.warnings.append(f"{relative}: included requirements not found or outside project: {included}")
            continue
        if stripped.startswith(("-c ", "--constraint ")):
            included = stripped.split(maxsplit=1)[1].strip()
            candidate = (path.parent / included).resolve()
            if candidate.is_relative_to(root) and candidate.is_file():
                _add_requirements(snapshot, root, candidate, seen, "constraint")
            else:
                snapshot.warnings.append(f"{relative}: constraint file not found or outside project: {included}")
            continue
        if stripped.startswith(("-e ", "--editable ")):
            snapshot.dependencies.append(_split_requirement(
                stripped.split(maxsplit=1)[1], relative, "editable", "editable", True, limitations,
            ))
            continue
        if stripped.startswith("-"):
            snapshot.warnings.append(f"{relative}: requirements option is not a dependency: {stripped.split()[0]}")
            continue
        snapshot.dependencies.append(_split_requirement(stripped, relative, group, "requirements", False, limitations))


def _pyproject(snapshot: MetadataSnapshot, root: Path, path: Path) -> None:
    text, _ = _snapshot_file(snapshot, root, path, "pyproject")
    if text is None:
        return
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        snapshot.files[-1].status, snapshot.files[-1].error = "failed", str(exc)
        snapshot.warnings.append(f"pyproject.toml: {exc}")
        return
    project = data.get("project", {}) if isinstance(data, dict) else {}
    for raw in project.get("dependencies", []) if isinstance(project, dict) else []:
        if isinstance(raw, str):
            snapshot.dependencies.append(_split_requirement(raw, "pyproject.toml", "runtime", "pyproject"))
    optional = project.get("optional-dependencies", {}) if isinstance(project, dict) else {}
    for group, values in optional.items() if isinstance(optional, dict) else []:
        for raw in values if isinstance(values, list) else []:
            if isinstance(raw, str):
                snapshot.dependencies.append(_split_requirement(raw, "pyproject.toml", f"extras:{group}", "pyproject"))
    groups = data.get("dependency-groups", {}) if isinstance(data, dict) else {}
    for group, values in groups.items() if isinstance(groups, dict) else []:
        for raw in values if isinstance(values, list) else []:
            if isinstance(raw, str):
                snapshot.dependencies.append(_split_requirement(raw, "pyproject.toml", f"group:{group}", "pyproject"))
    build = data.get("build-system", {}) if isinstance(data, dict) else {}
    for raw in build.get("requires", []) if isinstance(build, dict) else []:
        if isinstance(raw, str):
            snapshot.dependencies.append(_split_requirement(raw, "pyproject.toml", "build", "pyproject"))


def _setup_config(snapshot: MetadataSnapshot, root: Path, path: Path) -> None:
    text, _ = _snapshot_file(snapshot, root, path, "setup.cfg")
    if text is None:
        return
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(text)
    except configparser.Error as exc:
        snapshot.files[-1].status, snapshot.files[-1].error = "failed", str(exc)
        snapshot.warnings.append(f"setup.cfg: {exc}")
        return
    for raw in parser.get("options", "install_requires", fallback="").splitlines():
        if raw.strip():
            snapshot.dependencies.append(_split_requirement(raw, "setup.cfg", "runtime", "setup.cfg"))
    if parser.has_section("options.extras_require"):
        for group, raw in parser.items("options.extras_require"):
            for line in raw.splitlines():
                if line.strip():
                    snapshot.dependencies.append(_split_requirement(
                        line, "setup.cfg", f"extras:{group}", "setup.cfg",
                    ))


def _setup_py(snapshot: MetadataSnapshot, root: Path, path: Path) -> None:
    text, _ = _snapshot_file(snapshot, root, path, "setup.py")
    if text is None:
        return
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError as exc:
        snapshot.files[-1].status, snapshot.files[-1].error = "failed", exc.msg
        snapshot.warnings.append(f"setup.py: {exc.msg}")
        return
    for call in (node for node in ast.walk(tree) if isinstance(node, ast.Call)):
        name = call.func.id if isinstance(call.func, ast.Name) else getattr(call.func, "attr", "")
        if name != "setup":
            continue
        for keyword in call.keywords:
            if keyword.arg not in {"install_requires", "tests_require", "setup_requires"}:
                continue
            try:
                values = ast.literal_eval(keyword.value)
            except (ValueError, TypeError, SyntaxError):
                snapshot.files[-1].limitations.append(f"{keyword.arg} is not a literal")
                continue
            if isinstance(values, str):
                values = [values]
            if isinstance(values, list):
                for raw in values:
                    if isinstance(raw, str):
                        snapshot.dependencies.append(_split_requirement(
                            raw, "setup.py", "runtime" if keyword.arg == "install_requires" else keyword.arg,
                            "setup.py",
                        ))
        extras = next((keyword.value for keyword in call.keywords if keyword.arg == "extras_require"), None)
        if extras is not None:
            try:
                mapping = ast.literal_eval(extras)
            except (ValueError, TypeError, SyntaxError):
                snapshot.files[-1].limitations.append("extras_require is not a literal")
                continue
            if isinstance(mapping, dict):
                for group, values in mapping.items():
                    for raw in values if isinstance(values, list) else []:
                        if isinstance(raw, str):
                            snapshot.dependencies.append(_split_requirement(
                                raw, "setup.py", f"extras:{group}", "setup.py",
                            ))


def _pipfile(snapshot: MetadataSnapshot, root: Path, path: Path) -> None:
    text, _ = _snapshot_file(snapshot, root, path, "pipfile")
    if text is None:
        return
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        snapshot.files[-1].status, snapshot.files[-1].error = "failed", str(exc)
        snapshot.warnings.append(f"Pipfile: {exc}")
        return
    for group in ("packages", "dev-packages"):
        for name, value in data.get(group, {}).items():
            if isinstance(value, str):
                raw = f"{name}{value if value in {'*', ''} or value[0] in '<>=!~' else '==' + value}"
            elif isinstance(value, dict):
                version = str(value.get('version', ''))
                raw = f"{name}{version if not version or version[0] in '<>=!~' else '==' + version}"
                extras = value.get("extras", [])
                marker = value.get("markers")
                dependency = _split_requirement(raw, "Pipfile", group, "pipfile")
                dependency.extras = [str(item) for item in extras] if isinstance(extras, list) else []
                dependency.marker = str(marker) if marker else None
                dependency.path = str(value.get("path") or value.get("file") or "") or None
                dependency.direct_url = str(value.get("git") or "") or None
                dependency.editable = bool(value.get("editable", False))
                snapshot.dependencies.append(dependency)
                continue
            else:
                continue
            snapshot.dependencies.append(_split_requirement(raw, "Pipfile", group, "pipfile"))


def _lock_json(snapshot: MetadataSnapshot, root: Path, path: Path, kind: str) -> None:
    text, _ = _snapshot_file(snapshot, root, path, kind)
    if text is None:
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        snapshot.files[-1].status, snapshot.files[-1].error = "failed", str(exc)
        snapshot.warnings.append(f"{path.name}: {exc}")
        return
    for group in ("default", "develop"):
        values = data.get(group, {}) if isinstance(data, dict) else {}
        for name, value in values.items() if isinstance(values, dict) else []:
            version = str(value.get('version', '')) if isinstance(value, dict) else str(value)
            raw = f"{name}{version if not version or version[0] in '<>=!~' else '==' + version}"
            dependency = _split_requirement(raw, path.name, group, "pipfile.lock")
            if isinstance(value, dict):
                dependency.marker = value.get("markers") or dependency.marker
                dependency.extras = value.get("extras", []) if isinstance(value.get("extras", []), list) else []
                dependency.path = value.get("path") or value.get("file")
                dependency.direct_url = value.get("git")
            snapshot.dependencies.append(dependency)


def _toml_lock(snapshot: MetadataSnapshot, root: Path, path: Path, kind: str) -> None:
    text, _ = _snapshot_file(snapshot, root, path, kind)
    if text is None:
        return
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        snapshot.files[-1].status, snapshot.files[-1].error = "failed", str(exc)
        snapshot.warnings.append(f"{path.name}: {exc}")
        return
    packages = data.get("package", []) if isinstance(data, dict) else []
    for package in packages if isinstance(packages, list) else []:
        if not isinstance(package, dict) or not package.get("name"):
            continue
        version = str(package.get('version', ''))
        raw = f"{package['name']}{version if not version or version[0] in '<>=!~' else '==' + version}"
        dependency = _split_requirement(raw, path.name, "locked", kind)
        dependency.marker = package.get("markers") or dependency.marker
        dependency.extras = [str(item) for item in package.get("extras", [])] if isinstance(package.get("extras", []), list) else []
        source = package.get("source")
        if isinstance(source, dict):
            dependency.direct_url = source.get("url")
            dependency.path = source.get("path")
        snapshot.dependencies.append(dependency)
        children = package.get("dependencies")
        if isinstance(children, dict):
            for name, constraint in children.items():
                snapshot.dependencies.append(_split_requirement(
                    f"{name}{constraint if str(constraint).startswith(('<', '>', '=', '!', '~')) else ''}",
                    path.name, "locked-transitive", kind,
                ))
        elif isinstance(children, list):
            for child in children:
                if isinstance(child, dict) and child.get("name"):
                    snapshot.dependencies.append(_split_requirement(
                        str(child["name"]), path.name, "locked-transitive", kind,
                    ))
        optional = package.get("optional-dependencies")
        if isinstance(optional, dict):
            for group, values in optional.items():
                for child in values if isinstance(values, list) else []:
                    if isinstance(child, dict) and child.get("name"):
                        snapshot.dependencies.append(_split_requirement(
                            str(child["name"]), path.name, f"extras:{group}", kind,
                        ))


def _conda(snapshot: MetadataSnapshot, root: Path, path: Path) -> None:
    text, _ = _snapshot_file(snapshot, root, path, "conda")
    if text is None:
        return
    limitations = ["Conda environment parsed with a line-based YAML subset; complex YAML is not interpreted."]
    snapshot.files[-1].limitations.extend(limitations)
    in_dependencies = False
    pip_section = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "dependencies:":
            in_dependencies = True
            continue
        if not in_dependencies or not stripped.startswith("- "):
            continue
        raw = stripped[2:].strip()
        if raw == "pip:":
            pip_section = True
            continue
        if raw.startswith("pip:"):
            pip_section = True
            continue
        if raw.startswith(("python=", "pip=")) and not pip_section:
            snapshot.dependencies.append(_split_requirement(raw, path.name, "conda", "conda"))
        elif pip_section:
            snapshot.dependencies.append(_split_requirement(raw, path.name, "pip", "conda"))
        else:
            snapshot.dependencies.append(_split_requirement(raw, path.name, "conda", "conda"))


def parse_metadata(root: str | Path) -> MetadataSnapshot:
    """Parse recognised metadata files, preserving failures and unsupported files."""
    root = Path(root).resolve()
    snapshot = MetadataSnapshot()
    recognised: set[str] = set()

    def exists(name: str) -> Path | None:
        path = root / name
        if path.is_file():
            recognised.add(name)
            return path
        return None

    if (path := exists("pyproject.toml")):
        _pyproject(snapshot, root, path)
    if (path := exists("setup.cfg")):
        _setup_config(snapshot, root, path)
    if (path := exists("setup.py")):
        _setup_py(snapshot, root, path)
    if (path := exists("Pipfile")):
        _pipfile(snapshot, root, path)
    if (path := exists("Pipfile.lock")):
        _lock_json(snapshot, root, path, "pipfile.lock")
    if (path := exists("poetry.lock")):
        _toml_lock(snapshot, root, path, "poetry.lock")
    if (path := exists("uv.lock")):
        _toml_lock(snapshot, root, path, "uv.lock")
    for name in sorted({item.name for item in root.glob("requirements*.txt")}):
        if (path := exists(name)):
            _add_requirements(snapshot, root, path, set())
    conda_names = {path.name for path in root.iterdir() if path.is_file()
                   and (path.name in CONDA_NAMES or path.name.endswith((".conda.yml", ".conda.yaml")))}
    for name in sorted(conda_names):
        if (path := exists(name)):
            _conda(snapshot, root, path)
    for path in sorted(root.iterdir()):
        if not path.is_file() or path.name in recognised:
            continue
        if path.suffix.lower() not in {".toml", ".cfg", ".ini", ".yml", ".yaml", ".json", ".lock"}:
            continue
        text, digest, error = _read(path)
        snapshot.files.append(MetadataFile(
            path.name, "unclassified", "unsupported" if not error else "failed", digest, error,
            ["Recognised metadata parser does not cover this file; content is preserved by hash only."],
        ))
    return snapshot
