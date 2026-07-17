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
    error_json TEXT,
    started_at TEXT,
    finished_at TEXT,
    source_path TEXT,
    UNIQUE(session_id)
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

CREATE TABLE IF NOT EXISTS migration_sources (
    path TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    record_count INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_work_items_queue ON work_items(state, priority, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_experiment ON runs(experiment_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_verdict ON evaluations(verdict, evaluated_at);

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
