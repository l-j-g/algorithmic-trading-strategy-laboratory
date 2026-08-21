# Review remediation — fork task briefs

Coordinator-driven remediation of `docs/review-findings-2026-08.md`. Each
fork (separate opencode session) owns ONE lane, working inside its own git
worktree on its own branch. The coordinator session merges verified lane
branches into `main` and updates the register statuses.

## Fork rules (every lane)

1. Work ONLY inside your worktree directory. Commit ONLY to your lane branch.
   NEVER merge, rebase, or push. The coordinator merges.
2. One task = one focused commit (`fix:` / `refactor:` style, matching repo
   history). Run the FULL suite green before every commit:
   `PYTHONPATH=src python3 -m unittest discover -s tests`
   Also run `git diff --check` before committing.
3. Never edit `docs/review-findings-2026-08.md` or this file.
4. Stay inside your lane's allowed files. If a fix seems to require files
   outside your lane, STOP that task, leave the issue uncommitted or revert,
   and report it — do not expand scope silently.
5. Stdlib only. No comments unless genuinely needed (repo convention).
   Match existing code style. Add unittest tests for every behavior change.
6. If a task is blocked or ambiguous, skip it, move to the next task in your
   lane, and report the blocker at the end.
7. Read AGENTS.md before starting.

## Status (coordinator-maintained — do not edit)

Resolved and merged to main:

| Issue | Summary |
|---|---|
| RVW-015 | JSON-RPC response-id correlation + MCP session DELETE teardown |
| RVW-018 | Preflight required-tables subset check |
| RVW-019 | Asynchronous-start tolerance before dashboard fallback |
| MIN-21 | Preflight MCP probe envelope validation, probe teardown, candle check |
| RVW-020 | Legacy dashboard mutations gated to loopback binds |

Also landed earlier (data-layer cluster): RVW-008, RVW-009, RVW-010, RVW-011,
RVW-012, MIN-07, MIN-18.

## Lane A — execution boundary

Worktree: `../ats-lab-review-a` · Branch: `review/cluster-a`
Allowed files: `src/ats_lab/direct_mcp_executor.py`,
`src/ats_lab/agent_launcher.py`, `src/ats_lab/correctness_recovery.py`,
`src/ats_lab/resources.py` (only if a task says so),
`src/ats_lab/schemas/`, `scripts/jesse-workspace.sh`,
`docs/jesse-adapter-contract.md`, plus tests.

### A1 · RVW-016 · wire schema validators
Hand-roll minimal stdlib validators derived from `src/ats_lab/schemas/*.json`
(no jsonschema dependency). Enforce at request-build AND result-persist
boundaries in `direct_mcp_executor.py`. Request violation → executor's
existing harness-shaped error type; result violation → terminal invalid
result consistent with supervisor.py raw_result exact-key equality (do not
weaken that check). Keep .json files as documentation; update
`docs/jesse-adapter-contract.md` (:41-42 area) to state what is enforced
where. Tests via the fake-HTTP-server infrastructure in
tests/test_direct_mcp_executor.py.

### A2 · RVW-017 · circuit breaker on uncharged infra failures
Bound CONSECUTIVE uncharged infrastructure failures per work item
(threshold ~10, ResourcePolicy-configurable — resources.py edits allowed
here). On breach: block item with blocker code `infrastructure_circuit_broken`,
recoverable through the existing resolve-blocker flow. Store the counter in
executor-owned persisted state (execution checkpoint /
direct_execution_telemetry). Do NOT add database.py schema migrations; if
genuinely impossible without one, bound in-memory per supervisor process and
note the limitation in the commit message body.

### A3 · RVW-014 · replacement-reservation wedge recovery
direct_mcp_executor.py:1073-1098: crash between reservation UPDATE and
session-id persist permanently wedges the item ("manual reconciliation
required", unbounded infra retry). Make reservation+persist as atomic as
practical; add a preview/apply recovery command in correctness_recovery.py
(follow its existing command patterns incl. CLI exposure if present) that
clears orphaned replacement reservations when no replacement session exists.

### A4 · MIN-04 + MIN-22 · executor cleanups
MIN-04: sanitize non-finite floats to null before json.dumps persistence
(:1379 area). MIN-22: remove dead overall-outcome logic (:608-610); fix O(n²)
delegated split (:486-487); record event + cancel orphaned draft on
checkpoint-save failure (:688-694); deduplicate _create/_create_significance
(:920-1024, :1452-1469); dashboard fallback uses config snapshot not live
get_config() (:427); fix docs/jesse-adapter-contract.md monte_carlo snapshot
claim OR route monte_carlo through the direct path — choose the smaller
correct change.

### A5 · MIN-05 + MIN-20 · agent launcher hygiene
MIN-05: pass prompt via stdin instead of argv (:247). MIN-20: bound persisted
stderr detail (:492-494); lock telemetry JSONL appends (:324-328); derive
analyzer timeout window from ResourcePolicy instead of hardcoded duplicate
(:426-435).

### A6 · MIN-12 · jesse-workspace.sh robustness
Fix: :64 whole-status dies when a sibling dir is not a git repo; :174 build
stdout discarded; :179-184 stack down needlessly requires upstream repo +
current-HEAD tag; :203 missing `--` separator; :303 awk truncates paths with
spaces. Keep `set -euo pipefail` semantics intact.

## Lane B — interfaces

Worktree: `../ats-lab-review-b` · Branch: `review/cluster-b`
Allowed files: `src/ats_lab/cli.py`, `cli_ux.py`, `console.py`,
`tui_renderer.py`, `tui_controller.py`, `status.py`, `dashboard.py`,
`web_api.py`, `research_memory.py`, `local_commands.py`, `pyproject.toml`,
`README.md`, `.gitignore`, `docs/operator-dashboard.md`, plus tests.

### B1 · RVW-021 · CLI dispatch refactor
Convert the ~1,200-line if/elif chain in cli.main() (:197-1410) to
table-dispatched handlers preserving EXACT command names, flags, output
contracts, exit codes. Uniform error mapping: contract violations → clean
message + exit 2 (fix enqueue traceback leak :1182 and synthesize
missing/invalid --file :1292-1294); unexpected errors still traceback.
README usage examples must keep working.

### B2 · MIN-06 + MIN-11 · web hardening
MIN-06: validate Host header against loopback set {127.0.0.1, localhost,
::1} (+ literal bound host when explicitly non-loopback) on all HTTP
surfaces (web_api.py:636ff, dashboard.py:939ff); reject others 403.
Mutations stay behind the confirm-header gate. MIN-11:
research_memory.py:724-731 strip Authorization on cross-host redirects.

### B3 · MIN-09 + MIN-19 + MIN-24 · small fixes
console.py:738 uncaught float() on `watch abc` → clean error;
web_api.py:763 missing unquote on hpo study detail route (match siblings);
dashboard.py:1161 silence per-request log_message stdout noise;
.gitignore add .DS_Store, .coverage, htmlcov/.

### B4 · MIN-10 · dashboard/status performance
dashboard.py:139 O(n²) row scan → key by identity; :359-384 compute counts/
evidence snapshot once per request instead of per page+fragment refresh;
status.py:105-142 N+1 validation-jobs query per study → single grouped
query; web_api.py:212-214,463 repeated operator_status calls per request →
compute once; status.py:305 replace str.isdigit heuristic with proper type
check. No behavior/output changes — pure efficiency.

### B5 · MIN-08 + MIN-23 · config/docs/CLI dedup
pyproject.toml:37-38 remove dead [tool.unittest]; README.md:150 correct the
`ats-lab init` claim (database only, no config skeleton). cli.py:217-260
factor duplicated memory command families (memory-status vs memory status)
into shared argument/handler helpers (keep both surfaces); add ONE
justified comment at cli.py:1239-1246 explaining the intentional double
get_backtest_session (unchanged_observations=2).

## Lane D — research methodology

Worktree: `../ats-lab-review-d` · Branch: `review/cluster-d`
Allowed files: `src/ats_lab/gates.py`, `synthesis.py`, `batch_synthesis.py`,
`supervisor.py`, `hpo.py`, `hpo_routes.py`, `evaluation_windows.py`,
`resources.py`, `strategy_contracts.py`, `database.py` (only where a task
says so), `docs/jesse/BACKTEST_EVALUATION_PROTOCOL.md`,
`docs/jesse/STRATEGY_CONCEPT_PLAYBOOK.md`, plus tests.
Operator-approved decisions are binding; update the protocol doc wherever
semantics change.

### D1 · RVW-001 · remove hardcoded HPO verdicts
Delete EMA_V7_CLASSIFICATIONS and the study-name-prefix inference path
(hpo.py:45-78, :366-373). Classifications come ONLY from explicit
caller/operator-supplied mappings. Studies without an explicit mapping keep
today's no-mapping behavior minus prefix inference. Update tests relying on
the fixture map (construct explicit mappings instead) and any doc references.

### D2 · RVW-003 · first-test-wins significance
synthesis.py:196-206 `_latest_p_value` + :293-295: first finished
significance test per canonical entry fingerprint is BINDING; later tests
stored and visible but never flip readiness. Expose which test counted and
when in synthesis status output. Storage may use existing tables/events —
small database.py additions allowed if needed (use the ordered _MIGRATIONS
mechanism landed on main).

### D3 · RVW-002 · Benjamini-Hochberg FDR
Implement BH FDR control across each synthesis cohort's family of
entry-significance p-values (synthesis.py:273-274, batch_synthesis.py:354,
resources.py:83 cohort size). Effective per-hypothesis threshold surfaced in
gate findings so verdicts remain explainable. Document the procedure in
BACKTEST_EVALUATION_PROTOCOL.md. Deterministic unit tests with known
p-value families.

### D4 · RVW-007 · unify inconclusive semantics
database.py:2895-2897 archives dependent baselines on p≤0.10 while
synthesis.py:296-297 keeps them scheduled. Unify on KEEP-SCHEDULED (archive
stays a manual operator action). Verify sanitize.py:17-21 and worker.py
p-value tier enforcement points stay consistent. database.py edit confined
to the significance path.

### D5 · RVW-004 · deterministic hpo_candidate gate
Add evaluate_hpo_candidate in gates.py enforcing BACKTEST_EVALUATION_PROTOCOL.md
criteria (:110-122): activity floor per window, multi-window positivity, no
single dominant route. gates.missing ⇒ inconclusive (never hpo_candidate).
Wire into supervisor verdict path (:1009-1035) exactly like
evaluate_promotion is wired for paper_trade_candidate.

### D6 · RVW-006 · verify OOS at gate time
gates.py:76-83 accepts any evidence_split="oos" label. For promotion lanes
generally, verify split disjointness from route date ranges against training
routes; overlapping/mislabelled ⇒ ineligible for the OOS lane.

### D7 · RVW-005 · machine-generated cost-stress runs
At PROMOTION evaluation stage only: enqueue 2x-fee variant routes through
the EXISTING execution pipeline (request-level fee override — investigate
how fees flow through execution requests; NO direct_mcp_executor.py edits).
Gate consumes ONLY machine-generated stress results; legacy self-reported
cost_stress_status becomes inconclusive/unverified rather than passing
(gates.py:301-302, evidence.py:322-324 trust point). Update protocol doc.
If a required hook is missing outside your lane, stop and report.

### D8 · MIN-01 + MIN-16 · window semantics
evaluation_windows.py:56-64 + hpo_routes.py:240-250: half-open interval
semantics [start, finish) so adjacent windows share no candle days; update
the adjacency test; document. evaluation_windows.py:52: replace date.today()
fallback with an explicit configured anchor date (config field; document
default + pinning guidance).

### D9 · MIN-02 + MIN-14 · synthesis rigor
synthesis.py:272: significance across ALL routes (worst-case max-p,
mirroring supervisor.py:1625-1656 conservatism), not routes[0].
synthesis.py:68-73: job_fingerprint excludes hypothesis prose (canonical
entry rule + structural fields only).

### D10 · MIN-03 + MIN-13 · deterministic inputs
hpo.py:349-351: Optuna import identity keyed on content hash (file bytes) +
study_id, not resolved path. supervisor.py:559-560: remove attempt-count-
dependent truncation of canonical evidence — analysis inputs deterministic
regardless of attempt number.

### D11 · MIN-15 + MIN-17 · gates edge cases
gates.py:212-231: encode playbook MC interpretation rules
(STRATEGY_CONCEPT_PLAYBOOK.md:262-267): median comparison, worst-5% tail,
above-best-5%-tail suspicion flag in robustness findings. gates.py:358-369:
malformed window dates raise/flag (finding entry) instead of silent fallback
to the 50-trade floor.
