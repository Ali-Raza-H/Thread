"""Small scan-time records kept independent of SQLite implementation details."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ImportRecord:
    target_module: str
    imported_name: str | None
    alias: str | None
    level: int
    line_number: int
    column_number: int | None
    is_type_checking: bool = False
    conditional: bool = False


@dataclass
class SymbolRecord:
    qualified_name: str
    name: str
    kind: str
    parent_qualified_name: str | None
    signature: str | None
    docstring: str | None
    purpose: str
    decorators: list[str]
    line_start: int
    line_end: int | None
    column_start: int | None
    complexity: int | None
    parameters: list[str] = field(default_factory=list)


@dataclass
class AttributeRecord:
    class_qualified_name: str
    name: str
    kind: str
    line_number: int
    value_expression: str | None


@dataclass
class CallRecord:
    caller_qualified_name: str | None
    target_expression: str
    line_number: int
    column_number: int | None
    arguments: list[str] = field(default_factory=list)
    keywords: dict[str, str] = field(default_factory=dict)
    assignment_target: str | None = None
    in_main_guard: bool = False
    is_type_checking: bool = False


@dataclass
class FlowRecord:
    symbol_qualified_name: str
    variable_name: str
    event_kind: str
    expression: str | None
    line_number: int


@dataclass
class ScopeRecord:
    key: str
    kind: str
    qualified_name: str
    parent_key: str | None
    line_start: int
    line_end: int | None = None


@dataclass
class DefinitionRecord:
    scope_key: str
    name: str
    qualified_name: str
    kind: str
    source: str
    line_number: int
    expression: str | None = None
    exported: bool = False
    is_type_checking: bool = False
    conditional: bool = False


@dataclass
class ReferenceRecord:
    scope_key: str
    name: str
    expression: str
    kind: str
    line_number: int
    is_type_checking: bool = False


@dataclass
class BoundaryRecord:
    scope_key: str
    kind: str
    expression: str
    line_number: int
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedFile:
    relative_path: str
    module_name: str
    content_hash: str | None
    parse_status: str
    error_message: str | None = None
    error_line: int | None = None
    is_entry_point: bool = False
    symbols: list[SymbolRecord] = field(default_factory=list)
    attributes: list[AttributeRecord] = field(default_factory=list)
    imports: list[ImportRecord] = field(default_factory=list)
    calls: list[CallRecord] = field(default_factory=list)
    flows: list[FlowRecord] = field(default_factory=list)
    origin_kind: str = "file"
    original_path: str = ""
    resolved_path: str = ""
    parser: str = "ast"
    parser_version: str = ""
    capability: str = "Full"
    category: str = "source"
    line_offset: int = 0
    origin_id: str = ""
    scopes: list[ScopeRecord] = field(default_factory=list)
    definitions: list[DefinitionRecord] = field(default_factory=list)
    references: list[ReferenceRecord] = field(default_factory=list)
    boundaries: list[BoundaryRecord] = field(default_factory=list)
    exports: list[str] = field(default_factory=list)
