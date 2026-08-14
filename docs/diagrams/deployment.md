# Deployment model

The laboratory runs as local processes on a single research host with a
SQLite file database; the Jesse backtesting engine runs in Docker. All
service endpoints bind to loopback. An optional agent executor and memory
provider sit beside the core; a scheduler or CI may invoke the CLI.

```mermaid
flowchart TB
    subgraph Host["Research host"]
        subgraph Local["Local processes"]
            CLI["ats-lab CLI / TUI / web dashboard"]
            WRK["supervisor + continuous worker"]
            DB[("SQLite<br/>laboratory.sqlite3<br/>(not version controlled)")]
        end

        subgraph Docker["Docker"]
            JES["Jesse engine<br/>backtest sessions"]
        end

        subgraph Loopback["Loopback services"]
            JDASH["Jesse dashboard · 127.0.0.1:9000"]
            JMCP["Jesse MCP · 127.0.0.1:9002"]
            MEM["Memory provider · 127.0.0.1:18000"]
            WEB["Web dashboard · 127.0.0.1:8765"]
        end

        AGENT["Agent executor<br/>optional subprocess"]
    end

    subgraph External["External"]
        UP["Jesse engine upstream<br/>public template (fork)"]
        SCHED["Scheduler / CI"]
    end

    CLI --> DB
    CLI --> WEB
    WRK --> DB
    WRK --> JMCP
    JMCP --> JES
    JDASH --> JES
    WRK -. optional .-> AGENT
    WRK -. optional .-> MEM
    UP -. build image .-> JES
    SCHED --> CLI
```

## Data placement

- SQLite stores experiment specifications, work items, runs, evaluations,
  artifacts, events and synthesis cohorts — never candle data or strategy
  source.
- The Jesse workspace owns strategy source and candle data; the engine image
  is built from the public upstream template.
- Optional memory storage holds durable preferences and conclusions only.
