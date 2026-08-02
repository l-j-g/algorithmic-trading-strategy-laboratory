PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS strategies (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    source_path TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    strategy_id INTEGER REFERENCES strategies(id),
    experiment_type TEXT NOT NULL,
    hypothesis TEXT NOT NULL DEFAULT '',
    archetype TEXT NOT NULL DEFAULT '',
    target_regime TEXT NOT NULL DEFAULT '',
    failure_regime TEXT NOT NULL DEFAULT '',
    specification_json TEXT NOT NULL,
    parent_experiment_id TEXT REFERENCES experiments(id),
    source_path TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_items (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    priority INTEGER NOT NULL DEFAULT 999,
    state TEXT NOT NULL CHECK (state IN ('scheduled','ready','running','waiting_retry','blocked','finished','archived')),
    dependencies_json TEXT NOT NULL DEFAULT '[]',
    attempts INTEGER NOT NULL DEFAULT 0,
    retry_after TEXT,
    blocker_code TEXT,
    blocker_detail TEXT,
    specification_json TEXT NOT NULL DEFAULT '{}',
    claimed_by TEXT,
    claimed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    work_item_id TEXT REFERENCES work_items(id),
    session_id TEXT,
    status TEXT NOT NULL,
    route_json TEXT,
    dashboard_url TEXT,
    metrics_json TEXT,
    raw_result_json TEXT,
    error_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    source_path TEXT,
    UNIQUE(session_id)
);

CREATE TABLE IF NOT EXISTS direct_execution_sessions (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id),
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    session_id TEXT NOT NULL UNIQUE,
    request_fingerprint TEXT NOT NULL,
    state TEXT NOT NULL,
    metrics_json TEXT,
    error_text TEXT,
    first_observed_at TEXT,
    last_observed_at TEXT,
    last_jesse_updated_at TEXT,
    last_progress REAL,
    unchanged_observations INTEGER NOT NULL DEFAULT 0,
    recovery_attempted INTEGER NOT NULL DEFAULT 0,
    replacement_created INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS direct_execution_recoveries (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id),
    old_session_id TEXT NOT NULL UNIQUE,
    old_state TEXT NOT NULL,
    reason TEXT NOT NULL,
    replacement_allowed INTEGER NOT NULL CHECK (replacement_allowed IN (0,1)),
    replacement_reserved INTEGER NOT NULL DEFAULT 0 CHECK (replacement_reserved IN (0,1)),
    replacement_session_id TEXT UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS direct_execution_telemetry (
    id INTEGER PRIMARY KEY,
    work_item_id TEXT,
    outcome TEXT NOT NULL,
    mcp_call_count INTEGER NOT NULL,
    model_call_count INTEGER NOT NULL DEFAULT 0,
    request_bytes INTEGER NOT NULL,
    response_bytes INTEGER NOT NULL,
    poll_count INTEGER NOT NULL,
    recorded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS direct_strategy_preparations (
    work_item_id TEXT PRIMARY KEY REFERENCES work_items(id),
    request_fingerprint TEXT NOT NULL,
    prepared_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS normalized_evidence (
    evidence_key TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    strategy TEXT,
    strategy_version TEXT,
    lifecycle_stage TEXT,
    verdict TEXT,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    run_id TEXT REFERENCES runs(id),
    session_id TEXT,
    symbol TEXT,
    timeframe TEXT,
    start_date TEXT,
    finish_date TEXT,
    evidence_split TEXT,
    net_profit_percentage REAL,
    max_drawdown_percentage REAL,
    sharpe_ratio REAL,
    sortino_ratio REAL,
    calmar_ratio REAL,
    profit_factor REAL,
    win_rate REAL,
    trade_count INTEGER,
    fees REAL,
    expectancy REAL,
    leverage REAL,
    risk_per_trade_percentage REAL,
    optimizer_objective TEXT,
    cost_stress_status TEXT,
    significance_p_value REAL,
    completed_at TEXT,
    finding TEXT,
    next_action TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluations (
    id INTEGER PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    verdict TEXT NOT NULL CHECK (verdict IN ('reject','revise','hpo_candidate','paper_trade_candidate','inconclusive','infrastructure_failure','pass')),
    summary TEXT NOT NULL DEFAULT '',
    metrics_summary TEXT NOT NULL DEFAULT '',
    next_step TEXT NOT NULL DEFAULT '',
    gate_results_json TEXT NOT NULL DEFAULT '[]',
    evaluator TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    UNIQUE(experiment_id, evaluator, evaluated_at)
);

CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY,
    experiment_id TEXT REFERENCES experiments(id),
    artifact_type TEXT NOT NULL,
    path TEXT NOT NULL,
    content_hash TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(experiment_id, artifact_type, path)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    aggregate_type TEXT NOT NULL,
    aggregate_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS synthesis_cohorts (
    id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('planning','active','drained','failed')),
    requested_count INTEGER NOT NULL,
    generated_count INTEGER NOT NULL DEFAULT 0,
    remaining_at_trigger INTEGER NOT NULL,
    planned_by TEXT NOT NULL,
    lease_expires_at TEXT,
    failure_detail TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS synthesis_cohort_chains (
    cohort_id TEXT NOT NULL REFERENCES synthesis_cohorts(id),
    slot INTEGER NOT NULL,
    lane TEXT NOT NULL CHECK (lane IN ('new_concept','improvement')),
    source_experiment_id TEXT,
    work_item_ids_json TEXT NOT NULL,
    PRIMARY KEY (cohort_id, slot)
);

CREATE TABLE IF NOT EXISTS migration_sources (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS operator_control (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    desired_state TEXT NOT NULL CHECK (desired_state IN ('running','paused','stop_requested')),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL
);

INSERT OR IGNORE INTO operator_control(id, desired_state, updated_at, updated_by)
VALUES (1, 'running', strftime('%Y-%m-%dT%H:%M:%fZ', 'now'), 'system');

CREATE TABLE IF NOT EXISTS supervisor_runtime (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    worker_id TEXT NOT NULL,
    process_id INTEGER NOT NULL,
    phase TEXT NOT NULL,
    batch_id TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS hpo_studies (
    id TEXT PRIMARY KEY,
    study_name TEXT NOT NULL,
    strategy TEXT,
    parent_experiment_id TEXT REFERENCES experiments(id),
    parent_work_item_id TEXT REFERENCES work_items(id),
    hpo_experiment_id TEXT REFERENCES experiments(id),
    hpo_work_item_id TEXT REFERENCES work_items(id),
    lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN (
        'hpo_candidate','hpo_scheduled','hpo_running','hpo_analysis',
        'validation','paper_trade_candidate','revise','reject'
    )),
    objective_name TEXT,
    direction TEXT CHECK (direction IN ('maximize','minimize')),
    source_database_path TEXT,
    source_study_id INTEGER,
    trial_count INTEGER NOT NULL DEFAULT 0,
    completed_trial_count INTEGER NOT NULL DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source_database_path, source_study_id)
);

CREATE TABLE IF NOT EXISTS hpo_trials (
    id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES hpo_studies(id) ON DELETE CASCADE,
    trial_number INTEGER NOT NULL,
    state TEXT NOT NULL,
    objective_value REAL,
    started_at TEXT,
    completed_at TEXT,
    duration_ms INTEGER,
    params_json TEXT NOT NULL DEFAULT '{}',
    user_attrs_json TEXT NOT NULL DEFAULT '{}',
    system_attrs_json TEXT NOT NULL DEFAULT '{}',
    evidence_run_id TEXT REFERENCES runs(id),
    imported_at TEXT NOT NULL,
    UNIQUE(study_id, trial_number)
);

CREATE TABLE IF NOT EXISTS hpo_selected_trials (
    study_id TEXT NOT NULL REFERENCES hpo_studies(id) ON DELETE CASCADE,
    trial_id TEXT NOT NULL REFERENCES hpo_trials(id) ON DELETE CASCADE,
    rank INTEGER,
    classification TEXT NOT NULL CHECK (classification IN (
        'likely_overfit','validation_candidate','selected','not_selected'
    )),
    selection_reason TEXT NOT NULL DEFAULT '',
    selected_at TEXT NOT NULL,
    PRIMARY KEY(study_id, trial_id)
);

CREATE TABLE IF NOT EXISTS hpo_proposed_defaults (
    study_id TEXT NOT NULL REFERENCES hpo_studies(id) ON DELETE CASCADE,
    parameter_name TEXT NOT NULL,
    value_json TEXT NOT NULL,
    source_trial_id TEXT REFERENCES hpo_trials(id),
    rationale TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY(study_id, parameter_name)
);

CREATE TABLE IF NOT EXISTS hpo_narrowed_ranges (
    study_id TEXT NOT NULL REFERENCES hpo_studies(id) ON DELETE CASCADE,
    parameter_name TEXT NOT NULL,
    low_value REAL,
    high_value REAL,
    step_value REAL,
    logarithmic INTEGER,
    distribution_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY(study_id, parameter_name)
);

CREATE TABLE IF NOT EXISTS hpo_validation_jobs (
    id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES hpo_studies(id) ON DELETE CASCADE,
    trial_id TEXT NOT NULL REFERENCES hpo_trials(id),
    experiment_id TEXT REFERENCES experiments(id),
    work_item_id TEXT REFERENCES work_items(id),
    evidence_split TEXT CHECK (evidence_split IN ('train','holdout','oos','rolling')),
    state TEXT NOT NULL,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE(study_id, trial_id, evidence_split)
);

CREATE TABLE IF NOT EXISTS hpo_analysis_jobs (
    id TEXT PRIMARY KEY,
    study_id TEXT NOT NULL REFERENCES hpo_studies(id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK (state IN (
        'pending','running','waiting_retry','abandoned','completed','terminal'
    )),
    attempts INTEGER NOT NULL DEFAULT 0,
    cohort_id TEXT,
    claimed_by TEXT,
    claimed_at TEXT,
    retry_after TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS hpo_dispositions (
    study_id TEXT PRIMARY KEY REFERENCES hpo_studies(id) ON DELETE CASCADE,
    disposition TEXT NOT NULL CHECK (disposition IN (
        'paper_trade_candidate','revise','reject'
    )),
    finding TEXT NOT NULL,
    next_action TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS work_item_stage_timings (
    id INTEGER PRIMARY KEY,
    work_item_id TEXT NOT NULL REFERENCES work_items(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    state TEXT NOT NULL,
    analyzer_attempt INTEGER,
    cohort_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    outcome TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(work_item_id, stage, analyzer_attempt, started_at)
);

CREATE INDEX IF NOT EXISTS idx_work_items_queue ON work_items(state, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_experiment ON runs(experiment_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_verdict ON evaluations(verdict, evaluated_at);
CREATE INDEX IF NOT EXISTS idx_synthesis_cohorts_status ON synthesis_cohorts(status, created_at);
CREATE INDEX IF NOT EXISTS idx_normalized_evidence_experiment
ON normalized_evidence(experiment_id);
CREATE INDEX IF NOT EXISTS idx_normalized_evidence_run
ON normalized_evidence(run_id);
CREATE INDEX IF NOT EXISTS idx_normalized_evidence_verdict
ON normalized_evidence(verdict, completed_at);
CREATE INDEX IF NOT EXISTS idx_normalized_evidence_compatibility
ON normalized_evidence(symbol, timeframe, start_date, finish_date, evidence_split);
CREATE INDEX IF NOT EXISTS idx_normalized_evidence_lifecycle
ON normalized_evidence(lifecycle_stage, completed_at);
CREATE INDEX IF NOT EXISTS idx_hpo_studies_lifecycle
ON hpo_studies(lifecycle_state, updated_at);
CREATE INDEX IF NOT EXISTS idx_hpo_trials_study
ON hpo_trials(study_id, state, objective_value);
CREATE INDEX IF NOT EXISTS idx_hpo_analysis_queue
ON hpo_analysis_jobs(state, retry_after, created_at);
CREATE INDEX IF NOT EXISTS idx_work_item_stage_timings_recent
ON work_item_stage_timings(started_at, work_item_id);

CREATE VIEW IF NOT EXISTS active_queue AS
SELECT w.id, w.experiment_id, s.name AS strategy, w.priority, w.state,
       w.attempts, w.retry_after, w.blocker_code, w.blocker_detail, w.created_at
FROM work_items w
LEFT JOIN experiments e ON e.id = w.experiment_id
LEFT JOIN strategies s ON s.id = e.strategy_id
WHERE w.state IN ('scheduled','ready','running','waiting_retry','blocked');

CREATE VIEW IF NOT EXISTS candidate_summary AS
SELECT e.id AS experiment_id, s.name AS strategy, ev.verdict, ev.summary,
       ev.metrics_summary, ev.next_step, ev.evaluated_at,
       COUNT(r.id) AS run_count,
       SUM(CASE WHEN r.status = 'finished' THEN 1 ELSE 0 END) AS finished_runs
FROM evaluations ev
JOIN experiments e ON e.id = ev.experiment_id
LEFT JOIN strategies s ON s.id = e.strategy_id
LEFT JOIN runs r ON r.experiment_id = e.id
WHERE ev.verdict IN ('hpo_candidate','paper_trade_candidate','revise')
GROUP BY e.id, s.name, ev.verdict, ev.summary, ev.metrics_summary, ev.next_step, ev.evaluated_at;
