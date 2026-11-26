"""A light, keyboard-first terminal explorer built with prompt_toolkit."""

from __future__ import annotations

import ast
import difflib
import hashlib
import os
import re
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

from prompt_toolkit.application import Application, get_app_session
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.containers import to_container
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.widgets import Frame, TextArea

from .engine import Engine, ThreadError
from .query import QueryService

MIN_COLUMNS = 80
MIN_ROWS = 20

DEFAULT_KEYS = {
    "overview": "o", "entrypoints": "e", "architecture": "a", "resources": "u",
    "modules": "m", "functions": "f", "classes": "c", "findings": "d",
    "libraries": "l", "cycles": "x", "file-graph": "p", "source": "v",
    "analysis": "z", "lesson": "y", "search": "/", "command": ":",
    "rescan": "r", "help": "?", "quit": "q", "definition": "g", "references": "G",
}

COMMANDS = {
    "overview": "Project overview and detected components",
    "entrypoints": "Scripts, commands, routes, workers, and tests",
    "architecture": "Evidence-backed components and relationships",
    "resources": "External resource boundaries",
    "modules": "Modules in the latest completed scan",
    "functions": "Functions and methods",
    "classes": "Classes, bases, methods, and attributes",
    "findings": "Issues that need attention",
    "libraries": "Imported and declared libraries",
    "cycles": "Circular internal imports",
    "file-graph": "Interactive file relationship graph",
    "code-graph": "Interactive symbol relationship graph",
    "source": "Open the selected source file full screen",
    "analysis": "Open the selected result's full analysis",
    "lesson": "Open a lesson card for the selected symbol",
    "definition": "Open the selected symbol's definition",
    "references": "List references to the selected symbol",
    "search": "Focus symbol and file search",
    "ai": "Ask local Ollama to explain the selected result",
    "scan": "Rescan the project and refresh this view",
    "compact": "Toggle the compact single-pane layout",
    "ascii": "Toggle ASCII-only drawing characters",
    "colour": "Toggle colour highlights",
    "help": "Show keys, commands, and display limits",
    "quit": "Leave ThreadTrace",
}

UNICODE_GLYPHS = {
    "selected": "▶", "arrow": "→", "back": "←", "branch": "├─", "last": "└─",
    "pipe": "│  ", "space": "   ", "cycle": "↳", "bullet": "•", "separator": "·",
}

ASCII_GLYPHS = {
    "selected": ">", "arrow": "->", "back": "<-", "branch": "|-", "last": "`-",
    "pipe": "|  ", "space": "   ", "cycle": "\\->", "bullet": "*", "separator": "-",
}

_PYTHON_TOKEN = re.compile(
    r"(?P<comment>#[^\n]*)"
    r"|(?P<string>'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\")"
    r"|(?P<keyword>\b(?:def|class|return|import|from|as|if|elif|else|for|while|try|except|finally|with|raise|yield|async|await|lambda|pass|break|continue|in|is|not|and|or|None|True|False)\b)"
    r"|(?P<number>\b\d+(?:\.\d+)?\b)"
)
_TOKEN_STYLES = {
    "comment": "class:token.comment", "string": "class:token.string",
    "keyword": "class:token.keyword", "number": "class:token.number",
}


class PythonLexer(Lexer):
    """Small Python highlighter that needs no optional lexer package."""

    def lex_document(self, document: Document) -> Callable[[int], StyleAndTextTuples]:
        cache: dict[int, StyleAndTextTuples] = {}

        def line_fragments(lineno: int) -> StyleAndTextTuples:
            if lineno in cache:
                return cache[lineno]
            line = document.lines[lineno] if lineno < len(document.lines) else ""
            fragments: StyleAndTextTuples = []
            position = 0
            for match in _PYTHON_TOKEN.finditer(line):
                if match.start() > position:
                    fragments.append(("", line[position:match.start()]))
                fragments.append((_TOKEN_STYLES[match.lastgroup or ""], match.group()))
                position = match.end()
            if position < len(line):
                fragments.append(("", line[position:]))
            cache[lineno] = fragments
            return fragments

        return line_fragments

RESERVED_KEYS = frozenset({
    "t", "j", "k", "+", "=", "-", "[", "]", ",", ".", "c-p", "c-c", "tab",
    "s-tab", "enter", "escape", "backspace", "pageup", "pagedown", "up", "down",
    "f2", "f3", "c-s", "c-r", "f10",
})

LOADABLE_VIEWS = frozenset({
    "overview", "entrypoints", "architecture", "resources", "modules", "functions",
    "classes", "diagnostics", "findings", "libraries", "cycles",
})


def _terminal_size() -> tuple[int, int]:
    """Return the current terminal size, or a conventional default in tests."""
    try:
        size = get_app_session().output.get_size()
        return size.columns, size.rows
    except Exception:
        return MIN_COLUMNS, 24


def compact_required(columns: int, rows: int, requested: bool = False) -> bool:
    """Use one pane when the terminal cannot show the normal three-pane layout."""
    return requested or columns < MIN_COLUMNS or rows < MIN_ROWS


def resolve_keys(overrides: list[str]) -> dict[str, str]:
    """Apply action=key overrides, rejecting unknown actions and collisions."""
    keys = dict(DEFAULT_KEYS)
    for item in overrides:
        action, separator, key = item.partition("=")
        action, key = action.strip(), key.strip()
        if not separator or action not in DEFAULT_KEYS:
            raise ValueError(f"Unknown TUI keybinding '{item}'. Use action=key with one of: {', '.join(sorted(DEFAULT_KEYS))}.")
        if not key or " " in key:
            raise ValueError(f"TUI keybinding '{item}' needs a single non-space key.")
        keys[action] = key
    for action, key in keys.items():
        if key in RESERVED_KEYS:
            raise ValueError(f"TUI key '{key}' is reserved for a fixed action and cannot be bound to '{action}'.")
    bound: dict[str, str] = {}
    for action, key in keys.items():
        if key in bound:
            raise ValueError(f"TUI key '{key}' is assigned to both '{bound[key]}' and '{action}'.")
        bound[key] = action
    return keys


class ThreadApp:
    """A compact, evidence-first explorer for one project's latest scan."""

    def __init__(
        self, engine: Engine, queries: QueryService, project: str, options: dict[str, Any] | None = None,
        ai_settings: dict[str, Any] | None = None,
    ) -> None:
        settings = options or {}
        self.ai_settings = ai_settings or {}
        self.engine = engine
        self.queries = queries
        self.project = project
        self.project_row = engine.get_project(project)
        self.keys = resolve_keys(list(settings.get("keybindings", [])))
        self.ascii = bool(settings.get("ascii", False))
        self.colour = bool(settings.get("colour", True))
        self.glyphs = ASCII_GLYPHS if self.ascii else UNICODE_GLYPHS
        self.current_view = "overview"
        self.rows: list[dict[str, Any]] = []
        self.selected = 0
        self._status = "Ready. Press ? for help."
        self.status_level = "info"
        self._searching = False
        self.ai_narrative: str | None = None
        self.graph_data: dict[str, Any] | None = None
        self.graph_depth = 1
        self.graph_confidence = "possible"
        self.graph_kind_filter: str | None = None
        self.source_mode = False
        self.edit_mode = False
        self.source_path: Path | None = None
        self.source_editor: TextArea | None = None
        self.source_is_slice = False
        self.source_file_original = ""
        self.source_file_hash: str | None = None
        self.source_selected_line: int | None = None
        self.source_slice_start: int | None = None
        self.source_slice_end: int | None = None
        self.pending_save: tuple[str, bool] | None = None
        self.save_diff = ""
        self.confirm_mode = False
        self.compact_pane = "results"
        self.compact_preference = bool(settings.get("compact", False))

        self.navigation = FormattedTextControl(self._navigation_text, focusable=True)
        self.results = FormattedTextControl(self._results_text, focusable=True, get_cursor_position=self._results_cursor)
        self.status_bar = FormattedTextControl(self._status_text)
        self.search = TextArea(height=1, prompt="Search  ", multiline=False, style="class:search", wrap_lines=False)
        self.inspector = TextArea(
            text="", read_only=True, focusable=True, focus_on_click=True, wrap_lines=True,
            scrollbar=True, style="class:inspector",
        )
        self.search.buffer.on_text_changed += self._on_search_changed
        self.results_window = Window(self.results, always_hide_cursor=True, right_margins=[ScrollbarMargin(display_arrows=True)])

        columns, rows = _terminal_size()
        self.compact = compact_required(columns, rows, self.compact_preference)
        self.app = Application(
            layout=self._build_layout(self.compact), key_bindings=self._key_bindings(),
            full_screen=True, mouse_support=True, style=self._style(), before_render=self._sync_terminal_size,
        )
        self._load_view("overview")

    @property
    def status(self) -> str:
        return self._status

    @status.setter
    def status(self, value: str) -> None:
        self._status = value
        self.status_level = "info"

    def _set_status(self, message: str, level: str = "info") -> None:
        """Keep a non-colour cue alongside every status message."""
        self._status = message
        self.status_level = level
        self.app.invalidate()

    def _style(self) -> Any:
        from prompt_toolkit.styles import Style
        if not self.colour:
            return Style.from_dict({})
        return Style.from_dict({
            "header": "bg:#1d2635 #e8edf5 bold", "status": "bg:#25344a #d8e6f7",
            "search": "bg:#111827 #f8fafc", "inspector": "bg:#101722 #e6edf5", "frame.border": "#52677f", "frame.label": "#90caf9 bold",
            "selected": "bg:#315a8c #ffffff bold", "muted": "#91a4b8", "title": "#f6c978 bold",
            "good": "#8edb9d", "warning": "#f5c978", "token.comment": "#6a9955",
            "token.string": "#ce9178", "token.keyword": "#569cd6", "token.number": "#b5cea8",
        })

    def _build_layout(self, compact: bool) -> Layout:
        """Build the three-pane layout or the compact single-pane fallback."""
        header = Window(FormattedTextControl(self._header_text), height=1, style="class:header")
        footer = Window(self.status_bar, height=1, style="class:status")
        if compact:
            self.compact_body = HSplit([self.results_window], height=Dimension(weight=1))
            self.results_frame = Frame(self.compact_body, title="Explore")
            root = HSplit([header, self.search, self.results_frame, footer])
        else:
            self.results_frame = Frame(self.results_window, title="Explore", height=Dimension(weight=3))
            inspector = Frame(self.inspector, title="Inspector  (Tab to focus; arrows/PgUp/PgDn to scroll)", height=Dimension(weight=2))
            navigation = Frame(Window(self.navigation, always_hide_cursor=True), title="ThreadTrace", width=Dimension(preferred=25))
            root = HSplit([header, VSplit([navigation, HSplit([self.search, self.results_frame, inspector])]), footer])
        return Layout(root, focused_element=self.results)

    def _set_compact_pane(self, pane: str) -> None:
        """Show one pane at a time while the compact layout is active."""
        self.compact_pane = pane
        if not self.compact:
            return
        self.compact_body.children = [to_container(self.results_window if pane == "results" else self.inspector)]
        self.results_frame.title = "Explore" if pane == "results" else "Inspector"
        self.app.layout.focus(self.results if pane == "results" else self.inspector)
        self.app.invalidate()

    def _toggle_compact(self) -> str:
        self.compact = not self.compact
        self.compact_preference = self.compact
        self.compact_pane = "results"
        self.app.layout = self._build_layout(self.compact)
        self.app.layout.focus(self.results)
        self.app.invalidate()
        return "Compact single-pane mode." if self.compact else "Three-pane mode."

    def _sync_terminal_size(self, _application: Any = None) -> None:
        """Switch layout when a resize crosses the minimum usable size."""
        if self.source_mode:
            return
        columns, rows = _terminal_size()
        required = compact_required(columns, rows, self.compact_preference)
        if required != self.compact:
            self.compact = required
            self._rebuild_layout()

    def _rebuild_layout(self) -> None:
        """Rebuild the explorer layout without invalidating inside a render."""
        control = self.app.layout.current_control
        self.app.layout = self._build_layout(self.compact)
        if self.compact:
            self.compact_pane = "results"
            self.compact_body.children = [to_container(self.results_window)]
            self.results_frame.title = "Explore"
        target = self.inspector.control if control == self.inspector.control and not self.compact else self.results
        self.app.layout.focus(target)

    def _toggle_ascii(self) -> str:
        self.ascii = not self.ascii
        self.glyphs = ASCII_GLYPHS if self.ascii else UNICODE_GLYPHS
        self.app.invalidate()
        return "ASCII drawing characters." if self.ascii else "Unicode drawing characters."

    def _toggle_colour(self) -> str:
        self.colour = not self.colour
        self.app.style = self._style()
        self.app.invalidate()
        return "Colour highlights enabled." if self.colour else "Colour disabled; status cues and selection markers remain."

    def run(self) -> None:
        self.app.run()

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        keys = self.keys
        not_source = Condition(lambda: not self.source_mode)
        main_mode = Condition(lambda: not self._search_focused() and not self.source_mode)
        confirm_mode = Condition(lambda: self.confirm_mode)
        results_focused = Condition(lambda: self._results_focused())
        inspector_focused = Condition(lambda: self._inspector_focused())
        source_focused = Condition(lambda: self.source_mode and not self.confirm_mode)

        @bindings.add(keys["quit"], filter=main_mode)
        @bindings.add("c-c")
        def quit_app(event: Any) -> None:
            event.app.exit()

        @bindings.add(keys["help"], filter=main_mode)
        def help_view(event: Any) -> None:
            self._show_help()

        @bindings.add(keys["search"], filter=main_mode)
        def search(event: Any) -> None:
            self._focus_search("")
            self._searching = True

        @bindings.add(keys["command"], filter=main_mode)
        @bindings.add("c-p", filter=main_mode)
        def command(event: Any) -> None:
            self._focus_search(":")
            self._searching = False

        for action, view in (("overview", "overview"), ("entrypoints", "entrypoints"),
                             ("architecture", "architecture"), ("resources", "resources"),
                             ("functions", "functions"), ("findings", "findings"),
                             ("libraries", "libraries"), ("cycles", "cycles")):
            bindings.add(keys[action], filter=main_mode)(lambda event, view=view: self._load_view(view))

        @bindings.add(keys["modules"], filter=main_mode)
        def map_selected(event: Any) -> None:
            self._map_selected_or_modules()

        @bindings.add(keys["file-graph"], filter=main_mode)
        def open_file_graph(event: Any) -> None:
            self._open_graph("file")

        @bindings.add("t", filter=main_mode)
        def toggle_graph(event: Any) -> None:
            if self.graph_data:
                self._open_graph("code" if self.graph_data["kind"] == "file" else "file")
            else:
                self.status = f"Open the File Graph with {keys['file-graph']} first. In a graph, t switches between File and Code Graph."

        @bindings.add("+", filter=main_mode)
        @bindings.add("=", filter=main_mode)
        def graph_zoom_in(event: Any) -> None:
            self._change_graph_depth(1)

        @bindings.add("-", filter=main_mode)
        def graph_zoom_out(event: Any) -> None:
            self._change_graph_depth(-1)

        @bindings.add("[", filter=main_mode)
        def graph_filter_stronger(event: Any) -> None:
            self._change_graph_confidence(-1)

        @bindings.add("]", filter=main_mode)
        def graph_filter_broader(event: Any) -> None:
            self._change_graph_confidence(1)

        @bindings.add(",", filter=main_mode)
        def graph_previous_relationship(event: Any) -> None:
            self._change_graph_kind(-1)

        @bindings.add(".", filter=main_mode)
        def graph_next_relationship(event: Any) -> None:
            self._change_graph_kind(1)

        @bindings.add(keys["classes"], filter=main_mode)
        def code_or_classes(event: Any) -> None:
            if self.current_view in {"file_map", "file_graph", "code_graph"}:
                self._open_selected_source()
            else:
                self._load_view("classes")

        @bindings.add("down", filter=results_focused)
        @bindings.add("j", filter=results_focused)
        def next_row(event: Any) -> None:
            self._move_graph_neighbor(1) if self.graph_data else self._move(1)

        @bindings.add("up", filter=results_focused)
        @bindings.add("k", filter=results_focused)
        def previous_row(event: Any) -> None:
            self._move_graph_neighbor(-1) if self.graph_data else self._move(-1)

        @bindings.add(keys["rescan"], filter=main_mode)
        def rescan(event: Any) -> None:
            self._rescan()

        @bindings.add("enter", filter=not_source)
        def enter(event: Any) -> None:
            if self._search_focused():
                self._submit_search(event)
            else:
                self.status = "Inspector updated for selected result."

        @bindings.add("escape", filter=Condition(lambda: not self.confirm_mode))
        def escape(event: Any) -> None:
            if self.source_mode:
                self._close_fullscreen()
                return
            self.search.buffer.text = ""
            self._searching = False
            event.app.layout.focus(self.results)

        @bindings.add("backspace", filter=main_mode)
        def back(event: Any) -> None:
            self._load_view("overview")

        @bindings.add("pagedown", filter=inspector_focused)
        @bindings.add("c-f", filter=inspector_focused)
        def page_down(event: Any) -> None:
            self.inspector.buffer.cursor_down(count=10)

        @bindings.add("pageup", filter=inspector_focused)
        @bindings.add("c-b", filter=inspector_focused)
        def page_up(event: Any) -> None:
            self.inspector.buffer.cursor_up(count=10)

        @bindings.add("tab", filter=not_source)
        def next_pane(event: Any) -> None:
            if self.compact:
                self._set_compact_pane("inspector" if self.compact_pane == "results" else "results")
            elif self._search_focused():
                event.app.layout.focus(self.results)
            elif self._results_focused():
                event.app.layout.focus(self.inspector)
            else:
                event.app.layout.focus(self.results)

        @bindings.add("s-tab", filter=not_source)
        def previous_pane(event: Any) -> None:
            if self.compact:
                self._set_compact_pane("inspector" if self.compact_pane == "results" else "results")
            elif self._inspector_focused():
                event.app.layout.focus(self.results)
            else:
                event.app.layout.focus(self.search)

        @bindings.add(keys["source"], filter=main_mode)
        def full_source(event: Any) -> None:
            self._open_selected_source()

        @bindings.add(keys["analysis"], filter=main_mode)
        def full_analysis(event: Any) -> None:
            self._open_full_analysis()

        @bindings.add(keys["lesson"], filter=main_mode)
        def lesson(event: Any) -> None:
            self._open_lesson()

        @bindings.add(keys["definition"], filter=main_mode)
        def definition(event: Any) -> None:
            self._go_to_definition()

        @bindings.add(keys["references"], filter=main_mode)
        def references(event: Any) -> None:
            self._open_references()

        @bindings.add("f2", filter=source_focused)
        def toggle_edit(event: Any) -> None:
            self._toggle_edit_mode()

        @bindings.add("f3", filter=source_focused)
        def expand_to_file(event: Any) -> None:
            self._expand_to_containing_file()

        @bindings.add("c-s", filter=source_focused)
        def save_source(event: Any) -> None:
            self._save_source(rescan=False)

        @bindings.add("c-r", filter=source_focused)
        def save_and_scan(event: Any) -> None:
            self._save_source(rescan=True)

        @bindings.add("f10", filter=source_focused)
        def discard_source(event: Any) -> None:
            self._discard_source_changes()

        @bindings.add("y", filter=confirm_mode)
        @bindings.add("enter", filter=confirm_mode)
        def confirm_save(event: Any) -> None:
            self._confirm_save()

        @bindings.add("n", filter=confirm_mode)
        @bindings.add("escape", filter=confirm_mode)
        def cancel_save(event: Any) -> None:
            self._cancel_save()
        return bindings

    def _search_focused(self) -> bool:
        return self.app.layout.current_control == self.search.control

    def _results_focused(self) -> bool:
        return self.app.layout.current_control == self.results

    def _inspector_focused(self) -> bool:
        return self.app.layout.current_control == self.inspector.control

    def _focus_search(self, text: str) -> None:
        self.search.buffer.text = text
        self.app.layout.focus(self.search)

    def _row_identity(self, row: dict[str, Any]) -> tuple[str, ...]:
        """Identify a row across rescans without depending on its position."""
        return tuple(str(row.get(key) or "") for key in ("id", "qualified_name", "relative_path", "module_name", "name", "rule_id"))

    def _capture_location(self) -> dict[str, Any]:
        row = self.rows[self.selected] if self.rows else {}
        return {
            "view": self.current_view,
            "identity": self._row_identity(row),
            "graph": self.graph_data["kind"] if self.graph_data else None,
        }

    def _rescan(self) -> bool:
        """Rescan and restore the previous view and selected result where possible."""
        context = self._capture_location()
        self._set_status("Scanning project...")
        try:
            self.engine.scan(self.project)
        except ThreadError as exc:
            self._set_status(f"Scan failed: {exc}", "error")
            return False
        if context["graph"]:
            self._open_graph(str(context["graph"]))
        elif context["view"] in LOADABLE_VIEWS:
            self._load_view(str(context["view"]))
        else:
            self._load_view("overview")
        match = next((index for index, row in enumerate(self.rows) if self._row_identity(row) == context["identity"]), None)
        if match is not None:
            self.selected = match
            self._refresh_inspector()
        self._set_status("Scan completed; view refreshed.", "good")
        return True

    def _show_help(self) -> None:
        keyed = "\n".join(f"  {self.keys[action]:>4}  {action.replace('-', ' ')}" for action in DEFAULT_KEYS)
        commands = "\n".join(f"  :{name:<10} {description}" for name, description in COMMANDS.items())
        text = (
            "ThreadTrace keys\n\n" + keyed + "\n\n"
            "Graph view\n   t switch file/code - j/k follow a link - +/- hops - [/] confidence - ,/. relationship type\n\n"
            "Source view\n   F2 edit - F3 whole file - Ctrl-S save - Ctrl-R save and scan - F10 discard - Esc return\n\n"
            "Commands (: or Ctrl-P)\n" + commands + "\n\n"
            f"Display: {'compact single pane' if self.compact else 'three panes'} - "
            f"{'ASCII' if self.ascii else 'Unicode'} drawing - {'colour' if self.colour else 'no colour'}.\n"
            f"The three-pane layout needs at least {MIN_COLUMNS} columns and {MIN_ROWS} rows; below that ThreadTrace opens compact.\n\n"
            "Saving checks Python syntax, shows a diff, asks for confirmation, verifies the file hash, and writes atomically.\n"
        )
        self._open_fullscreen(text, "Keys and commands", None, None)

    def _map_selected_or_modules(self) -> None:
        if self.current_view not in {"entrypoints", "file_map"}:
            self._load_view("modules")
            return
        row = self.rows[self.selected] if self.rows else {}
        target = row.get("entry_point_id") or row.get("qualified_name") or row.get("module_name") or row.get("target") or row.get("name")
        if not target:
            self.status = "Select a starting point, path step, or symbol before opening its file map."
            return
        try:
            mapped = self.queries.file_map(self.project, str(target))
        except ThreadError as exc:
            self.status = str(exc)
            return
        self.current_view = "file_map"
        self.rows = [dict(item, name=item["relative_path"], kind="linked file") for item in mapped["files"]]
        self.selected = 0
        self.ai_narrative = None
        self._refresh_inspector()
        self.status = f"{len(self.rows)} file(s) linked to {target}."
        self.app.invalidate()

    def _open_graph(self, kind: str) -> None:
        """Open a deliberately scoped graph centred on one high-signal node."""
        context = self.rows[self.selected] if self.rows else {}
        try:
            graph = self.queries.file_graph(self.project) if kind == "file" else self.queries.code_graph(self.project)
        except ThreadError as exc:
            self.status = str(exc)
            return
        self.graph_data = graph
        self.graph_depth = 1
        self.graph_confidence = "possible"
        self.graph_kind_filter = None
        self.current_view = f"{kind}_graph"
        degrees: dict[str, int] = defaultdict(int)
        for edge in graph["edges"]:
            degrees[edge["source"]] += 1
            degrees[edge["target"]] += 1
        self.rows = sorted(graph["nodes"], key=lambda node: (-degrees[node["id"]], str(node["name"])))
        preferred_id = context.get("module_name") if kind == "file" else context.get("symbol_id") or context.get("id")
        self.selected = next((index for index, node in enumerate(self.rows) if node["id"] == preferred_id), 0)
        self.ai_narrative = None
        self._refresh_inspector()
        noun = "files" if kind == "file" else "functions and classes"
        separator = self.glyphs["separator"]
        self.status = f"{kind.title()} Graph: {len(self.rows)} {noun}. j/k follows links {separator} +/- zoom {separator} [/] confidence {separator} ,/. relationship {separator} t switches graph."
        self.app.invalidate()

    def _graph_edges(self) -> list[dict[str, Any]]:
        if not self.graph_data:
            return []
        levels = ["confirmed", "high", "possible", "unknown"]
        permitted = set(levels[: levels.index(self.graph_confidence) + 1])
        return [
            edge for edge in self.graph_data["edges"]
            if edge["confidence"] in permitted and (self.graph_kind_filter is None or edge["kind"] == self.graph_kind_filter)
        ]

    def _change_graph_depth(self, delta: int) -> None:
        if not self.graph_data:
            self.status = "Open a graph with p first."
            return
        self.graph_depth = max(1, min(4, self.graph_depth + delta))
        self._refresh_inspector()
        self.status = f"Graph zoom: {self.graph_depth} hop(s) from the selected node."
        self.app.invalidate()

    def _change_graph_confidence(self, delta: int) -> None:
        if not self.graph_data:
            self.status = "Open a graph with p first."
            return
        levels = ["confirmed", "high", "possible", "unknown"]
        index = levels.index(self.graph_confidence)
        self.graph_confidence = levels[max(0, min(len(levels) - 1, index + delta))]
        self._refresh_inspector()
        self.status = f"Graph filter: showing relationships through {self.graph_confidence}."
        self.app.invalidate()

    def _change_graph_kind(self, delta: int) -> None:
        if not self.graph_data:
            self.status = "Open a graph with p first."
            return
        options: list[str | None] = [None, *sorted({edge["kind"] for edge in self.graph_data["edges"]})]
        current = options.index(self.graph_kind_filter)
        self.graph_kind_filter = options[(current + delta) % len(options)]
        label = self.graph_kind_filter.replace("_", " ") if self.graph_kind_filter else "all relationship types"
        self._refresh_inspector()
        self.status = f"Graph filter: {label}."
        self.app.invalidate()

    def _move_graph_neighbor(self, delta: int) -> None:
        if not self.graph_data or not self.rows:
            return
        current = self.rows[self.selected]["id"]
        neighbors = sorted({
            edge["target"] if edge["source"] == current else edge["source"]
            for edge in self._graph_edges() if current in {edge["source"], edge["target"]}
        })
        if not neighbors:
            self.status = "The selected graph node has no links under the current confidence filter."
            return
        current_neighbor = getattr(self, "_graph_neighbor", -1)
        self._graph_neighbor = (current_neighbor + delta) % len(neighbors)
        target = neighbors[self._graph_neighbor]
        self.selected = next(index for index, node in enumerate(self.rows) if node["id"] == target)
        self._graph_neighbor = -1
        self._refresh_inspector()
        self.status = "Moved to a linked node. +/- changes scope; [/] changes confidence; ,/. changes relationship type." 
        self.app.invalidate()

    def _open_selected_source(self) -> None:
        row = self.rows[self.selected] if self.rows else {}
        relative = row.get("relative_path")
        if not relative:
            self.status = "This result has no direct source file. Press z for its full analysis instead."
            return
        root = Path(self.project_row["root_path"]).resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            self.status = "Source file is outside the registered project or no longer exists."
            return
        try:
            source = path.read_text(encoding="utf-8")
        except OSError as exc:
            self.status = f"Cannot open source file: {exc}"
            return
        start = row.get("line_start") or row.get("line_number")
        end = row.get("line_end")
        if isinstance(start, int) and isinstance(end, int) and start >= 1 and end >= start:
            lines = source.splitlines(keepends=True)
            definition = "".join(lines[start - 1:end])
            self._open_fullscreen(
                definition,
                f"Definition  /  {relative}:{start}-{end}",
                path,
                None,
                full_source=source,
                is_slice=True,
                selected_line=start,
                slice_end=end,
            )
            return
        self._open_fullscreen(source, f"Code  /  {relative}", path, start, full_source=source, selected_line=start)

    def _open_full_analysis(self) -> None:
        content = "".join(text for _, text in self._detail_text())
        self._open_fullscreen(content, "Detailed analysis", None, None)

    def _open_lesson(self) -> None:
        row = self.rows[self.selected] if self.rows else {}
        target = row.get("qualified_name")
        if not target:
            self.status = "Select a function, method, or class first."
            return
        try:
            lesson = self.queries.lesson(self.project, str(target))
        except ThreadError as exc:
            self.status = str(exc)
            return
        bullet = self.glyphs["bullet"]
        lines = [
            f"{lesson['kind'].replace('_', ' ').title()}: {lesson['qualified_name']}\n\n",
            f"Purpose\n{lesson['purpose']}\n\n",
            f"Source\n{lesson['source']['path']}:{lesson['source']['line_start']}-{lesson['source']['line_end']}\n",
        ]
        if lesson.get("signature"):
            lines.extend([f"\nSignature\n{lesson['signature']}\n"])
        if lesson["inputs"]:
            lines.append("\nInputs\n")
            lines.extend(f"{bullet} {item['variable_name']} at line {item['line_number']}\n" for item in lesson["inputs"])
        if lesson["outputs"]:
            lines.append("\nOutputs\n")
            lines.extend(f"{bullet} {item['event_kind']} {item['expression'] or ''} (line {item['line_number']})\n" for item in lesson["outputs"])
        if lesson["calls"]:
            lines.append("\nCalls\n")
            lines.extend(f"{bullet} {item['name']} ({item['resolution']}, line {item['line']})\n" for item in lesson["calls"][:20])
        if lesson["used_by"]:
            lines.append("\nUsed by\n")
            lines.extend(f"{bullet} {item['name']} - {item['path']}:{item['line']}\n" for item in lesson["used_by"][:20])
        lines.append("\nPatterns detected\n")
        if lesson["patterns"]:
            lines.extend(f"{bullet} {item['name']} [{item['confidence']}]\n  {item['reason']}\n" for item in lesson["patterns"])
        else:
            lines.append("No supported local code pattern was recognised. That is not evidence that the function has no pattern.\n")
        lines.extend([f"\nNote\n{lesson['note']}\n", "\nPress Esc to return to the explorer.\n"])
        self._open_fullscreen("".join(lines), f"Lesson  /  {target}", None, None)

    def _go_to_definition(self) -> None:
        row = self.rows[self.selected] if self.rows else {}
        target = row.get("qualified_name") or row.get("symbol_name") or row.get("definition_name")
        if not target:
            self._set_status("Select a symbol to open its definition.", "warning")
            return
        try:
            rows = self.queries.symbol(self.project, str(target))
        except ThreadError as exc:
            self._set_status(str(exc), "warning")
            return
        if not rows:
            self._set_status(f"No stored definition matches {target}.", "warning")
            return
        self.graph_data = None
        self.current_view = "definitions"
        self.rows = [dict(item, kind=item["kind"]) for item in rows]
        self.selected = 0
        self._refresh_inspector()
        self._set_status(f"{len(rows)} definition(s) for {target}. Press {self.keys['source']} to open the source.")

    def _open_references(self) -> None:
        row = self.rows[self.selected] if self.rows else {}
        target = row.get("qualified_name") or row.get("symbol_name") or row.get("target")
        if not target:
            self._set_status("Select a symbol or import to list its references.", "warning")
            return
        try:
            rows = self.queries.references(self.project, str(target))
        except ThreadError as exc:
            self._set_status(str(exc), "warning")
            return
        self.graph_data = None
        self.current_view = "references"
        self.rows = [dict(item, kind=item.get("resolution", "reference")) for item in rows]
        self.selected = 0
        self._refresh_inspector()
        self._set_status(f"{len(rows)} reference(s) to {target}. Press {self.keys['source']} to inspect one.")

    def _open_fullscreen(
        self,
        content: str,
        title: str,
        path: Path | None,
        line: int | None,
        full_source: str | None = None,
        is_slice: bool = False,
        selected_line: int | None = None,
        slice_end: int | None = None,
    ) -> None:
        self.source_mode = True
        self.edit_mode = False
        self.confirm_mode = False
        self.pending_save = None
        self.source_path = path
        self.source_original = content
        self.source_file_original = full_source or content
        self.source_file_hash = self._content_hash(self.source_file_original) if path else None
        self.source_is_slice = is_slice
        self.source_selected_line = selected_line or line
        self.source_slice_start = selected_line if is_slice else None
        self.source_slice_end = slice_end if is_slice else None
        self.fullscreen_title = title
        self._set_fullscreen_editor(content, line)
        self.status = "Read-only view. F2 enables editing for source files; Esc returns to the explorer."

    def _set_fullscreen_editor(self, content: str, line: int | None = None, cursor: int | None = None) -> None:
        self.source_editor = TextArea(
            text=content,
            multiline=True,
            read_only=not self.edit_mode,
            focusable=True,
            focus_on_click=True,
            wrap_lines=False,
            scrollbar=True,
            line_numbers=self.source_path is not None,
            style="class:code",
            lexer=PythonLexer(),
        )
        if cursor is not None:
            self.source_editor.buffer.cursor_position = min(cursor, len(content))
        elif line and self.source_path:
            row = max(0, line - 1)
            self.source_editor.buffer.cursor_position = self.source_editor.buffer.document.translate_row_col_to_index(row, 0)
        mode = "EDITING" if self.edit_mode else "read-only"
        header = FormattedTextControl(lambda: [("class:title", f" {self.fullscreen_title}     {mode}")])
        footer = FormattedTextControl(
            lambda: [("class:status", f" {self.status}     F2 edit/lock | F3 full file | Ctrl-S save | Ctrl-R save + scan | F10 discard | Esc return")]
        )
        root = HSplit([
            Window(header, height=1, style="class:header"),
            Frame(self.source_editor, title="Source" if self.source_path else "Analysis"),
            Window(footer, height=1, style="class:status"),
        ])
        self.app.layout = Layout(root, focused_element=self.source_editor)
        self.app.invalidate()

    def _toggle_edit_mode(self) -> None:
        if not self.source_path or not self.source_editor:
            self.status = "Only a source file can be edited. Press Esc, select a symbol or file, then press v."
            return
        cursor = self.source_editor.buffer.cursor_position
        self.edit_mode = not self.edit_mode
        self._set_fullscreen_editor(self.source_editor.text, cursor=cursor)
        scope = "the selected definition" if self.source_is_slice else "this file"
        self.status = f"Editing enabled for {scope}. Ctrl-S saves to disk." if self.edit_mode else "Editing locked. Ctrl-S is disabled until F2 enables editing."

    def _expand_to_containing_file(self) -> None:
        """Deliberately switch a clean definition view to an editable whole-file view."""
        if not self.source_path or not self.source_editor:
            self.status = "Only a source definition can be expanded to its containing file."
            return
        if not self.source_is_slice:
            self.status = "This view already contains the entire file."
            return
        if self.source_editor.text != self.source_original:
            self.status = "Save or discard the definition edit before opening the whole file."
            return
        self.source_is_slice = False
        self.source_slice_start = None
        self.source_slice_end = None
        self.edit_mode = False
        self.source_original = self.source_file_original
        relative = self.source_path.relative_to(Path(self.project_row["root_path"]).resolve()).as_posix()
        self.fullscreen_title = f"Code  /  {relative}"
        self._set_fullscreen_editor(self.source_original, line=self.source_selected_line)
        self.status = "Whole-file read-only view. Press F2 to edit it."

    @staticmethod
    def _content_hash(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    def _save_source(self, rescan: bool) -> None:
        """Check the pending edit, then show a diff before any write."""
        content = self._pending_content()
        if content is None:
            return
        try:
            ast.parse(content, filename=str(self.source_path))
        except SyntaxError as exc:
            self._set_status(f"Save refused: Python syntax error at line {exc.lineno}: {exc.msg}.", "error")
            return
        try:
            on_disk = self.source_path.read_text(encoding="utf-8")
        except OSError as exc:
            self._set_status(f"Cannot verify source before saving: {exc}", "error")
            return
        if self.source_file_hash and self._content_hash(on_disk) != self.source_file_hash:
            self._set_status("Save refused: this file changed outside ThreadTrace after it was opened. Reopen it and merge the edits.", "warning")
            return
        diff = "".join(difflib.unified_diff(
            self.source_file_original.splitlines(keepends=True),
            content.splitlines(keepends=True),
            fromfile=f"{self.source_path.name} (opened)",
            tofile=f"{self.source_path.name} (pending)",
            n=3,
        ))
        if not diff:
            self._set_status("No changes to save.")
            return
        diff_lines = diff.splitlines(keepends=True)
        self.save_diff = "".join(diff_lines[:400])
        if len(diff_lines) > 400:
            self.save_diff += f"... {len(diff_lines) - 400} more diff lines are not shown.\n"
        self.pending_save = (content, rescan)
        self._show_diff_confirmation()

    def _pending_content(self) -> str | None:
        """Return the whole-file text that the current editor would save."""
        if not self.source_path or not self.source_editor:
            self._set_status("Detailed analysis is read-only. Open a source file with v to edit it.", "warning")
            return None
        if not self.edit_mode:
            self._set_status("Press F2 before saving source changes.", "warning")
            return None
        replacement = self.source_editor.text
        if not self.source_is_slice:
            return replacement
        if self.source_slice_start is None or self.source_slice_end is None:
            self._set_status("Save refused: ThreadTrace lost the selected definition boundary. Reopen the definition.", "error")
            return None
        original_lines = self.source_file_original.splitlines(keepends=True)
        return "".join(
            original_lines[: self.source_slice_start - 1]
            + replacement.splitlines(keepends=True)
            + original_lines[self.source_slice_end :]
        )

    def _show_diff_confirmation(self) -> None:
        self.confirm_mode = True
        self.edit_mode = False
        diff_area = TextArea(text=self.save_diff, read_only=True, focusable=True, wrap_lines=False, scrollbar=True, style="class:code")
        root = HSplit([
            Window(FormattedTextControl(lambda: [("class:title", f" Confirm save  /  {self.fullscreen_title}")]), height=1, style="class:header"),
            Frame(diff_area, title="Pending changes (read-only diff)"),
            Window(FormattedTextControl(lambda: [("class:status", " y or Enter: write the diff atomically - n or Esc: cancel")]), height=1, style="class:status"),
        ])
        self.app.layout = Layout(root, focused_element=diff_area)
        self._set_status("Review the diff, then press y to save or n to cancel.", "warning")

    def _confirm_save(self) -> None:
        if not self.confirm_mode or not self.pending_save:
            return
        content, rescan = self.pending_save
        name = self.source_path.name if self.source_path else "source"
        self.confirm_mode = False
        self.pending_save = None
        if not self._write_atomically(content):
            self.edit_mode = True
            self._restore_fullscreen()
            return
        replacement = self.source_editor.text if self.source_editor else ""
        if self.source_is_slice and self.source_slice_start is not None:
            self.source_slice_end = self.source_slice_start + max(1, len(replacement.splitlines())) - 1
        self.source_original = replacement
        self.source_file_original = content
        self.source_file_hash = self._content_hash(content)
        self.edit_mode = False
        if not rescan:
            self._restore_fullscreen()
            self._set_status(f"Saved {name}. Scan results are stale; press r to rescan.", "good")
            return
        self._close_fullscreen()
        if self._rescan():
            self._set_status(f"Saved {name} and refreshed the {self.current_view.title()} view.", "good")

    def _write_atomically(self, content: str) -> bool:
        """Replace the file through a same-directory temporary file."""
        if not self.source_path:
            return False
        temporary: str | None = None
        try:
            mode = self.source_path.stat().st_mode & 0o7777
            descriptor, temporary = tempfile.mkstemp(dir=self.source_path.parent, prefix=".threadtrace-", suffix=".tmp")
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, self.source_path)
            return True
        except OSError as exc:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            self._set_status(f"Save failed: {exc}", "error")
            return False

    def _restore_fullscreen(self) -> None:
        self.confirm_mode = False
        self.pending_save = None
        self._set_fullscreen_editor(self.source_original, line=self.source_selected_line)

    def _cancel_save(self) -> None:
        if not self.confirm_mode:
            return
        self.edit_mode = True
        self._restore_fullscreen()
        self._set_status("Save cancelled; the file is unchanged.", "warning")

    def _discard_source_changes(self) -> None:
        if not self.source_editor or not self.source_path:
            self._set_status("Detailed analysis has no editable source changes.", "warning")
            return
        self.edit_mode = False
        self.confirm_mode = False
        self.pending_save = None
        self._set_fullscreen_editor(self.source_original, line=self.source_selected_line)
        self._set_status("Unsaved changes discarded.", "warning")

    def _close_fullscreen(self) -> None:
        if self.source_path and self.source_editor and self.source_editor.text != self.source_original:
            self._set_status("Unsaved edits. Ctrl-S saves them; F10 discards them.", "warning")
            return
        self.source_mode = False
        self.edit_mode = False
        self.confirm_mode = False
        self.pending_save = None
        self.save_diff = ""
        self.source_path = None
        self.source_editor = None
        self.source_is_slice = False
        self.source_file_hash = None
        self.source_slice_start = None
        self.source_slice_end = None
        self.app.layout = self._build_layout(self.compact)
        self.app.layout.focus(self.results)
        self._set_status("Returned to explorer.")
        self.app.invalidate()

    def _submit_search(self, event: Any) -> None:
        value = self.search.text.strip()
        if value.startswith(":"):
            fragment = value[1:].strip()
            if fragment and " " not in fragment and fragment not in COMMANDS:
                matches = [entry["name"] for entry in self._command_entries(fragment)]
                if len(matches) == 1:
                    fragment = matches[0]
            self._run_command(fragment)
        elif value:
            self._searching = True
            self._on_search_changed(None)
            self.status = f"Search results for {value!r}."
        event.app.layout.focus(self.results)

    def _command_entries(self, prefix: str = "") -> list[dict[str, Any]]:
        return [{"name": name, "description": description, "kind": "command"}
                for name, description in COMMANDS.items() if name.startswith(prefix)]

    def _start_search(self) -> None:
        self._searching = True
        self._focus_search("")

    def _command_scan(self) -> str | None:
        return "Scan completed; view refreshed." if self._rescan() else None

    def _run_command(self, command: str) -> None:
        name = command.split(" ", 1)[0]
        actions: dict[str, Callable[[], str | None]] = {
            "overview": lambda: self._load_view("overview"),
            "entrypoints": lambda: self._load_view("entrypoints"),
            "architecture": lambda: self._load_view("architecture"),
            "resources": lambda: self._load_view("resources"),
            "modules": lambda: self._load_view("modules"),
            "functions": lambda: self._load_view("functions"),
            "classes": lambda: self._load_view("classes"),
            "findings": lambda: self._load_view("findings"),
            "libraries": lambda: self._load_view("libraries"),
            "cycles": lambda: self._load_view("cycles"),
            "file-graph": lambda: self._open_graph("file"),
            "code-graph": lambda: self._open_graph("code"),
            "source": self._open_selected_source,
            "analysis": self._open_full_analysis,
            "lesson": self._open_lesson,
            "definition": self._go_to_definition,
            "references": self._open_references,
            "search": self._start_search,
            "ai": self._ai_explain,
            "scan": self._command_scan,
            "compact": self._toggle_compact,
            "ascii": self._toggle_ascii,
            "colour": self._toggle_colour,
            "help": self._show_help,
            "quit": lambda: self.app.exit(),
        }
        action = actions.get(name)
        if not action:
            self._set_status(f"Unknown command: {name or '(empty)'}. Type : to list commands.", "error")
            return
        try:
            message = action()
        except ThreadError as exc:
            self._set_status(f"Command failed: {exc}", "error")
            return
        if message:
            self._set_status(message)

    def _ai_explain(self) -> None:
        row = self.rows[self.selected] if self.rows else {}
        target = row.get("qualified_name") or row.get("name") or row.get("module_name") or "overview"
        self._set_status("Requesting a validated evidence-only explanation...")
        try:
            result = self.queries.ai_explain(self.project, str(target), self.ai_settings)
            self.ai_narrative = result["narrative"]
            self._refresh_inspector()
            self._set_status(f"AI interpretation ready for {target}. Verify cited evidence.", "good")
        except ThreadError as exc:
            self._set_status(f"AI explanation unavailable: {exc}", "error")

    def _on_search_changed(self, _: Any) -> None:
        if not self._search_focused() and not self._searching:
            return
        value = self.search.text.strip()
        if value.startswith(":"):
            self.graph_data = None
            self.rows = self._command_entries(value[1:].split(" ", 1)[0])
            self.selected = 0
            self.current_view = "commands"
            self._refresh_inspector()
            self.app.invalidate()
            return
        if not value:
            return
        try:
            self.rows = self.queries.search(self.project, value)
            self.selected = 0
            self.current_view = "search"
        except ThreadError as exc:
            self.rows = []
            self._set_status(str(exc), "error")
        self._refresh_inspector()
        self.app.invalidate()

    def _load_view(self, view: str) -> None:
        self.current_view = view
        self.graph_data = None
        self._searching = False
        self.search.buffer.text = ""
        try:
            if view == "overview":
                overview = self.queries.overview(self.project)
                self.rows = [dict(item, kind=item["role"], module_name=item["name"]) for item in overview["components"]]
                self.status = overview["purpose"]
            elif view == "entrypoints":
                self.rows, self.status = self.queries.entrypoints(self.project), "Scripts, commands, routes, workers, and tests."
            elif view == "architecture":
                self.rows, self.status = self.queries.architecture(self.project)["components"], "Evidence-backed components and relationships."
            elif view == "resources":
                self.rows, self.status = self.queries.resources(self.project), "External resource boundaries."
            elif view == "modules":
                self.rows, self.status = self.queries.modules(self.project), "Modules in the latest completed scan."
            elif view == "functions":
                self.rows, self.status = self.queries.functions(self.project), "Functions and methods."
            elif view == "classes":
                self.rows, self.status = self.queries.classes(self.project), "Classes, declared bases, methods, and attributes."
            elif view == "diagnostics":
                self.rows, self.status = self.queries.diagnostics(self.project, include_unresolved_calls=True), "Findings and dynamic boundaries."
            elif view == "findings":
                self.rows, self.status = self.queries.findings(self.project), "Needs attention. Technical call-resolution details are hidden here."
            elif view == "libraries":
                self.rows, self.status = self.queries.libraries(self.project), "Imported and declared libraries."
            else:
                self.rows, self.status = self.queries.cycles(self.project), "Circular internal imports."
        except ThreadError as exc:
            self.rows, self.status = [], str(exc)
        self.selected = 0
        self.ai_narrative = None
        self._refresh_inspector()
        self.app.invalidate()

    def _move(self, delta: int) -> None:
        if self.rows:
            self.selected = max(0, min(len(self.rows) - 1, self.selected + delta))
            self._refresh_inspector()
            self.app.invalidate()

    def _results_cursor(self) -> Point:
        if self.graph_data:
            return Point(x=0, y=0)
        return Point(x=0, y=self.selected)

    def _refresh_inspector(self) -> None:
        self.inspector.text = "".join(text for _, text in self._detail_text())
        self.inspector.buffer.cursor_position = 0

    def _header_text(self) -> StyleAndTextTuples:
        mode = " - compact" if self.compact else ""
        return [("class:title", f" ThreadTrace  /  {self.project_row['name']}"), ("", f"     {self.current_view.title()}{mode}")]

    def _navigation_text(self) -> StyleAndTextTuples:
        separator = self.glyphs["separator"]
        keys = self.keys
        return [
            ("class:title", "Explore\n"),
            ("", f" [{keys['overview']}] Overview\n [{keys['entrypoints']}] Entrypoints\n [{keys['architecture']}] Architecture\n [{keys['resources']}] Resources\n\n"),
            ("class:title", "Browse\n"),
            ("", f" [{keys['modules']}] Modules\n [{keys['functions']}] Functions\n [{keys['classes']}] Classes\n [{keys['file-graph']}] File graph\n [{keys['findings']}] Findings\n [{keys['libraries']}] Libraries\n [{keys['cycles']}] Cycles\n\n"),
            ("class:muted", f" Details: {keys['modules']} files {separator} {keys['classes']} code {separator} {keys['lesson']} lesson\n"
                            f" Graph: {keys['file-graph']} file {separator} t switch {separator} j/k follow {separator} +/- zoom {separator} [/] confidence {separator} ,/. type\n"
                            f" {keys['search']} Search\n {keys['command']} Commands\n {keys['rescan']} Rescan\n {keys['help']} Help\n {keys['quit']} Quit"),
        ]

    def _results_text(self) -> StyleAndTextTuples:
        if self.graph_data:
            return self._graph_text()
        if not self.rows:
            return [("class:muted", "\n  No results.")]
        output: StyleAndTextTuples = []
        for index, row in enumerate(self.rows):
            marker = self.glyphs["selected"] if index == self.selected else " "
            if self.current_view == "commands":
                text = f"{marker} :{str(row['name']):20}  {row['description'][:60]}\n"
            else:
                name = str(row.get("qualified_name") or row.get("name") or row.get("module_name") or row.get("rule_id") or "—")
                kind = str(row.get("kind") or row.get("role") or row.get("severity") or row.get("category") or "—")
                text = f"{marker} {name[:58]:58}  {kind[:16]:16}  {self._location(row)[:24]}\n"
            output.append(("class:selected" if index == self.selected else "", text))
        return output

    def _graph_text(self) -> StyleAndTextTuples:
        """Render an inspectable local graph; selected nodes keep a readable scope."""
        if not self.graph_data or not self.rows:
            return [("class:muted", "\n  No graph nodes are available.")]
        glyphs = self.glyphs
        current = self.rows[self.selected]["id"]
        names = {node["id"]: str(node["name"]) for node in self.rows}
        edges = self._graph_edges()
        adjacency: dict[str, list[tuple[dict[str, Any], str, str]]] = defaultdict(list)
        for edge in edges:
            adjacency[edge["source"]].append((edge, edge["target"], glyphs["arrow"]))
            adjacency[edge["target"]].append((edge, edge["source"], glyphs["back"]))
        title = "FILE" if self.graph_data["kind"] == "file" else "CODE"
        separator = glyphs["separator"]
        lines: StyleAndTextTuples = [
            ("class:title", f"\n {title} RELATIONSHIP GRAPH\n"),
            ("class:muted", f" Focused view: {self.graph_depth} hop(s) {separator} confidence through {self.graph_confidence} {separator} {self.graph_kind_filter or 'all links'}\n"),
            ("class:muted", f" j/k or up/down follow a link {separator} +/- zoom {separator} [/] confidence {separator} ,/. type {separator} t switch graph {separator} c code\n\n"),
        ]
        shown = 0

        def render_node(node_id: str, level: int, prefix: str, ancestry: set[str]) -> None:
            nonlocal shown
            if shown >= 18:
                return
            label = names.get(node_id, node_id)
            if level == 0:
                lines.append(("class:selected", f" {glyphs['selected']} [{label}]\n"))
            else:
                lines.append(("class:title" if node_id == current else "", f"{prefix}[{label}]\n"))
            shown += 1
            if level >= self.graph_depth:
                return
            related = sorted(adjacency.get(node_id, []), key=lambda item: (item[0]["kind"], names.get(item[1], item[1])))
            for index, (edge, other, direction) in enumerate(related[:8]):
                branch = glyphs["last"] if index == min(len(related), 8) - 1 else glyphs["branch"]
                relation = edge["kind"].replace("_", " ")
                label_hint = edge["labels"][0] if edge.get("labels") else relation
                lines.append(("class:good" if edge["confidence"] == "confirmed" else "class:warning", f"{prefix}{branch} {direction} {relation} [{edge['confidence']}]  {label_hint[:72]}\n"))
                if other in ancestry:
                    lines.append(("class:muted", f"{prefix}   {glyphs['cycle']} [{names.get(other, other)}] (already shown)\n"))
                else:
                    render_node(other, level + 1, prefix + (glyphs["space"] if branch == glyphs["last"] else glyphs["pipe"]), ancestry | {node_id})

        render_node(current, 0, " ", set())
        if len(adjacency.get(current, [])) > 8 or shown >= 18:
            lines.append(("class:muted", "\n Graph is intentionally scoped. Follow a link with j/k or move focus to reveal more.\n"))
        return lines

    def _detail_text(self) -> StyleAndTextTuples:
        if not self.rows:
            return [("class:muted", "\nNo supported starting point was detected. Try [e] Entrypoints or inspect the project configuration.\n")]
        if self.current_view == "commands":
            selected = self.rows[self.selected]
            return [("class:title", f"\n:{selected['name']}\n"), ("", f"{selected['description']}\n"),
                    ("class:muted", "\nPress Enter to run the selected command or keep typing to narrow the list.\n")]
        row = self.rows[self.selected]
        name = str(row.get("qualified_name") or row.get("name") or row.get("module_name") or "Selected result")
        lines: StyleAndTextTuples = [("class:title", f"\n{name}\n"), ("class:muted", f"{row.get('kind') or row.get('role') or 'item'}\n\n")]
        for label in ("purpose", "summary", "signature", "confidence", "operation", "message", "plain_summary", "plain_explanation", "explanation", "confidence_explanation"):
            if row.get(label):
                lines.append(("", f"{label.replace('_', ' ').title()}: {row[label]}\n"))
        if row.get("description"):
            lines.extend([("class:good", "\nWhat it does\n"), ("", f"{row['description']}\n")])
        if row.get("usage_files"):
            lines.append(("class:good", "\nUsed in\n"))
            lines.extend(("", f"{path}\n") for path in row["usage_files"])
        evidence = row.get("evidence")
        if isinstance(evidence, dict):
            lines.extend([("class:good", "\nEvidence\n"), ("", f"{evidence.get('kind', 'source')}: {evidence.get('path', 'unknown')}:{evidence.get('line_start', '?')}\n")])
        if row.get("entry_point_id"):
            try:
                path = self.queries.path(self.project, row["entry_point_id"])
                lines.append(("class:good", "\nStatic path\n"))
                for step in path["steps"][:8]:
                    lines.append(("", f"{step.get('source_name', 'entry')} {self.glyphs['arrow']} {step.get('target_name', '?')} [{step['confidence']}]\n"))
            except ThreadError as exc:
                lines.append(("class:warning", f"\n{exc}\n"))
        if row.get("qualified_name"):
            source = self._source_excerpt(row)
            if source:
                lines.extend([("class:good", "\nSource\n"), ("", source)])
        if self.graph_data:
            node_id = row.get("id")
            graph_edges = [edge for edge in self._graph_edges() if node_id in {edge["source"], edge["target"]}]
            lines.append(("class:good", "\nGraph links\n"))
            if not graph_edges:
                lines.append(("class:muted", "No relationships match the current confidence filter.\n"))
            else:
                names = {node["id"]: str(node["name"]) for node in self.rows}
                for edge in graph_edges:
                    other = edge["target"] if edge["source"] == node_id else edge["source"]
                    direction = "calls / points to" if edge["source"] == node_id else "is used by / receives from"
                    reason = edge["labels"][0] if edge.get("labels") else edge["kind"].replace("_", " ")
                    lines.append(("", f"{direction} {names.get(other, other)} — {edge['kind'].replace('_', ' ')} [{edge['confidence']}]\n  {reason}\n"))
        if self.ai_narrative:
            lines.extend([("class:warning", "\nAI-assisted explanation — verify citations\n"), ("", self.ai_narrative + "\n")])
        if len(lines) <= 2:
            lines.append(("class:muted", "No additional source evidence is available for this result."))
        return lines

    def _status_text(self) -> StyleAndTextTuples:
        cue = {"info": "INFO", "good": " OK ", "warning": "WARN", "error": "FAIL"}.get(self.status_level, "INFO")
        return [("", f" [{cue}] {self.status}")]

    @staticmethod
    def _location(row: dict[str, Any]) -> str:
        path = str(row.get("relative_path") or row.get("module_name") or "")
        line = row.get("line_start") or row.get("line_number") or ""
        return f"{path}{':' if path and line else ''}{line}" or "—"

    def _source_excerpt(self, row: dict[str, Any]) -> str | None:
        path, start = row.get("relative_path"), row.get("line_start")
        if not path or not start:
            return None
        try:
            lines = (Path(self.project_row["root_path"]) / path).read_text(encoding="utf-8").splitlines()
        except OSError:
            return None
        first = max(0, start - 3)
        last = min(len(lines), max(row.get("line_end") or start, start) + 2)
        return "\n".join(f"{number + 1:>4}  {lines[number]}" for number in range(first, last))
