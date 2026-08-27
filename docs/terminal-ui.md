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

For the attached operator display, use:

```bash
ats-lab start
```

This starts or resumes the supervisor, then follows durable activity events.
Event rows stay on screen; one bottom line replaces `LIVE` and refreshes every
second while waiting:

```text
PREFLIGHT: Docker OK · Jesse OK · Memory OK
SYNTHESIS: Synthesising 25 new tests
  => 01  NEW  MeanReversionStrategy · BTC-USDT · 1h
      ↳ evaluating: mean reversion after volatility compression
RUNNING (3/25): Backtest Complete · MeanReversionStrategy · BTC-USDT · 1h · 2023-01-01 → 2026-06-02 · trades=42 · net=+12.35% · sharpe=1.23 · max_dd=-4.50% · Jesse ↗
RULE TEST (4/25): Rule Test Complete · AtrContractedVwapReversion · SOL-USDT · 1h · 2023-01-01 → 2026-06-02 · observed_mean=+0.001235 · annualized_return=+12.3457 · p_value=0.0312 · n_simulations=5,000 · n_observations=1,200 · Jesse ↗
ANALYSIS: Completed (24/24)
  01  PASS  MeanReversionStrategy · lifecycle gates cleared
└─ WAITING          · 4 hrs : 24 min (+55 sec) · (^ 469)
```

The footer shows total research duration, time since the last event, and the
latest provider token usage when available. `RULE TEST`, `MONTE CARLO`, and
`HPO` use the same one-line run layout. Rule tests show observed mean,
annualized return, p-value, simulation count, and observation count. HPO
analysis remains under `ANALYSIS`. Completed runs with hard quality-gate
failures receive a deterministic decision without an agent turn; missing-only
evidence still enters analysis.

Execution events from one bounded turn are compacted in the terminal. The
`n/m` counter is that turn's progress; it is not the synthesis cohort size.
Detailed per-item events remain in the optional daily activity log.

Press `Ctrl-C` once to request a graceful stop. The supervisor finishes its
current bounded dispatch, records `STOPPING`, and the attached display waits
until the supervisor reaches `STOPPED`. A second `Ctrl-C` returns control to
the shell while the supervisor continues its safe finish.

Optional file logging is configured in ignored `.ats-lab/config.toml`:

```toml
[logging]
log_to_file = true
log_dir = "{ats-lab}/logs/{date}_log"
```

Files contain plain event rows only: no ANSI colour, hyperlinks, or ticking
footer. File failures do not stop research.

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
