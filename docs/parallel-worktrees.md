# Parallel worktree protocol

Use Git worktrees for independent code and documentation tasks. The main checkout remains the integration and runtime-operations lane.

## Invariants

1. One task, one `task/<slug>` branch, one worktree, one owner.
2. Agents may edit only their assigned worktree. Never run two writers in one checkout.
3. Only the main integration lane may mutate `.ats-lab/laboratory.sqlite3`, operate the shared supervisor, or perform recovery against live Jesse sessions.
4. Worktree tests use temporary databases and fixtures. Do not copy `.ats-lab`, `.env`, Docker volumes, credentials, private strategy source, or generated runtime results into a worktree commit.
5. Shared Jesse, PostgreSQL, Redis, MCP, dashboard, and Memory services are read-only to ordinary development lanes. Any task requiring shared-state writes is serialized through the main lane.
6. Each lane commits only a verified logical change. Before integration: focused tests, full relevant suite, `git diff --check`, and a secret/runtime-artifact review.
7. Integration is performed by commit SHA, normally with `git cherry-pick`. Resolve conflicts and rerun verification in the main lane. Never merge an unreviewed worktree wholesale.
8. A lane reports its branch, worktree path, commit SHA, changed files, tests, and any shared-state assumptions.

## Start a lane

From the main checkout:

```bash
scripts/worktree-task.sh create <task-slug> [base-ref]
```

This creates:

- branch: `task/<task-slug>`
- path: sibling directory `<repo>-worktrees/<task-slug>`

Assign the worker that exact path and a file/symbol ownership boundary. Parallel lanes should not modify the same files unless explicitly coordinated.

## Worker completion contract

The worker must:

1. Rebase or reset only when explicitly authorized; never rewrite another lane.
2. Run focused tests and the repository suite.
3. Run `git diff --check`.
4. Inspect staged paths for secrets, runtime databases, generated outputs, and private strategy source.
5. Commit with a conventional message.
6. Return the commit SHA and verification evidence.

The worker must not push, merge, operate the live supervisor, or alter the live ATS database unless its lane is designated as the integration/operations lane.

## Integrate

In the main checkout:

```bash
git status --short
git cherry-pick <verified-task-sha>
PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'
git diff --check
```

If integration changes behavior involving Jesse infrastructure, also run the deterministic stack preflight before resuming the supervisor.

## Remove a completed lane

After its commit has been integrated and verified:

```bash
scripts/worktree-task.sh remove <task-slug>
git branch -d task/<task-slug>
```

Do not remove a lane with uncommitted changes. The helper intentionally lets Git refuse unsafe removal.

## Lane selection

Good parallel lanes:

- independent tests;
- documentation;
- isolated adapters or pure transformations;
- independent strategy research specifications;
- code review and static analysis.

Serialize these through the main lane:

- live database recovery or migration;
- supervisor control;
- shared Jesse session creation;
- changes touching the same state machine or schema migration;
- final integration and release commits.
