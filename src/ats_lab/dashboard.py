"""Read-only local operator dashboard backed by the laboratory database."""
from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .console import distinct_candidate_evidence
from .database import WorkflowDatabase
from .status import hpo_detail_snapshot, hpo_lifecycle_snapshot


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
            "verdict": "ev.verdict ASC, ev.evaluated_at DESC",
        },
        "default_sort": "newest",
        "sql": "",
        "search": (),
    },
    "runs": {
        "title": "Run history",
        "filters": {},
        "sorts": {
            "newest": "COALESCE(r.finished_at, r.started_at) DESC",
            "strategy": "s.name COLLATE NOCASE ASC, r.started_at DESC",
        },
        "default_sort": "newest",
        "sql": "",
        "search": (),
    },
    "hpo": {
        "title": "HPO lifecycle",
        "filters": {
            "lifecycle_state": (
                "hpo_candidate", "hpo_scheduled", "hpo_running",
                "hpo_analysis", "validation", "paper_trade_candidate",
                "revise", "reject",
            ),
        },
        "sorts": {
            "newest": "updated_at DESC",
            "strategy": "strategy ASC, updated_at DESC",
            "state": "lifecycle_state ASC, updated_at DESC",
        },
        "default_sort": "newest",
        "sql": "",
        "search": (),
    },
}

RANK_METRICS = {
    "sharpe": ("sharpe_ratio", True),
    "net_profit": ("net_profit_percentage", True),
    "calmar": ("calmar_ratio", True),
    "profit_factor": ("profit_factor", True),
    "drawdown": ("max_drawdown_percentage", False),
    "expectancy": ("expectancy", True),
}


def _evidence_dict(value: object) -> dict[str, Any]:
    """Serialize only the canonical evidence contract."""
    if hasattr(value, "to_dict"):
        return dict(value.to_dict())
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("normalized evidence must provide to_dict()")


def _compatibility_key(value: Mapping[str, Any]) -> tuple[object, ...]:
    return tuple(
        value.get(name)
        for name in (
            "symbol", "timeframe", "start_date", "finish_date", "evidence_split",
        )
    )


def _complete_compatibility(value: Mapping[str, Any]) -> bool:
    # Unsplit baseline evidence is valid. Split remains part of the exact peer
    # key, but it is optional for route-complete baseline rows.
    return all(
        value.get(name) not in (None, "")
        for name in ("symbol", "timeframe", "start_date", "finish_date")
    )


def _matches_comparison_filters(
    value: Mapping[str, Any], filters: Mapping[str, str],
) -> bool:
    for name, expected in filters.items():
        if value.get(name) != expected:
            return False
    return True


def _newest_complete_evidence(
    database: WorkflowDatabase, filters: Mapping[str, object] | None = None,
) -> object | None:
    evidence = database.query_normalized_evidence(
        filters=dict(filters or {}), limit=2000,
    )
    complete = [item for item in evidence if _complete_compatibility(_evidence_dict(item))]
    if not complete:
        return None
    return max(
        complete,
        key=lambda item: _evidence_dict(item).get("completed_at") or "",
    )


def top_backtests(database: WorkflowDatabase, metric: str = "sharpe", limit: int = 20,
                  minimum_trades: int = 0, *, symbol: str = "", period: str = "",
                  timeframe: str = "", experiment_type: str = "",
                  evidence_split: str = "") -> list[dict]:
    metric = metric if metric in RANK_METRICS else "sharpe"
    limit = max(1, min(int(limit), 100))
    minimum_trades = max(0, min(int(minimum_trades), 1_000_000))
    key, higher_is_better = RANK_METRICS[metric]
    filters: dict[str, object] = {}
    comparison_filters: dict[str, str] = {}
    if symbol:
        filters["symbol"] = symbol
        comparison_filters["symbol"] = symbol
    if timeframe:
        filters["timeframe"] = timeframe
        comparison_filters["timeframe"] = timeframe
    if experiment_type:
        filters["lifecycle_stage"] = experiment_type
    if evidence_split:
        filters["evidence_split"] = evidence_split
        comparison_filters["evidence_split"] = evidence_split
    if period and " to " in period:
        start_date, finish_date = period.split(" to ", 1)
        filters["start_date"], filters["finish_date"] = start_date, finish_date
        comparison_filters.update({
            "start_date": start_date, "finish_date": finish_date,
        })
    if comparison_filters:
        evidence = [
            _evidence_dict(item)
            for item in database.query_normalized_evidence(limit=5000)
            if _complete_compatibility(_evidence_dict(item))
            and _matches_comparison_filters(
                _evidence_dict(item), comparison_filters,
            )
        ]
    else:
        anchor = _newest_complete_evidence(database, filters)
        if anchor is None:
            return []
        evidence = [
            _evidence_dict(item)
            for item in database.compatible_evidence(anchor, limit=2000)
        ]
    ranked = []
    for item in evidence:
        row = item
        if (
            experiment_type
            and row.get("lifecycle_stage") != experiment_type
        ):
            continue
        score = row.get(key)
        trades = row.get("trade_count")
        if score is None or trades is None or trades < minimum_trades:
            continue
        ranked.append({
            **row, "score": score, "rank_metric": metric,
        })
    if metric == "drawdown":
        ranked.sort(key=lambda row: abs(row["score"]))
    else:
        ranked.sort(key=lambda row: row["score"], reverse=higher_is_better)
    return ranked[:limit]


def comparison_options(database: WorkflowDatabase) -> dict[str, list[str]]:
    rows = [
        _evidence_dict(item)
        for item in database.query_normalized_evidence(limit=5000)
    ]
    result: dict[str, set[str]] = {
        "symbols": set(), "periods": set(), "timeframes": set(),
        "experiment_types": set(), "evidence_splits": set(),
    }
    for row in rows:
        if row.get("symbol"):
            result["symbols"].add(row["symbol"])
        if row.get("timeframe"):
            result["timeframes"].add(row["timeframe"])
        if row.get("start_date") and row.get("finish_date"):
            result["periods"].add(f'{row["start_date"]} to {row["finish_date"]}')
        if row.get("lifecycle_stage"):
            result["experiment_types"].add(row["lifecycle_stage"])
        if row.get("evidence_split"):
            result["evidence_splits"].add(row["evidence_split"])
    return {key: sorted(values) for key, values in result.items()}


def query_page(database: WorkflowDatabase, page: str, params: dict[str, str]) -> tuple[list[dict], dict]:
    """Query a page using only whitelisted filters and sort expressions."""
    if page == "hpo":
        clean: dict[str, str] = {}
        filters: dict[str, object] = {}
        state = params.get("lifecycle_state", "")
        if state in PAGE_SPECS["hpo"]["filters"]["lifecycle_state"]:
            filters["lifecycle_state"] = state
            clean["lifecycle_state"] = state
        query = getattr(database, "hpo_studies", None)
        rows = query(filters=filters, limit=500) if query else []
        search = params.get("q", "").strip()[:200]
        if search:
            clean["q"] = search
            needle = search.casefold()
            rows = [
                row for row in rows
                if any(
                    needle in str(row.get(field) or "").casefold()
                    for field in (
                        "study_id", "strategy", "parent_experiment_id",
                        "hpo_experiment_id", "finding", "next_action",
                    )
                )
            ]
        sort = params.get("sort", PAGE_SPECS["hpo"]["default_sort"])
        if sort not in PAGE_SPECS["hpo"]["sorts"]:
            sort = PAGE_SPECS["hpo"]["default_sort"]
        clean["sort"] = sort
        if sort == "strategy":
            rows.sort(
                key=lambda row: (
                    str(row.get("strategy") or "").casefold(),
                    row.get("updated_at") or "",
                )
            )
        elif sort == "state":
            rows.sort(
                key=lambda row: (
                    row.get("lifecycle_state") or "",
                    row.get("updated_at") or "",
                )
            )
        else:
            rows.sort(
                key=lambda row: row.get("updated_at") or "", reverse=True,
            )
        return rows, clean

    if page in {"candidates", "runs"}:
        clean: dict[str, str] = {}
        filters: dict[str, object] = {}
        if page == "candidates":
            verdict = params.get("verdict", "")
            if verdict in PAGE_SPECS["candidates"]["filters"]["verdict"]:
                filters["verdict"] = verdict
                clean["verdict"] = verdict
        items = database.query_normalized_evidence(
            filters=filters, limit=5000 if page == "candidates" else 500,
        )
        if page == "candidates":
            items = distinct_candidate_evidence(items)
        evidence = [_evidence_dict(item) for item in items]
        if page == "candidates" and "verdict" not in filters:
            allowed = set(PAGE_SPECS["candidates"]["filters"]["verdict"])
            evidence = [row for row in evidence if row.get("verdict") in allowed]
        search = params.get("q", "").strip()[:200]
        if search:
            clean["q"] = search
            needle = search.casefold()
            evidence = [
                row for row in evidence
                if any(
                    needle in str(row.get(field) or "").casefold()
                    for field in (
                        "experiment_id", "run_id", "session_id", "strategy",
                        "finding", "next_action",
                    )
                )
            ]
        sort = params.get("sort", PAGE_SPECS[page]["default_sort"])
        if sort not in PAGE_SPECS[page]["sorts"]:
            sort = PAGE_SPECS[page]["default_sort"]
        clean["sort"] = sort
        if sort == "strategy":
            evidence.sort(
                key=lambda row: (
                    str(row.get("strategy") or "").casefold(),
                    row.get("completed_at") or "",
                )
            )
        elif sort == "verdict":
            evidence.sort(
                key=lambda row: (
                    row.get("verdict") or "", row.get("completed_at") or "",
                )
            )
        else:
            evidence.sort(
                key=lambda row: row.get("completed_at") or "", reverse=True,
            )
        return evidence, clean

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
    return rows, clean


def dashboard_counts(database: WorkflowDatabase) -> dict[str, object]:
    row = database.rows("""SELECT
        SUM(CASE WHEN state IN ('scheduled','ready','running','waiting_retry','blocked') THEN 1 ELSE 0 END) AS queue,
        SUM(CASE WHEN state='running' THEN 1 ELSE 0 END) AS running,
        SUM(CASE WHEN state='blocked' THEN 1 ELSE 0 END) AS blocked,
        SUM(CASE WHEN state='waiting_retry' THEN 1 ELSE 0 END) AS retry
        FROM work_items""")[0]
    candidates = len({
        item.experiment_id
        for item in database.query_normalized_evidence(limit=5000)
        if item.verdict in {
            "hpo_candidate", "paper_trade_candidate", "revise",
        }
    })
    awaiting = database.rows(
        """SELECT COUNT(*) AS count FROM work_items
           WHERE state='running' AND blocker_code='awaiting_batch_evaluation'"""
    )[0]["count"]
    synthesis = database.synthesis_status()
    hpo = hpo_lifecycle_snapshot(database)
    analyzer = hpo.get("analyzer")
    return {"queue": row["queue"] or 0, "running": row["running"] or 0,
            "blocked": row["blocked"] or 0, "retry": row["retry"] or 0,
            "candidates": candidates, "hpo_active": hpo["active"],
            "analyzer": analyzer.get("state") if analyzer else "idle",
            "awaiting_evaluation": awaiting, "remaining_chains": synthesis["remaining_chains"]}


def _display(value: object) -> str:
    if value in (None, ""):
        return "—"
    return str(value)


def render_page(database: WorkflowDatabase, page: str, params: dict[str, str]) -> str:
    if page not in PAGE_SPECS:
        page = "queue"
    spec = PAGE_SPECS[page]
    rows, clean = query_page(database, page, params)
    counts = dashboard_counts(database)
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
    table = (
        _render_evidence_table(rows)
        if page in {"candidates", "runs"}
        else _render_hpo_table(rows) if page == "hpo"
        else _render_generic_table(rows)
    )
    live = f"""<section class=cards>{cards}</section>
<h2>{html.escape(spec['title'])} <small>{len(rows)} shown</small></h2>{table}"""
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
    table = (
        _render_evidence_table(rows)
        if page in {"candidates", "runs"}
        else _render_hpo_table(rows) if page == "hpo"
        else _render_generic_table(rows)
    )
    return f"""<section class=cards>{_render_cards(dashboard_counts(database))}</section>
<h2>{html.escape(spec['title'])} <small>{len(rows)} shown</small></h2>
{table}"""


def render_overview(database: WorkflowDatabase, params: dict[str, str]) -> str:
    metric = params.get("metric", "sharpe")
    if metric not in RANK_METRICS:
        metric = "sharpe"
    try:
        minimum_trades = max(0, min(int(params.get("minimum_trades", "0")), 1_000_000))
    except ValueError:
        minimum_trades = 0
    filters = {
        name: params.get(name, "")
        for name in (
            "symbol", "period", "timeframe", "experiment_type", "evidence_split",
        )
    }
    if not any(filters.values()):
        anchor = _newest_complete_evidence(database)
        if anchor is not None:
            row = _evidence_dict(anchor)
            filters.update({
                "symbol": row["symbol"],
                "period": f'{row["start_date"]} to {row["finish_date"]}',
                "timeframe": row["timeframe"],
                "evidence_split": row["evidence_split"],
            })
    available = comparison_options(database)
    def options(name: str) -> str:
        singular = {
            "symbols": "symbol", "periods": "period",
            "timeframes": "timeframe", "experiment_types": "experiment_type",
            "evidence_splits": "evidence_split",
        }[name]
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
<section class=chart-panel><div class=chart-heading><div><h2>Top 20 comparable results</h2><small>Rank by one metric; default uses one exact tuple. Selected filters leave other dimensions unfiltered.</small></div>
<form id=chart-controls method=get action=/overview><label>metric<select name=metric>{metric_options}</select></label>
<label>pair<select name=symbol>{options("symbols")}</select></label><label>period<select name=period>{options("periods")}</select></label>
<label>timeframe<select name=timeframe>{options("timeframes")}</select></label><label>run type<select name=experiment_type>{options("experiment_types")}</select></label>
<label>split<select name=evidence_split>{options("evidence_splits")}</select></label>
<label>minimum trades<input name=minimum_trades type=number min=0 value="{minimum_trades}"></label><button>Apply</button></form></div>
<div id=top-chart>{chart}</div></section></main><footer>Read-only local view · refreshes automatically</footer></body></html>"""


def render_hpo_detail_page(
    database: WorkflowDatabase, study_id: str,
) -> str | None:
    detail = hpo_detail_snapshot(database, study_id)
    if detail is None:
        return None
    nav = '<a href="/overview">Overview</a>' + "".join(
        f'<a class="{"active" if key == "hpo" else ""}" href="/{key}">{html.escape(item["title"])}</a>'
        for key, item in PAGE_SPECS.items()
    )
    return f"""<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>ATS Lab — HPO {html.escape(study_id)}</title>
<style>{STYLE}</style></head><body><header><h1>ATS Lab</h1><nav>{nav}</nav></header>
<main><p><a href="/hpo">← HPO lifecycle</a></p>{_render_hpo_detail(detail)}</main>
<footer>Canonical lifecycle view · raw trial evidence excluded</footer></body></html>"""


def _render_chart(rows: list[dict], metric: str) -> str:
    if not rows:
        return '<p class=empty>No qualifying finished backtests yet.</p>'
    body = []
    for index, row in enumerate(rows, 1):
        label = row["strategy"] or row["experiment_id"]
        metric_cell = lambda key, suffix="": _metric_text(row.get(key), suffix)
        strategy = html.escape(label)
        if row.get("strategy_version"):
            strategy += f'<small class=context>{html.escape(str(row["strategy_version"]))}</small>'
        stage = " / ".join(
            str(part) for part in (row.get("lifecycle_stage"), row.get("verdict"))
            if part
        )
        market = " · ".join(
            str(part) for part in (row.get("symbol"), row.get("timeframe")) if part
        )
        period = html.escape(" to ".join(
            str(part) for part in (row.get("start_date"), row.get("finish_date"))
            if part
        ))
        split = row.get("evidence_split")
        if split:
            period += f'<small class=context>{html.escape(str(split))}</small>'
        details = _render_evidence_details(row)
        body.append(
            f'''<tr title="{html.escape(row["experiment_id"], quote=True)}">'''
            f"<td>{index}</td><td>{strategy}</td><td>{html.escape(stage) or '—'}</td>"
            f"<td>{html.escape(market) or '—'}</td><td>{period or '—'}</td>"
            f'<td class=ranked>{row["score"]:,.2f}</td>'
            f'<td>{metric_cell("net_profit_percentage", "%")}</td>'
            f'<td>{metric_cell("max_drawdown_percentage", "%")}</td>'
            f'<td>{metric_cell("sharpe_ratio")}</td>'
            f'<td>{_integer_text(row.get("trade_count"))}</td>'
            f'<td class=detail>{html.escape(str(row.get("finding") or "—"))}</td>'
            f'<td class=detail>{html.escape(str(row.get("next_action") or "—"))}</td>'
            f"<td>{details}</td></tr>"
        )
    headers = (
        "#", "strategy", "stage / verdict", "market", "period / split",
        f"rank: {metric.replace('_', ' ')}", "net profit", "drawdown",
        "Sharpe", "trades", "finding", "next action", "details",
    )
    return '<div class="table-wrap leaderboard"><table><thead><tr>' + "".join(f"<th>{html.escape(value)}</th>" for value in headers) + "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"


def _duration_text(value: object) -> str:
    if value is None:
        return "—"
    seconds = max(0, int(float(value)))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60}m"


def _render_hpo_table(rows: list[dict]) -> str:
    if not rows:
        return (
            '<div class=table-wrap><table><tbody>'
            '<tr><td class=empty>No HPO studies.</td></tr>'
            "</tbody></table></div>"
        )
    headers = (
        "lifecycle", "strategy", "study", "objective", "trials",
        "selected", "validation", "disposition", "next action", "details",
    )
    body = []
    for row in rows:
        study_id = str(row.get("study_id") or "")
        study_link = (
            f'<a href="/hpo/{quote(study_id, safe="")}">{html.escape(study_id)}</a>'
            if study_id else "—"
        )
        progress = (
            f'{row.get("completed_trial_count") or 0}/'
            f'{row.get("trial_count") or 0}'
        )
        details = (
            "<details><summary>standardized</summary><dl>"
            + "".join(
                f"<dt>{html.escape(label)}</dt>"
                f"<dd>{html.escape(str(row.get(key) or '—'))}</dd>"
                for label, key in (
                    ("parent experiment", "parent_experiment_id"),
                    ("parent job", "parent_work_item_id"),
                    ("HPO experiment", "hpo_experiment_id"),
                    ("HPO job", "hpo_work_item_id"),
                    ("direction", "direction"),
                    ("finding", "finding"),
                    ("started", "started_at"),
                    ("completed", "completed_at"),
                    ("updated", "updated_at"),
                )
            )
            + "</dl></details>"
        )
        body.append(
            "<tr>"
            f'<td><span class=lifecycle>{html.escape(str(row.get("lifecycle_state") or "—"))}</span></td>'
            f'<td>{html.escape(str(row.get("strategy") or "—"))}</td>'
            f"<td>{study_link}</td>"
            f'<td>{html.escape(str(row.get("objective_name") or "—"))}</td>'
            f"<td>{progress}</td>"
            f'<td>{row.get("selected_trial_count") or 0}</td>'
            f'<td>{row.get("validation_count") or 0}</td>'
            f'<td>{html.escape(str(row.get("disposition") or "—"))}</td>'
            f'<td class=detail>{html.escape(str(row.get("next_action") or "—"))}</td>'
            f"<td>{details}</td></tr>"
        )
    return (
        '<div class="table-wrap hpo"><table><thead><tr>'
        + "".join(f"<th>{html.escape(value)}</th>" for value in headers)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _render_linked_id(value: object) -> str:
    if value in (None, ""):
        return "—"
    text = str(value)
    return (
        f'<a href="/runs?q={quote(text, safe="")}">{html.escape(text)}</a>'
    )


def _render_hpo_detail(detail: Mapping[str, Any]) -> str:
    study_value = detail.get("study")
    study = dict(study_value) if isinstance(study_value, Mapping) else dict(detail)
    selected = detail.get("selected_trials")
    selected_rows = selected if isinstance(selected, list) else []
    trial_body = []
    for row in selected_rows:
        if not isinstance(row, Mapping):
            continue
        run_link = _render_linked_id(row.get("run_id"))
        session_link = _render_linked_id(row.get("session_id"))
        evidence_link = (
            run_link if row.get("evidence_key") else "—"
        )
        trial_body.append(
            "<tr>"
            f'<td>{html.escape(str(row.get("rank") or "—"))}</td>'
            f'<td>{html.escape(str(row.get("trial_number") if row.get("trial_number") is not None else "—"))}</td>'
            f'<td>{html.escape(str(row.get("objective_value") if row.get("objective_value") is not None else "—"))}</td>'
            f'<td>{html.escape(str(row.get("classification") or "—"))}</td>'
            f"<td>{evidence_link}</td><td>{run_link}</td><td>{session_link}</td>"
            f'<td class=detail>{html.escape(str(row.get("selection_reason") or "—"))}</td>'
            "</tr>"
        )
    selected_table = (
        '<div class=table-wrap><table><thead><tr>'
        "<th>rank</th><th>trial</th><th>objective</th><th>classification</th>"
        "<th>evidence</th><th>run</th><th>session</th><th>reason</th>"
        "</tr></thead><tbody>"
        + ("".join(trial_body) or '<tr><td class=empty>No selected trials.</td></tr>')
        + "</tbody></table></div>"
    )
    validations_value = detail.get("validations")
    validations = validations_value if isinstance(validations_value, list) else []
    validation_body = []
    for row in validations:
        if not isinstance(row, Mapping):
            continue
        validation_body.append(
            "<tr>"
            f'<td>{html.escape(str(row.get("status") or row.get("validation_status") or row.get("state") or "—"))}</td>'
            f'<td>{html.escape(str(row.get("readiness_status") or "—"))}</td>'
            f'<td>{html.escape(str(row.get("experiment_id") or "—"))}</td>'
            f'<td>{_render_linked_id(row.get("run_id"))}</td>'
            f'<td>{_render_linked_id(row.get("session_id"))}</td>'
            f'<td class=detail>{html.escape(str(row.get("blocker_detail") or row.get("finding") or "—"))}</td>'
            "</tr>"
        )
    validation_table = (
        '<div class=table-wrap><table><thead><tr>'
        "<th>status</th><th>readiness</th><th>experiment</th><th>run</th>"
        "<th>session</th><th>blocker / finding</th>"
        "</tr></thead><tbody>"
        + ("".join(validation_body) or '<tr><td class=empty>No validation runs.</td></tr>')
        + "</tbody></table></div>"
    )
    timings_value = detail.get("timings")
    timings = timings_value if isinstance(timings_value, list) else []
    timing_body = []
    for row in timings:
        if not isinstance(row, Mapping):
            continue
        timing_body.append(
            "<tr>"
            f'<td>{html.escape(str(row.get("stage") or "—"))}</td>'
            f'<td>{html.escape(str(row.get("attempt") or "—"))}</td>'
            f'<td>{html.escape(str(row.get("state") or "—"))}</td>'
            f'<td>{html.escape(_duration_text(row.get("duration_seconds")))}</td>'
            f'<td>{html.escape(str(row.get("outcome") or "—"))}</td>'
            f'<td>{html.escape(str(row.get("started_at") or "—"))}</td>'
            f'<td>{html.escape(str(row.get("completed_at") or "—"))}</td>'
            "</tr>"
        )
    timing_table = (
        '<div class=table-wrap><table><thead><tr>'
        "<th>stage</th><th>try</th><th>state</th><th>duration</th>"
        "<th>outcome</th><th>started</th><th>completed</th>"
        "</tr></thead><tbody>"
        + ("".join(timing_body) or '<tr><td class=empty>No stage timings.</td></tr>')
        + "</tbody></table></div>"
    )
    analyzer_value = detail.get("analysis_job")
    analyzer = analyzer_value if isinstance(analyzer_value, Mapping) else {}
    analyzer_text = " · ".join(
        f"{label}={analyzer.get(key) if analyzer.get(key) not in (None, '') else '—'}"
        for label, key in (
            ("state", "state"), ("job", "job_id"), ("tries", "attempts"),
            ("retry", "retry_after"), ("error", "last_error"),
        )
    )
    study_metadata = "".join(
        f"<dt>{html.escape(label)}</dt>"
        f"<dd>{html.escape(str(study.get(key) or '—'))}</dd>"
        for label, key in (
            ("study", "study_id"), ("name", "name"),
            ("objective", "objective_name"), ("direction", "direction"),
            ("HPO experiment", "hpo_experiment_id"),
            ("HPO job", "hpo_work_item_id"),
            ("started", "started_at"), ("completed", "completed_at"),
        )
    )
    return (
        "<section class=cards>"
        f'<div class=card><b>{html.escape(str(study.get("lifecycle_state") or "—"))}</b><span>lifecycle</span></div>'
        f'<div class=card><b>{study.get("completed_trial_count") or 0}/{study.get("trial_count") or 0}</b><span>trials</span></div>'
        f'<div class=card><b>{study.get("selected_trial_count") or 0}</b><span>selected</span></div>'
        f'<div class=card><b>{study.get("validation_count") or 0}</b><span>validation</span></div>'
        "</section>"
        f'<h2>{html.escape(str(study.get("strategy") or "HPO study"))}</h2>'
        f"<dl>{study_metadata}</dl>"
        f'<p>{html.escape(str(study.get("finding") or "—"))}</p>'
        f'<p><b>Next:</b> {html.escape(str(study.get("next_action") or "—"))}</p>'
        f"<p><b>Analyzer:</b> {html.escape(analyzer_text)}</p>"
        "<h3>Selected trials</h3>" + selected_table
        + "<h3>Validation</h3>" + validation_table
        + "<h3>Stage timings</h3>" + timing_table
    )


def _render_generic_table(rows: list[dict]) -> str:
    columns = list(rows[0]) if rows else []
    header = "".join(
        f"<th>{html.escape(column.replace('_', ' '))}</th>"
        for column in columns
    )
    body = "".join(
        "<tr>"
        + "".join(_render_cell(column, row[column]) for column in columns)
        + "</tr>"
        for row in rows
    )
    if not rows:
        body = '<tr><td class=empty>No matching records.</td></tr>'
    return (
        f'<div class=table-wrap><table><thead><tr>{header}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def _render_evidence_table(rows: list[dict]) -> str:
    if not rows:
        return (
            '<div class=table-wrap><table><tbody>'
            '<tr><td class=empty>No matching records.</td></tr>'
            "</tbody></table></div>"
        )
    headers = (
        "strategy", "stage / verdict", "market", "period / split",
        "net profit", "drawdown", "Sharpe", "trades", "finding",
        "next action", "details",
    )
    body: list[str] = []
    for row in rows:
        strategy = html.escape(str(row.get("strategy") or "—"))
        if row.get("strategy_version"):
            strategy += (
                f'<small class=context>{html.escape(str(row["strategy_version"]))}</small>'
            )
        stage = " / ".join(
            str(part) for part in (row.get("lifecycle_stage"), row.get("verdict"))
            if part
        )
        market = " · ".join(
            str(part) for part in (row.get("symbol"), row.get("timeframe")) if part
        )
        period = html.escape(" to ".join(
            str(part) for part in (row.get("start_date"), row.get("finish_date"))
            if part
        ))
        if row.get("evidence_split"):
            period += (
                f'<small class=context>{html.escape(str(row["evidence_split"]))}</small>'
            )
        body.append(
            "<tr>"
            f"<td>{strategy}</td><td>{html.escape(stage) if stage else '—'}</td>"
            f"<td>{html.escape(market) if market else '—'}</td><td>{period or '—'}</td>"
            f'<td>{_metric_text(row.get("net_profit_percentage"), "%")}</td>'
            f'<td>{_metric_text(row.get("max_drawdown_percentage"), "%")}</td>'
            f'<td>{_metric_text(row.get("sharpe_ratio"))}</td>'
            f'<td>{_integer_text(row.get("trade_count"))}</td>'
            f'<td class=detail>{html.escape(str(row.get("finding") or "—"))}</td>'
            f'<td class=detail>{html.escape(str(row.get("next_action") or "—"))}</td>'
            f"<td>{_render_evidence_details(row)}</td></tr>"
        )
    return (
        '<div class="table-wrap evidence"><table><thead><tr>'
        + "".join(f"<th>{html.escape(value)}</th>" for value in headers)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table></div>"
    )


def _render_evidence_details(row: Mapping[str, Any]) -> str:
    fields = (
        ("experiment", "experiment_id", ""),
        ("run", "run_id", ""),
        ("session", "session_id", ""),
        ("Sortino", "sortino_ratio", ""),
        ("Calmar", "calmar_ratio", ""),
        ("profit factor", "profit_factor", ""),
        ("win rate", "win_rate", "%"),
        ("fees", "fees", ""),
        ("expectancy", "expectancy", ""),
        ("leverage", "leverage", "x"),
        ("risk / trade", "risk_per_trade_percentage", "%"),
        ("optimizer objective", "optimizer_objective", ""),
        ("cost stress", "cost_stress_status", ""),
        ("significance p", "significance_p_value", ""),
        ("completed", "completed_at", ""),
    )
    items = []
    for label, key, suffix in fields:
        value = row.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            shown = f"{value:,.2f}{suffix}"
        else:
            shown = "—" if value in (None, "") else f"{value}{suffix}"
        items.append(
            f"<dt>{html.escape(label)}</dt><dd>{html.escape(str(shown))}</dd>"
        )
    return (
        "<details><summary>standardized</summary><dl>"
        + "".join(items)
        + "</dl></details>"
    )


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _render_cards(counts: Mapping[str, object]) -> str:
    return "".join(f"<div class=card><b>{value}</b><span>{html.escape(key)}</span></div>" for key, value in counts.items())


def _render_cell(column: str, value: object) -> str:
    if column in {"net_profit_percentage", "max_drawdown_percentage", "win_rate"}:
        shown = _metric_text(value, "%")
    elif column in {"sharpe_ratio", "sortino_ratio", "calmar_ratio", "profit_factor", "expectancy"}:
        shown = _metric_text(value)
    elif column == "trade_count":
        shown = _integer_text(value)
    elif column == "fees":
        shown = _metric_text(value)
    else:
        shown = html.escape(_display(value))
    css = " class=detail" if column in {"blocker_detail", "finding", "next_action"} else ""
    return f"<td{css}>{shown}</td>"


def _metric_text(value: object, suffix: str = "", *, prefix: bool = False) -> str:
    if value is None:
        return "—"
    number = float(value)
    css = "positive" if number > 0 else "negative" if number < 0 else ""
    text = f"{abs(number):,.2f}" if prefix else f"{number:,.2f}"
    text = f"{suffix}{text}" if prefix else f"{text}{suffix}"
    return f'<span class="{css}">{text}</span>'


def _integer_text(value: object) -> str:
    return "—" if value is None else f"{int(value):,}"


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
            if request.path == "/api/analyzer-status":
                query = getattr(database, "current_analyzer_status", None)
                self._send(
                    _json_bytes(query() if query else None), "application/json",
                )
                return
            if request.path == "/api/lifecycle-timings":
                query = getattr(database, "work_item_stage_timings", None)
                self._send(
                    _json_bytes(
                        query(
                            work_item_id=params.get("work_item_id"),
                            limit=500,
                        ) if query else []
                    ),
                    "application/json",
                )
                return
            if request.path == "/api/hpo-studies":
                query = getattr(database, "hpo_studies", None)
                filters = {
                    key: value for key, value in (
                        ("lifecycle_state", params.get("lifecycle_state")),
                        ("strategy", params.get("strategy")),
                    ) if value
                }
                self._send(
                    _json_bytes(
                        query(filters=filters, limit=500) if query else []
                    ),
                    "application/json",
                )
                return
            if request.path.startswith("/api/hpo-studies/"):
                study_id = unquote(
                    request.path.removeprefix("/api/hpo-studies/")
                )
                if not study_id or "/" in study_id:
                    self.send_error(404)
                    return
                detail = hpo_detail_snapshot(database, study_id)
                if detail is None:
                    self.send_error(404)
                    return
                self._send(_json_bytes(detail), "application/json")
                return
            if request.path == "/api/top-backtests":
                try:
                    limit = int(params.get("limit", "20"))
                    minimum = int(params.get("minimum_trades", "0"))
                except ValueError:
                    self.send_error(400, "limit and minimum_trades must be integers")
                    return
                filters = {
                    name: params.get(name, "")
                    for name in (
                        "symbol", "period", "timeframe", "experiment_type",
                        "evidence_split",
                    )
                }
                self._send(_json_bytes(top_backtests(database, params.get("metric", "sharpe"), limit, minimum, **filters)), "application/json")
                return
            if request.path.startswith("/api/diagnostics/runs/"):
                run_id = unquote(
                    request.path.removeprefix("/api/diagnostics/runs/")
                )
                if not run_id or "/" in run_id:
                    self.send_error(404)
                    return
                evidence = database.diagnostic_raw_evidence(run_id)
                if evidence is None:
                    self.send_error(404)
                    return
                self._send(_json_bytes(evidence), "application/json")
                return
            if request.path.startswith("/api/diagnostics/hpo/"):
                parts = request.path.removeprefix(
                    "/api/diagnostics/hpo/"
                ).split("/")
                if len(parts) != 3 or parts[1] != "trials":
                    self.send_error(404)
                    return
                study_id = unquote(parts[0])
                try:
                    trial_number = int(parts[2])
                except ValueError:
                    self.send_error(404)
                    return
                query = getattr(database, "diagnostic_hpo_trial_details", None)
                detail = (
                    query(study_id, trial_number) if query else None
                )
                if detail is None:
                    self.send_error(404)
                    return
                self._send(_json_bytes(detail), "application/json")
                return
            if request.path == "/api/queue" or request.path == "/api/candidates" or request.path == "/api/runs":
                api_page = request.path.rsplit("/", 1)[-1]
                rows, clean = query_page(database, api_page, params)
                self._send(_json_bytes({"filters": clean, "rows": rows}), "application/json")
                return
            if page == "overview":
                self._send(render_overview(database, params).encode(), "text/html; charset=utf-8")
                return
            if request.path.startswith("/hpo/"):
                study_id = unquote(request.path.removeprefix("/hpo/"))
                if not study_id or "/" in study_id:
                    self.send_error(404)
                    return
                payload = render_hpo_detail_page(database, study_id)
                if payload is None:
                    self.send_error(404)
                    return
                self._send(payload.encode(), "text/html; charset=utf-8")
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
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,sans-serif}header{display:flex;align-items:center;gap:2rem;padding:1rem 2rem;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--bg);z-index:3}h1{font-size:1.2rem;margin:0}nav{display:flex;gap:.5rem}a{color:var(--accent);text-decoration:none}nav a{padding:.45rem .7rem;border-radius:6px}nav a.active{background:var(--panel)}main{padding:1.5rem 2rem}.live-controls{margin-left:auto;display:flex;align-items:center;gap:.6rem}.live-controls label{display:flex;align-items:center;gap:.3rem}.live-controls input{min-width:auto}.cards{display:flex;gap:1rem;flex-wrap:wrap}.card{background:var(--panel);border:1px solid var(--line);padding:.8rem 1.2rem;border-radius:8px;min-width:110px}.card b{font-size:1.5rem;display:block}.card span,small,footer{color:var(--muted)}form{display:flex;gap:1rem;align-items:end;flex-wrap:wrap;margin:1.5rem 0}label{display:grid;gap:.3rem;color:var(--muted)}input,select,button{background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:5px;padding:.5rem}input{min-width:260px}button{cursor:pointer}h2{margin-top:1.5rem}.table-wrap{overflow:auto;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%;min-width:900px}th,td{text-align:left;padding:.65rem;border-bottom:1px solid var(--line);vertical-align:top}th{position:sticky;top:0;background:var(--panel)}td.detail{min-width:200px;max-width:520px;white-space:normal}.empty{text-align:center;color:var(--muted);padding:2rem}.positive{color:#55d6a9}.negative{color:#ff7b86}details dl{display:grid;grid-template-columns:max-content minmax(100px,1fr);gap:.25rem .7rem;min-width:320px}details dt{color:var(--muted)}details dd{margin:0}.chart-panel{margin-top:1.5rem;background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:1.2rem}.chart-heading{display:flex;align-items:start;justify-content:space-between;gap:2rem}.chart-heading h2{margin:0}.chart-heading form{margin:0}.chart-heading input{min-width:100px;width:120px}.leaderboard{margin-top:1.3rem}.leaderboard table{min-width:1150px}.leaderboard td{font-variant-numeric:tabular-nums}.leaderboard td:nth-child(2){min-width:180px}.leaderboard .context{display:block}.leaderboard .ranked{background:#20352f;color:var(--accent);font-weight:700}footer{padding:1rem 2rem}@media(max-width:800px){header{position:static;display:block;padding:1rem}nav{margin-top:.7rem;overflow:auto}.live-controls{margin-top:.8rem}.chart-heading{display:block}.chart-heading form{margin-top:1rem}main{padding:1rem}input{min-width:200px}}
"""

DASHBOARD_JS = r"""
(() => {
  const toggle = document.querySelector('#live-toggle');
  const interval = document.querySelector('#refresh-interval');
  const updated = document.querySelector('#last-updated');
  const requestTimeoutMs = 10000;
  let busy = false;
  const stamp = (ok = true, detail = '') => {
    if (updated) {
      updated.textContent = (ok ? 'updated ' : 'error ') + new Date().toLocaleTimeString();
      updated.className = ok ? '' : 'negative';
      updated.title = detail;
    }
  };
  const wait = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
  async function request(input) {
    let lastError;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), requestTimeoutMs);
      try {
        const response = await fetch(input, {
          cache: 'no-store',
          headers: {'Cache-Control': 'no-cache'},
          signal: controller.signal,
        });
        if (!response.ok) {
          const error = new Error(`HTTP ${response.status} ${response.statusText || 'request failed'}`);
          error.name = 'RefreshHttpError';
          error.status = response.status;
          error.statusText = response.statusText;
          error.url = response.url || String(input);
          throw error;
        }
        return response;
      } catch (error) {
        lastError = error;
        if (error && typeof error === 'object') error.requestUrl = String(input);
        if (attempt === 0) await wait(250);
      } finally {
        clearTimeout(timeout);
      }
    }
    throw lastError;
  }
  async function refresh() {
    if (busy || !toggle?.checked || document.hidden) return;
    busy = true;
    const page = document.body.dataset.page;
    const targets = page === 'overview'
      ? [document.querySelector('#summary-cards'), document.querySelector('#top-chart')]
      : [document.querySelector('#live-content')];
    const previousContent = targets.map(target => target?.innerHTML);
    try {
      if (page === 'overview') {
        const form = document.querySelector('#chart-controls');
        const query = new URLSearchParams(new FormData(form));
        query.set('limit', '20');
        const [summaryResponse, chartResponse] = await Promise.all([request('/api/summary'), request('/api/top-backtests?' + query)]);
        const summary = await summaryResponse.json();
        const rows = await chartResponse.json();
        if (!summary || typeof summary !== 'object' || !Array.isArray(rows)) throw new Error('invalid dashboard response');
        document.querySelector('#summary-cards').innerHTML = Object.entries(summary).map(([key,value]) => `<div class=card><b>${value}</b><span>${key}</span></div>`).join('');
        drawChart(rows);
      } else {
        const url = new URL(location.href); url.searchParams.set('fragment', '1');
        const response = await request(url);
        const content = await response.text();
        if (!content.trim()) throw new Error(`refresh returned empty content: ${response.url || url}`);
        document.querySelector('#live-content').innerHTML = content;
      }
      stamp(true);
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error);
      targets.forEach((target, index) => {
        if (target && previousContent[index] !== undefined) target.innerHTML = previousContent[index];
      });
      console.error('ATS dashboard refresh failed', {
        page,
        detail,
        requestUrl: error?.requestUrl,
        status: error?.status,
        statusText: error?.statusText,
        timeoutMs: requestTimeoutMs,
        error,
      });
      stamp(false, detail);
    }
    finally { busy = false; }
  }
  function drawChart(rows) {
    const target = document.querySelector('#top-chart');
    if (!rows.length) { target.innerHTML = '<p class=empty>No qualifying finished backtests yet.</p>'; return; }
    const metric = document.querySelector('[name=metric]').value.replaceAll('_', ' ');
    const number = (value, suffix = '') => value == null ? '—' : `<span class="${value > 0 ? 'positive' : value < 0 ? 'negative' : ''}">${Number(value).toFixed(2)}${suffix}</span>`;
    const shown = value => value == null || value === '' ? '—' : escapeHtml(value);
    const details = row => {
      const fields = [
        ['experiment',row.experiment_id],['run',row.run_id],['session',row.session_id],
        ['Sortino',row.sortino_ratio],['Calmar',row.calmar_ratio],
        ['profit factor',row.profit_factor],['win rate',row.win_rate],
        ['fees',row.fees],['expectancy',row.expectancy],['leverage',row.leverage],
        ['risk / trade',row.risk_per_trade_percentage],
        ['optimizer objective',row.optimizer_objective],
        ['cost stress',row.cost_stress_status],
        ['significance p',row.significance_p_value],['completed',row.completed_at],
      ];
      return `<details><summary>standardized</summary><dl>${fields.map(([label,value]) => `<dt>${escapeHtml(label)}</dt><dd>${shown(value)}</dd>`).join('')}</dl></details>`;
    };
    const headers = ['#','strategy','stage / verdict','market','period / split',`rank: ${metric}`,'net profit','drawdown','Sharpe','trades','finding','next action','details'];
    const body = rows.map((row, index) => {
      const version = row.strategy_version ? `<small class=context>${escapeHtml(row.strategy_version)}</small>` : '';
      const stage = [row.lifecycle_stage,row.verdict].filter(Boolean).join(' / ');
      const market = [row.symbol,row.timeframe].filter(Boolean).join(' · ');
      const period = [row.start_date,row.finish_date].filter(Boolean).join(' to ');
      const split = row.evidence_split ? `<small class=context>${escapeHtml(row.evidence_split)}</small>` : '';
      return `<tr title="${escapeAttr(row.experiment_id)}"><td>${index + 1}</td><td>${shown(row.strategy || row.experiment_id)}${version}</td><td>${shown(stage)}</td><td>${shown(market)}</td><td>${shown(period)}${split}</td><td class=ranked>${number(row.score)}</td><td>${number(row.net_profit_percentage,'%')}</td><td>${number(row.max_drawdown_percentage,'%')}</td><td>${number(row.sharpe_ratio)}</td><td>${row.trade_count == null ? '—' : Math.round(row.trade_count)}</td><td class=detail>${shown(row.finding)}</td><td class=detail>${shown(row.next_action)}</td><td>${details(row)}</td></tr>`;
    }).join('');
    target.innerHTML = `<div class="table-wrap leaderboard"><table><thead><tr>${headers.map(value => `<th>${escapeHtml(value)}</th>`).join('')}</tr></thead><tbody>${body}</tbody></table></div>`;
  }
  const escapeHtml = value => String(value ?? '').replace(/[&<>]/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[char]));
  const escapeAttr = value => escapeHtml(value).replace(/"/g, '&quot;');
  const delay = () => Number(interval?.value || 5) * 1000;
  let timer; const schedule = () => { clearInterval(timer); timer = setInterval(refresh, delay()); };
  interval?.addEventListener('change', schedule); toggle?.addEventListener('change', () => { if (toggle.checked) refresh(); });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  window.addEventListener('focus', refresh); stamp(false, 'waiting for first refresh'); refresh(); schedule();
})();
"""


if __name__ == "__main__":
    raise SystemExit(main())
