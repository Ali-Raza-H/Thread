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


@dataclass
class FlowRecord:
    symbol_qualified_name: str
    variable_name: str
    event_kind: str
    expression: str | None
    line_number: int


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
