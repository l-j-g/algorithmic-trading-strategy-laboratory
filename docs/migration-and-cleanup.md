# Legacy Workflow Cleanup

Canonical transition completed for new automation. Historical files remain
read-only until their evidence is verified in SQLite.

## Retain as canonical

- `AGENTS.md`: Jesse MCP and trading-safety contract; generated Jesse rules
  remain owner-managed in the Jesse research workspace.
- `src/ats_lab/`: schema, services, CLI and migration adapters.
- `tests/`: public contract, storage and migration tests.
- `docs/jesse/`: Jesse evaluation gates and thesis/concept library.
- `<jesse-src>/research/experiments/`: only reports that cannot yet be
  regenerated.

## Replace with generated views

- archived legacy `TEST_JOB_QUEUE.md` -> `active_queue` query.
- archived legacy `RESEARCH_JOURNAL.md` -> evaluation/history report.
- candidate HTML -> database candidate view.
- handoff/progress logs -> events and operational-status view.

## Legacy cleanup complete

Markdown/JSON sidecars, migrate-legacy, legacy_adapter/import removed.
All state is in SQLite. Historical references archived in docs only.

New operations via supervisor only.

## Keep outside database

- secrets and `.env` files.
- private strategy source.
- candle/runtime databases and generated charts.
- memory provider state and agent executor profile configuration.
