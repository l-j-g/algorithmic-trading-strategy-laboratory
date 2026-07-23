"""Read-only local operator dashboard backed by the laboratory database."""
from __future__ import annotations

import argparse
import html
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .database import WorkflowDatabase


PAGE_SPECS = {
    "queue": {
        "title": "Active queue",
        "filters": {"state": ("scheduled", "ready", "running", "waiting_retry", "blocked")},
        "sorts": {
            "priority": "w.priority ASC, w.created_at ASC",
            "newest": "w.created_at DESC",
            "updated": "w.updated_at DESC",
            "strategy": "s.name COLLATE NOCASE ASC, w.priority ASC",
        },
        "default_sort": "priority",
        "sql": """SELECT w.id, w.experiment_id, s.name AS strategy, w.priority, w.state,
                    w.attempts, w.claimed_by, w.retry_after, w.blocker_code,
                    w.blocker_detail, w.updated_at
                 FROM work_items w
                 JOIN experiments e ON e.id=w.experiment_id
                 LEFT JOIN strategies s ON s.id=e.strategy_id
                 WHERE w.state IN ('scheduled','ready','running','waiting_retry','blocked')""",
        "search": ("w.id", "w.experiment_id", "s.name", "w.blocker_code", "w.blocker_detail"),
    },
    "candidates": {
        "title": "Candidates",
        "filters": {"verdict": ("hpo_candidate", "paper_trade_candidate", "revise")},
        "sorts": {
            "newest": "ev.evaluated_at DESC",
            "strategy": "s.name COLLATE NOCASE ASC, ev.evaluated_at DESC",
            "runs": "run_count DESC, ev.evaluated_at DESC",
            "verdict": "ev.verdict ASC, ev.evaluated_at DESC",
        },
        "default_sort": "newest",
        "sql": """SELECT e.id AS experiment_id, s.name AS strategy, ev.verdict,
                    ev.summary, ev.metrics_summary, ev.next_step, ev.evaluated_at,
                    COUNT(r.id) AS run_count,
                    SUM(CASE WHEN r.status='finished' THEN 1 ELSE 0 END) AS finished_runs
                 FROM evaluations ev
                 JOIN experiments e ON e.id=ev.experiment_id
                 LEFT JOIN strategies s ON s.id=e.strategy_id
                 LEFT JOIN runs r ON r.experiment_id=e.id
                 WHERE ev.verdict IN ('hpo_candidate','paper_trade_candidate','revise')""",
        "search": ("e.id", "s.name", "ev.summary", "ev.metrics_summary", "ev.next_step"),
        "group": " GROUP BY ev.id, e.id, s.name",
    },
    "runs": {
        "title": "Run history",
        "filters": {"status": ("draft", "running", "finished", "stopped", "terminated")},
        "sorts": {
            "newest": "COALESCE(r.finished_at, r.started_at) DESC",
            "strategy": "s.name COLLATE NOCASE ASC, r.started_at DESC",
            "status": "r.status ASC, r.started_at DESC",
        },
        "default_sort": "newest",
        "sql": """SELECT r.id, r.experiment_id, s.name AS strategy, r.session_id,
                    r.status, r.dashboard_url, r.metrics_json, r.error_json,
                    r.started_at, r.finished_at
                 FROM runs r
                 JOIN experiments e ON e.id=r.experiment_id
                 LEFT JOIN strategies s ON s.id=e.strategy_id
                 WHERE 1=1""",
        "search": ("r.id", "r.experiment_id", "r.session_id", "s.name", "r.metrics_json", "r.error_json"),
    },
}

BACKTEST_TYPES = ("baseline", "multi_window", "cost_sensitivity", "out_of_sample", "harness_check", "hpo", "monte_carlo")
METRIC_ALIASES = {
    "net_profit_percentage": ("net_profit_percentage", "net_profit_pct", "net_profit_percent"),
    "sharpe_ratio": ("sharpe_ratio", "sharpe"),
    "sortino_ratio": ("sortino_ratio", "sortino"),
    "calmar_ratio": ("calmar_ratio", "calmar"),
    "profit_factor": ("profit_factor", "gross_profit_loss_ratio"),
    "max_drawdown": ("max_drawdown", "max_drawdown_percentage", "max_drawdown_pct"),
    "total_trades": ("total_trades", "trade_count", "total", "trades"),
    "win_rate": ("win_rate", "win_rate_percentage"),
    "expectancy": ("expectancy", "expectancy_percentage"),
    "fees": ("fees", "total_fees", "fee"),
}
RANK_METRICS = {
    "sharpe": ("sharpe_ratio", True),
    "net_profit": ("net_profit_percentage", True),
    "calmar": ("calmar_ratio", True),
    "profit_factor": ("profit_factor", True),
    "drawdown": ("max_drawdown", False),
    "expectancy": ("expectancy", True),
}


def normalize_metrics(value: object) -> dict[str, float | None]:
    payload: dict = {}
    if isinstance(value, dict):
        payload = value
    elif isinstance(value, str) and value.strip():
        try:
            decoded = json.loads(value)
            payload = decoded if isinstance(decoded, dict) else {}
        except json.JSONDecodeError:
            payload = {}
    normalized: dict[str, float | None] = {}
    for canonical, aliases in METRIC_ALIASES.items():
        result = None
        for alias in aliases:
            raw = payload.get(alias)
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and math.isfinite(float(raw)):
                result = float(raw)
                break
        normalized[canonical] = result
    return normalized


def top_backtests(database: WorkflowDatabase, metric: str = "sharpe", limit: int = 20,
                  minimum_trades: int = 0, *, symbol: str = "", period: str = "",
                  timeframe: str = "", experiment_type: str = "") -> list[dict]:
    metric = metric if metric in RANK_METRICS else "sharpe"
    limit = max(1, min(int(limit), 100))
    minimum_trades = max(0, min(int(minimum_trades), 1_000_000))
    placeholders = ",".join("?" for _ in BACKTEST_TYPES)
    rows = database.rows(
        f"""SELECT r.id, r.experiment_id, e.experiment_type, s.name AS strategy, r.session_id,
                   r.dashboard_url, r.route_json, r.metrics_json, r.finished_at
            FROM runs r JOIN experiments e ON e.id=r.experiment_id
            LEFT JOIN strategies s ON s.id=e.strategy_id
            WHERE r.status='finished' AND e.experiment_type IN ({placeholders})
            ORDER BY r.finished_at DESC LIMIT 2000""",
        BACKTEST_TYPES,
    )
    key, higher_is_better = RANK_METRICS[metric]
    ranked = []
    for row in rows:
        metrics = normalize_metrics(row["metrics_json"])
        score = metrics[key]
        trades = metrics["total_trades"]
        if score is None or trades is None or trades < minimum_trades:
            continue
        route = {}
        try:
            route = json.loads(row["route_json"] or "{}")
            if not isinstance(route, dict):
                route = {}
        except json.JSONDecodeError:
            pass
        row_period = f'{route.get("start_date", "")} to {route.get("finish_date", "")}'
        if symbol and route.get("symbol") != symbol:
            continue
        if period and row_period != period:
            continue
        if timeframe and route.get("timeframe") != timeframe:
            continue
        if experiment_type and row["experiment_type"] != experiment_type:
            continue
        ranked.append({
            "id": row["id"], "experiment_id": row["experiment_id"], "strategy": row["strategy"],
            "experiment_type": row["experiment_type"],
            "session_id": row["session_id"], "dashboard_url": row["dashboard_url"],
            "symbol": route.get("symbol"), "timeframe": route.get("timeframe"),
            "start_date": route.get("start_date"), "finish_date": route.get("finish_date"),
            "finished_at": row["finished_at"], "score": score, "rank_metric": metric,
            **metrics,
        })
    if metric == "drawdown":
        ranked.sort(key=lambda row: abs(row["score"]))
    else:
        ranked.sort(key=lambda row: row["score"], reverse=higher_is_better)
    return ranked[:limit]


def comparison_options(database: WorkflowDatabase) -> dict[str, list[str]]:
    placeholders = ",".join("?" for _ in BACKTEST_TYPES)
    rows = database.rows(
        f"""SELECT r.route_json, e.experiment_type FROM runs r
            JOIN experiments e ON e.id=r.experiment_id
            WHERE r.status='finished' AND r.metrics_json IS NOT NULL
              AND e.experiment_type IN ({placeholders})""", BACKTEST_TYPES,
    )
    result: dict[str, set[str]] = {"symbols": set(), "periods": set(), "timeframes": set(), "experiment_types": set()}
    for row in rows:
        try:
            route = json.loads(row["route_json"] or "{}")
        except json.JSONDecodeError:
            route = {}
        if not isinstance(route, dict):
            continue
        if route.get("symbol"):
            result["symbols"].add(route["symbol"])
        if route.get("timeframe"):
            result["timeframes"].add(route["timeframe"])
        if route.get("start_date") and route.get("finish_date"):
            result["periods"].add(f'{route["start_date"]} to {route["finish_date"]}')
        result["experiment_types"].add(row["experiment_type"])
    return {key: sorted(values) for key, values in result.items()}


def query_page(database: WorkflowDatabase, page: str, params: dict[str, str]) -> tuple[list[dict], dict]:
    """Query a page using only whitelisted filters and sort expressions."""
    spec = PAGE_SPECS.get(page, PAGE_SPECS["queue"])
    clauses: list[str] = []
    values: list[str] = []
    clean: dict[str, str] = {}
    search = params.get("q", "").strip()[:200]
    if search:
        clauses.append("(" + " OR ".join(f"{column} LIKE ?" for column in spec["search"]) + ")")
        values.extend([f"%{search}%"] * len(spec["search"]))
        clean["q"] = search
    for name, allowed in spec["filters"].items():
        value = params.get(name, "")
        if value in allowed:
            column = {"state": "w.state", "verdict": "ev.verdict", "status": "r.status"}[name]
            clauses.append(f"{column} = ?")
            values.append(value)
            clean[name] = value
    sort = params.get("sort", spec["default_sort"])
    if sort not in spec["sorts"]:
        sort = spec["default_sort"]
    clean["sort"] = sort
    sql = spec["sql"] + (" AND " + " AND ".join(clauses) if clauses else "")
    sql += spec.get("group", "") + " ORDER BY " + spec["sorts"][sort] + " LIMIT 500"
    rows = database.rows(sql, tuple(values))
    if page == "runs":
        display_rows = []
        for row in rows:
            metrics = normalize_metrics(row.get("metrics_json"))
            display_rows.append({
                "id": row["id"], "experiment_id": row["experiment_id"], "strategy": row["strategy"],
                "status": row["status"], "net_profit_percentage": metrics["net_profit_percentage"],
                "sharpe_ratio": metrics["sharpe_ratio"], "profit_factor": metrics["profit_factor"],
                "max_drawdown": metrics["max_drawdown"], "total_trades": metrics["total_trades"],
                "win_rate": metrics["win_rate"], "dashboard_url": row["dashboard_url"],
                "finished_at": row["finished_at"], "metrics_json": row["metrics_json"],
            })
        rows = display_rows
    return rows, clean


def dashboard_counts(database: WorkflowDatabase) -> dict[str, int]:
    row = database.rows("""SELECT
        SUM(CASE WHEN state IN ('scheduled','ready','running','waiting_retry','blocked') THEN 1 ELSE 0 END) AS queue,
        SUM(CASE WHEN state='running' THEN 1 ELSE 0 END) AS running,
        SUM(CASE WHEN state='blocked' THEN 1 ELSE 0 END) AS blocked
        FROM work_items""")[0]
    candidates = database.rows("SELECT COUNT(*) AS count FROM candidate_summary")[0]["count"]
    synthesis = database.synthesis_status()
    return {"queue": row["queue"] or 0, "running": row["running"] or 0,
            "blocked": row["blocked"] or 0, "candidates": candidates,
            "remaining_chains": synthesis["remaining_chains"]}


def _display(value: object) -> str:
    if value in (None, ""):
        return "—"
    if isinstance(value, str) and value[:1] in ("{", "["):
        try:
            parsed = json.loads(value)
            return json.dumps(parsed, sort_keys=True, separators=(", ", ": "))
        except json.JSONDecodeError:
            pass
    return str(value)


def render_page(database: WorkflowDatabase, page: str, params: dict[str, str]) -> str:
    if page not in PAGE_SPECS:
        page = "queue"
    spec = PAGE_SPECS[page]
    rows, clean = query_page(database, page, params)
    counts = dashboard_counts(database)
    columns = list(rows[0]) if rows else []
    nav = '<a href="/overview">Overview</a>' + "".join(
        f'<a class="{"active" if key == page else ""}" href="/{key}">{html.escape(item["title"])}</a>'
        for key, item in PAGE_SPECS.items()
    )
    cards = _render_cards(counts)
    options = ['<option value="">all</option>']
    filter_name = next(iter(spec["filters"]), "")
    if filter_name:
        options += [f'<option value="{value}" {"selected" if clean.get(filter_name) == value else ""}>{value}</option>'
                    for value in spec["filters"][filter_name]]
    sort_options = "".join(f'<option value="{value}" {"selected" if clean["sort"] == value else ""}>{value}</option>'
                           for value in spec["sorts"])
    filter_control = (f'<label>{filter_name}<select name="{filter_name}">{"".join(options)}</select></label>'
                      if filter_name else "")
    header = "".join(f"<th>{html.escape(column.replace('_', ' '))}</th>" for column in columns)
    body = "".join("<tr>" + "".join(_render_cell(column, row[column]) for column in columns) + "</tr>" for row in rows)
    if not rows:
        body = '<tr><td class=empty>No matching records.</td></tr>'
    live = f"""<section class=cards>{cards}</section>
<h2>{html.escape(spec['title'])} <small>{len(rows)} shown</small></h2><div class=table-wrap><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>"""
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ATS Lab — {html.escape(spec['title'])}</title>
<style>{STYLE}</style><script src="/assets/dashboard.js" defer></script></head>
<body data-page="{page}" data-refresh-url="/{page}"><header><h1>ATS Lab</h1><nav>{nav}</nav><div class=live-controls><label><input id=live-toggle type=checkbox checked> live</label><select id=refresh-interval><option value=5>5s</option><option value=15>15s</option><option value=30>30s</option></select><span id=last-updated>—</span></div></header><main>
<form method=get action="/{page}">
<label>search<input name=q value="{html.escape(clean.get('q', ''), quote=True)}" maxlength=200 placeholder="ID, strategy, metrics…"></label>
{filter_control}<label>sort<select name=sort>{sort_options}</select></label><button>Apply</button><a href="/{page}">Clear</a></form>
<div id=live-content>{live}</div>
</main><footer>Read-only local view · maximum 500 rows</footer></body></html>"""


def render_fragment(database: WorkflowDatabase, page: str, params: dict[str, str]) -> str:
    rows, _ = query_page(database, page, params)
    spec = PAGE_SPECS[page]
    columns = list(rows[0]) if rows else []
    header = "".join(f"<th>{html.escape(column.replace('_', ' '))}</th>" for column in columns)
    body = "".join("<tr>" + "".join(_render_cell(column, row[column]) for column in columns) + "</tr>" for row in rows)
    if not rows:
        body = '<tr><td class=empty>No matching records.</td></tr>'
    return f"""<section class=cards>{_render_cards(dashboard_counts(database))}</section>
<h2>{html.escape(spec['title'])} <small>{len(rows)} shown</small></h2>
<div class=table-wrap><table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table></div>"""


def render_overview(database: WorkflowDatabase, params: dict[str, str]) -> str:
    metric = params.get("metric", "sharpe")
    if metric not in RANK_METRICS:
        metric = "sharpe"
    try:
        minimum_trades = max(0, min(int(params.get("minimum_trades", "0")), 1_000_000))
    except ValueError:
        minimum_trades = 0
    filters = {name: params.get(name, "") for name in ("symbol", "period", "timeframe", "experiment_type")}
    available = comparison_options(database)
    def options(name: str) -> str:
        singular = {"symbols": "symbol", "periods": "period", "timeframes": "timeframe", "experiment_types": "experiment_type"}[name]
        return '<option value="">all</option>' + "".join(
            f'<option value="{html.escape(value, quote=True)}" {"selected" if filters[singular] == value else ""}>{html.escape(value)}</option>'
            for value in available[name]
        )
    metric_options = "".join(
        f'<option value="{key}" {"selected" if key == metric else ""}>{key.replace("_", " ")}</option>'
        for key in RANK_METRICS
    )
    nav = '<a class="active" href="/overview">Overview</a>' + "".join(
        f'<a href="/{key}">{html.escape(item["title"])}</a>' for key, item in PAGE_SPECS.items()
    )
    chart = _render_chart(top_backtests(database, metric, 20, minimum_trades, **filters), metric)
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ATS Lab — Overview</title>
<style>{STYLE}</style><script src="/assets/dashboard.js" defer></script></head>
<body data-page="overview" data-refresh-url="/overview"><header><h1>ATS Lab</h1><nav>{nav}</nav>
<div class=live-controls><label><input id=live-toggle type=checkbox checked> live</label><select id=refresh-interval><option value=5>5s</option><option value=15>15s</option><option value=30>30s</option></select><span id=last-updated>—</span></div></header><main>
<section class=cards id=summary-cards>{_render_cards(dashboard_counts(database))}</section>
<section class=chart-panel><div class=chart-heading><div><h2>Top 20 comparable results</h2><small>Rank by one metric; inspect all metrics across the same market and period.</small></div>
<form id=chart-controls method=get action=/overview><label>metric<select name=metric>{metric_options}</select></label>
<label>pair<select name=symbol>{options("symbols")}</select></label><label>period<select name=period>{options("periods")}</select></label>
<label>timeframe<select name=timeframe>{options("timeframes")}</select></label><label>run type<select name=experiment_type>{options("experiment_types")}</select></label>
<label>minimum trades<input name=minimum_trades type=number min=0 value="{minimum_trades}"></label><button>Apply</button></form></div>
<div id=top-chart>{chart}</div></section></main><footer>Read-only local view · refreshes automatically</footer></body></html>"""


def _render_chart(rows: list[dict], metric: str) -> str:
    if not rows:
        return '<p class=empty>No qualifying finished backtests yet.</p>'
    body = []
    for index, row in enumerate(rows, 1):
        label = row["strategy"] or row["experiment_id"]
        context = " · ".join(part for part in (row.get("symbol"), row.get("timeframe"), row.get("experiment_type")) if part)
        link_start = f'<a href="{html.escape(row["dashboard_url"], quote=True)}" target=_blank rel="noopener noreferrer">' if row.get("dashboard_url") else ""
        link_end = "</a>" if link_start else ""
        metric_cell = lambda key, suffix="": _metric_text(row.get(key), suffix)
        body.append(f'''<tr title="{html.escape(row["experiment_id"], quote=True)}"><td>{index}</td><td>{link_start}{html.escape(label)}{link_end}<small class=context>{html.escape(context)}</small></td><td class=ranked>{row["score"]:,.2f}</td><td>{metric_cell("sharpe_ratio")}</td><td>{metric_cell("net_profit_percentage", "%")}</td><td>{metric_cell("calmar_ratio")}</td><td>{metric_cell("profit_factor")}</td><td>{metric_cell("max_drawdown", "%")}</td><td>{metric_cell("win_rate", "%")}</td><td>{metric_cell("expectancy")}</td><td>{metric_cell("total_trades")}</td></tr>''')
    headers = ("#", "strategy", f"rank: {metric.replace('_', ' ')}", "Sharpe", "net profit", "Calmar", "profit factor", "drawdown", "win rate", "expectancy", "trades")
    return '<div class="table-wrap leaderboard"><table><thead><tr>' + "".join(f"<th>{html.escape(value)}</th>" for value in headers) + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _render_cards(counts: dict[str, int]) -> str:
    return "".join(f"<div class=card><b>{value}</b><span>{html.escape(key)}</span></div>" for key, value in counts.items())


def _render_cell(column: str, value: object) -> str:
    if column in {"net_profit_percentage", "max_drawdown", "win_rate"}:
        shown = _metric_text(value, "%")
    elif column in {"sharpe_ratio", "sortino_ratio", "calmar_ratio", "profit_factor", "expectancy"}:
        shown = _metric_text(value)
    elif column == "total_trades":
        shown = "—" if value is None else f"{float(value):,.0f}"
    elif column == "fees":
        shown = _metric_text(value, "$", prefix=True)
    elif column == "metrics_json" and value:
        shown = f"<details><summary>raw</summary><pre>{html.escape(_pretty_json(value))}</pre></details>"
    else:
        shown = html.escape(_display(value))
    if column == "dashboard_url" and isinstance(value, str) and value.startswith(("http://", "https://")):
        return f'<td><a href="{html.escape(value, quote=True)}" target=_blank rel="noopener noreferrer">dashboard</a></td>'
    css = " class=detail" if column in {"blocker_detail", "summary", "metrics_summary", "next_step", "metrics_json", "error_json"} else ""
    return f"<td{css}>{shown}</td>"


def _metric_text(value: object, suffix: str = "", *, prefix: bool = False) -> str:
    if value is None:
        return "—"
    number = float(value)
    css = "positive" if number > 0 else "negative" if number < 0 else ""
    text = f"{abs(number):,.2f}" if prefix else f"{number:,.2f}"
    text = f"{suffix}{text}" if prefix else f"{text}{suffix}"
    return f'<span class="{css}">{text}</span>'


def _pretty_json(value: object) -> str:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
        return json.dumps(parsed, indent=2, sort_keys=True)
    except (json.JSONDecodeError, TypeError):
        return str(value)


def make_handler(database: WorkflowDatabase) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            request = urlsplit(self.path)
            page = request.path.strip("/") or "queue"
            raw = parse_qs(request.query, keep_blank_values=False)
            params = {key: values[-1] for key, values in raw.items()}
            if request.path == "/assets/dashboard.js":
                self._send(DASHBOARD_JS.encode(), "text/javascript; charset=utf-8")
                return
            if request.path == "/api/summary":
                self._send(_json_bytes(dashboard_counts(database)), "application/json")
                return
            if request.path == "/api/synthesis-status":
                self._send(_json_bytes(database.synthesis_status()), "application/json")
                return
            if request.path == "/api/top-backtests":
                try:
                    limit = int(params.get("limit", "20"))
                    minimum = int(params.get("minimum_trades", "0"))
                except ValueError:
                    self.send_error(400, "limit and minimum_trades must be integers")
                    return
                filters = {name: params.get(name, "") for name in ("symbol", "period", "timeframe", "experiment_type")}
                self._send(_json_bytes(top_backtests(database, params.get("metric", "sharpe"), limit, minimum, **filters)), "application/json")
                return
            if request.path == "/api/queue" or request.path == "/api/candidates" or request.path == "/api/runs":
                api_page = request.path.rsplit("/", 1)[-1]
                rows, clean = query_page(database, api_page, params)
                self._send(_json_bytes({"filters": clean, "rows": rows}), "application/json")
                return
            if page == "overview":
                self._send(render_overview(database, params).encode(), "text/html; charset=utf-8")
                return
            if page not in PAGE_SPECS:
                self.send_error(404)
                return
            payload = (render_fragment(database, page, params) if params.get("fragment") == "1"
                       else render_page(database, page, params)).encode()
            self._send(payload, "text/html; charset=utf-8")

        def _send(self, payload: bytes, content_type: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            print(f"dashboard: {format % args}")

    return DashboardHandler


def serve(database: WorkflowDatabase, host: str = "127.0.0.1", port: int = 8765) -> None:
    database.initialize()
    server = ThreadingHTTPServer((host, port), make_handler(database))
    print(f"ATS Lab dashboard: http://{host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(".ats-lab/laboratory.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    serve(WorkflowDatabase(args.database.resolve()), args.host, args.port)
    return 0


STYLE = """
:root{color-scheme:dark;--bg:#101418;--panel:#192027;--line:#303a44;--text:#e7edf2;--muted:#9eabb6;--accent:#55d6a9}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif}header{display:flex;align-items:center;gap:2rem;padding:1rem 2rem;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:3}h1{font-size:1.2rem;margin:0}nav{display:flex;gap:.5rem}a{color:var(--accent);text-decoration:none}nav a{padding:.45rem .7rem;border-radius:6px}nav a.active{background:var(--panel)}main{padding:1.5rem 2rem}.live-controls{margin-left:auto;display:flex;align-items:center;gap:.6rem}.live-controls label{display:flex;align-items:center;gap:.3rem}.live-controls input{min-width:auto}.cards{display:flex;gap:1rem;flex-wrap:wrap}.card{background:var(--panel);border:1px solid var(--line);padding:.8rem 1.2rem;border-radius:8px;min-width:110px}.card b{font-size:1.5rem;display:block}.card span,small,footer{color:var(--muted)}form{display:flex;gap:1rem;align-items:end;flex-wrap:wrap;margin:1.5rem 0}label{display:grid;gap:.3rem;color:var(--muted)}input,select,button{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:5px;padding:.5rem}input{min-width:260px}button{cursor:pointer}h2{margin-top:1.5rem}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%;min-width:900px}th,td{text-align:left;padding:.65rem;border-bottom:1px solid var(--line);vertical-align:top}th{position:sticky;top:0;background:var(--panel)}td.detail{min-width:200px;max-width:520px;white-space:normal}.empty{text-align:center;color:var(--muted);padding:2rem}.positive{color:#55d6a9}.negative{color:#ff7b86}details pre{max-width:520px;max-height:240px;overflow:auto;white-space:pre-wrap;color:var(--muted)}.chart-panel{margin-top:1.5rem;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:1.2rem}.chart-heading{display:flex;align-items:start;justify-content:space-between;gap:2rem}.chart-heading h2{margin:0}.chart-heading form{margin:0}.chart-heading input{min-width:100px;width:120px}.leaderboard{margin-top:1.3rem}.leaderboard table{min-width:1150px}.leaderboard td{font-variant-numeric:tabular-nums}.leaderboard td:nth-child(2){min-width:180px}.leaderboard .context{display:block}.leaderboard .ranked{background:#20352f;color:var(--accent);font-weight:700}footer{padding:1rem 2rem}@media(max-width:800px){header{position:static;display:block;padding:1rem}nav{margin-top:.7rem;overflow:auto}.live-controls{margin-top:.8rem}.chart-heading{display:block}.chart-heading form{margin-top:1rem}main{padding:1rem}input{min-width:200px}}
"""

DASHBOARD_JS = r"""
(() => {
  const toggle = document.querySelector('#live-toggle');
  const interval = document.querySelector('#refresh-interval');
  const updated = document.querySelector('#last-updated');
  let busy = false;
  const stamp = (ok = true) => { if (updated) { updated.textContent = (ok ? 'updated ' : 'error ') + new Date().toLocaleTimeString(); updated.className = ok ? '' : 'negative'; } };
  async function refresh() {
    if (busy || !toggle?.checked || document.hidden) return;
    busy = true;
    try {
      if (document.body.dataset.page === 'overview') {
        const form = document.querySelector('#chart-controls');
        const query = new URLSearchParams(new FormData(form));
        query.set('limit', '20');
        const [summaryResponse, chartResponse] = await Promise.all([fetch('/api/summary'), fetch('/api/top-backtests?' + query)]);
        if (!summaryResponse.ok || !chartResponse.ok) throw new Error('refresh failed');
        const summary = await summaryResponse.json();
        const rows = await chartResponse.json();
        document.querySelector('#summary-cards').innerHTML = Object.entries(summary).map(([key,value]) => `<div class=card><b>${value}</b><span>${key}</span></div>`).join('');
        drawChart(rows);
      } else {
        const url = new URL(location.href); url.searchParams.set('fragment', '1');
        const response = await fetch(url); if (!response.ok) throw new Error('refresh failed');
        document.querySelector('#live-content').innerHTML = await response.text();
      }
      stamp(true);
    } catch (_) { stamp(false); }
    finally { busy = false; }
  }
  function drawChart(rows) {
    const target = document.querySelector('#top-chart');
    if (!rows.length) { target.innerHTML = '<p class=empty>No qualifying finished backtests yet.</p>'; return; }
    const metric = document.querySelector('[name=metric]').value.replaceAll('_', ' ');
    const number = (value, suffix = '') => value == null ? '—' : `<span class="${value > 0 ? 'positive' : value < 0 ? 'negative' : ''}">${Number(value).toFixed(2)}${suffix}</span>`;
    const headers = ['#','strategy',`rank: ${metric}`,'Sharpe','net profit','Calmar','profit factor','drawdown','win rate','expectancy','trades'];
    const body = rows.map((row, index) => {
      const context = [row.symbol, row.timeframe, row.experiment_type].filter(Boolean).join(' · ');
      const label = escapeHtml(row.strategy || row.experiment_id);
      const linked = row.dashboard_url ? `<a href="${escapeAttr(row.dashboard_url)}" target=_blank rel="noopener noreferrer">${label}</a>` : label;
      return `<tr title="${escapeAttr(row.experiment_id)}"><td>${index + 1}</td><td>${linked}<small class=context>${escapeHtml(context)}</small></td><td class=ranked>${number(row.score)}</td><td>${number(row.sharpe_ratio)}</td><td>${number(row.net_profit_percentage,'%')}</td><td>${number(row.calmar_ratio)}</td><td>${number(row.profit_factor)}</td><td>${number(row.max_drawdown,'%')}</td><td>${number(row.win_rate,'%')}</td><td>${number(row.expectancy)}</td><td>${row.total_trades == null ? '—' : Math.round(row.total_trades)}</td></tr>`;
    }).join('');
    target.innerHTML = `<div class="table-wrap leaderboard"><table><thead><tr>${headers.map(value => `<th>${escapeHtml(value)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  const escapeHtml = value => String(value ?? '').replace(/[&<>]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[char]));
  const escapeAttr = value => escapeHtml(value).replace(/"/g, '&quot;');
  const delay = () => Number(interval?.value || 5) * 1000;
  let timer; const schedule = () => { clearInterval(timer); timer = setInterval(refresh, delay()); };
  interval?.addEventListener('change', schedule); toggle?.addEventListener('change', () => { if (toggle.checked) refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('focus', refresh); stamp(true); schedule();
})();
"""


if __name__ == "__main__":
    raise SystemExit(main())
