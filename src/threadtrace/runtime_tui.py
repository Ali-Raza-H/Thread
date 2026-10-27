"""Interactive, deliberately opt-in dashboard for one paused runtime session."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import has_focus
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.widgets import Frame, TextArea

from .engine import Engine, ThreadError
from .runtime.session import RuntimeSession


def _value(summary: Any) -> str:
    """Render a safe runtime value summary for the inspector."""
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


class RuntimeApp:
    """A slow, source-focused viewer for a command the user elected to run."""

    def __init__(self, engine: Engine, project: str, command: list[str], timeout: int, output_cap: int,
                 runtime_environment: dict[str, str] | None = None) -> None:
        self.engine = engine
        self.project_row = engine.get_project(project)
        self.project = project
        self.session = RuntimeSession(Path(self.project_row["root_path"]), command, timeout, output_cap, runtime_environment=runtime_environment)
        self.events: list[dict[str, Any]] = []
        self.frame_events: dict[str, list[int]] = {}
        self.frame_values: dict[str, dict[str, Any]] = {}
        self.selected = 0
        self.status = "Ready. The project has not started. Press s to start paused, n to step, or c to run."
        self.recorded = False
        self._output_size = (0, 0)
        self.events_control = FormattedTextControl(self._events_text, focusable=True, get_cursor_position=self._cursor)
        self.inspector = TextArea(text="", read_only=True, focusable=True, focus_on_click=True, wrap_lines=True, scrollbar=True)
        self.program_input = TextArea(
            text="", multiline=False, focusable=True, focus_on_click=True,
            prompt="input> ",
        )
        self.events_window = Window(self.events_control, always_hide_cursor=True, right_margins=[ScrollbarMargin(display_arrows=True)])
        root = HSplit([
            Window(FormattedTextControl(self._header_text), height=1, style="class:header"),
            VSplit([
                Frame(self.events_window, title="Runtime timeline", width=Dimension(weight=3)),
                Frame(self.inspector, title="Event details / variables / source", width=Dimension(weight=4)),
            ]),
            Frame(self.program_input, title="Program input · i focus · Enter send · Esc cancel", height=3),
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
        controls = ~has_focus(self.program_input)

        @keys.add("s", filter=controls)
        def start(event: Any) -> None:
            try:
                self.session.start()
                self.status = "Process started and will pause at its first project execution event. Press n to step."
            except (OSError, ValueError) as exc:
                self.status = f"Could not start runtime session: {exc}"

        @keys.add("n", filter=controls)
        @keys.add("enter", filter=controls)
        def step(event: Any) -> None:
            try:
                self.session.step()
                self.status = "Stepping one project execution event…"
            except (OSError, ValueError) as exc:
                self.status = f"Could not step runtime session: {exc}"

        @keys.add("o", filter=controls)
        def step_over(event: Any) -> None:
            self.session.step_over()
            self.status = "Stepping over nested calls…"

        @keys.add("u", filter=controls)
        def step_out(event: Any) -> None:
            self.session.step_out()
            self.status = "Running until the current frame returns…"

        @keys.add("c", filter=controls)
        def continue_run(event: Any) -> None:
            try:
                self.session.continue_run()
                self.status = "Running until completion, pause, timeout, or the event limit."
            except (OSError, ValueError) as exc:
                self.status = f"Could not continue runtime session: {exc}"

        @keys.add("p", filter=controls)
        def pause(event: Any) -> None:
            self.session.pause()
            self.status = "Pause requested; the process will stop at its next traced project event."

        @keys.add("b", filter=controls)
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

        @keys.add("f", filter=controls)
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

        @keys.add("down", filter=controls)
        @keys.add("j", filter=controls)
        def next_event(event: Any) -> None:
            if self.events:
                self.selected = min(len(self.events) - 1, self.selected + 1)
                self._refresh_inspector()

        @keys.add("up", filter=controls)
        @keys.add("k", filter=controls)
        def previous_event(event: Any) -> None:
            if self.events:
                self.selected = max(0, self.selected - 1)
                self._refresh_inspector()

        @keys.add("tab", filter=controls)
        def tab(event: Any) -> None:
            event.app.layout.focus(self.inspector if event.app.layout.current_control == self.events_control else self.events_control)

        @keys.add("i", filter=controls)
        def focus_program_input(event: Any) -> None:
            if not self.session.started or self.session.finished:
                self.status = "Start the runtime process before sending program input."
                return
            event.app.layout.focus(self.program_input)
            self.status = "Type one line for the program, then press Enter to send it."

        @keys.add("enter", filter=has_focus(self.program_input))
        def send_program_input(event: Any) -> None:
            value = self.program_input.text
            if self.session.send_input(value):
                self.program_input.text = ""
                event.app.layout.focus(self.events_control)
                self.status = "Sent one input line to the runtime process."
            else:
                self.status = "Could not send input: the runtime process is not running."

        @keys.add("escape", filter=has_focus(self.program_input))
        def cancel_program_input(event: Any) -> None:
            self.program_input.text = ""
            event.app.layout.focus(self.events_control)
            self.status = "Program input cancelled."

        @keys.add("q", filter=controls)
        @keys.add("c-c", filter=controls)
        def quit_app(event: Any) -> None:
            if self.session.started and not self.session.finished:
                self.session.stop()
                self.status = "Stopping the runtime subprocess…"
            event.app.exit()
        return keys

    def _tick(self, _application: Any | None = None) -> None:
        fresh = self.session.poll()
        if fresh:
            for item in fresh:
                index = len(self.events)
                self.events.append(item)
                frame_id = item.get("frame_id")
                if isinstance(frame_id, str):
                    self.frame_events.setdefault(frame_id, []).append(index)
                    values = self.frame_values.setdefault(frame_id, {})
                    self._apply_changes(values, item)
            self.selected = len(self.events) - 1
            self._refresh_inspector()
        result = self.session.result() if self.session.started else None
        if result:
            output_size = (len(result["stdout"]), len(result["stderr"]))
            if output_size != self._output_size:
                self._output_size = output_size
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
        return [("class:title", f" Thread Runtime  /  {self.project_row['name']}  /  {state}"), ("", f"     {focus} · s start · n into · o over · u out · c continue · p pause · i program input · b breakpoint ({points}) · f task focus · j/k inspect · q stop/quit")]

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

    def _variables_at(self, item: dict[str, Any]) -> dict[str, Any] | None:
        frame_id = item.get("frame_id")
        if not isinstance(frame_id, str):
            return None
        indices = self.frame_events.get(frame_id, [])
        try:
            position = indices.index(self.selected) + 1
        except ValueError:
            return None
        if position == len(indices):
            return self.frame_values.get(frame_id, {}).copy()
        values: dict[str, Any] = {}
        for index in indices[:position]:
            self._apply_changes(values, self.events[index])
        return values

    def _refresh_inspector(self) -> None:
        if not self.events:
            self.inspector.text = (
                "Runtime session is ready but has not executed the target project.\n\n"
                "Press s to launch it paused at its first project event. Press n to let one source event run at a time, "
                "or c to continue. The dashboard observes application files inside the registered project root; nested "
                "Python virtual environments are excluded.\n\n"
                "Values are summarised safely: sensitive variable names are redacted, strings are capped, and arbitrary objects are not repr()'d."
            )
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
        result = self.session.result() if self.session.started else None
        if result and (result["stdout"] or result["stderr"]):
            lines.append("\nProcess output\n")
            if result["stdout"]:
                lines.extend(["stdout\n", result["stdout"][-2_000:], "\n"])
            if result["stderr"]:
                lines.extend(["stderr\n", result["stderr"][-2_000:], "\n"])
        self.inspector.text = "".join(lines)
        self.inspector.buffer.cursor_position = 0

    def _source_excerpt(self, item: dict[str, Any]) -> str:
        relative, line = item.get("relative_path"), item.get("line")
        if not isinstance(relative, str) or not isinstance(line, int) or line < 1:
            return "Source unavailable."
        root = Path(self.project_row["root_path"]).resolve()
        path = (root / relative).resolve()
        try:
            if not path.is_relative_to(root):
                return "Source unavailable."
            source = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "Source unavailable."
        if line > len(source):
            return "Source line unavailable (the file may have changed)."
        return "\n".join(
            f"{'→' if index + 1 == line else ' '} {index + 1:>4}  {source[index]}"
            for index in range(max(0, line - 3), min(len(source), line + 2))
        )
