# Terminal operator UI

Start the full-screen interface from any directory:

```bash
ats-lab tui
```

The UI refreshes canonical SQLite projections continuously. It never reads raw
strategy source, credentials, Jesse payloads, trades, or generated artifacts.

## Views and keys

| Key | Action |
|---|---|
| `1`-`5`, left/right | Overview, queue, candidates, HPO, memory |
| up/down, `j`/`k` | Move selected row |
| Page Up/Page Down | Move ten rows |
| `g` / `G` | First / last row |
| Enter or `d` | Toggle selected-row details |
| `p` | Pause supervisor control |
| `r` | Resume supervisor control |
| `s`, then `s` | Confirm graceful supervisor stop |
| `?` | Help overlay |
| `q` | Exit TUI; supervisor keeps its current state |

Color is semantic: green healthy/running/delivered, cyan ready/commands, yellow
retry or attention, red blocked/failure, and magenta candidate evidence. Layout
clips columns to terminal width rather than wrapping into unreadable tables.

For plain logs or non-interactive terminals, use:

```bash
ats-lab monitor --watch
```

## Reusable architecture

- `tui_types.py`: typed views, actions, roles, states, and immutable lines.
- `tui_repository.py`: bounded read-only projections behind `TuiDataSource`.
- `tui_renderer.py`: responsive semantic layout behind `ScreenRenderer`.
- `tui_controller.py`: refresh/navigation/control orchestration.
- `terminal_ui.py`: stable public façade for other consumers.

Tests can substitute repository, renderer, and screen objects without curses or
the live database.
