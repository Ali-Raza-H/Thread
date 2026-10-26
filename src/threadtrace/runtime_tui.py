"""Interactive, deliberately opt-in dashboard for one paused runtime session."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.widgets import Frame, TextArea

from .engine import Engine, ThreadError
from .runtime.session import RuntimeSession


class RuntimeApp:
    """A slow, source-focused viewer for a command the user elected to run."""

    def __init__(self, engine: Engine, project: str, command: list[str], timeout: int, output_cap: int,
                 runtime_environment: dict[str, str] | None = None) -> None:
        self.engine = engine
        self.project_row = engine.get_project(project)
        self.project = project
        self.session = RuntimeSession(Path(self.project_row["root_path"]), command, timeout, output_cap, runtime_environment=runtime_environment)
        self.events: list[dict[str, Any]] = []
        self.selected = 0
        self.status = "Ready. The project has not started. Press s to start paused, n to step, or c to run."
        self.recorded = False
        self.events_control = FormattedTextControl(self._events_text, focusable=True, get_cursor_position=self._cursor)
        self.inspector = TextArea(text="", read_only=True, focusable=True, focus_on_click=True, wrap_lines=True, scrollbar=True)
        self.events_window = Window(self.events_control, always_hide_cursor=True, right_margins=[ScrollbarMargin(display_arrows=True)])
        root = HSplit([
            Window(FormattedTextControl(self._header_text), height=1, style="class:header"),
            VSplit([
                Frame(self.events_window, title="Runtime timeline", width=Dimension(weight=3)),
                Frame(self.inspector, title="Active step / data", width=Dimension(weight=4)),
            ]),
            Window(FormattedTextControl(self._status_text), height=1, style="class:status"),
        ])
        self.app = Application(
            layout=Layout(root, focused_element=self.events_control), key_bindings=self._keys(), full_screen=True,
            mouse_support=True, refresh_interval=0.12, before_render=self._tick,
            style=self._style(),
        )
        self._refresh_inspector()

    @staticmethod
    def _style() -> Any:
        from prompt_toolkit.styles import Style
        return Style.from_dict({
            "header": "bg:#3b1f1f #fff1e6 bold", "status": "bg:#4b2c20 #fff1e6",
            "frame.border": "#9b7059", "frame.label": "#ffd2a8 bold", "selected": "bg:#875035 #ffffff bold",
            "call": "#9fd8ff", "return": "#9ee6a8", "line": "#d9d9d9", "exception": "#ffadad",
        })

    def run(self) -> None:
        try:
            self.app.run()
        finally:
            self._record_completed()
            self.session.close()

    def _keys(self) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("s")
        def start(event: Any) -> None:
            try:
                self.session.start()
                self.status = "Process started and will pause at its first project execution event. Press n to step."
            except (OSError, ValueError) as exc:
                self.status = f"Could not start runtime session: {exc}"

        @keys.add("n")
        @keys.add("enter")
        def step(event: Any) -> None:
            try:
                self.session.step()
                self.status = "Stepping one project execution event…"
            except (OSError, ValueError) as exc:
                self.status = f"Could not step runtime session: {exc}"

        @keys.add("o")
        def step_over(event: Any) -> None:
            self.session.step_over()
            self.status = "Stepping over nested calls…"

        @keys.add("u")
        def step_out(event: Any) -> None:
            self.session.step_out()
            self.status = "Running until the current frame returns…"

        @keys.add("c")
        def continue_run(event: Any) -> None:
            try:
                self.session.continue_run()
                self.status = "Running until completion, pause, timeout, or the event limit."
            except (OSError, ValueError) as exc:
                self.status = f"Could not continue runtime session: {exc}"

        @keys.add("p")
        def pause(event: Any) -> None:
            self.session.pause()
            self.status = "Pause requested; the process will stop at its next traced project event."

        @keys.add("b")
        def breakpoint(event: Any) -> None:
            if not self.events:
                self.status = "Run to a source event first, then press b to toggle a breakpoint on its line."
                return
            item = self.events[self.selected]
            path, line = item.get("path"), item.get("line")
            if not isinstance(path, str) or not isinstance(line, int):
                self.status = "This event has no usable source location."
                return
            try:
                enabled = self.session.toggle_breakpoint(path, line)
                relative = item.get("relative_path", path)
                self.status = f"Breakpoint {'set' if enabled else 'cleared'} at {relative}:{line}."
            except ValueError as exc:
                self.status = str(exc)

        @keys.add("f")
        def focus_task(event: Any) -> None:
            if not self.events:
                self.status = "Run to an asyncio task event first, then press f to focus that task."
                return
            item = self.events[self.selected]
            task_id = item.get("task_id")
            if not isinstance(task_id, str):
                self.session.focus_task(None)
                self.status = "All lanes are visible. This event is not owned by an asyncio task."
                return
            if task_id == self.session.focused_task_id:
                self.session.focus_task(None)
                self.status = "Task focus cleared; all threads and tasks are visible."
            else:
                self.session.focus_task(task_id)
                self.status = f"Focused {item.get('task_name') or task_id}; background task events are hidden but still run."

        @keys.add("down")
        @keys.add("j")
        def next_event(event: Any) -> None:
            if self.events:
                self.selected = min(len(self.events) - 1, self.selected + 1)
                self._refresh_inspector()

        @keys.add("up")
        @keys.add("k")
        def previous_event(event: Any) -> None:
            if self.events:
                self.selected = max(0, self.selected - 1)
                self._refresh_inspector()

        @keys.add("tab")
        def tab(event: Any) -> None:
            event.app.layout.focus(self.inspector if event.app.layout.current_control == self.events_control else self.events_control)

        @keys.add("q")
        @keys.add("c-c")
        def quit_app(event: Any) -> None:
            if self.session.started and not self.session.finished:
                self.session.stop()
                self.status = "Stopping the runtime subprocess…"
            event.app.exit()
        return keys

    def _tick(self, _application: Any | None = None) -> None:
        fresh = self.session.poll()
        if fresh:
            self.events.extend(fresh)
            self.selected = len(self.events) - 1
            self._refresh_inspector()
        if self.session.finished:
            self._record_completed()
            result = self.session.result()
            state = "timed out" if result["timed_out"] else f"exited with code {result['exit_code']}"
            self.status = f"Runtime process {state}. {len(self.events)} event(s) captured. Press q to close."

    def _record_completed(self) -> None:
        if self.recorded or not self.session.finished:
            return
        try:
            self.engine.record_runtime(self.project, self.session.result())
            self.recorded = True
        except ThreadError as exc:
            self.status = f"Runtime ended, but Thread could not store the session: {exc}"

    def _header_text(self) -> StyleAndTextTuples:
        state = "not started" if not self.session.started else "finished" if self.session.finished else "paused / running"
        points = len(self.session.breakpoints)
        focus = "all lanes" if not self.session.focused_task_id else "focused task"
        return [("class:title", f" Thread Runtime  /  {self.project_row['name']}  /  {state}"), ("", f"     {focus} · s start · n into · o over · u out · c continue · p pause · b breakpoint ({points}) · f task focus · j/k inspect · q stop/quit")]

    def _status_text(self) -> StyleAndTextTuples:
        return [("", f" {self.status}")]

    def _events_text(self) -> StyleAndTextTuples:
        if not self.events:
            return [("", "\n  Waiting for the explicit Start command.\n\n  This dashboard executes the supplied command only after you press s or n.\n")]
        lines: StyleAndTextTuples = []
        for index, item in enumerate(self.events):
            kind = item.get("kind", "event")
            location = f"{item.get('relative_path', '?')}:{item.get('line', '?')}"
            lane = item.get("task_name") or f"thread-{item.get('thread_id', '?')}"
            text = f" {index + 1:>4}  {lane[:14]:14}  {kind:9}  {str(item.get('name', '?'))[:24]:24}  {location}\n"
            lines.append(("class:selected" if index == self.selected else f"class:{kind}", text))
        return lines

    def _cursor(self) -> Point:
        return Point(x=0, y=self.selected)

    def _refresh_inspector(self) -> None:
        if not self.events:
            self.inspector.text = (
                "Runtime session is ready but has not executed the target project.\n\n"
                "Press s to launch it paused at its first project event. Press n to let one source event run at a time, "
                "or c to continue. The dashboard only observes files inside the registered project root.\n\n"
                "Values are summarised safely: sensitive variable names are redacted, strings are capped, and arbitrary objects are not repr()'d."
            )
            return
        item = self.events[self.selected]
        lane = item.get("task_name") or f"thread-{item.get('thread_id', '?')}"
        lines = [
            f"Event {self.selected + 1}: {item.get('kind')}\n",
            f"Function: {item.get('name')}\n",
            f"Source: {item.get('relative_path')}:{item.get('line')}\n",
            f"Lane: {lane}  /  depth {item.get('call_depth', '?')}\n",
        ]
        for label in ("inputs", "variables", "output", "exception"):
            if label in item and item[label] not in ({}, None):
                lines.extend([f"\n{label.title()}\n", json.dumps(item[label], indent=2, sort_keys=True), "\n"])
        source = self._source_excerpt(item)
        if source:
            lines.extend(["\nSource context\n", source])
        result = self.session.result() if self.session.finished else None
        if result and (result["stdout"] or result["stderr"]):
            lines.extend(["\nProcess output\n", result["stdout"][-2_000:], result["stderr"][-2_000:]])
        self.inspector.text = "".join(lines)
        self.inspector.buffer.cursor_position = 0

    def _source_excerpt(self, item: dict[str, Any]) -> str:
        relative, line = item.get("relative_path"), item.get("line")
        if not relative or not isinstance(line, int):
            return ""
        path = Path(self.project_row["root_path"]) / relative
        try:
            source = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""
        first, last = max(0, line - 3), min(len(source), line + 2)
        return "\n".join(f"{index + 1:>4}  {source[index]}" for index in range(first, last)) + "\n"
