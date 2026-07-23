# Operator dashboard

The local dashboard is a read-only view of the SQLite laboratory database. It
shows active work, running and blocked counts, research candidates, run history,
metrics and dashboard links. It refreshes automatically every five seconds while
the tab is visible; the header controls can pause it or select 15/30 seconds.

Start it from the repository root:

```bash
python3 -m ats_lab.dashboard
```

Open <http://127.0.0.1:8765>. Use a different database or port when needed:

```bash
python3 -m ats_lab.dashboard \
  --database .ats-lab/laboratory.sqlite3 \
  --host 127.0.0.1 \
  --port 9001
```

Views support free-text search, page-specific filters and safe, predefined sort
orders:

- **Active queue** — future and actionable work, including worker claims,
  retries and blocker details.
- **Candidates** — HPO, paper-trade and revision candidates with evaluation
  summaries and run counts.
- **Run history** — sessions, status, normalized metrics, errors and links to
  execution dashboards.
- **Overview** — live queue/candidate counts and a horizontal chart of the top
  20 finished backtests. Rank by Sharpe, net profit, Calmar, profit factor,
  drawdown or expectancy, and set a minimum trade count to suppress tiny samples.

Run history promotes common Jesse metric aliases into readable columns. The
original payload remains available under the `raw` disclosure for diagnosis.

Read-only JSON endpoints are available for scripts and alternate frontends:

- `/api/summary`
- `/api/top-backtests?metric=sharpe&limit=20&minimum_trades=20`
- `/api/queue`, `/api/candidates`, `/api/runs` (accept the same filters as pages)
- `/api/synthesis-status` (remaining chains plus latest cohort lease/failure state)

The server has no write endpoints. Queries use bound parameters and predefined
sort expressions; database content is HTML-escaped before rendering. Responses
disable caching and include restrictive browser security headers.

The default bind address is loopback-only. Do not bind it to `0.0.0.0` on an
untrusted network: the server has no authentication and research evidence may be
sensitive.

Stop it with `Ctrl-C`.
