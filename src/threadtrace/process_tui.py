"""Confirmation-first selector for currently running local Python processes."""

from __future__ import annotations

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
from .processes import process_tree, running_python_processes, stop_process_tree


class ProcessApp:
    """Select a process, inspect what Thread inferred, then choose a safe path."""

    def __init__(self, engine: Engine, pid: int | None = None, timeout: int = 30, output_cap: int = 100_000) -> None:
        self.engine = engine
        self.timeout = timeout
        self.output_cap = output_cap
        self.processes = running_python_processes()
        self.selected = next((index for index, item in enumerate(self.processes) if item["pid"] == pid), 0)
        self.confirming = False
        self.status = "Select one of your running Python processes, then press Enter to review it."
        self.list_control = FormattedTextControl(self._list_text, focusable=True, get_cursor_position=self._cursor)
        self.inspector = TextArea(text="", read_only=True, focusable=True, focus_on_click=True, wrap_lines=True, scrollbar=True)
        root = HSplit([
            Window(FormattedTextControl(self._header_text), height=1, style="class:header"),
            VSplit([
                Frame(Window(self.list_control, always_hide_cursor=True, right_margins=[ScrollbarMargin(display_arrows=True)]), title="Your running Python processes", width=Dimension(weight=3)),
                Frame(self.inspector, title="Process review / confirmation", width=Dimension(weight=4)),
            ]),
            Window(FormattedTextControl(self._status_text), height=1, style="class:status"),
        ])
        self.app = Application(layout=Layout(root, focused_element=self.list_control), key_bindings=self._keys(), full_screen=True, mouse_support=True, style=self._style())
        self._refresh_inspector()

    @staticmethod
    def _style() -> Any:
        from prompt_toolkit.styles import Style
        return Style.from_dict({
            "header": "bg:#163442 #e8fbff bold", "status": "bg:#1d4a5c #e8fbff", "frame.border": "#5b98aa",
            "frame.label": "#a5e7f5 bold", "selected": "bg:#2a7189 #ffffff bold", "warning": "#ffd08a", "good": "#9de1b2",
        })

    def run(self) -> None:
        action = self.app.run()
        if not action:
            return
        kind, selected, project = action
        if kind == "scan":
            from .query import QueryService
            from .tui import ThreadApp
            ThreadApp(self.engine, QueryService(self.engine), project["name"]).run()
            return
        # The confirmation screen is the only route here.  Scan before stopping
        # so the source snapshot is available even if restart fails.
        stop_process_tree(selected)
        from .runtime_tui import RuntimeApp
        RuntimeApp(self.engine, project["name"], selected["_restart_command"], self.timeout, self.output_cap,
                   runtime_environment=selected["runtime_environment"]).run()

    def _keys(self) -> KeyBindings:
        keys = KeyBindings()

        @keys.add("down")
        @keys.add("j")
        def next_process(event: Any) -> None:
            if not self.confirming and self.processes:
                self.selected = min(len(self.processes) - 1, self.selected + 1)
                self._refresh_inspector()

        @keys.add("up")
        @keys.add("k")
        def previous_process(event: Any) -> None:
            if not self.confirming and self.processes:
                self.selected = max(0, self.selected - 1)
                self._refresh_inspector()

        @keys.add("enter")
        def confirm(event: Any) -> None:
            if not self.processes:
                self.status = "No current-user Python process is available. Press q to exit."
                return
            self.confirming = True
            self.status = "Confirmation required: s scans normally; r stops the selected process tree and opens the runtime debugger."
            self._refresh_inspector()

        @keys.add("s")
        def normal_scan(event: Any) -> None:
            if not self.confirming:
                self.status = "Press Enter to review and confirm a process before scanning it."
                return
            project = self._scan_selected()
            if project:
                event.app.exit(result=("scan", self.processes[self.selected], project))

        @keys.add("r")
        def restart_debug(event: Any) -> None:
            if not self.confirming:
                self.status = "Press Enter to review and confirm a process before restarting it under the debugger."
                return
            project = self._scan_selected()
            if project:
                event.app.exit(result=("restart", self.processes[self.selected], project))

        @keys.add("escape")
        def cancel_confirmation(event: Any) -> None:
            self.confirming = False
            self.status = "Restart cancelled. Select a process and press Enter to review it."
            self._refresh_inspector()

        @keys.add("q")
        @keys.add("c-c")
        def quit_app(event: Any) -> None:
            event.app.exit()
        return keys

    def _scan_selected(self) -> dict[str, Any] | None:
        selected = self.processes[self.selected]
        root = selected.get("project_root")
        if not root:
            self.status = "Thread could not infer a Python project root for this process, so it will not scan or restart it."
            return None
        try:
            project = self.engine.add_project(root)
            self.engine.scan(project["name"])
            return project
        except ThreadError as exc:
            self.status = f"Cannot prepare this project: {exc}"
            return None

    def _header_text(self) -> StyleAndTextTuples:
        return [("class:title", " Thread Process Discovery"), ("", "     Enter review · s normal scan · r restart under debugger · Esc back · q quit")]

    def _status_text(self) -> StyleAndTextTuples:
        return [("", f" {self.status}")]

    def _list_text(self) -> StyleAndTextTuples:
        if not self.processes:
            return [("", "\n  No current-user Python processes were found.\n")]
        lines: StyleAndTextTuples = []
        for index, item in enumerate(self.processes):
            root = item.get("project_root") or "no project root inferred"
            command = item["command_text"][:66]
            lines.append(("class:selected" if index == self.selected else "", f" {item['pid']:>7}  {command:66}  {root}\n"))
        return lines

    def _cursor(self) -> Point:
        return Point(x=0, y=self.selected)

    def _refresh_inspector(self) -> None:
        if not self.processes:
            self.inspector.text = "Thread only lists Python interpreters owned by your current user. No process has been selected."
            return
        selected = self.processes[self.selected]
        descendants = process_tree(self.processes, selected["pid"])[1:]
        lines = [
            f"PID: {selected['pid']}\n", f"Interpreter: {selected['executable']}\n", f"Working directory: {selected['cwd']}\n",
            f"Inferred project root: {selected.get('project_root') or 'unknown'}\n", f"Virtual environment: {selected.get('venv') or 'not detected'}\n",
            f"Command:\n  {selected['command_text']}\n", f"Python descendants: {', '.join(str(item['pid']) for item in descendants) or 'none'}\n",
        ]
        if self.confirming:
            lines.extend([
                "\nConfirmation\n",
                "[s] Normal reverse engineering: register the inferred root if needed, scan it, then open Thread's normal explorer.\n\n",
                "[r] Restart under debugger: scan the inferred root, terminate the selected current-user Python process and discovered Python descendants, then relaunch the exact command with its detected venv/PATH under Thread Runtime.\n\n",
                "Thread cannot attach to this already-running interpreter in this release.\n",
            ])
        else:
            lines.append("\nPress Enter to review actions. This screen does not execute, scan, or stop anything until you choose an action.\n")
        self.inspector.text = "".join(lines)
        self.inspector.buffer.cursor_position = 0
