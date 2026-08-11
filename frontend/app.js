/*
 * Static Control Room adapter.
 * Set window.ATS_LAB_API_BASE before this file when the API is on another origin.
 */
(function () {
  "use strict";

  const API_ROUTES = Object.freeze({
    summary: "/api/v1/summary",
    health: "/api/v1/health",
    queue: "/api/v1/queue",
    hpoStudies: "/api/v1/hpo/studies",
  });
  const API_BASE = String(window.ATS_LAB_API_BASE || "").replace(/\/$/, "");
  const REFRESH_INTERVAL_MS = 30000;
  const STALE_AFTER_MS = 90000;

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
    demo: true,
  };

  const $ = (selector) => document.querySelector(selector);
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
    return { getJson, routes };
  }

  function normalizeSummary(payload) {
    const source = firstObject(payload, ["summary", "data"]);
    return {
      queue: number(source.queue ?? source.unresolved ?? source.active_queue),
      ready: number(source.ready), running: number(source.running ?? source.active),
      retry: number(source.retry ?? source.waiting_retry), blocked: number(source.blocked),
      hpo_active: number(source.hpo_active ?? source.active_hpo), hpo_waiting: number(source.hpo_waiting ?? source.requirements_pending),
      validation: number(source.validation), candidates: number(source.candidates ?? source.candidate_count),
      heartbeat_at: source.heartbeat_at ?? source.last_heartbeat, updated_at: source.updated_at ?? source.as_of ?? null,
      attention: Array.isArray(source.attention) ? source.attention : [],
    };
  }

  function normalizeHealth(payload) {
    const source = firstObject(payload, ["health", "data"]);
    const status = String(source.status ?? source.state ?? (source.ok === true ? "healthy" : "unknown")).toLowerCase();
    return { status, label: source.label ?? status, detail: source.detail ?? source.message ?? "" };
  }

  function normalizeQueue(payload) {
    return arrayPayload(payload, ["items", "queue", "work_items"]).map((item) => ({
      id: item.id ?? item.work_item_id ?? item.job_id,
      state: item.state ?? item.lifecycle_state ?? "unknown",
      stage: item.stage ?? item.kind ?? "—",
      strategy: item.strategy ?? item.strategy_name ?? "—",
      route: item.route ?? [item.symbol, item.timeframe].filter(Boolean).join(" · "),
      priority: item.priority ?? item.priority_score,
      next_action: item.next_action ?? item.blocker ?? item.finding ?? "—",
    }));
  }

  function normalizeHpo(payload) {
    return arrayPayload(payload, ["studies", "items", "hpo_studies"]).map((item) => ({
      id: item.id ?? item.study_id ?? "—", name: item.name ?? item.study_name ?? "HPO study",
      strategy: item.strategy ?? "—", state: item.state ?? item.lifecycle_state ?? "unknown",
      completed_trials: number(item.completed_trials ?? item.completed_trial_count),
      total_trials: number(item.total_trials ?? item.trial_count), selected: number(item.selected ?? item.selected_trial_count),
      validation: number(item.validation ?? item.validation_count), next_action: item.next_action ?? item.finding ?? "—",
    }));
  }

  async function loadSnapshot(client) {
    const entries = await Promise.allSettled([
      client.getJson(client.routes.summary), client.getJson(client.routes.health),
      client.getJson(client.routes.queue), client.getJson(client.routes.hpoStudies),
    ]);
    const [summary, health, queue, hpoStudies] = entries;
    const failures = entries.filter((entry) => entry.status === "rejected").map((entry) => entry.reason?.message || "request failed");
    return {
      summary: summary.status === "fulfilled" ? normalizeSummary(summary.value) : DEMO.summary,
      health: health.status === "fulfilled" ? normalizeHealth(health.value) : { status: "degraded", label: "Health unavailable", detail: "Health endpoint failed" },
      queue: queue.status === "fulfilled" ? normalizeQueue(queue.value) : DEMO.queue,
      hpoStudies: hpoStudies.status === "fulfilled" ? normalizeHpo(hpoStudies.value) : DEMO.hpoStudies,
      failures, demo: failures.length === entries.length,
    };
  }

  function statusClass(state) {
    const value = String(state || "unknown").toLowerCase();
    if (["healthy", "running", "completed", "finished", "delivered"].includes(value)) return "healthy";
    if (["ready", "pending", "scheduled"].includes(value)) return "ready";
    if (["waiting_retry", "retry", "requirements_pending", "waiting"].includes(value)) return "waiting";
    if (["blocked", "failed", "error", "degraded"].includes(value)) return "blocked";
    if (["hpo_candidate", "paper_trade_candidate", "candidate", "hpo_analysis", "validation"].includes(value)) return "candidate";
    return "unknown";
  }

  function statusPill(state) {
    const value = text(state, "unknown");
    return `<span class="status-pill" data-status="${statusClass(value)}">${escapeHtml(value)}</span>`;
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
    const health = snapshot.health;
    const state = statusClass(health.status);
    const badge = $("#health-status");
    badge.dataset.status = state;
    $("#health-label").textContent = health.label || "Unknown";
    $("#snapshot-source").textContent = snapshot.demo ? "Demo fallback" : (snapshot.failures.length ? "Partial API" : "Live API");
    $("#last-updated").textContent = `Updated ${formatTime(snapshot.summary.updated_at, "just now")}`;
  }

  function renderAttention(snapshot) {
    const items = [...snapshot.summary.attention];
    if (snapshot.failures.length) items.unshift({ severity: "critical", title: "API snapshot incomplete", detail: `${snapshot.failures.length} endpoint${snapshot.failures.length === 1 ? "" : "s"} unavailable` });
    if (snapshot.summary.blocked) items.push({ severity: "critical", title: `${snapshot.summary.blocked} blocked queue item${snapshot.summary.blocked === 1 ? "" : "s"}`, detail: "Review requirements or blocker detail" });
    if (snapshot.summary.hpo_waiting) items.push({ severity: "info", title: `${snapshot.summary.hpo_waiting} HPO study waiting`, detail: "Check route or trial readiness" });
    if (snapshot.summary.candidates) items.push({ severity: "candidate", title: `${snapshot.summary.candidates} candidate${snapshot.summary.candidates === 1 ? "" : "s"} need review`, detail: "Promotion remains gated" });
    const list = $("#attention-list");
    $("#attention-count").textContent = `${items.length} item${items.length === 1 ? "" : "s"}`;
    list.innerHTML = items.length ? items.slice(0, 6).map((item) => `<li class="attention-item" data-severity="${escapeHtml(item.severity || "info")}"><div><strong>${escapeHtml(item.title || item.message || "Attention")}</strong><span>${escapeHtml(item.detail || item.next_action || "Review operator state")}</span></div></li>`).join("") : '<li class="empty-state">No operator attention items.</li>';
  }

  function renderQueue(snapshot) {
    const body = $("#queue-table-body");
    body.innerHTML = snapshot.queue.length ? snapshot.queue.slice(0, 12).map((item) => `<tr><td><strong>${escapeHtml(item.id)}</strong><small>${escapeHtml(item.stage)}</small></td><td>${statusPill(item.state)}</td><td><strong>${escapeHtml(item.strategy)}</strong><small>${escapeHtml(item.route)}</small></td><td>${escapeHtml(item.priority ?? "—")}</td><td>${escapeHtml(item.next_action)}</td></tr>`).join("") : '<tr><td class="empty-state" colspan="5">No queue items returned.</td></tr>';
  }

  function renderHpo(snapshot) {
    const list = $("#hpo-list");
    list.innerHTML = snapshot.hpoStudies.length ? snapshot.hpoStudies.slice(0, 8).map((study) => {
      const total = Math.max(number(study.total_trials), 0);
      const completed = Math.min(number(study.completed_trials), total || number(study.completed_trials));
      const progress = total ? Math.round((completed / total) * 100) : 0;
      return `<article class="hpo-item"><div class="hpo-item-header"><div><h3>${escapeHtml(study.name)}</h3><p>${escapeHtml(study.id)} · ${escapeHtml(study.strategy)}</p></div>${statusPill(study.state)}</div><div class="progress-track" role="progressbar" aria-label="${escapeHtml(study.name)} trial progress" aria-valuemin="0" aria-valuemax="${total || 1}" aria-valuenow="${completed}"><div class="progress-bar" style="width: ${progress}%"></div></div><div class="hpo-meta"><span>${completed}/${total || "—"} trials · ${number(study.selected)} selected</span><span>${number(study.validation)} validation</span></div><p class="card-footnote">Next: ${escapeHtml(study.next_action)}</p></article>`;
    }).join("") : '<p class="empty-state">No HPO studies returned.</p>';
  }

  function renderStale(snapshot) {
    const updated = Date.parse(snapshot.summary.updated_at || "");
    const old = Number.isFinite(updated) && Date.now() - updated > STALE_AFTER_MS;
    const stale = snapshot.demo || snapshot.failures.length > 0 || old;
    $("#stale-banner").hidden = !stale;
    if (stale) $("#stale-message").textContent = snapshot.demo ? "Live API unavailable. Showing safe demo values; no operator action is connected." : snapshot.failures.length ? "Some API endpoints failed. Values may be mixed with the last safe fallback." : "Last API snapshot is older than 90 seconds. Refresh before acting.";
  }

  function formatTime(value, fallback = "—") {
    if (!value) return fallback;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? text(value, fallback) : date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function bindCommands() {
    document.querySelectorAll("[data-command]").forEach((button) => button.addEventListener("click", () => {
      const command = button.dataset.command;
      $("#command-output").textContent = `Placeholder only: copy and run “${command}” in the operator terminal. No command was executed.`;
    }));
  }

  async function refresh() {
    const button = $("#refresh-button");
    button.disabled = true;
    button.textContent = "Refreshing…";
    try {
      const snapshot = await loadSnapshot(createApiClient());
      renderSummary(snapshot); renderHealth(snapshot); renderAttention(snapshot); renderQueue(snapshot); renderHpo(snapshot); renderStale(snapshot);
    } finally {
      button.disabled = false;
      button.textContent = "Refresh";
    }
  }

  window.ATS_LAB_CONTROL_ROOM = Object.freeze({ API_ROUTES, createApiClient, normalizeSummary, normalizeHealth, normalizeQueue, normalizeHpo, loadSnapshot, refresh });
  document.addEventListener("DOMContentLoaded", () => { bindCommands(); refresh(); window.setInterval(refresh, REFRESH_INTERVAL_MS); });
}());
