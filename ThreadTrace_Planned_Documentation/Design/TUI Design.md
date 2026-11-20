# TUI Design

> [!summary]
> The terminal user interface is ThreadTrace's primary control interface. It adapts to terminal dimensions and focused panes.

## Conceptual layout

```text
ThreadTrace                         Project: example-app
Static | Dynamic | Hybrid

EXPLORER              CODE VIEW                    INSPECTOR
▾ src/                12 def process(data):       process()
  ▾ main.py            13   result = parse(data)   Calls: parse, save
    ƒ main            ▶14   save(result)           Args: data
    ƒ process          15   return result
▸ Routes  ▸ Imports  ▸ Resources

OUTPUT | EVENTS | NETWORK | VARIABLES | STACK
$ python -m example_app
DYNAMIC | main.py:14 | Scan fresh                 F1 Help
```

## Interaction requirements

- Command palette, configurable keybindings, pane navigation, search, contextual inspectors, and clear execution states.
- TUI owns early-stage execution control; browser views are initially read-only companions.
- Target terminal I/O remains separate from ThreadTrace UI output.

## Proposed keymap

| Action | Proposed key |
| --- | --- |
| Command palette | `Ctrl+P` or `:` |
| Switch pane | `Tab` / `Shift+Tab` |
| Search symbols | `/` |
| Go to definition | `g` |
| Continue execution | `F5` |
| Step over | `F10` |
| Step into | `F11` |
| Step out | `Shift+F11` |
| Pause | `p` |
| Toggle breakpoint | `b` |
| Navigate recorded history | `[` / `]` |
| Rescan | `R` |
| Open graph | `m` |

> [!note]
> These are proposed, configurable bindings. The command palette exposes all actions and accommodates terminal limitations.
