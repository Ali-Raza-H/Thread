"""Bind stored names through lexical scopes, imports, and re-exports."""

from __future__ import annotations

import builtins
import json
import re
from collections import defaultdict
from typing import Any

from .store import Store


class Resolution:
    """Resolve reference facts without importing or executing target modules."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def resolve(self, scan_id: str) -> None:
        self.scan_id = scan_id
        self.scopes = {row["scope_id"]: dict(row) for row in self.store.rows(
            "SELECT * FROM scopes WHERE scan_id=?", (scan_id,))}
        self.definitions = [dict(row) for row in self.store.rows(
            "SELECT * FROM definitions WHERE scan_id=?", (scan_id,))]
        self.references = [dict(row) for row in self.store.rows(
            "SELECT * FROM symbol_references WHERE scan_id=?", (scan_id,))]
        self.imports = [dict(row) for row in self.store.rows(
            "SELECT * FROM imports WHERE scan_id=?", (scan_id,))]
        self.symbols = [dict(row) for row in self.store.rows(
            "SELECT * FROM symbols WHERE scan_id=?", (scan_id,))]
        self.files = {row["file_id"]: dict(row) for row in self.store.rows(
            "SELECT file_id,module_name FROM files WHERE scan_id=?", (scan_id,))}
        self.attributes = [dict(row) for row in self.store.rows(
            """SELECT a.name,a.kind,s.qualified_name AS class_name FROM attributes a
               JOIN symbols s ON s.symbol_id=a.class_symbol_id WHERE a.scan_id=?""", (scan_id,))]
        self.symbols_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.symbols:
            self.symbols_by_name[row["qualified_name"]].append(row)
        self.definitions_by_qname: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.definitions:
            self.definitions_by_qname[row["qualified_name"]].append(row)
        self.imports_by_visible: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in self.imports:
            if row["visible_name"]:
                self.imports_by_visible[(row["source_file_id"], row["visible_name"])].append(row)
        self.attributes_by_class: dict[tuple[str, str], dict[str, Any]] = defaultdict(dict)
        for row in self.attributes:
            self.attributes_by_class[(row["class_name"], row["name"])] = row
        self.module_by_file = {file_id: row["module_name"] for file_id, row in self.files.items()}
        self.file_by_module = {row["module_name"]: file_id for file_id, row in self.files.items()}
        self._build_exports()
        for definition in self.definitions:
            self._resolve_definition(definition)
        self.definitions_by_qname = defaultdict(list)
        self.definitions_by_scope: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in self.definitions:
            self.definitions_by_qname[row["qualified_name"]].append(row)
            self.definitions_by_scope[(row["scope_id"], row["name"])].append(row)
        with self.store.transaction():
            for reference in self.references:
                self._resolve_reference(reference)
            for definition in self.definitions:
                self.store.execute(
                    """UPDATE definitions SET resolved_symbol_id=?,resolution=?,binding_path_json=?
                       WHERE definition_id=?""",
                    (definition["resolved_symbol_id"], definition["resolution"],
                     self.store.json(definition["binding_path"]), definition["definition_id"]),
                )
            for reference in self.references:
                self.store.execute(
                    """UPDATE symbol_references SET resolved_definition_id=?,resolved_symbol_id=?,
                       resolution=?,binding_path_json=? WHERE reference_id=?""",
                    (reference["resolved_definition_id"], reference["resolved_symbol_id"],
                     reference["resolution"], self.store.json(reference["binding_path"]),
                     reference["reference_id"]),
                )
            self._resolve_calls()
            for reference in self.references:
                if reference["resolution"] == "unresolved_attribute":
                    self.store.execute(
                        """INSERT INTO boundaries(boundary_id,scan_id,file_id,scope_id,kind,expression,
                           line_number,details_json) VALUES(?,?,?,?,?,?,?,?)""",
                        (f"{reference['reference_id']}:attribute", self.scan_id, reference["file_id"],
                         reference["scope_id"], "unresolved_attribute", reference["expression"],
                         reference["line_number"], self.store.json({"reference": reference["reference_id"]})),
                    )

    def _resolve_calls(self) -> None:
        by_place: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
        for reference in self.references:
            by_place[(reference["file_id"], reference["line_number"])].append(reference)
        for call in self.store.rows("SELECT * FROM calls WHERE scan_id=?", (self.scan_id,)):
            if call["resolution"] == "resolved" and call["callee_symbol_id"]:
                continue
            candidates = by_place.get((call["call_file_id"], call["line_number"]), [])
            match = next((reference for reference in candidates
                          if call["target_expression"] == reference["expression"]
                          and reference["resolved_symbol_id"]), None)
            if not match:
                continue
            self.store.execute(
                """UPDATE calls SET callee_symbol_id=?,resolution='resolved',candidate_json=?,
                   basis='binding',binding_path_json=? WHERE call_id=?""",
                (match["resolved_symbol_id"], self.store.json([match["resolved_symbol_id"]]),
                 self.store.json(match["binding_path"]), call["call_id"]),
            )

    def _build_exports(self) -> None:
        explicit: dict[str, set[str]] = defaultdict(set)
        for row in self.store.rows(
                """SELECT f.module_name,e.name FROM exports e JOIN files f ON f.file_id=e.file_id
                   WHERE e.scan_id=?""", (self.scan_id,)):
            explicit[row["module_name"]].add(row["name"])
        self.exports: dict[str, set[str]] = {}
        for module, file_id in self.file_by_module.items():
            if module in explicit:
                self.exports[module] = explicit[module]
                continue
            self.exports[module] = {
                row["name"] for row in self.definitions
                if row["file_id"] == file_id and row["exported"] and not row["name"].startswith("_")
            }

    def _symbol(self, qualified_name: str) -> dict[str, Any] | None:
        rows = self.symbols_by_name.get(qualified_name, [])
        return max(rows, key=lambda row: row["line_start"]) if rows else None

    def _binding_symbol(self, binding: str, depth: int = 0) -> dict[str, Any] | None:
        if not binding or depth > 6:
            return None
        symbol = self._symbol(binding)
        if symbol:
            return symbol
        for definition in self.definitions_by_qname.get(binding, []):
            if definition["kind"] != "import":
                continue
            path = definition.get("binding_path") or json.loads(definition["binding_path_json"])
            if not path:
                row = self._import_for(definition)
                path = json.loads(row["binding_path_json"]) if row else []
            if path:
                return self._binding_symbol(path[0], depth + 1)
        return None

    def _definition_binding(self, definition: dict[str, Any]) -> list[str]:
        if definition["kind"] == "import":
            return json.loads(definition["binding_path_json"])
        if definition["kind"] in {"function", "async_function", "method", "async_method", "class"}:
            return [definition["qualified_name"]]
        if definition["kind"] in {"local", "parameter", "comprehension", "variable", "constant"}:
            scope = self.scopes.get(definition["scope_id"], {})
            return [scope.get("qualified_name", ""), definition["name"]]
        return []

    def _resolve_definition(self, definition: dict[str, Any]) -> None:
        definition["binding_path"] = self._definition_binding(definition)
        definition["resolved_symbol_id"] = None
        if definition["kind"] == "overload":
            definition["resolution"] = "overload"
            return
        if definition["kind"] == "import":
            row = self._import_for(definition)
            if row:
                path = json.loads(row["binding_path_json"])
                definition["binding_path"] = path or [row["target_module"]]
                symbol = self._binding_symbol(definition["binding_path"][0])
                definition["resolved_symbol_id"] = symbol["symbol_id"] if symbol else None
                definition["resolution"] = (
                    "import" if symbol else "external" if row["category"] != "internal" else "unresolved"
                )
            else:
                definition["resolution"] = "unresolved"
            return
        if definition["kind"] in {"function", "async_function", "method", "async_method", "class"}:
            symbol = self._symbol(definition["qualified_name"])
            definition["resolved_symbol_id"] = symbol["symbol_id"] if symbol else None
            definition["resolution"] = "definition" if symbol else "unresolved"
            return
        if definition["kind"] in {"global", "nonlocal"}:
            definition["resolution"] = "declaration"
            return
        definition["resolution"] = "local"

    def _import_for(self, definition: dict[str, Any]) -> dict[str, Any] | None:
        rows = self.imports_by_visible.get((definition["file_id"], definition["name"]), [])
        if not rows:
            return None
        nearest = [row for row in rows if row["line_number"] <= definition["line_number"]]
        return max(nearest or rows, key=lambda row: row["line_number"])

    def _scope_chain(self, scope_id: str | None) -> list[str]:
        chain: list[str] = []
        while scope_id and scope_id in self.scopes:
            chain.append(scope_id)
            scope_id = self.scopes[scope_id]["parent_scope_id"]
        return chain

    def _visible_definition(self, scope_id: str | None, name: str) -> dict[str, Any] | None:
        for current in self._scope_chain(scope_id):
            rows = self.definitions_by_scope.get((current, name), [])
            actual = [row for row in rows if row["kind"] not in {"global", "nonlocal", "overload"}]
            if any(row["kind"] == "import" for row in actual):
                import_rows = [row for row in actual if row["kind"] == "import"]
                non_fallback = [row for row in actual if row["expression"] != "None"]
                actual = non_fallback or import_rows
            if actual:
                return max(actual, key=lambda row: row["line_number"])
            if any(row["kind"] == "overload" for row in rows):
                return max((row for row in rows if row["kind"] == "overload"),
                           key=lambda row: row["line_number"])
        return None

    def _resolve_reference(self, reference: dict[str, Any]) -> None:
        reference["resolved_definition_id"] = None
        reference["resolved_symbol_id"] = None
        reference["binding_path"] = []
        expression = reference["expression"]
        first = re.split(r"[.\[]", expression, maxsplit=1)[0]
        suffix = expression[len(first) + 1:] if len(expression) > len(first) else ""
        if first in {"self", "cls"} and suffix:
            self._resolve_class_reference(reference, suffix)
            return
        definition = self._visible_definition(reference["scope_id"], first)
        if definition:
            self._bind_reference(reference, definition, suffix)
            return
        wildcard = self._wildcard_binding(reference["file_id"], first)
        if wildcard:
            reference["resolved_symbol_id"] = wildcard[0]
            reference["resolution"] = "wildcard"
            reference["binding_path"] = wildcard[1]
            return
        if hasattr(builtins, first):
            reference["resolution"] = "builtin"
            reference["binding_path"] = [f"builtins.{first}"]
            return
        if any(row["source_file_id"] == reference["file_id"] and row["imported_name"] == "*"
               and not row["resolved_module"] for row in self.imports):
            reference["resolution"] = "wildcard_unknown"
            return
        reference["resolution"] = "unresolved"

    def _bind_reference(self, reference: dict[str, Any], definition: dict[str, Any], suffix: str) -> None:
        reference["resolved_definition_id"] = definition["definition_id"]
        binding = definition["binding_path"][0] if definition["binding_path"] else ""
        target = f"{binding}.{suffix}" if suffix and binding else binding
        symbol = self._binding_symbol(target)
        if definition["kind"] in {"local", "parameter", "comprehension"}:
            reference["resolution"] = "unresolved_attribute" if suffix else "local"
            reference["binding_path"] = definition["binding_path"]
            return
        if symbol:
            reference["resolved_symbol_id"] = symbol["symbol_id"]
            reference["binding_path"] = [target, symbol["qualified_name"]]
            status = "import" if definition["kind"] == "import" else "definition"
            reference["resolution"] = self._uncertainty(definition, reference, status)
        else:
            reference["binding_path"] = [target] if target else definition["binding_path"]
            reference["resolution"] = self._uncertainty(
                definition, reference,
                "external" if definition["resolution"] == "external" else "unresolved_attribute" if suffix else "unresolved",
            )

    def _uncertainty(self, definition: dict[str, Any], reference: dict[str, Any], status: str) -> str:
        if definition["is_type_checking"] and not reference["is_type_checking"]:
            return "type_checking"
        if definition["conditional"]:
            return "conditional"
        return status

    def _resolve_class_reference(self, reference: dict[str, Any], suffix: str) -> None:
        class_scope = next((self.scopes[scope] for scope in self._scope_chain(reference["scope_id"])
                            if self.scopes[scope]["kind"] == "class"), None)
        if not class_scope:
            reference["resolution"] = "unresolved_attribute"
            return
        found = self._class_attribute(class_scope["qualified_name"], suffix.split(".", 1)[0], set())
        if not found:
            reference["resolution"] = "unresolved_attribute"
            return
        symbol, path = found
        reference["resolved_symbol_id"] = symbol["symbol_id"] if symbol else None
        reference["binding_path"] = path
        reference["resolution"] = "inherited" if path[0] != class_scope["qualified_name"] else "class"

    def _class_attribute(self, class_name: str, name: str,
                         seen: set[str]) -> tuple[dict[str, Any] | None, list[str]] | None:
        if class_name in seen:
            return None
        seen.add(class_name)
        symbol = self._symbol(f"{class_name}.{name}")
        if symbol:
            return symbol, [f"{class_name}.{name}", symbol["qualified_name"]]
        attribute = self.attributes_by_class.get((class_name, name))
        if attribute:
            return None, [f"{class_name}.{name}"]
        class_symbol = self._symbol(class_name)
        if not class_symbol:
            return None
        for base in self._class_bases(class_symbol):
            found = self._class_attribute(base, name, seen)
            if found:
                return found
        return None

    def _class_bases(self, class_symbol: dict[str, Any]) -> list[str]:
        signature = class_symbol.get("signature") or ""
        match = re.match(r"\((.*)\)", signature)
        if not match or not match.group(1).strip():
            return []
        file_id = class_symbol["file_id"]
        module = self.module_by_file.get(file_id, "")
        bases: list[str] = []
        for token in match.group(1).split(","):
            name = token.split("[", 1)[0].strip().split(".")[-1]
            if not name or not name.isidentifier():
                continue
            definition = self._visible_definition(self._module_scope(file_id), name)
            path = definition["binding_path"] if definition and definition["binding_path"] else []
            bases.append(path[0] if path else f"{module}.{name}")
        return bases

    def _module_scope(self, file_id: str) -> str | None:
        return next((scope_id for scope_id, row in self.scopes.items()
                     if row["file_id"] == file_id and row["kind"] == "module"), None)

    def _wildcard_binding(self, file_id: str, name: str) -> tuple[str | None, list[str]] | None:
        candidates: list[tuple[str | None, list[str]]] = []
        for row in self.imports:
            if row["source_file_id"] != file_id or row["imported_name"] != "*":
                continue
            module = row["resolved_module"]
            if not module or module not in self.exports:
                return None
            definition = self._visible_definition(
                self._module_scope(self.file_by_module.get(module, "")), name,
            ) if module in self.file_by_module else None
            target = definition["binding_path"][0] if definition and definition["binding_path"] else f"{module}.{name}"
            symbol = self._binding_symbol(target)
            if name in self.exports[module]:
                candidates.append((symbol["symbol_id"] if symbol else None, [module, target]))
        if len(candidates) == 1:
            return candidates[0]
        return (None, []) if not candidates else None
