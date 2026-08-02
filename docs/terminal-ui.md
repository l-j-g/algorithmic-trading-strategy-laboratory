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
| `1`-`6`, left/right | Overview, queue, candidates, HPO, memory, columns |
| up/down, `j`/`k` | Move selected row |
| Page Up/Page Down | Move ten rows |
| `g` / `G` | First / last row |
| `c` | Cycle compact, standard, and wide Org column profiles |
| Enter or `d` | Toggle selected-row details |
| `p` | Pause supervisor control |
| `r` | Resume supervisor control |
| `s`, then `s` | Confirm graceful supervisor stop |
| `?` | Help overlay |
| `q` | Exit TUI; supervisor keeps its current state |

Color is semantic: green healthy/running/delivered, cyan ready/commands, yellow
retry or attention, red blocked/failure, and magenta candidate evidence. Layout
clips columns to terminal width rather than wrapping into unreadable tables.

## Org-style columns

View `6` groups unresolved work beneath state headings and aligns properties:

```text
* WAITING RETRY (3)
ITEM  STATE  PRI  TYPE  STRATEGY  SYMBOL  TF  VERDICT  NET%  SHARPE  TRADES  NEXT
```

Compact terminals retain item, state, strategy, and next/blocker. Standard mode
adds priority, experiment type, and verdict. Wide mode adds route and comparable
normalized metrics. Press `c` to cycle profiles; columns still shrink or drop
automatically when the terminal cannot fit them.

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
