# Architecture

High-level component view of the Algorithmic Trading Strategy Laboratory.
Typed JSON contracts flow from the interface layers into a transactional
queue; execution adapters isolate the backtesting framework; deterministic
gates and evaluation produce research verdicts.

```mermaid
flowchart LR
    subgraph Interface["Interface layers"]
        CLI["CLI<br/>one bounded operation per invocation<br/>JSON in / JSON out"]
        TUI["Terminal UI<br/>operator console"]
        WEB["Web dashboard<br/>loopback only"]
    end

    subgraph Core["Laboratory core"]
        SYN["Synthesis<br/>typed ideas to deterministic queue jobs<br/>idempotent · entry fingerprint (SHA-256)"]
        SUP["Supervisor<br/>batch-first execution<br/>isolated analysis<br/>cohort replenishment"]
        WRK["Continuous worker<br/>agent-neutral dispatch boundary"]
        EVAL["Evaluation and gates<br/>deterministic gates to verdicts<br/>promotion requires OOS and cost-stress"]
        HPO["HPO imports and routes<br/>read-only optimizer trials"]
        DB[("SQLite laboratory<br/>queue · evidence · evaluations<br/>artifacts · HPO state · cohorts")]
    end

    subgraph Adapters["Execution adapters"]
        MCP["Jesse MCP executor<br/>streamable HTTP<br/>mechanical draft / start / poll / fetch"]
        CMD["Command dispatcher<br/>JSON stdin to stdout subprocess"]
        AGENT["Agent executor<br/>optional · model-backed<br/>strategy preparation"]
    end

    subgraph External["External systems"]
        JESSE["Jesse engine<br/>Docker · backtest sessions<br/>owns strategy source and candle data"]
        MEM["Memory provider<br/>optional · durable conclusions only"]
        SCHED["Scheduler / CI / research agent"]
    end

    SCHED --> CLI
    CLI --> SYN
    SYN --> DB
    SUP --> DB
    SUP --> WRK
    WRK --> DB
    DB --> EVAL
    EVAL --> HPO
    HPO --> DB
    WRK --> MCP
    WRK --> CMD
    WRK --> AGENT
    MCP --> JESSE
    CMD --> JESSE
    AGENT --> MEM
    SUP -. optional .-> MEM
    TUI --> DB
    WEB --> DB
```

## Ownership boundaries

- The laboratory owns specifications, queue state, evidence, evaluations and views.
- Execution adapters own communication with the backtesting framework.
- The continuous worker claims work and calls an external JSON adapter; it
  does not contain backtester or agent operations.
- A memory provider may store durable preferences and conclusions, never
  locks or authoritative experiment results.
- Markdown and HTML are output formats, not operational databases.
