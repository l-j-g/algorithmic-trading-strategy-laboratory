# Jesse workspace integration

## Repository roles

The laboratory repository owns the research harness and durable operational
state. The configured Jesse repository owns strategies, Jesse configuration,
and candle/market data. The public Jesse engine is a third, clean checkout;
never mix engine source into the private research workspace.

| Concern | Canonical owner |
|---|---|
| Queue and leases | `.ats-lab/laboratory.sqlite3` |
| Experiment specifications | `.ats-lab/laboratory.sqlite3` |
| Run metrics and session references | `.ats-lab/laboratory.sqlite3` |
| Evaluations and synthesis cohorts | `.ats-lab/laboratory.sqlite3` |
| Harness code and JSON contracts | `src/ats_lab/` |
| Worker/resource configuration | `.ats-lab/config.toml` |
| Jesse evaluation gates | `docs/jesse/BACKTEST_EVALUATION_PROTOCOL.md` |
| Jesse strategy concept library | `docs/jesse/STRATEGY_CONCEPT_PLAYBOOK.md` |
| Jesse engine source | sibling `jesse-upstream/` |
| Strategy source | configured `jesse-src/` repository |
| Candles and market data | Jesse, accessed through Jesse MCP |
| Legacy readable research notes | `jesse-src/research/` |

The laboratory does not copy Jesse candles into SQLite. “Training data” in this
workflow means the historical market data selected in each experiment route;
Jesse owns and serves that data. ATS Lab stores the experiment definition and
normalized evidence produced from it.

## Local binding

Default sibling layout:

```text
<workspace-root>/
├── algorithmic-trading-strategy-laboratory/  # harness + canonical SQLite
├── jesse-src/                                # private research workspace
└── jesse-upstream/                           # clean public Jesse engine
```

`jesse-upstream` tracks `https://github.com/jesse-ai/jesse.git` on its public
default branch. Refresh it with:

```bash
scripts/jesse-workspace.sh upstream update
```

Update refuses dirty state and non-fast-forward history. Build and start a
provenance-labelled Jesse image with:

```bash
scripts/jesse-workspace.sh image build
scripts/jesse-workspace.sh stack up
scripts/jesse-workspace.sh stack up --no-update  # deliberate pinned/offline start
```

`stack up` polls upstream before starting by default. Use `--no-update` only
when an intentionally pinned/offline start is required. For periodic polling
while the stack remains running, schedule this idempotent command in the host
scheduler:

```bash
scripts/jesse-workspace.sh upstream refresh
```

It fetches the public branch, fast-forwards only, and builds the commit-tagged
image only when that tag is not already local. Keep scheduling serialized: one
refresh/image build at a time.

`scripts/jesse-workspace.sh status` performs a read-only inspection of the
`jesse` container (override its name with `JESSE_CONTAINER_NAME`). It prints the
canonical future image/revision from `jesse-upstream` alongside the running
image and its `org.opencontainers.image.revision` label. It reports
`provenance_status=canonical` only when both values match exactly; missing or
mismatched provenance is never presented as a match. If Docker is unavailable,
the status is `unavailable` and no provenance or restart decision is inferred.
A running non-canonical image is reported as
`provenance_status=transitional_exception`: preserve the active batch and do
not rebuild, replace, restart, or relabel it. After that batch completes, use
`scripts/jesse-workspace.sh stack up` as the controlled restart/rebuild path.

The compose override selects `ats-lab/jesse:<upstream-commit>` while the
existing `jesse-src/docker/docker-compose.yml` supplies the private workspace
volume, PostgreSQL, Redis, dashboard, and MCP services. The upstream image
contains Jesse engine code only; it does not contain private strategy source,
ATS SQLite state, credentials, or generated results.

The canonical full runtime stack therefore remains
`<workspace-root>/jesse-src/docker/docker-compose.yml`. ATS owns only the
control script and image-selection override; it does not duplicate Jesse's
service definitions.

The ignored `.ats-lab/config.toml` contains:

```toml
[repositories]
jesse = "<workspace-root>/jesse-src"
```

The agent executor uses that workspace and performs all strategy, candle,
configuration, and backtest operations through Jesse MCP. The ATS Lab worker
owns claims and persists the returned run plus evaluation in one transaction.

On the current workstation, `jesse-src/.ats-lab` points to this repository's
`.ats-lab` directory for read-compatible local access. This pointer is machine
configuration, not portable committed state.

From either repository, use the installed ATS CLI:

```bash
ats-lab queue
ats-lab candidates
ats-lab audit
```

`ATS_LAB_REPOSITORY` and `ATS_LAB_DATABASE` override the defaults.

Direct Jesse commands from `jesse-src/docker` still use the template's default
image. For reproducible ATS research, use the ATS control surface above so the
image tag and upstream commit are recorded in the operator handoff.

## Safety and recovery

- Never initialize a second database from the Jesse workspace.
- Never commit `.ats-lab/config.toml`, SQLite files, WAL/SHM files, credentials,
  or candle data.
- Never edit SQLite manually to change lifecycle state.
- Use `ats-lab reconcile`, `ats-lab normalize-blockers`, and `ats-lab audit`.
- Keep one database writer boundary: the ATS Lab worker/CLI.
- Treat Markdown imports as migration input, not a competing operational queue.
