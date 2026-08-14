# Data flow

One research-loop iteration: a typed strategy idea becomes deterministic queue
jobs, backtests run inside the Jesse engine, normalized evidence feeds
deterministic gates, and a verdict routes the candidate to rejection,
revision, HPO, or promotion.

```mermaid
sequenceDiagram
    autonumber
    participant OP as Operator / research agent
    participant SYN as Synthesis
    participant DB as SQLite laboratory
    participant WRK as Continuous worker
    participant JES as Jesse engine (Docker)
    participant EV as Evaluation and gates
    participant HPO as HPO and routes
    participant MEM as Memory provider (optional)

    OP->>SYN: typed strategy idea (JSON contract)
    SYN->>SYN: normalize entry rule · SHA-256 fingerprint
    SYN->>DB: enqueue entry-significance job
    DB-->>SYN: queued (idempotent)
    WRK->>DB: claim work item (transactional)
    WRK->>JES: dispatch backtest session (Jesse MCP)
    JES-->>WRK: run result and metrics
    WRK->>DB: persist normalized evidence
    DB->>EV: evidence ready
    EV->>EV: deterministic gates (p-value · OOS · cost-stress)
    EV-->>DB: verdict (reject · revise · inconclusive · hpo_candidate · paper_trade_candidate)

    alt hpo_candidate
        EV->>HPO: import optimizer trials
        HPO->>DB: route plan and HPO state
        HPO->>JES: parameterized batch backtests
        JES-->>HPO: trial metrics
        HPO-->>DB: results re-enter evidence
    end

    Note over DB,EV: synthesis replenishes the cohort at low watermark<br/>five new concepts and twenty controlled improvements
    WRK->>MEM: sync durable learnings (outbox, optional)
```

## Gate behavior

- `p_value < 0.05`: baseline becomes ready.
- `0.05 <= p_value <= 0.10`: baseline stays scheduled as inconclusive.
- `p_value > 0.10`: baseline is archived.
- Promotion to `paper_trade_candidate` requires positive out-of-sample or
  rolling evidence and an explicit passing cost-stress result. Missing
  validation is `inconclusive`; failed validation is `reject`.

## State machines

Work state: `scheduled -> ready -> running -> finished`, with retry, failure
evidence, and operator-requirement branches.

Verdicts: `reject | revise | inconclusive | hpo_candidate | paper_trade_candidate`.

Execution completion and research quality are intentionally independent:
poor metrics and terminal harness failures both reach evaluation.
