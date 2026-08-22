# Framework review findings — August 2026

Engineering issue register from the full framework review of 2026-08-21
(four parallel subsystem reviews: data layer, execution boundary, research
methodology, interfaces/tests/docs; test suite verified at 344/344 passing).

Engineering backlog only. Never import these entries into ATS research
`work_items`, synthesis cohorts, or strategy queue state.

Status values: `open` → `in_progress` → `resolved` (or `wontfix` with reason).

## Resolution summary (2026-08-22)

All issues are resolved on `main` except RVW-013 (god-class split — approved
to run after the fixes land; it is next). 450 tests pass, up from 344 at
review time. Highlights:

- Data-layer cluster (RVW-008..012): guarded queue upserts, provenance-clean
  evidence normalization, append-only verdict history (`evaluation_history`
  + compatibility view), ordered guarded migrations as single versioning
  mechanism.
- Methodology cluster (RVW-001..007): explicit HPO mappings only,
  Benjamini-Hochberg FDR across cohort significance families,
  first-test-wins per entry fingerprint, deterministic `hpo_candidate` gate,
  machine-generated 2x-fee cost-stress runs gating promotion (self-reported
  status no longer persisted outside `-COST2X` experiments), date-verified
  OOS disjointness, unified keep-scheduled inconclusive semantics.
- Execution cluster (RVW-014..019): JSON-RPC id correlation + session
  teardown, wired contract validators, infrastructure circuit breaker with
  policy-injected threshold, preflight subset check, asynchronous-start
  tolerance, orphaned-reservation recovery CLI.
- Interfaces cluster (RVW-020/021): loopback-gated dashboard mutations,
  table-dispatched CLI with uniform exit-code contract, Host-header
  validation, redirect auth stripping.
- All minors MIN-01..MIN-24 resolved; MIN-15 activated by persisting MC
  tail-summary fields (schema v8) consumed by the encoded playbook rules.

## Index

| ID | Sev | Area | Title | Status |
|---|---|---|---|---|
| [RVW-001](#rvw-001) | critical | methodology | Hardcoded historical HPO verdicts applied by study-name prefix | resolved |
| [RVW-002](#rvw-002) | major | methodology | No multiple-comparison control across concept cohorts | resolved |
| [RVW-003](#rvw-003) | major | methodology | Optional stopping: latest-p-wins significance semantics | resolved |
| [RVW-004](#rvw-004) | major | methodology | `hpo_candidate` verdict bypasses deterministic gates | resolved |
| [RVW-005](#rvw-005) | major | methodology | Cost-stress check is self-reported, never machine-generated | resolved |
| [RVW-006](#rvw-006) | major | methodology | OOS status is a trusted label, not verified at gate time | resolved |
| [RVW-007](#rvw-007) | major | methodology | Inconclusive semantics contradict between gate paths | resolved |
| [RVW-008](#rvw-008) | major | data layer | `upsert_work_item` overwrites live state without guard | resolved |
| [RVW-009](#rvw-009) | major | data layer | Evidence normalization silently distorts canonical metrics | resolved |
| [RVW-010](#rvw-010) | major | data layer | Batch finalization selects work item by recency, not identity | resolved |
| [RVW-011](#rvw-011) | major | data layer | Evaluations delete-before-insert destroys verdict history | resolved |
| [RVW-012](#rvw-012) | major | data layer | Ad-hoc migration strategy can drift fresh vs migrated DBs | resolved |
| [RVW-013](#rvw-013) | major | data layer | `WorkflowDatabase` is a ~3,000-line god class | open (next) |
| [RVW-014](#rvw-014) | major | execution | Replacement-reservation crash leaves permanent wedge state | resolved |
| [RVW-015](#rvw-015) | major | execution | MCP client lacks JSON-RPC id correlation and session teardown | resolved |
| [RVW-016](#rvw-016) | major | execution | Versioned JSON schemas are dead artifacts on the execution path | resolved |
| [RVW-017](#rvw-017) | major | execution | Harness defects loop forever as uncharged infrastructure retries | resolved |
| [RVW-018](#rvw-018) | major | execution | Preflight exact-table-set equality bricks after upstream refresh | resolved |
| [RVW-019](#rvw-019) | major | execution | TOCTOU double-start race in dashboard-start fallback | resolved |
| [RVW-020](#rvw-020) | major | interfaces | Legacy dashboard serves mutations regardless of bind host | resolved |
| [RVW-021](#rvw-021) | major | interfaces | `cli.main()` is a ~1,200-line dispatch chain; inconsistent errors | resolved |

Minor findings are tracked in the [minor register](#minor-register) without
individual sections.

## Major issues

### RVW-001

**Severity:** critical · **Area:** methodology · **Status:** resolved

`EMA_V7_CLASSIFICATIONS` (`src/ats_lab/hpo.py:45-78`, applied at `:366-373`)
maps frozen trial numbers to verdicts ("likely_overfit", "validation_candidate")
with metric prose, and is silently applied to any future Optuna study named
`EmaConvictionTrendV7_*` when no explicit mapping is passed. A re-run study
reusing those trial numbers inherits stale conclusions that then flow into
`schedule_hpo_validations`. Verdicts detached from evidence.

Resolution direction: remove the hardcoded map or require an explicit
operator-supplied mapping file per study; never infer classifications from
study-name prefix.

### RVW-002

**Severity:** major · **Area:** methodology · **Status:** resolved

No multiple-comparison control anywhere in the repository (no Bonferroni,
Holm, Benjamini-Hochberg, SPA, or deflated Sharpe). Cohorts mint up to 25
concepts per batch (`batch_synthesis.py:354`, `resources.py:83`), each entry
rule tested at raw α=0.05 (`synthesis.py:273-274`). Family-wise/FDR false
positive rate across a campaign is uncontrolled — the single biggest
statistical gap.

Resolution direction: choose and implement a correction policy across the
cohort family (e.g., Holm or BH within cohort, documented in the evaluation
protocol), with the effective threshold surfaced in gate findings.

### RVW-003

**Severity:** major · **Area:** methodology · **Status:** resolved

Optional stopping via "latest p wins": `_latest_p_value`
(`synthesis.py:196-206`) orders by `finished_at DESC` and takes the newest;
`:293-295` uses it for baseline readiness. Re-running significance until
p<0.05 flips the baseline to ready. No test-count ledger, no alpha-spending,
no retest penalty.

Resolution direction: immutable first-test semantics per entry fingerprint
(or an explicit alpha-spending ledger counting every finished significance
test against the same hypothesis).

### RVW-004

**Severity:** major · **Area:** methodology · **Status:** resolved

`hpo_candidate` is not deterministically gated: `supervisor.py:1009-1035`
forces REJECT only on `gates.failed`; `gates.missing` does not block an
agent-assigned `hpo_candidate`, and unlike `paper_trade_candidate` it never
runs `evaluate_promotion`. The documented HPO-candidate criteria
(`docs/jesse/BACKTEST_EVALUATION_PROTOCOL.md:110-122`) are not enforced in
code. An expensive optimization cycle can be unlocked by the analyzer's
say-so.

Resolution direction: deterministic `evaluate_hpo_candidate` gate mirroring
the protocol criteria (activity floor per window, multi-window positivity,
no single dominant route); missing evidence ⇒ `inconclusive`.

### RVW-005

**Severity:** major · **Area:** methodology · **Status:** resolved

Cost-stress is self-reported and dilutable: no code path creates a
fee-stressed run (only fee forwarding exists, `direct_mcp_executor.py:1042`),
`cost_stress_status` is accepted from run metrics on trust
(`evidence.py:322-324`), and `evaluate_gates` counts merely having fees
recorded as passing `fees_cost_sensitivity` (`gates.py:301-302`). The
protocol's "collapses under 2x fee sensitivity ⇒ reject"
(`BACKTEST_EVALUATION_PROTOCOL.md:94`) has no machine-checked counterpart.

Resolution direction: generate actual stressed runs (e.g., 2x fee multiplier
route variants through Jesse) and compute the status from their evidence;
gates consume only machine-generated stress results.

### RVW-006

**Severity:** major · **Area:** methodology · **Status:** resolved

OOS status is a trusted label outside the HPO flow: `gates.py:76-83` accepts
any row tagged `evidence_split="oos"` into the promotion lane; no date
comparison against training routes occurs at gate time. Disjointness is only
enforced when routes enter via `configure_hpo_validation_routes`. A
mislabeled or lucky-window OOS run satisfies promotion.

Resolution direction: verify split disjointness from route dates at gate time
for all promotion lanes, not just configured validation routes.

### RVW-007

**Severity:** major · **Area:** methodology · **Status:** resolved

Inconclusive semantics contradict between paths: `database.py:2895-2897`
archives dependent baselines on p≤0.10 (`significance_inconclusive`) while
`synthesis.py:296-297` keeps them `scheduled`. Docs define inconclusive as
its own verdict, not archive. Same input, two terminal outcomes depending on
which code path observes it.

Resolution direction: pick one semantic (keep-scheduled pending more evidence
vs archive as terminal-inconclusive) and encode it once.

### RVW-008

**Severity:** major · **Area:** data layer · **Status:** resolved

`upsert_work_item` (`database.py:1947-1962`, caller `cli.py:1184`) blindly
overwrites `state`, `attempts`, `blocker_code` with no state guard.
Re-running enqueue on a live contract resets a running/claimed item to
`scheduled`, enabling double execution and duplicate evidence rows — unlike
the guarded `transition_work_item`.

Resolution direction: insert-if-absent plus guarded transitions for existing
rows; refuse or no-op on state regression.

### RVW-009

**Severity:** major · **Area:** data layer · **Status:** resolved

Evidence normalization can silently distort canonical numbers:
parent-metric merge `{**raw_metrics, **child_metrics}` lets experiment-level
aggregates leak into per-route rows when a child omits the metric
(`evidence.py:239`); win-rate unit heuristic (`0 <= x <= 1` ⇒ ×100)
misreads genuine values ≤1.0 stored as percentages (`evidence.py:311-320`).
Also `leverage`/`configured_futures_leverage` alias lists cross-reference
each other's keys (`:150-157`). This is the authoritative store — quiet
corruption here poisons everything downstream.

Resolution direction: explicit provenance rules (never inherit parent
aggregates into route rows), unit-explicit fields instead of heuristics,
disentangled alias lists.

### RVW-010

**Severity:** major · **Area:** data layer · **Status:** resolved

`finalize_batch_evaluation` picks the awaiting work item by
`experiment_id ... ORDER BY updated_at DESC LIMIT 1` rather than identity
(`database.py:2305-2311`); two concurrently-awaiting items for one experiment
can finish the wrong item and strand the other's claim.

Resolution direction: select by work-item identity propagated from the
evaluation context.

### RVW-011

**Severity:** major · **Area:** data layer · **Status:** resolved

Evaluations are delete-before-insert per evaluator (`database.py:2048`,
`:2091`, `:2313`; `schema.sql:170`), destroying verdict history — a research
decision audit gap the events table does not cover — and rendering
`UNIQUE(experiment_id, evaluator, evaluated_at)` dead.

Resolution direction: append-only evaluations with a superseded flag or
sequence column; readers take the latest.

### RVW-012

**Severity:** major · **Area:** data layer · **Status:** resolved

Migration strategy is nominal: single version row (`SCHEMA_VERSION=6`),
inline ALTER dicts whose column definitions duplicate `schema.sql`
(`database.py:120-176`). Fresh vs migrated DBs can silently diverge if one
side is edited; concurrent `initialize()` during upgrade can race on ALTER
TABLE.

Resolution direction: ordered migration list derived from a single schema
source of truth; idempotent guarded DDL; document the concurrency contract.

### RVW-013

**Severity:** major · **Area:** data layer · **Status:** open (next)

`WorkflowDatabase` accreted into a ~2,900-line god class spanning queue, HPO
lifecycle, synthesis leasing, telemetry, and evidence. Worst offenders:
`configure_hpo_validation_routes` (~236 lines, `database.py:1092-1327`),
`schedule_hpo_validations` (~160 lines, `:931-1090`), `complete_hpo_study`
(~120 lines). Visible copy-paste duplication (evaluation delete+insert
triplicated; `_remaining_chain_count` duplicated verbatim at `:2711-2743` vs
`:2794-2825`).

Resolution direction: split along seams (queue, HPO lifecycle, synthesis
leasing, evidence/telemetry) behind the existing public API so callers do not
change.

### RVW-014

**Severity:** major · **Area:** execution · **Status:** resolved

Crash between the replacement-reservation UPDATE and session-id persist
leaves `replacement_reserved=1` with NULL `replacement_session_id` permanently
(`direct_mcp_executor.py:1073-1098`); every retry raises "manual
reconciliation required", which maps to unbounded infrastructure retry, and
no tool in `correctness_recovery.py` clears this state.

Resolution direction: make reservation+persist atomic where possible and add
a recovery command that clears orphaned reservations when no replacement
session exists.

### RVW-015

**Severity:** major · **Area:** execution · **Status:** resolved

`McpClient.post` returns the first SSE `data:` line and never correlates the
JSON-RPC response `id` to the request `id` (`direct_mcp_executor.py:295-328`);
multi-event frames or interleaved notifications can misbind responses. The
client also never sends HTTP DELETE to terminate Streamable HTTP sessions and
re-initializes per attempt (`:275-285`, `:671`), accumulating server-side
sessions.

Resolution direction: response-id correlation with discard-and-wait on
mismatch; DELETE session teardown on terminal/final poll.

### RVW-016

**Severity:** major · **Area:** execution · **Status:** resolved

The versioned JSON schemas (`src/ats_lab/schemas/*.json`) and
`jesse_contracts` types are dead artifacts: nothing loads them (stdlib-only,
no jsonschema), and the documented validation layer
(`docs/jesse-adapter-contract.md:41-42`) does not exist on the execution
path, which emits/validates raw dicts ad hoc.

Resolution direction: either wire minimal hand-rolled validators derived
from the schemas into request-build/result-persist paths, or descope the
schemas and fix the docs to describe the real envelope checks.

### RVW-017

**Severity:** major · **Area:** execution · **Status:** resolved

Harness bugs (KeyError/TypeError/ValueError from malformed lab-built
requests) surface as `direct_mcp_error` infrastructure retries with
`attempt_charged=False` (`direct_mcp_executor.py:863-880` +
`database.py:2586-2616`); infrastructure deferrals never increment attempts —
a persistent harness defect loops forever silently instead of blocking or
routing to operator.

Resolution direction: bound consecutive uncharged infrastructure failures
per work item (circuit-breaker → blocked with blocker code), distinguishing
transport flake from structurally malformed requests.

### RVW-018

**Severity:** major · **Area:** execution · **Status:** resolved

Preflight public-tables check uses exact set equality
(`stack_preflight.py:83-92`); any table added by a routine upstream refresh
(auto-run on every `stack up`, `scripts/jesse-workspace.sh:157-177`)
permanently fails preflight and stalls all research.

Resolution direction: required-tables subset check (plus optional warning on
unknown extras).

### RVW-019

**Severity:** major · **Area:** execution · **Status:** resolved

TOCTOU race in `_start_and_verify` (`direct_mcp_executor.py:882-907`): MCP
start may land asynchronously after `_has_started` is checked, then the
dashboard fallback starts the same session again — double execution risk.

Resolution direction: treat start as idempotent (verify-after-start with
tolerance window) or serialize fallback behind a persisted started-flag.

### RVW-020

**Severity:** major · **Area:** interfaces · **Status:** resolved

Legacy `ats-lab dashboard` serves state-mutating POST
`/api/work-items/{id}/retry|rectify` regardless of bind host
(`dashboard.py:1078-1134`, `:1167-1176`); the loopback gating that exists in
`serve_web` (`web_api.py:938-944`) was never applied here. `--host 0.0.0.0`
exposes unauthenticated mutations (the Confirm header is client-sendable,
not auth). `docs/operator-dashboard.md:3,210` still claims "read-only" /
"no write endpoints".

Resolution direction: apply the same non-loopback mutation gate to the
legacy surface; correct the doc.

### RVW-021

**Severity:** major · **Area:** interfaces · **Status:** resolved

`cli.main()` is a ~1,200-line if/elif dispatch chain (`cli.py:197-1410`);
error handling is inconsistent — `enqueue` raises uncaught ValueError
(traceback, exit 1, `cli.py:1182`), `synthesize` leaves missing/invalid
`--file` uncaught (`:1292-1294`), while sibling commands map error classes
to `parser.error`/exit 2 (`:1105-1113`).

Resolution direction: table-dispatched handlers with a uniform error-mapping
wrapper (contract violations → exit 2 + message; unexpected → traceback).

## Minor register

| ID | Location | Finding |
|---|---|---|
| MIN-01 | `evaluation_windows.py:56-64`, `hpo_routes.py:240-250` | Adjacent windows share boundary dates; strict-inequality overlap permits one-day bleed if backtests include finish_date |
| MIN-02 | `synthesis.py:272` | Significance tested on first route only while baselines sweep many routes |
| MIN-03 | `hpo.py:349-351` | Optuna import identity keyed on resolved path, not content hash — copies mint duplicates, in-place replace silently upserts |
| MIN-04 | `direct_mcp_executor.py:1379` | `json.dumps` default allow_nan persists NaN/Infinity into metrics_json |
| MIN-05 | `agent_launcher.py:247` | Full prompt passed as argv — visible in local process list |
| MIN-06 | `web_api.py:636ff`, `dashboard.py:939ff` | No Host-header validation (DNS-rebinding read risk; mutations still gated) |
| MIN-07 | `schema.sql:48-63,183-190` | Missing indexes: `runs(work_item_id)`, events `(aggregate_type, aggregate_id, occurred_at)` |
| MIN-08 | `pyproject.toml:37-38`, `README.md:150` | Dead `[tool.unittest]` config; README claims `init` creates config skeleton (it does not) |
| MIN-09 | `console.py:738`, `web_api.py:763` | Uncaught `float()` parse crashes console `watch abc`; hpo study detail route missing `unquote` |
| MIN-10 | `dashboard.py:139,359-384`, `status.py:105-142,305` | O(n²) scans, full-evidence re-query per refresh, N+1 per-study queries, repeated `operator_status` per request, fragile `isdigit` heuristic |
| MIN-11 | `research_memory.py:724-731` | urllib follows redirects without stripping Authorization header |
| MIN-12 | `scripts/jesse-workspace.sh:64,174,179-184,203,303` | `stack down` needlessly requires upstream repo; build stdout discarded; sibling-dir git check kills whole status; no `--` separator; awk truncates paths with spaces |
| MIN-13 | `supervisor.py:559-560` | Analyzer retry truncates canonical evidence to half — analysis inputs depend on attempt count |
| MIN-14 | `synthesis.py:68-73` | job_fingerprint mixes in hypothesis prose — wording edits mint new job IDs for identical science |
| MIN-15 | `gates.py:212-231` | Playbook MC interpretation rules (median comparison, worst-5% tail suspicion) not encoded in robustness checks |
| MIN-16 | `evaluation_windows.py:52` | Wall-clock `date.today()` default anchor unpinned — cross-cohort comparability |
| MIN-17 | `gates.py:358-369` | Silent fallback to 50-trade floor on unparseable dates masks malformation |
| MIN-18 | `database.py:596-651` | Synthetic HPO candidate filtering has Python-vs-SQL NULL semantics mismatch; inline remap dict hard to follow |
| MIN-19 | `dashboard.py:1161` | `log_message` prints every request to stdout (pollutes unittest output) |
| MIN-20 | `agent_launcher.py:492-494,324-328,426-435` | Unbounded agent stderr persisted to blocker_detail; unsynchronized telemetry appends; hardcoded timeout window duplicating ResourcePolicy invariant |
| MIN-21 | `stack_preflight.py:162-179` | MCP probe accepts any HTTP 200 without JSON-RPC body validation; probe session left open; dead status>=400 branch; no candle-data presence check |
| MIN-22 | `direct_mcp_executor.py:608-610,486-487,688-694,830-843,920-1024,1452-1469,427,370-384` | Dead overall-outcome logic; O(n²) delegated split; draft checkpoint failure orphans Jesse session (no cancel tool); zombie recovery blind restart; ~50-line create duplication; monte_carlo excluded from direct path despite doc claim; fallback rereads live global config; no token-expiry re-auth |
| MIN-23 | `cli.py:217-260,1239-1246` | Duplicated memory command families; intentional double `get_backtest_session` needs comment |
| MIN-24 | `.gitignore` | No `.DS_Store` / coverage-output entries |
