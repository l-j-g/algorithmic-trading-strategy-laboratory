# Terminal operator UI

Start the full-screen interface from any directory:

```bash
ats-lab tui
```

Start and stop the research process without managing terminal jobs manually:

```bash
ats-lab loop start
ats-lab loop status
ats-lab loop stop
```

`loop start` resumes an existing supervisor or launches one detached when none
is alive. Output goes to ignored runtime file `.ats-lab/supervisor.log`.
`loop stop` requests a graceful stop; it does not kill an executing batch.

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
| `r` | Start a missing loop, or resume an existing loop |
| `p` | Pause loop without ending its process |
| `s`, then `s` | Confirm graceful loop stop |
| `?` | Help overlay |
| `q` | Exit TUI; supervisor keeps its current state |

Top action bar always shows `START LOOP`, `PAUSE`, `STOP LOOP`, and `CLOSE UI`.
Color is semantic: green healthy/running/delivered, cyan ready/commands, yellow
waiting/retry, red stalled/blocked/failure, and magenta candidate evidence.

Every data view uses one reusable fitted-table engine. Header and row cells use
the same calculated widths. Numbers align right. Text aligns left. Wider
terminals expand useful text columns; narrow terminals shrink them and then drop
low-priority optional columns while retaining required titled columns.

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

In an interactive terminal this view clears and redraws a compact live panel:
the status/stage/heartbeat banner, queue counters, active jobs, and recent
completed results (`strategy`, `pair`, profit, trades, Sharpe, drawdown, and
disposition).  Rows are colour-coded by state.  Set `NO_COLOR=1` for plain
ANSI-free output; JSON output remains unchanged.

## Reusable architecture

- `tui_types.py`: typed views, actions, roles, states, and immutable lines.
- `terminal_table.py`: reusable fitted columns, title rendering, and alignment.
- `tui_tables.py`: named table definitions without scattered layout strings.
- `tui_repository.py`: bounded read-only projections behind `TuiDataSource`.
- `tui_renderer.py`: responsive semantic layout behind `ScreenRenderer`.
- `tui_controller.py`: refresh/navigation/control orchestration.
- `loop_control.py`: reusable supervisor process lifecycle boundary.
- `retry_schedule.py`: absolute and relative retry-time normalization.
- `terminal_ui.py`: stable public façade for other consumers.

Tests can substitute repository, renderer, and screen objects without curses or
the live database.
