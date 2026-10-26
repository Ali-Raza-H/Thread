"""A light, keyboard-first terminal explorer built with prompt_toolkit."""

from __future__ import annotations

import os
import hashlib
from collections import defaultdict
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.widgets import Frame, TextArea

from .engine import Engine, ThreadError
from .query import QueryService


class ThreadApp:
    """A compact, evidence-first explorer for one project's latest scan."""

    def __init__(self, engine: Engine, queries: QueryService, project: str) -> None:
        self.engine = engine
        self.queries = queries
        self.project = project
        self.project_row = engine.get_project(project)
        self.current_view = "guide"
        self.rows: list[dict[str, Any]] = []
        self.selected = 0
        self.status = "Ready. Press ? for help."
        self._searching = False
        self.ai_narrative: str | None = None
        self.guide_data: dict[str, Any] | None = None
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

        root = HSplit([
            Window(FormattedTextControl(self._header_text), height=1, style="class:header"),
            VSplit([
                Frame(Window(self.navigation, always_hide_cursor=True), title="Thread", width=Dimension(preferred=25)),
                HSplit([
                    self.search,
                    Frame(self.results_window, title="Explore", height=Dimension(weight=3)),
                    Frame(self.inspector, title="Inspector  (Tab to focus; arrows/PgUp/PgDn to scroll)", height=Dimension(weight=2)),
                ]),
            ]),
            Window(self.status_bar, height=1, style="class:status"),
        ])
        self.main_layout = Layout(root, focused_element=self.results)
        self.app = Application(
            layout=self.main_layout, key_bindings=self._key_bindings(),
            full_screen=True, mouse_support=True, style=self._style(),
        )
        self._load_view("guide")

    @staticmethod
    def _style() -> Any:
        from prompt_toolkit.styles import Style
        return Style.from_dict({
            "header": "bg:#1d2635 #e8edf5 bold", "status": "bg:#25344a #d8e6f7",
            "search": "bg:#111827 #f8fafc", "inspector": "bg:#101722 #e6edf5", "frame.border": "#52677f", "frame.label": "#90caf9 bold",
            "selected": "bg:#315a8c #ffffff bold", "muted": "#91a4b8", "title": "#f6c978 bold",
            "good": "#8edb9d", "warning": "#f5c978",
        })

    def run(self) -> None:
        self.app.run()

    def _key_bindings(self) -> KeyBindings:
        bindings = KeyBindings()
        main_mode = Condition(lambda: not self._search_focused() and not self.source_mode)
        not_source = Condition(lambda: not self.source_mode)
        results_focused = Condition(lambda: self._results_focused())
        inspector_focused = Condition(lambda: self._inspector_focused())
        source_focused = Condition(lambda: self.source_mode)

        @bindings.add("q", filter=main_mode)
        @bindings.add("c-c")
        def quit_app(event: Any) -> None:
            event.app.exit()

        @bindings.add("?", filter=main_mode)
        def help_view(event: Any) -> None:
            self.status = "g guide · 1-9 choose start · w walk · m files · c code · / search · Tab inspector · r rescan · q quit"

        @bindings.add("/", filter=main_mode)
        def search(event: Any) -> None:
            self.search.buffer.text = ""
            self._searching = True
            event.app.layout.focus(self.search)

        @bindings.add(":", filter=main_mode)
        def command(event: Any) -> None:
            self.search.buffer.text = ":"
            self._searching = False
            event.app.layout.focus(self.search)

        for key, view in (("g", "guide"), ("o", "overview"), ("e", "entrypoints"), ("a", "architecture"), ("u", "resources"),
                          ("f", "functions"), ("d", "findings"),
                          ("l", "libraries"), ("x", "cycles")):
            bindings.add(key, filter=main_mode)(lambda event, view=view: self._load_view(view))

        @bindings.add("w", filter=main_mode)
        def walk_selected(event: Any) -> None:
            self._walk_selected()

        @bindings.add("m", filter=main_mode)
        def map_selected(event: Any) -> None:
            self._map_selected_or_modules()

        @bindings.add("p", filter=main_mode)
        def open_file_graph(event: Any) -> None:
            self._open_graph("file")

        @bindings.add("t", filter=main_mode)
        def toggle_graph(event: Any) -> None:
            if self.graph_data:
                self._open_graph("code" if self.graph_data["kind"] == "file" else "file")
            else:
                self.status = "Open the File Graph with p first. In a graph, t switches between File and Code Graph."

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

        @bindings.add("c", filter=main_mode)
        def code_or_classes(event: Any) -> None:
            if self.current_view in {"guide", "walk", "file_map", "file_graph", "code_graph"}:
                self._open_selected_source()
            else:
                self._load_view("classes")

        for index in range(1, 10):
            bindings.add(str(index), filter=main_mode)(lambda event, index=index: self._choose_guide_start(index - 1))

        @bindings.add("down", filter=results_focused)
        @bindings.add("j", filter=results_focused)
        def next_row(event: Any) -> None:
            self._move_graph_neighbor(1) if self.graph_data else self._move(1)

        @bindings.add("up", filter=results_focused)
        @bindings.add("k", filter=results_focused)
        def previous_row(event: Any) -> None:
            self._move_graph_neighbor(-1) if self.graph_data else self._move(-1)

        @bindings.add("r", filter=main_mode)
        def rescan(event: Any) -> None:
            self.status = "Scanning project…"
            event.app.invalidate()
            try:
                self.engine.scan(self.project)
                self._load_view("guide")
                self.status = "Scan completed."
            except ThreadError as exc:
                self.status = f"Scan failed: {exc}"

        @bindings.add("enter", filter=not_source)
        def enter(event: Any) -> None:
            if self._search_focused():
                self._submit_search(event)
            else:
                self.status = "Inspector updated for selected result."

        @bindings.add("escape")
        def escape(event: Any) -> None:
            if self.source_mode:
                self._close_fullscreen()
                return
            self.search.buffer.text = ""
            self._searching = False
            event.app.layout.focus(self.results)

        @bindings.add("backspace", filter=main_mode)
        def back(event: Any) -> None:
            self._load_view("guide")

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
            if self._search_focused():
                event.app.layout.focus(self.results)
            elif self._results_focused():
                event.app.layout.focus(self.inspector)
            else:
                event.app.layout.focus(self.results)

        @bindings.add("s-tab", filter=not_source)
        def previous_pane(event: Any) -> None:
            if self._inspector_focused():
                event.app.layout.focus(self.results)
            else:
                event.app.layout.focus(self.search)

        @bindings.add("v", filter=main_mode)
        def full_source(event: Any) -> None:
            self._open_selected_source()

        @bindings.add("z", filter=main_mode)
        def full_analysis(event: Any) -> None:
            self._open_full_analysis()

        @bindings.add("y", filter=main_mode)
        def lesson(event: Any) -> None:
            self._open_lesson()

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
        return bindings

    def _search_focused(self) -> bool:
        return self.app.layout.current_control == self.search.control

    def _results_focused(self) -> bool:
        return self.app.layout.current_control == self.results

    def _inspector_focused(self) -> bool:
        return self.app.layout.current_control == self.inspector.control

    def _choose_guide_start(self, index: int) -> None:
        if self.current_view != "guide":
            self.status = "Number shortcuts choose a starting point in Guided mode. Press g first."
            return
        if index >= len(self.rows):
            self.status = f"There is no starting point {index + 1}."
            return
        self.selected = index
        self._walk_selected()

    def _walk_selected(self) -> None:
        row = self.rows[self.selected] if self.rows else {}
        entry = row.get("entry_point_id") or row.get("name")
        if not entry:
            self.status = "Select a starting point from Guided mode or Entrypoints before opening a walkthrough."
            return
        try:
            walkthrough = self.queries.walk(self.project, str(entry))
        except ThreadError as exc:
            self.status = str(exc)
            return
        self.current_view = "walk"
        self.rows = walkthrough["steps"]
        self.selected = 0
        self.ai_narrative = None
        self._refresh_inspector()
        if walkthrough["steps"]:
            self.status = f"Guided walkthrough: {walkthrough['entry_point']}. Use m for linked files or c for code."
        else:
            self.status = walkthrough.get("stopped_at") or "Thread found no resolved static steps after this starting point."
        self.app.invalidate()

    def _map_selected_or_modules(self) -> None:
        if self.current_view not in {"guide", "walk", "entrypoints", "file_map"}:
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
        self.guide_data = None
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
        self.status = f"{kind.title()} Graph: {len(self.rows)} {noun}. j/k follows links · +/- zoom · [/] confidence · ,/. relationship · t switches graph."
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
        start = row.get("line_start")
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
        lines = [
            f"{lesson['kind'].replace('_', ' ').title()}: {lesson['qualified_name']}\n\n",
            f"Purpose\n{lesson['purpose']}\n\n",
            f"Source\n{lesson['source']['path']}:{lesson['source']['line_start']}-{lesson['source']['line_end']}\n",
        ]
        if lesson.get("signature"):
            lines.extend([f"\nSignature\n{lesson['signature']}\n"])
        if lesson["inputs"]:
            lines.append("\nInputs\n")
            lines.extend(f"• {item['variable_name']} at line {item['line_number']}\n" for item in lesson["inputs"])
        if lesson["outputs"]:
            lines.append("\nOutputs\n")
            lines.extend(f"• {item['event_kind']} {item['expression'] or ''} (line {item['line_number']})\n" for item in lesson["outputs"])
        if lesson["calls"]:
            lines.append("\nCalls\n")
            lines.extend(f"• {item['name']} ({item['resolution']}, line {item['line']})\n" for item in lesson["calls"][:20])
        if lesson["used_by"]:
            lines.append("\nUsed by\n")
            lines.extend(f"• {item['name']} — {item['path']}:{item['line']}\n" for item in lesson["used_by"][:20])
        lines.append("\nPatterns detected\n")
        if lesson["patterns"]:
            lines.extend(f"• {item['name']} [{item['confidence']}]\n  {item['reason']}\n" for item in lesson["patterns"])
        else:
            lines.append("No supported local code pattern was recognised. That is not evidence that the function has no pattern.\n")
        lines.extend([f"\nNote\n{lesson['note']}\n", "\nPress Esc to return to the explorer.\n"])
        self._open_fullscreen("".join(lines), f"Lesson  /  {target}", None, None)

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
        )
        if cursor is not None:
            self.source_editor.buffer.cursor_position = min(cursor, len(content))
        elif line and self.source_path:
            row = max(0, line - 1)
            self.source_editor.buffer.cursor_position = self.source_editor.buffer.document.translate_row_col_to_index(row, 0)
        mode = "EDITING" if self.edit_mode else "read-only"
        header = FormattedTextControl(lambda: [("class:title", f" {self.fullscreen_title}     {mode}")])
        footer = FormattedTextControl(
            lambda: [("class:status", f" {self.status}     F2 edit/lock · F3 full file · Ctrl-S save · Ctrl-R save + scan · F10 discard · Esc return")]
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
        if not self.source_path or not self.source_editor:
            self.status = "Detailed analysis is read-only. Open a source file with v to edit it."
            return
        if not self.edit_mode:
            self.status = "Press F2 before saving source changes."
            return
        try:
            on_disk = self.source_path.read_text(encoding="utf-8")
        except OSError as exc:
            self.status = f"Cannot verify source before saving: {exc}"
            return
        if self.source_file_hash and self._content_hash(on_disk) != self.source_file_hash:
            self.status = "Save refused: this file changed outside Thread after it was opened. Reopen it and merge the edits."
            return
        replacement = self.source_editor.text
        if self.source_is_slice:
            if self.source_slice_start is None or self.source_slice_end is None:
                self.status = "Save refused: Thread lost the selected definition boundary. Reopen the definition."
                return
            original_lines = self.source_file_original.splitlines(keepends=True)
            replacement_lines = replacement.splitlines(keepends=True)
            content = "".join(
                original_lines[: self.source_slice_start - 1]
                + replacement_lines
                + original_lines[self.source_slice_end :]
            )
            replacement_line_count = max(1, len(replacement.splitlines()))
            self.source_slice_end = self.source_slice_start + replacement_line_count - 1
        else:
            content = replacement
        try:
            self.source_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            self.status = f"Save failed: {exc}"
            return
        self.source_original = replacement
        self.source_file_original = content
        self.source_file_hash = self._content_hash(content)
        if not rescan:
            self.status = f"Saved {self.source_path.name}. Scan results are stale; use Ctrl-R or r after returning."
            return
        self.status = "Saved. Scanning project…"
        self.app.invalidate()
        try:
            self.engine.scan(self.project)
            self.status = "Saved and scan completed."
        except ThreadError as exc:
            self.status = f"Saved, but scan failed: {exc}"

    def _discard_source_changes(self) -> None:
        if not self.source_editor or not self.source_path:
            self.status = "Detailed analysis has no editable source changes."
            return
        self.edit_mode = False
        self._set_fullscreen_editor(self.source_original)
        self.status = "Unsaved changes discarded."

    def _close_fullscreen(self) -> None:
        if self.source_path and self.source_editor and self.source_editor.text != self.source_original:
            self.status = "Unsaved edits. Ctrl-S saves them; F10 discards them."
            return
        self.source_mode = False
        self.edit_mode = False
        self.source_path = None
        self.source_editor = None
        self.source_is_slice = False
        self.source_file_hash = None
        self.source_slice_start = None
        self.source_slice_end = None
        self.app.layout = self.main_layout
        self.app.layout.focus(self.results)
        self.status = "Returned to explorer."
        self.app.invalidate()

    def _submit_search(self, event: Any) -> None:
        value = self.search.text.strip()
        if value.startswith(":"):
            self._run_command(value[1:].strip())
        elif value:
            self._searching = True
            self._on_search_changed(None)
            self.status = f"Search results for {value!r}."
        event.app.layout.focus(self.results)

    def _run_command(self, command: str) -> None:
        if command == "ai":
            self._ai_explain()
            return
        actions = {
            "guide": lambda: self._load_view("guide"), "overview": lambda: self._load_view("overview"), "entrypoints": lambda: self._load_view("entrypoints"),
            "architecture": lambda: self._load_view("architecture"), "resources": lambda: self._load_view("resources"),
            "file-graph": lambda: self._open_graph("file"), "code-graph": lambda: self._open_graph("code"),
            "scan": lambda: self.engine.scan(self.project), "quit": lambda: self.app.exit(), "q": lambda: self.app.exit(),
        }
        action = actions.get(command)
        if not action:
            self.status = f"Unknown command: {command or '(empty)'}. Try :guide, :file-graph, :code-graph, :overview, :ai, :scan, or :quit."
            return
        try:
            action()
            if command == "scan":
                self._load_view("guide")
            self.status = f"Executed :{command}."
        except ThreadError as exc:
            self.status = f"Command failed: {exc}"

    def _ai_explain(self) -> None:
        row = self.rows[self.selected] if self.rows else {}
        target = row.get("qualified_name") or row.get("name") or row.get("module_name") or "overview"
        self.status = "Requesting an evidence-only explanation from local Ollama…"
        self.app.invalidate()
        try:
            result = self.queries.ai_explain(self.project, str(target), os.environ.get("THREADTRACE_OLLAMA_MODEL"))
            self.ai_narrative = result["narrative"]
            self._refresh_inspector()
            self.status = f"AI explanation ready for {target}. Verify cited evidence."
        except ThreadError as exc:
            self.status = f"AI explanation unavailable: {exc}"

    def _on_search_changed(self, _: Any) -> None:
        if not self._search_focused() and not self._searching:
            return
        value = self.search.text.strip()
        if not value or value.startswith(":"):
            return
        try:
            self.rows = self.queries.search(self.project, value)
            self.selected = 0
            self.current_view = "search"
        except ThreadError as exc:
            self.rows = []
            self.status = str(exc)
        self._refresh_inspector()
        self.app.invalidate()

    def _load_view(self, view: str) -> None:
        self.current_view = view
        self.graph_data = None
        self._searching = False
        self.search.buffer.text = ""
        try:
            if view == "guide":
                guided = self.queries.guide(self.project)
                self.guide_data = guided
                self.rows = guided["starting_points"]
                self.status = guided["purpose"]
            elif view == "overview":
                self.guide_data = None
                overview = self.queries.overview(self.project)
                self.rows = [dict(item, kind=item["role"], module_name=item["name"]) for item in overview["components"]]
                self.status = overview["purpose"]
            elif view == "entrypoints":
                self.guide_data = None
                self.rows, self.status = self.queries.entrypoints(self.project), "Scripts, commands, routes, workers, and tests."
            elif view == "architecture":
                self.guide_data = None
                self.rows, self.status = self.queries.architecture(self.project)["components"], "Evidence-backed components and relationships."
            elif view == "resources":
                self.guide_data = None
                self.rows, self.status = self.queries.resources(self.project), "External resource boundaries."
            elif view == "modules":
                self.guide_data = None
                self.rows, self.status = self.queries.modules(self.project), "Modules in the latest completed scan."
            elif view == "functions":
                self.guide_data = None
                self.rows, self.status = self.queries.functions(self.project), "Functions and methods."
            elif view == "classes":
                self.guide_data = None
                self.rows, self.status = self.queries.classes(self.project), "Classes, declared bases, methods, and attributes."
            elif view == "diagnostics":
                self.guide_data = None
                self.rows, self.status = self.queries.diagnostics(self.project, include_unresolved_calls=True), "Findings and dynamic boundaries."
            elif view == "findings":
                self.guide_data = None
                self.rows, self.status = self.queries.findings(self.project), "Needs attention. Technical call-resolution details are hidden here."
            elif view == "libraries":
                self.guide_data = None
                self.rows, self.status = self.queries.libraries(self.project), "Imported and declared libraries."
            else:
                self.guide_data = None
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
        return [("class:title", f" Thread  /  {self.project_row['name']}"), ("", f"     {self.current_view.title()}")]

    def _navigation_text(self) -> StyleAndTextTuples:
        return [
            ("class:title", "Start here\n"), ("", " [g] Guided tour\n [e] Entrypoints\n [a] Architecture\n [u] Resources\n\n"),
            ("class:title", "Browse\n"), ("", " [m] Modules\n [f] Functions\n [c] Classes\n [p] File graph\n [d] Findings\n [l] Libraries\n [x] Cycles\n\n"),
            ("class:muted", " Guided: 1-9 walk · w walk · m files · c code · y lesson\n Graph: p file · t switch · j/k follow · +/- zoom · [/] confidence · ,/. type\n / Search\n : Commands\n r Rescan\n ? Help\n q Quit"),
        ]

    def _results_text(self) -> StyleAndTextTuples:
        if self.graph_data:
            return self._graph_text()
        if not self.rows:
            return [("class:muted", "\n  No results.")]
        output: StyleAndTextTuples = []
        for index, row in enumerate(self.rows):
            name = str(row.get("qualified_name") or row.get("name") or row.get("module_name") or row.get("rule_id") or "—")
            kind = str(row.get("kind") or row.get("role") or row.get("severity") or row.get("category") or "—")
            text = f" {name[:58]:58}  {kind[:16]:16}  {self._location(row)[:24]}\n"
            output.append(("class:selected" if index == self.selected else "", text))
        return output

    def _graph_text(self) -> StyleAndTextTuples:
        """Render an inspectable local graph; selected nodes keep a readable scope."""
        if not self.graph_data or not self.rows:
            return [("class:muted", "\n  No graph nodes are available.")]
        current = self.rows[self.selected]["id"]
        names = {node["id"]: str(node["name"]) for node in self.rows}
        edges = self._graph_edges()
        adjacency: dict[str, list[tuple[dict[str, Any], str, str]]] = defaultdict(list)
        for edge in edges:
            adjacency[edge["source"]].append((edge, edge["target"], "→"))
            adjacency[edge["target"]].append((edge, edge["source"], "←"))
        title = "FILE" if self.graph_data["kind"] == "file" else "CODE"
        lines: StyleAndTextTuples = [
            ("class:title", f"\n {title} RELATIONSHIP GRAPH\n"),
            ("class:muted", f" Focused view: {self.graph_depth} hop(s) · confidence through {self.graph_confidence} · {self.graph_kind_filter or 'all links'}\n"),
            ("class:muted", " j/k or up/down follow a link · +/- zoom · [/] confidence · ,/. type · t switch graph · c code\n\n"),
        ]
        shown = 0

        def render_node(node_id: str, level: int, prefix: str, ancestry: set[str]) -> None:
            nonlocal shown
            if shown >= 18:
                return
            label = names.get(node_id, node_id)
            if level == 0:
                lines.append(("class:selected", f" ● [{label}]\n"))
            else:
                lines.append(("class:title" if node_id == current else "", f"{prefix}[{label}]\n"))
            shown += 1
            if level >= self.graph_depth:
                return
            related = sorted(adjacency.get(node_id, []), key=lambda item: (item[0]["kind"], names.get(item[1], item[1])))
            for index, (edge, other, direction) in enumerate(related[:8]):
                branch = "└─" if index == min(len(related), 8) - 1 else "├─"
                relation = edge["kind"].replace("_", " ")
                label_hint = edge["labels"][0] if edge.get("labels") else relation
                lines.append(("class:good" if edge["confidence"] == "confirmed" else "class:warning", f"{prefix}{branch} {direction} {relation} [{edge['confidence']}]  {label_hint[:72]}\n"))
                if other in ancestry:
                    lines.append(("class:muted", f"{prefix}   ↳ [{names.get(other, other)}] (already shown)\n"))
                else:
                    render_node(other, level + 1, prefix + ("   " if branch == "└─" else "│  "), ancestry | {node_id})

        render_node(current, 0, " ", set())
        if len(adjacency.get(current, [])) > 8 or shown >= 18:
            lines.append(("class:muted", "\n Graph is intentionally scoped. Follow a link with j/k or move focus to reveal more.\n"))
        return lines

    def _detail_text(self) -> StyleAndTextTuples:
        guide_intro = self._guide_intro()
        if not self.rows:
            return guide_intro + [("class:muted", "\nNo supported starting point was detected. Try [e] Entrypoints or inspect the project configuration.\n")]
        row = self.rows[self.selected]
        name = str(row.get("qualified_name") or row.get("name") or row.get("module_name") or "Selected result")
        lines: StyleAndTextTuples = guide_intro + [("class:title", f"\n{name}\n"), ("class:muted", f"{row.get('kind') or row.get('role') or 'item'}\n\n")]
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
                    lines.append(("", f"{step.get('source_name', 'entry')} → {step.get('target_name', '?')} [{step['confidence']}]\n"))
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

    def _guide_intro(self) -> StyleAndTextTuples:
        if self.current_view != "guide" or not self.guide_data:
            return []
        lines: StyleAndTextTuples = [
            ("class:title", "\nWelcome — start here\n"),
            ("", f"{self.guide_data['purpose']}\n\n"),
            ("class:good", "Choose a starting point\n"),
            ("", "Press 1–9 or select an item and press w to follow one supported path.\n"),
        ]
        findings = self.guide_data.get("needs_attention", [])
        if findings:
            lines.append(("class:warning", "\nNeeds attention\n"))
            for finding in findings[:3]:
                lines.append(("", f"• {finding['plain_title']}: {finding['plain_explanation']}\n"))
        else:
            lines.extend([("class:good", "\nAnalysis health\n"), ("", "No important analysis gaps were detected.\n")])
        return lines

    def _status_text(self) -> StyleAndTextTuples:
        return [("", f" {self.status}")]

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
