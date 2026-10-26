"""Read-only live runtime listener for a process hosted in another terminal."""

from __future__ import annotations

import bisect
import json
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
from .runtime.host import live_sessions


def _value(summary: Any) -> str:
    """Display only the safe summaries provided by the trace agent."""
    if not isinstance(summary, dict):
        return "<unavailable>"
    kind = str(summary.get("type", "unknown"))
    if "value" in summary:
        value = summary["value"]
        if value in ("<redacted>", "<object>"):
            return f"{value} ({kind})"
        return f"{value!r} ({kind})"
    if "preview" in summary:
        preview = summary["preview"]
        suffix = "…" if summary.get("length", 0) > len(preview) else ""
        return f"{preview!r}{suffix} (str, length {summary.get('length', '?')})"
    if "length" in summary:
        keys = summary.get("keys")
        suffix = f", keys: {', '.join(map(str, keys))}" if isinstance(keys, list) and keys else ""
        return f"<{kind}, length {summary['length']}{suffix}>"
    return f"<{kind}>"


class LiveRuntimeApp:
    """Follow a RuntimeHost session without controlling the interactive target."""

    def __init__(self, engine: Engine, project: str, session_id: str | None = None) -> None:
        self.engine = engine
        self.project_row = engine.get_project(project)
        self.project = project
        self.root = Path(self.project_row["root_path"]).resolve()
        sessions = live_sessions(self.root, session_id)
        if not sessions:
            raise ThreadError("No Runtime Host session exists for this project. Start one with: threadtrace runtime-host PROJECT -- COMMAND")
        self.metadata = sessions[0]
        self.log_path = Path(self.metadata["log_path"])
        self.offset = 0
        self.events: list[dict[str, Any]] = []
        self.frame_events: dict[str, list[int]] = {}
        self.frame_values: dict[str, dict[str, Any]] = {}
        self.selected = 0
        self.following = True
        self.status = "Waiting for project events."
        self.events_control = FormattedTextControl(self._events_text, focusable=True, get_cursor_position=self._cursor)
        self.inspector = TextArea(text="", read_only=True, focusable=True, focus_on_click=True, wrap_lines=True, scrollbar=True)
        root = HSplit([
            Window(FormattedTextControl(self._header_text), height=1, style="class:header"),
            VSplit([
                Frame(Window(self.events_control, always_hide_cursor=True, right_margins=[ScrollbarMargin(display_arrows=True)]), title="Events", width=Dimension(weight=3)),
                Frame(self.inspector, title="Event details / variables / source", width=Dimension(weight=4)),
            ]),
            Window(FormattedTextControl(self._status_text), height=1, style="class:status"),
        ])
        self.app = Application(layout=Layout(root, focused_element=self.events_control), key_bindings=self._keys(), full_screen=True,
                               mouse_support=True, refresh_interval=0.15, before_render=self._tick, style=self._style())
        self._refresh_inspector()

    @staticmethod
    def _style() -> Any:
        from prompt_toolkit.styles import Style
        return Style.from_dict({"header": "bg:#1c3144 #eaf6ff bold", "status": "bg:#244d68 #eaf6ff", "frame.border": "#6098b8", "frame.label": "#a9e2ff bold", "selected": "bg:#327397 #ffffff bold", "call": "#a9ddff", "return": "#a8e6ae", "line": "#e4e4e4", "exception": "#ffb3b3"})

    def run(self) -> None:
        self.app.run()

    def _select_event(self, index: int) -> None:
        if self.events:
            self.selected = max(0, min(len(self.events) - 1, index))
            self.following = False
            self._refresh_inspector()

    def _follow_live(self) -> None:
        self.following = True
        if self.events:
            self.selected = len(self.events) - 1
            self._refresh_inspector()

    def _keys(self) -> KeyBindings:
        keys = KeyBindings()
        timeline_focused = Condition(lambda: self.app.layout.current_control == self.events_control)

        @keys.add("down", filter=timeline_focused)
        @keys.add("j", filter=timeline_focused)
        def next_event(event: Any) -> None:
            self._select_event(self.selected + 1)

        @keys.add("up", filter=timeline_focused)
        @keys.add("k", filter=timeline_focused)
        def previous_event(event: Any) -> None:
            self._select_event(self.selected - 1)

        @keys.add("l", filter=timeline_focused)
        def follow(event: Any) -> None:
            self._follow_live()

        @keys.add("tab")
        def tab(event: Any) -> None:
            event.app.layout.focus(self.inspector if event.app.layout.current_control == self.events_control else self.events_control)

        @keys.add("q", filter=timeline_focused)
        @keys.add("c-c")
        def quit_app(event: Any) -> None:
            event.app.exit()
        return keys

    @staticmethod
    def _apply_changes(values: dict[str, Any], item: dict[str, Any]) -> None:
        changes = item.get("variables")
        if not isinstance(changes, dict):
            return
        for key in ("created", "changed"):
            updates = changes.get(key)
            if isinstance(updates, dict):
                values.update(updates)
        for name in changes.get("removed", []):
            values.pop(name, None)

    def _tick(self, _application: Any | None = None) -> None:
        self._refresh_metadata()
        previous_count = len(self.events)
        try:
            with self.log_path.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self.offset)
                while True:
                    start = handle.tell()
                    line = handle.readline()
                    if not line:
                        break
                    if not line.endswith("\n"):
                        handle.seek(start)
                        break
                    self.offset = handle.tell()
                    try:
                        item = json.loads(line)
                        path = Path(item["path"]).resolve()
                        if not path.is_relative_to(self.root):
                            continue
                        item["relative_path"] = path.relative_to(self.root).as_posix()
                        index = len(self.events)
                        self.events.append(item)
                        frame_id = item.get("frame_id")
                        if isinstance(frame_id, str):
                            self.frame_events.setdefault(frame_id, []).append(index)
                            values = self.frame_values.setdefault(frame_id, {})
                            self._apply_changes(values, item)
                    except (OSError, ValueError, KeyError, TypeError):
                        continue
        except OSError:
            return
        if self.following and len(self.events) > previous_count:
            self.selected = len(self.events) - 1
            self._refresh_inspector()

    def _refresh_metadata(self) -> None:
        try:
            sessions = live_sessions(self.root, self.metadata["session_id"])
            if sessions:
                self.metadata = sessions[0]
        except (ThreadError, OSError):
            pass

    def _header_text(self) -> StyleAndTextTuples:
        return [("class:title", f" Thread Live Runtime / {self.project_row['name']} / {self.metadata['status']}"),
                ("", "   j/k events · l live · Tab details · q quit")]

    def _status_text(self) -> StyleAndTextTuples:
        mode = "LIVE" if self.following else "HISTORY (l to follow)"
        return [("", f" {mode} · {len(self.events)} events · passive listener · values are safe summaries")]

    def _events_text(self) -> StyleAndTextTuples:
        if not self.events:
            return [("", "\n  Waiting for project Python events…\n")]
        lines: StyleAndTextTuples = []
        for index, item in enumerate(self.events):
            lane = item.get("task_name") or f"thread-{item.get('thread_id', '?')}"
            text = f" {index + 1:>5}  {item.get('kind', 'event'):9}  {str(item.get('name', '?'))[:24]:24}  {item.get('relative_path', '?')}:{item.get('line', '?')}  [{lane}]\n"
            lines.append(("class:selected" if index == self.selected else f"class:{item.get('kind', 'line')}", text))
        return lines

    def _cursor(self) -> Point:
        return Point(x=0, y=self.selected)

    def _variables_at(self, item: dict[str, Any]) -> dict[str, Any] | None:
        frame_id = item.get("frame_id")
        if not isinstance(frame_id, str):
            return None
        indices = self.frame_events.get(frame_id, [])
        position = bisect.bisect_right(indices, self.selected)
        if position == len(indices):
            return self.frame_values.get(frame_id, {}).copy()
        values: dict[str, Any] = {}
        for index in indices[:position]:
            self._apply_changes(values, self.events[index])
        return values

    def _source_excerpt(self, item: dict[str, Any]) -> str:
        path, line = item.get("relative_path"), item.get("line")
        if not isinstance(path, str) or not isinstance(line, int) or line < 1:
            return "Source unavailable."
        try:
            source = (self.root / path).resolve()
            if not source.is_relative_to(self.root):
                return "Source unavailable."
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "Source unavailable."
        if line > len(lines):
            return "Source line unavailable (the file may have changed)."
        return "\n".join(f"{'→' if index + 1 == line else ' '} {index + 1:>4}  {lines[index]}"
                         for index in range(max(0, line - 3), min(len(lines), line + 2)))

    def _refresh_inspector(self) -> None:
        if not self.events:
            self.inspector.text = "Waiting for events. Use the program in the host terminal; this listener never controls it."
            return
        item = self.events[self.selected]
        lane = item.get("task_name") or f"thread-{item.get('thread_id', '?')}"
        lines = [f"Event {self.selected + 1} / {len(self.events)}: {item.get('kind', 'event')}\n",
                 f"Function: {item.get('name', '?')}\n",
                 f"Location: {item.get('relative_path', '?')}:{item.get('line', '?')}\n",
                 f"Lane: {lane} · depth {item.get('call_depth', '?')}\n",
                 "\nSource context (line events are before execution)\n",
                 self._source_excerpt(item), "\n"]
        changes = item.get("variables")
        if isinstance(changes, dict):
            for key, label in (("created", "Created"), ("changed", "Changed")):
                entries = changes.get(key)
                if isinstance(entries, dict) and entries:
                    lines.append(f"\n{label}\n")
                    lines.extend(f"  {name} = {_value(value)}\n" for name, value in entries.items())
            removed = changes.get("removed")
            if isinstance(removed, list) and removed:
                lines.append("\nRemoved\n")
                lines.extend(f"  {name}\n" for name in removed)
        if item.get("kind") == "call" and isinstance(item.get("inputs"), dict) and item["inputs"]:
            lines.append("\nInputs / locals at call\n")
            lines.extend(f"  {name} = {_value(value)}\n" for name, value in item["inputs"].items())
        if item.get("kind") == "return" and "output" in item:
            lines.append(f"\nReturned: {_value(item['output'])}\n")
        if item.get("exception"):
            lines.append(f"\nException: {item['exception']}\n")
        values = self._variables_at(item)
        lines.append("\nVariables in this frame at this event\n")
        if values is None:
            lines.append("  Unavailable for older sessions without frame IDs.\n")
        elif not values:
            lines.append("  No recorded variables.\n")
        else:
            lines.extend(f"  {name} = {_value(value)}\n" for name, value in sorted(values.items()))
        self.inspector.text = "".join(lines)
        self.inspector.buffer.cursor_position = 0
