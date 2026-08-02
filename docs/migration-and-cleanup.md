# Legacy Workflow Cleanup

Canonical transition completed for new automation. Historical files remain
read-only until their evidence is verified in SQLite.

## Retain as canonical

- `AGENTS.md`: Jesse MCP and trading-safety contract, shortened later.
- `.executor.md`: concise Agent project routing.
- `src/ats_lab/`: schema, services, CLI and migration adapters.
- `tests/`: public contract, storage and migration tests.
- `research/experiments/`: only reports that cannot yet be regenerated.
- licensed-source provenance manifest.

## Replace with generated views

- archived legacy `TEST_JOB_QUEUE.md` -> `active_queue` query.
- archived legacy `RESEARCH_JOURNAL.md` -> evaluation/history report.
- candidate HTML -> database candidate view.
- handoff/progress logs -> events and operational-status view.

## Compatibility-only; do not operate

- Markdown queue mutation scripts.
- JSON operational sidecars superseded by SQLite.
- duplicate starter/analyzer/promotion shell loops.
- archived inline prompt variants and obsolete plans.
- redundant experiment logging/review skills.
- obsolete orchestration-framework references and artifacts.

Jesse legacy loop and Markdown/JSON sidecars now live only under
`research/archive/legacy_workflow/`. New queue, run, evaluation and synthesis
writes must go through `ats-lab supervisor`.

## Keep outside database

- secrets and `.env` files.
- private strategy source.
- candle/runtime databases and generated charts.
- Memory memories and Agent profile configuration.
