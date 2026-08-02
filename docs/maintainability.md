# Maintainability and SOLID boundaries

Use objects for components with state, lifecycle, or replaceable dependencies.
Keep deterministic transformations as small pure functions. OOP is a boundary
tool, not a requirement to wrap every function.

## Rules

1. Represent closed vocabularies with enums at domain boundaries. Do not add a
   new queue, verdict, lifecycle, control, or UI-role string in multiple files.
2. Put I/O behind narrow protocols. Controllers depend on interfaces; SQLite,
   curses, HTTP, and subprocess implementations remain replaceable.
3. Keep rendering separate from data access and mutations.
4. Keep transaction-owning operations cohesive. Do not split one atomic SQLite
   workflow across generic repositories merely to create more classes.
5. Prefer one class responsibility and constructor-injected collaborators.
6. Preserve machine-readable contracts while adding human projections.
7. Add behavior tests at each seam before moving established code.

## Current boundaries

- `WorkflowDatabase`: canonical transaction boundary.
- `BatchSupervisor`: application orchestration boundary.
- `DirectMcpDispatcher`: Jesse execution adapter.
- `ExecutionDispositionPolicy`: pure transient/analyzable/operator classifier.
- `ExecutionFailureRecorder`: atomic durable-failure to analysis transition.
- `TerminalFailureRecovery`: bounded legacy retry-limit migration.
- `ExecutionAnalysisInputBuilder`: safe success/failure analyzer contracts.
- `MemoryResearchAdapter`: advisory-memory adapter.
- Terminal UI: typed value objects, projection repository, renderer, controller,
  and public façade in separate modules.

## Incremental cleanup order

These are refactoring targets, not permission for a whole-codebase rewrite:

1. Extract CLI parser construction and domain command handlers from `cli.py`
   behind a command protocol while preserving every existing invocation.
2. Continue splitting HPO and synthesis policy behind typed collaborators;
   execution disposition and analysis-input policy are already extracted.
3. Introduce one typed HPO lifecycle enum shared by status, dashboard, console,
   supervisor, and persistence adapters.
4. Split large read projections from `WorkflowDatabase`; keep writes and
   cross-table transactions in the canonical transaction service.
5. Replace duplicated queue-state ordering maps with shared typed policy.

Each extraction must pass the full suite, `compileall`, `git diff --check`, and
live read-only operator checks before merge.
