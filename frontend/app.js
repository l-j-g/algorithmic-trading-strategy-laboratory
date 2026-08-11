/*
 * Local Control Room frontend.
 * Uses browser-native fetch, URLSearchParams, and event delegation so the
 * same static shell works from file:// and from the same-origin API server.
 */
(function () {
  "use strict";

  const API_ROUTES = Object.freeze({
    summary: "/api/v1/summary",
    health: "/api/v1/health",
    queue: "/api/v1/queue",
    hpoStudies: "/api/v1/hpo/studies",
    control: "/api/v1/control",
    attention: "/api/v1/attention",
    backtests: "/api/v1/backtests",
    commands: "/api/v1/commands",
  });
  const API_BASE = String(window.ATS_LAB_API_BASE || "").replace(/\/$/, "");
  const REFRESH_INTERVAL_MS = 30000;
  const STALE_AFTER_MS = 90000;
  const state = { view: "dashboard", snapshot: null, detail: null, refreshTimer: null };

  const DEMO = {
    summary: {
      queue: 12, ready: 4, running: 3, retry: 1, blocked: 2,
      hpo_active: 2, hpo_waiting: 1, validation: 1, candidates: 3,
      heartbeat_at: "2026-08-11T01:20:00Z", updated_at: null, attention: [],
    },
    health: { status: "healthy", label: "Healthy", detail: "Demo snapshot" },
    queue: [
      { id: "JOB-1042", state: "running", stage: "execution", strategy: "KamaAdxPullback", route: "BTC-USDT · 1h", priority: 80, next_action: "Await Jesse result" },
      { id: "JOB-1041", state: "ready", stage: "execution", strategy: "RangeBreakout", route: "ETH-USDT · 1h", priority: 70, next_action: "Claim worker slot" },
      { id: "JOB-1039", state: "waiting_retry", stage: "recovery", strategy: "KamaAdxPullback", route: "OOS · 2025", priority: 60, next_action: "Retry after cooldown" },
      { id: "JOB-1036", state: "blocked", stage: "requirements_pending", strategy: "KamaAdxPullback", route: "Rolling validation", priority: 50, next_action: "Configure verified route" },
    ],
    hpoStudies: [
      { id: "HPO-018", name: "Kama ADX defaults", strategy: "KamaAdxPullback", state: "hpo_analysis", completed_trials: 42, total_trials: 50, selected: 3, validation: 1, next_action: "Review analyzer evidence" },
      { id: "HPO-017", name: "Range breakout search", strategy: "RangeBreakout", state: "requirements_pending", completed_trials: 0, total_trials: 30, selected: 0, validation: 0, next_action: "Attach completed study" },
    ],
    control: { available: false, desired_state: "unknown", supervisor_phase: "unknown" },
    demo: true,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const text = (value, fallback = "—") => value === 0 ? "0" : (value === null || value === undefined || value === "" ? fallback : String(value));
  const number = (value, fallback = 0) => Number.isFinite(Number(value)) ? Number(value) : fallback;
  const escapeHtml = (value) => text(value, "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
  const firstObject = (payload, keys) => {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return {};
    for (const key of keys) if (payload[key] && typeof payload[key] === "object") return payload[key];
    return payload;
  };
  const arrayPayload = (payload, keys) => {
    if (Array.isArray(payload)) return payload;
    if (!payload || typeof payload !== "object") return [];
    for (const key of keys) if (Array.isArray(payload[key])) return payload[key];
    if (payload.data && typeof payload.data === "object") return arrayPayload(payload.data, keys);
    return [];
  };

  function createApiClient({ fetchImpl = window.fetch.bind(window), base = API_BASE, routes = API_ROUTES } = {}) {
    async function getJson(path) {
      const response = await fetchImpl(`${base}${path}`, { headers: { Accept: "application/json" }, cache: "no-store" });
      if (!response.ok) throw new Error(`${response.status} ${response.statusText || "API request failed"}`);
      return response.json();
    }
    async function postJson(path, payload = {}, confirmation = "") {
      const headers = { Accept: "application/json", "Content-Type": "application/json" };
      if (confirmation) headers["X-ATS-Lab-Confirm"] = confirmation;
      const response = await fetchImpl(`${base}${path}`, { method: "POST", headers, body: JSON.stringify(payload), cache: "no-store" });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result?.error?.detail || `${response.status} request failed`);
      return result;
    }
    return { getJson, postJson, routes };
  }

  function normalizeSummary(payload) {
    const source = firstObject(payload, ["summary", "data"]);
    const workStates = source.work_states || {};
    const hpo = source.hpo || {};
    const hpoCounts = hpo.counts || {};
    const validationJobs = hpo.route_readiness?.validation_jobs || {};
    return {
      queue: number(source.queue ?? source.unresolved ?? source.active_queue ?? source.active),
      ready: number(source.ready ?? workStates.ready),
      running: number(source.running ?? source.running_execution_claims ?? workStates.running),
      retry: number(source.retry ?? source.waiting_retry ?? workStates.waiting_retry),
      blocked: number(source.blocked ?? workStates.blocked),
      hpo_active: number(source.hpo_active ?? source.active_hpo ?? hpo.active),
      hpo_waiting: number(source.hpo_waiting ?? source.requirements_pending ?? hpo.waiting),
      validation: number(source.validation ?? validationJobs.total),
      candidates: number(source.candidates ?? source.candidate_count ?? hpoCounts.paper_trade_candidate),
      unresolved_execution_claims: number(source.unresolved_execution_claims),
      heartbeat_at: source.heartbeat_at ?? source.last_heartbeat ?? source.checked_at,
      updated_at: source.updated_at ?? source.as_of ?? source.checked_at ?? null,
      attention: Array.isArray(source.attention) ? source.attention : [],
    };
  }

  function normalizeHealth(payload) {
    const source = firstObject(payload, ["health", "data"]);
    const status = String(source.status ?? source.state ?? source.progress_state ?? (source.healthy === true ? "healthy" : "degraded")).toLowerCase();
    const labels = { healthy: "Healthy", stalled: "Stalled", degraded: "Degraded", unknown: "Unknown" };
    return { status, label: source.label ?? labels[status] ?? status, detail: source.detail ?? source.message ?? source.next_action ?? "" };
  }

  function normalizeQueue(payload) {
    return arrayPayload(payload, ["items", "rows", "queue", "work_items"]).map((item) => ({
      id: item.id ?? item.work_item_id ?? item.job_id,
      state: item.state ?? item.lifecycle_state ?? "unknown",
      stage: item.stage ?? item.kind ?? item.experiment_id ?? "—",
      strategy: item.strategy ?? item.strategy_name ?? "—",
      route: item.route ?? ([item.symbol, item.timeframe].filter(Boolean).join(" · ") || item.claimed_by || "—"),
      priority: item.priority ?? item.priority_score,
      next_action: item.next_action ?? item.blocker_detail ?? item.blocker_code ?? item.blocker ?? item.finding ?? "—",
      raw: item,
    }));
  }

  function normalizeHpo(payload) {
    return arrayPayload(payload, ["studies", "rows", "items", "hpo_studies"]).map((item) => ({
      id: item.id ?? item.study_id ?? "—", name: item.name ?? item.study_name ?? "HPO study",
      strategy: item.strategy ?? "—", state: item.state ?? item.lifecycle_state ?? "unknown",
      completed_trials: number(item.completed_trials ?? item.completed_trial_count),
      total_trials: number(item.total_trials ?? item.trial_count), selected: number(item.selected ?? item.selected_trial_count),
      validation: number(item.validation ?? item.validation_count), next_action: item.next_action ?? item.finding ?? "Review study evidence", raw: item,
    }));
  }

  function normalizeControl(payload) {
    const source = firstObject(payload, ["control", "data"]);
    const supervisor = payload?.supervisor || {};
    return { available: true, desired_state: source.desired_state ?? "unknown", supervisor_phase: supervisor.phase ?? "not_reported", process_id: supervisor.process_id ?? null };
  }

  async function loadSnapshot(client) {
    const entries = await Promise.allSettled([
      client.getJson(client.routes.summary), client.getJson(client.routes.health),
      client.getJson(client.routes.queue), client.getJson(client.routes.hpoStudies), client.getJson(client.routes.control),
    ]);
    const [summary, health, queue, hpoStudies, control] = entries;
    const dataEntries = entries.slice(0, 4);
    const failures = dataEntries.filter((entry) => entry.status === "rejected").map((entry) => entry.reason?.message || "request failed");
    return {
      summary: summary.status === "fulfilled" ? normalizeSummary(summary.value) : DEMO.summary,
      health: health.status === "fulfilled" ? normalizeHealth(health.value) : { status: "degraded", label: "Health unavailable", detail: "Health endpoint failed" },
      queue: queue.status === "fulfilled" ? normalizeQueue(queue.value) : DEMO.queue,
      hpoStudies: hpoStudies.status === "fulfilled" ? normalizeHpo(hpoStudies.value) : DEMO.hpoStudies,
      control: control.status === "fulfilled" ? normalizeControl(control.value) : DEMO.control,
      failures, controlFailure: control.status === "rejected", demo: failures.length === dataEntries.length,
    };
  }

  function statusClass(value) {
    const stateValue = String(value || "unknown").toLowerCase();
    if (["healthy", "running", "completed", "finished", "delivered", "ready"].includes(stateValue)) return stateValue === "ready" ? "ready" : "healthy";
    if (["waiting_retry", "retry", "requirements_pending", "waiting"].includes(stateValue)) return "waiting";
    if (["blocked", "failed", "error", "degraded", "stalled"].includes(stateValue)) return "blocked";
    if (["hpo_candidate", "paper_trade_candidate", "candidate", "hpo_analysis", "validation"].includes(stateValue)) return "candidate";
    return "unknown";
  }

  function statusPill(value) {
    return `<span class="status-pill" data-status="${statusClass(value)}">${escapeHtml(value || "unknown")}</span>`;
  }

  function formatTime(value, fallback = "—") {
    if (!value) return fallback;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? text(value, fallback) : date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
  }

  function formatMetric(value, suffix = "") {
    return value === null || value === undefined ? "—" : `${number(value).toLocaleString(undefined, { maximumFractionDigits: 2 })}${suffix}`;
  }

  function renderSummary(snapshot) {
    const summary = snapshot.summary;
    $("#queue-count").textContent = text(summary.queue);
    $("#queue-footnote").textContent = `Ready ${text(summary.ready)} · Running ${text(summary.running)}`;
    $("#running-count").textContent = text(summary.running);
    $("#running-footnote").textContent = `Heartbeat ${formatTime(summary.heartbeat_at)}`;
    $("#hpo-count").textContent = text(summary.hpo_active);
    $("#hpo-footnote").textContent = `Waiting ${text(summary.hpo_waiting)} · Validation ${text(summary.validation)}`;
    $("#candidate-count").textContent = text(summary.candidates);
    $("#candidate-footnote").textContent = `Evidence gate ${summary.candidates ? "review" : "clear"}`;
  }

  function renderHealth(snapshot) {
    const badge = $("#health-status");
    badge.dataset.status = statusClass(snapshot.health.status);
    $("#health-label").textContent = snapshot.health.label || "Unknown";
    $("#snapshot-source").textContent = snapshot.demo ? "Demo fallback" : (snapshot.failures.length ? "Partial API" : "Live API");
    $("#last-updated").textContent = `Updated ${formatTime(snapshot.summary.updated_at, "just now")}`;
  }

  function renderControl(snapshot) {
    const control = snapshot.control;
    $("#control-state").textContent = control.available ? `Desired ${control.desired_state} · ${control.supervisor_phase}` : "Control API unavailable";
    document.querySelectorAll("[data-control]").forEach((button) => { button.disabled = !control.available; });
    if (snapshot.controlFailure) $("#control-output").textContent = "Controls unavailable. Open the live loopback Control Room or use the CLI.";
  }

  function attentionItems(snapshot, payload = null) {
    if (payload) return Array.isArray(payload.items) ? payload.items : [];
    const items = [...snapshot.summary.attention];
    if (snapshot.failures.length) items.unshift({ severity: "critical", title: "API snapshot incomplete", detail: `${snapshot.failures.length} endpoint${snapshot.failures.length === 1 ? "" : "s"} unavailable` });
    if (snapshot.summary.blocked) items.push({ severity: "critical", title: `${snapshot.summary.blocked} blocked queue item${snapshot.summary.blocked === 1 ? "" : "s"}`, detail: "Review requirements or blocker detail" });
    if (snapshot.summary.hpo_waiting) items.push({ severity: "info", title: `${snapshot.summary.hpo_waiting} HPO study waiting`, detail: "Check route or trial readiness" });
    if (snapshot.summary.unresolved_execution_claims) items.push({ id: "execution-claims", severity: "critical", title: `${snapshot.summary.unresolved_execution_claims} unresolved execution claim${snapshot.summary.unresolved_execution_claims === 1 ? "" : "s"}`, detail: "Recover or inspect running claims before restarting work" });
    if (snapshot.summary.candidates) items.push({ severity: "candidate", title: `${snapshot.summary.candidates} candidate${snapshot.summary.candidates === 1 ? "" : "s"} need review`, detail: "Promotion remains gated" });
    return items;
  }

  function renderAttention(snapshot) {
    const items = attentionItems(snapshot);
    $("#attention-count").textContent = `${items.length} item${items.length === 1 ? "" : "s"}`;
    $("#attention-list").innerHTML = items.length ? items.slice(0, 6).map((item) => {
      const view = item.work_item_id ? ` data-detail-work-item="${escapeHtml(item.work_item_id)}"` : item.id === "execution-claims" ? ` data-view="attention"` : "";
      return `<li class="attention-item" data-severity="${escapeHtml(item.severity || "info")}"${view}><div><strong>${escapeHtml(item.title || item.message || "Attention")}</strong><span>${escapeHtml(item.detail || item.next_action || "Review operator state")}</span></div></li>`;
    }).join("") : '<li class="empty-state">No operator attention items.</li>';
  }

  function queueRows(rows, empty = "No queue items returned.") {
    return rows.length ? rows.map((item) => `<tr data-detail-work-item="${escapeHtml(item.id)}"><td><strong>${escapeHtml(item.id)}</strong><small>${escapeHtml(item.stage)}</small></td><td>${statusPill(item.state)}</td><td><strong>${escapeHtml(item.strategy)}</strong><small>${escapeHtml(item.route)}</small></td><td>${escapeHtml(item.priority ?? "—")}</td><td>${escapeHtml(item.next_action)}</td></tr>`).join("") : `<tr><td class="empty-state" colspan="5">${escapeHtml(empty)}</td></tr>`;
  }

  function renderQueue(snapshot) {
    $("#queue-table-body").innerHTML = queueRows(snapshot.queue.slice(0, 12));
  }

  function hpoCards(studies) {
    return studies.length ? studies.slice(0, 8).map((study) => {
      const total = Math.max(number(study.total_trials), 0);
      const completed = Math.min(number(study.completed_trials), total || number(study.completed_trials));
      const progress = total ? Math.round((completed / total) * 100) : 0;
      return `<article class="hpo-item" data-detail-hpo="${escapeHtml(study.id)}"><div class="hpo-item-header"><div><h3>${escapeHtml(study.name)}</h3><p>${escapeHtml(study.id)} · ${escapeHtml(study.strategy)}</p></div>${statusPill(study.state)}</div><div class="progress-track" role="progressbar" aria-label="${escapeHtml(study.name)} trial progress" aria-valuemin="0" aria-valuemax="${total || 1}" aria-valuenow="${completed}"><div class="progress-bar" style="width: ${progress}%"></div></div><div class="hpo-meta"><span>${completed}/${total || "—"} trials · ${number(study.selected)} selected</span><span>${number(study.validation)} validation</span></div><p class="card-footnote">Next: ${escapeHtml(study.next_action)}</p></article>`;
    }).join("") : '<p class="empty-state">No HPO studies returned.</p>';
  }

  function renderHpo(snapshot) { $("#hpo-list").innerHTML = hpoCards(snapshot.hpoStudies); }

  function renderStale(snapshot) {
    const updated = Date.parse(snapshot.summary.updated_at || "");
    const old = Number.isFinite(updated) && Date.now() - updated > STALE_AFTER_MS;
    const stale = snapshot.demo || snapshot.failures.length > 0 || old;
    $("#stale-banner").hidden = !stale;
    $("#start-api-button").hidden = !snapshot.demo;
    if (stale) {
      $("#stale-title").textContent = snapshot.demo ? "Control API offline." : snapshot.failures.length ? "API snapshot incomplete." : "Data may be stale.";
      $("#stale-message").textContent = snapshot.demo ? "This file view cannot launch Python. Start the local API, then open the live Control Room." : snapshot.failures.length ? "Some API endpoints failed. Values may be incomplete." : "Last API snapshot is older than 90 seconds. Refresh before acting.";
    }
  }

  function setView(view) {
    const valid = ["dashboard", "queue", "running", "hpo", "candidates", "attention", "backtests"];
    state.view = valid.includes(view) ? view : "dashboard";
    document.querySelectorAll("[data-view]").forEach((element) => {
      if (element.classList.contains("view-nav-button")) element.classList.toggle("active", element.dataset.view === state.view);
    });
    $("#dashboard-view").hidden = state.view !== "dashboard";
    $("#detail-view").hidden = state.view === "dashboard";
    if (state.view !== "dashboard") loadView(state.view);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function detailHeader(kicker, title, caption = "") {
    return `<div class="detail-header"><div><p class="eyebrow">${escapeHtml(kicker)}</p><h2>${escapeHtml(title)}</h2><p>${escapeHtml(caption)}</p></div><button class="detail-back" type="button" data-view="dashboard">← Overview</button></div>`;
  }

  function detailNotice(message) { return `<div class="notice">${escapeHtml(message)}</div>`; }

  function renderQueueView(rows, view) {
    const running = view === "running";
    const filtered = rows.filter((row) => running ? row.state === "running" : row.state !== "running");
    const title = running ? "Running" : "Queue";
    const caption = running ? "Active execution claims and worker-owned items." : "Every unresolved item, with next action and blocker state.";
    $("#detail-view").innerHTML = `${detailHeader("Execution lane", title, caption)}<div class="filter-bar"><label>Search <input id="queue-filter" type="search" placeholder="work item or strategy"></label><button type="button" data-filter-queue>Apply</button><span class="panel-caption">${filtered.length} shown</span></div><div class="panel"><div class="table-wrap"><table class="detail-table"><thead><tr><th>Work item</th><th>State</th><th>Strategy / route</th><th>Priority</th><th>Next action</th></tr></thead><tbody id="detail-queue-body">${queueRows(filtered)}</tbody></table></div></div>`;
    const applyFilter = () => {
      const needle = $("#queue-filter").value.trim().toLowerCase();
      $("#detail-queue-body").innerHTML = queueRows(filtered.filter((row) => `${row.id} ${row.strategy} ${row.route}`.toLowerCase().includes(needle)));
    };
    $("#queue-filter").addEventListener("input", applyFilter);
    $("[data-filter-queue]").addEventListener("click", applyFilter);
  }

  function renderHpoView(studies) {
    $("#detail-view").innerHTML = `${detailHeader("Optimizer lane", "HPO studies", "Study progress, selected trials, and route readiness.")}<div class="hpo-list">${hpoCards(studies)}</div>`;
  }

  function renderCandidatesView(rows) {
    const candidates = rows.filter((row) => ["paper_trade_candidate", "hpo_candidate", "candidate"].includes(String(row.verdict || "").toLowerCase()) || String(row.lifecycle_stage || "").toLowerCase() === "paper_trade");
    const body = candidates.length ? candidates.map((row) => `<tr data-detail-evidence="${escapeHtml(row.run_id || "")}"><td><strong>${escapeHtml(row.strategy)}</strong><small>${escapeHtml(row.experiment_id)}</small></td><td>${statusPill(row.verdict || row.lifecycle_stage)}</td><td>${escapeHtml(row.symbol || "—")} · ${escapeHtml(row.timeframe || "—")}</td><td>${formatMetric(row.net_profit_percentage, "%")}</td><td>${escapeHtml(row.next_action || "Review evidence")}</td></tr>`).join("") : `<tr><td class="empty-state" colspan="5">No candidate evidence returned.</td></tr>`;
    $("#detail-view").innerHTML = `${detailHeader("Evidence lane", "Candidates", "Candidate rows remain provisional until train, out-of-sample, rolling, and promotion gates pass.")}<div class="panel"><div class="table-wrap"><table class="detail-table"><thead><tr><th>Strategy / experiment</th><th>Verdict</th><th>Route</th><th>Profit</th><th>Next action</th></tr></thead><tbody>${body}</tbody></table></div></div>`;
  }

  function renderAttentionView(items) {
    const body = items.length ? items.map((item) => {
      const action = item.work_item_id ? ` data-detail-work-item="${escapeHtml(item.work_item_id)}"` : "";
      return `<tr${action}><td>${statusPill(item.severity)}</td><td><strong>${escapeHtml(item.title || "Attention")}</strong><small>${escapeHtml(item.kind || "operator")}</small></td><td>${escapeHtml(item.detail || "—")}</td><td>${escapeHtml(item.next_action || item.resolution || "Review")}</td></tr>`;
    }).join("") : `<tr><td class="empty-state" colspan="4">No operator attention items.</td></tr>`;
    $("#detail-view").innerHTML = `${detailHeader("Operator lane", "Needs attention", "Each item leads to a bounded inspection or resolution path.")}<div class="panel"><div class="table-wrap"><table class="detail-table"><thead><tr><th>Severity</th><th>Item</th><th>Detail</th><th>Next action</th></tr></thead><tbody>${body}</tbody></table></div></div>`;
  }

  function statCard(label, value) { return `<div class="stat-card"><strong>${escapeHtml(value)}</strong><span>${escapeHtml(label)}</span></div>`; }

  function renderBacktestsView(payload) {
    const stats = payload.statistics || {};
    const options = payload.options || {};
    const rows = payload.rows || [];
    const select = (name, label, values) => `<label>${label}<select name="${name}"><option value="">Any</option>${(values || []).map((value) => `<option value="${escapeHtml(value)}">${escapeHtml(value)}</option>`).join("")}</select></label>`;
    const body = rows.length ? rows.map((row) => `<tr data-detail-evidence="${escapeHtml(row.run_id || "")}"><td><strong>${escapeHtml(row.strategy || "—")}</strong><small>${escapeHtml(row.experiment_id || "—")}</small></td><td>${escapeHtml(row.symbol || "—")} · ${escapeHtml(row.timeframe || "—")}</td><td>${escapeHtml(row.evidence_split || row.lifecycle_stage || "—")}</td><td>${formatMetric(row.trade_count)}</td><td>${formatMetric(row.net_profit_percentage, "%")}</td><td>${formatMetric(row.sharpe_ratio)}</td><td>${formatMetric(row.max_drawdown_percentage, "%")}</td><td>${statusPill(row.verdict || "reported")}</td></tr>`).join("") : `<tr><td class="empty-state" colspan="8">No backtest evidence matches these filters.</td></tr>`;
    $("#detail-view").innerHTML = `${detailHeader("Research database", "Backtests / DB", "Query canonical normalized evidence. Reported metrics stay separate from promotion decisions.")}<form class="filter-bar" id="backtest-filters"><label>Search <input name="q" type="search" value="${escapeHtml(payload.filters?.q || "")}" placeholder="strategy, run, finding"></label>${select("strategy", "Strategy", options.strategies)}${select("verdict", "Verdict", options.verdicts)}${select("symbol", "Symbol", options.symbols)}${select("timeframe", "Timeframe", options.timeframes)}${select("evidence_split", "Split", options.splits)}<label>Min trades <input name="minimum_trades" type="number" min="0" step="1" value="${escapeHtml(payload.filters?.minimum_trades || "0")}"></label><label>Sort <select name="sort"><option value="newest">Newest</option><option value="profit">Profit</option><option value="sharpe">Sharpe</option><option value="trades">Trades</option><option value="drawdown">Drawdown</option></select></label><button type="submit">Apply filters</button></form><div class="stat-grid">${statCard("Reported runs", formatMetric(stats.reported_runs))}${statCard("Metric runs", formatMetric(stats.metric_runs))}${statCard("Total trades", formatMetric(stats.total_trades))}${statCard("Best profit", formatMetric(stats.best_profit_percentage, "%"))}${statCard("Worst drawdown", formatMetric(stats.worst_drawdown_percentage, "%"))}${statCard("Average Sharpe", formatMetric(stats.average_sharpe_ratio))}</div><div class="panel"><div class="table-wrap"><table class="detail-table"><thead><tr><th>Strategy / experiment</th><th>Route</th><th>Split</th><th>Trades</th><th>Profit</th><th>Sharpe</th><th>Drawdown</th><th>Verdict</th></tr></thead><tbody>${body}</tbody></table></div></div>`;
    const form = $("#backtest-filters");
    for (const [name, value] of Object.entries(payload.filters || {})) if (form.elements[name]) form.elements[name].value = value;
    form.addEventListener("submit", (event) => { event.preventDefault(); loadBacktests(new URLSearchParams(new FormData(form))); });
  }

  function renderWorkItemDetail(payload) {
    const item = payload.work_item || {};
    const evidence = payload.evidence || [];
    const timings = payload.stage_timings || [];
    const events = payload.events || [];
    const rows = evidence.length ? evidence.map((row) => `<tr data-detail-evidence="${escapeHtml(row.run_id || "")}"><td>${escapeHtml(row.symbol || "—")} · ${escapeHtml(row.timeframe || "—")}</td><td>${escapeHtml(row.evidence_split || "—")}</td><td>${formatMetric(row.trade_count)}</td><td>${formatMetric(row.net_profit_percentage, "%")}</td><td>${statusPill(row.verdict || "reported")}</td></tr>`).join("") : `<tr><td class="empty-state" colspan="5">No normalized evidence attached.</td></tr>`;
    const timingList = timings.length ? timings.map((row) => `<li><strong>${escapeHtml(row.stage || row.kind || "stage")}</strong><span>${statusPill(row.state || row.outcome || "unknown")} · ${escapeHtml(row.completed_at || row.started_at || "—")}</span></li>`).join("") : "<li class='empty-state'>No stage timings.</li>";
    const eventList = events.length ? events.map((row) => `<li><strong>${escapeHtml(row.event_type)}</strong><span>${escapeHtml(row.occurred_at)}</span></li>`).join("") : "<li class='empty-state'>No events.</li>";
    const form = item.state === "blocked" ? `<form class="resolution-form" id="resolution-form"><h3>Resolve blocker</h3><p class="muted">This reopens the item as ready and records an auditable resolution event.</p><label>Resolution code <input name="resolution_code" required maxlength="200" placeholder="verified_route"></label><label>Detail <textarea name="detail" required maxlength="4000" placeholder="What was checked or changed?"></textarea></label><button type="submit">Confirm resolution</button><p class="command-output" id="resolution-output" role="status"></p></form>` : `<div class="notice">Item is ${escapeHtml(item.state || "not blocked")}. Resolution form appears only for blocked items.</div>`;
    $("#detail-view").innerHTML = `${detailHeader("Work item", item.id || "Unknown item", `${item.strategy || "—"} · ${item.experiment_name || item.experiment_id || "—"}`)}<div class="stat-grid">${statCard("State", item.state || "—")}${statCard("Stage", item.stage || "—")}${statCard("Priority", formatMetric(item.priority))}${statCard("Attempts", formatMetric(item.attempt_count))}</div><div class="panel"><h3>Evidence</h3><div class="table-wrap"><table class="detail-table"><thead><tr><th>Route</th><th>Split</th><th>Trades</th><th>Profit</th><th>Verdict</th></tr></thead><tbody>${rows}</tbody></table></div></div><div class="content-grid"><div class="panel"><h3>Stage timings</h3><ul class="timeline-list">${timingList}</ul></div><div class="panel"><h3>Recent events</h3><ul class="timeline-list">${eventList}</ul></div></div>${form}`;
    if (item.state === "blocked") $("#resolution-form").addEventListener("submit", (event) => resolveWorkItem(event, item.id));
  }

  function renderEvidenceDetail(payload) {
    const run = payload.run || {};
    const evidence = payload.evidence || [];
    const body = evidence.length ? evidence.map((row) => `<tr><td>${escapeHtml(row.strategy || "—")}</td><td>${escapeHtml(row.symbol || "—")} · ${escapeHtml(row.timeframe || "—")}</td><td>${escapeHtml(row.evidence_split || row.lifecycle_stage || "—")}</td><td>${formatMetric(row.trade_count)}</td><td>${formatMetric(row.net_profit_percentage, "%")}</td><td>${formatMetric(row.sharpe_ratio)}</td><td>${statusPill(row.verdict || "reported")}</td></tr>`).join("") : `<tr><td class="empty-state" colspan="7">No normalized evidence for this run.</td></tr>`;
    $("#detail-view").innerHTML = `${detailHeader("Backtest evidence", run.id || "Evidence detail", `${run.status || "reported"} · ${run.started_at || ""}`)}<div class="notice">Experiment ${escapeHtml(run.experiment_id || "—")} · Work item ${escapeHtml(run.work_item_id || "—")} · Session ${escapeHtml(run.session_id || "—")}</div><div class="panel"><div class="table-wrap"><table class="detail-table"><thead><tr><th>Strategy</th><th>Route</th><th>Split</th><th>Trades</th><th>Profit</th><th>Sharpe</th><th>Verdict</th></tr></thead><tbody>${body}</tbody></table></div></div>`;
  }

  async function loadBacktests(params = new URLSearchParams()) {
    $("#detail-view").innerHTML = detailNotice("Loading canonical evidence…");
    try { renderBacktestsView(await createApiClient().getJson(`${API_ROUTES.backtests}?${params.toString()}`)); }
    catch (error) { $("#detail-view").innerHTML = `${detailHeader("Research database", "Backtests / DB", "The live API is required for database queries.")}${detailNotice(error.message || "Backtest query failed")}`; }
  }

  async function loadView(view) {
    const detail = $("#detail-view");
    if (view === "backtests") { await loadBacktests(); return; }
    detail.innerHTML = detailNotice("Loading live view…");
    try {
      const api = createApiClient();
      if (view === "queue" || view === "running") {
        const payload = await api.getJson(`${API_ROUTES.queue}?limit=500`);
        renderQueueView(normalizeQueue(payload), view);
      } else if (view === "hpo") {
        renderHpoView(normalizeHpo(await api.getJson(API_ROUTES.hpoStudies)));
      } else if (view === "candidates") {
        renderCandidatesView(arrayPayload(await api.getJson("/api/v1/candidates"), ["rows", "items"]));
      } else if (view === "attention") {
        renderAttentionView(attentionItems(state.snapshot, await api.getJson(API_ROUTES.attention)));
      }
    } catch (error) { detail.innerHTML = `${detailHeader("Control Room", view, "Live API required for this view.")}${detailNotice(error.message || "View unavailable")}`; }
  }

  async function openWorkItem(id) {
    setView("dashboard");
    $("#dashboard-view").hidden = true; $("#detail-view").hidden = false; state.view = "item";
    $("#detail-view").innerHTML = detailNotice("Loading work item…");
    try { renderWorkItemDetail(await createApiClient().getJson(`/api/v1/work-items/${encodeURIComponent(id)}`)); }
    catch (error) { $("#detail-view").innerHTML = `${detailHeader("Work item", id)}${detailNotice(error.message || "Work item unavailable")}`; }
  }

  async function openEvidence(id) {
    if (!id) return;
    $("#dashboard-view").hidden = true; $("#detail-view").hidden = false; state.view = "evidence";
    $("#detail-view").innerHTML = detailNotice("Loading evidence…");
    try { renderEvidenceDetail(await createApiClient().getJson(`/api/v1/evidence/${encodeURIComponent(id)}`)); }
    catch (error) { $("#detail-view").innerHTML = `${detailHeader("Evidence", id)}${detailNotice(error.message || "Evidence unavailable")}`; }
  }

  async function openHpo(id) {
    if (!id || id === "—") return;
    $("#dashboard-view").hidden = true; $("#detail-view").hidden = false; state.view = "hpo-detail";
    $("#detail-view").innerHTML = detailNotice("Loading HPO study…");
    try {
      const payload = await createApiClient().getJson(`/api/v1/hpo/studies/${encodeURIComponent(id)}`);
      const study = payload.study || payload;
      $("#detail-view").innerHTML = `${detailHeader("Optimizer study", study.study_id || study.id || id, `${study.strategy || "—"} · ${study.lifecycle_state || study.state || "—"}`)}<div class="notice">${escapeHtml(study.next_action || study.finding || "Review study evidence")}</div><pre class="json-view">${escapeHtml(JSON.stringify(payload, null, 2))}</pre>`;
    } catch (error) { $("#detail-view").innerHTML = `${detailHeader("Optimizer study", id)}${detailNotice(error.message || "HPO detail unavailable")}`; }
  }

  async function resolveWorkItem(event, id) {
    event.preventDefault();
    if (!window.confirm(`Resolve blocked work item ${id} and return it to ready?`)) return;
    const form = event.currentTarget; const output = $("#resolution-output"); const payload = Object.fromEntries(new FormData(form).entries());
    output.textContent = "Recording resolution…";
    try { await createApiClient().postJson(`/api/v1/work-items/${encodeURIComponent(id)}/resolve`, payload, "resolve"); output.textContent = "Resolution recorded. Refreshing…"; await refresh(); await openWorkItem(id); }
    catch (error) { output.textContent = `Resolution failed: ${error.message || "request failed"}`; }
  }

  async function runCommand(action, button) {
    const output = $("#command-output"); button.disabled = true; output.textContent = `${action} running…`;
    try {
      const result = await createApiClient().postJson(`${API_ROUTES.commands}/${encodeURIComponent(action)}`, {}, "command");
      output.textContent = result.output || result.detail || `${action} complete (exit ${result.returncode ?? 0})`;
      if (result.output) output.title = result.output;
    } catch (error) { output.textContent = `Command unavailable: ${error.message || "local API required"}`; }
    finally { button.disabled = false; }
  }

  async function bindControls() {
    document.querySelectorAll("[data-control]").forEach((button) => button.addEventListener("click", async () => {
      const action = button.dataset.control;
      if (["pause", "stop"].includes(action) && !window.confirm(`Confirm research loop ${action}?`)) return;
      const output = $("#control-output"); button.disabled = true; output.textContent = `${action} requested…`;
      try {
        const result = await createApiClient().postJson(`${API_ROUTES.control}/${action}`, {}, action);
        output.textContent = `${action} accepted. Desired state: ${result.control?.desired_state || "updated"}.`;
        await refresh();
      } catch (error) { output.textContent = `Control failed: ${error.message || "request failed"}`; button.disabled = false; }
    }));
  }

  function bindEvents() {
    document.addEventListener("click", (event) => {
      const viewTarget = event.target.closest("[data-view]");
      if (viewTarget && !event.target.closest(".detail-table")) { setView(viewTarget.dataset.view); return; }
      const workItem = event.target.closest("[data-detail-work-item]"); if (workItem) { openWorkItem(workItem.dataset.detailWorkItem); return; }
      const evidence = event.target.closest("[data-detail-evidence]"); if (evidence) { openEvidence(evidence.dataset.detailEvidence); return; }
      const hpo = event.target.closest("[data-detail-hpo]"); if (hpo) { openHpo(hpo.dataset.detailHpo); return; }
      const command = event.target.closest("[data-command-action]"); if (command) { runCommand(command.dataset.commandAction, command); }
    });
    document.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && event.target.matches(".nav-card")) { event.preventDefault(); setView(event.target.dataset.view); }
    });
    $("#refresh-button").addEventListener("click", refresh);
    $("#start-api-button").addEventListener("click", async () => {
      const command = $("#start-api-command").textContent;
      $("#command-output").textContent = `Run in the ATS Lab repository: ${command}`;
      try { await navigator.clipboard.writeText(command); $("#command-output").textContent += " (copied)"; } catch (_) { /* visible command remains */ }
      $("#commands-title").scrollIntoView({ behavior: "smooth", block: "center" });
    });
    bindControls();
  }

  async function refresh() {
    const button = $("#refresh-button"); button.disabled = true; button.textContent = "Refreshing…";
    try {
      state.snapshot = await loadSnapshot(createApiClient());
      renderSummary(state.snapshot); renderHealth(state.snapshot); renderControl(state.snapshot); renderAttention(state.snapshot); renderQueue(state.snapshot); renderHpo(state.snapshot); renderStale(state.snapshot);
      if (state.view !== "dashboard" && !["item", "evidence", "hpo-detail"].includes(state.view)) loadView(state.view);
    } finally { button.disabled = false; button.textContent = "Refresh"; }
  }

  window.ATS_LAB_CONTROL_ROOM = Object.freeze({ API_ROUTES, createApiClient, normalizeSummary, normalizeHealth, normalizeQueue, normalizeHpo, normalizeControl, loadSnapshot, refresh });
  document.addEventListener("DOMContentLoaded", () => { bindEvents(); refresh(); state.refreshTimer = window.setInterval(refresh, REFRESH_INTERVAL_MS); });
}());
