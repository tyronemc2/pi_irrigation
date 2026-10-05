"use strict";
const $ = (s) => document.querySelector(s);
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let state = null, fetchedAt = 0, editing = null, editingRule = null, failures = 0;
let temps = null, tempSel = null, chartKey = "";

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const mmss = (s) => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso), now = new Date();
  const hm = d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false});
  if (d.toDateString() === now.toDateString()) return `today ${hm}`;
  const tmr = new Date(now); tmr.setDate(now.getDate() + 1);
  if (d.toDateString() === tmr.toDateString()) return `tomorrow ${hm}`;
  return `${DAYS[(d.getDay() + 6) % 7]} ${hm}`;
}
function shortTime(iso) {
  const d = new Date(iso);
  const hm = d.toLocaleTimeString([], {hour: "2-digit", minute: "2-digit", hour12: false});
  return d.toDateString() === new Date().toDateString() ? hm : `${DAYS[(d.getDay() + 6) % 7]} ${hm}`;
}
function dayText(days) {
  const k = days.join();
  if (k === "0,1,2,3,4,5,6") return "Every day";
  if (k === "0,1,2,3,4") return "Weekdays";
  if (k === "5,6") return "Weekends";
  return days.map((d) => DAYS[d]).join(", ");
}

// ---------- API ----------
async function api(path, method = "GET", body) {
  const res = await fetch(path, {method, headers: body ? {"Content-Type": "application/json"} : {},
    body: body ? JSON.stringify(body) : undefined, cache: "no-store"});
  let data = {};
  try { data = await res.json(); } catch (_) { /* non-JSON error */ }
  if (!res.ok) throw new Error(data.message || `Request failed (${res.status})`);
  return data;
}
async function act(path, body, method = "POST") {
  try {
    const r = await api(path, method, body);
    toast(r.message || "Done");
    await refresh();
    return r;
  } catch (e) { toast(e.message, true); throw e; }
}
let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg; t.className = "toast" + (bad ? " bad" : ""); t.hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => (t.hidden = true), 3500);
}

// ---------- rendering ----------
function renderLink() {
  const el = $("#link"), text = $("#link-text");
  if (failures > 0 && !state) { el.className = "link bad"; text.textContent = "Can't reach the Pi"; return; }
  if (failures > 1) { el.className = "link bad"; text.textContent = "Pi not responding"; return; }
  if (!state) return;
  const c = state.controller;
  if (!c.online) { el.className = "link bad"; text.textContent = "Greenhouse offline"; }
  else { el.className = "link ok"; text.textContent = c.mode === "simulated" ? "Simulation" : "Online"; }
}

function renderTank() {
  const t = state.tank, sec = $(".tank");
  const level = {Full: .9, OK: .6, Low: .18}[t.label] ?? .5;
  const h = 166 * level, w = $("#water");
  w.setAttribute("y", 180 - h); w.setAttribute("height", h);
  sec.classList.toggle("unknown", t.low_ok === null);
  sec.classList.toggle("low", t.low_ok === false);
  $("#float-high").classList.toggle("wet", t.high_reached === true);
  $("#float-low").classList.toggle("wet", t.low_ok === true);
  $("#tank-state").textContent = t.enabled === false ? "Not monitored" : t.label === "Unknown" ? "Unknown" :
    t.label === "Low" ? "Low" : t.label === "Full" ? "Full" : "OK";
  const r = state.refill;
  let refill = "Not connected";
  if (r && r.enabled) refill = r.fault ? (r.fault_reason || "Fault") : r.running ? `Filling, ${Math.floor(r.running_s / 60)} min so far` : "Standing by";
  else if (r) refill = "Manual";
  $("#refill").textContent = state.controller.online ? refill : "–";
  $("#refill-reset").hidden = !(r && r.fault);
  const p = state.pump;
  $("#pump").textContent = !state.controller.online || !p ? "–" : !p.enabled ? "Runs on its own pressure switch" : p.on ? "Running" : "Off";
}

function zoneState(z) {
  const run = state.running;
  if (run && run.zone_id === z.id) {
    const left = Math.max(0, run.remaining_s - Math.floor((Date.now() - fetchedAt) / 1000));
    return `Watering, ${mmss(left)} left`;
  }
  if (state.queue.some((q) => q.zone_id === z.id)) return "Waiting its turn";
  if (z.on) return "Valve open";
  return z.next_run ? `Next ${when(z.next_run)}` : "No schedule";
}

function renderZones() {
  const online = state.controller.online;
  $("#zones").innerHTML = state.zones.map((z) => {
    const running = (state.running && state.running.zone_id === z.id) || z.on;
    const queued = state.queue.some((q) => q.zone_id === z.id);
    let body = "";
    if (z.sensors.length) {
      const m = z.moisture;
      body += `<div class="moisture"><strong>${m == null ? "–" : Math.round(m) + "%"}</strong><span>soil moisture</span></div>
        <div class="probes">${z.sensors.map((s) => `<div class="probe">${esc(s.name)} ${s.percent == null ? "no reading" : Math.round(s.percent) + "%"}
        <div class="bar"><i style="width:${s.percent ?? 0}%"></i></div></div>`).join("")}</div>`;
      if (z.skip_if_wet) body += `<p class="zone-note">Scheduled watering is skipped above ${state.limits.moist_threshold}%.</p>`;
    }
    if (running && state.flow_lpm != null && state.running && state.running.litres != null) {
      body += `<p class="zone-note">${state.flow_lpm.toFixed(1)} L/min, ${state.running.litres} L so far</p>`;
    }
    return `<article class="zone${running ? " on" : ""}">
      <div class="zone-head"><h3>${esc(z.name)}</h3><p class="state">${esc(zoneState(z))}</p></div>
      ${body}
      <div class="run-row"><span>Water now</span>
        ${[5, 10, 15].map((m) => `<button class="btn small water" data-run="${z.id}" data-min="${m}" ${online && !running && !queued ? "" : "disabled"}>${m} min</button>`).join("")}
      </div></article>`;
  }).join("");
}

function renderSchedules() {
  const zones = Object.fromEntries(state.zones.map((z) => [z.id, z.name]));
  const list = state.schedules;
  $("#schedules").innerHTML = list.length ? list.map((s) => {
    const what = s.kind === "cycle"
      ? `${s.minutes} min every ${s.every_minutes} min, ${s.start} to ${s.end}`
      : `${s.minutes} min at ${s.start}`;
    return `<li class="sched${s.enabled ? "" : " off"}"><div class="sched-main"><b>${esc(zones[s.zone_id] || "Unknown zone")}</b>
      <small>${esc(what)}. ${esc(dayText(s.days))}${s.enabled ? "" : ". Turned off"}</small></div>
      <button class="btn small ghost" data-edit="${s.id}">Edit</button></li>`;
  }).join("") : `<li class="empty">No schedules yet. Add one to water automatically.</li>`;
  const p = $("#paused");
  if (state.paused_until) {
    p.hidden = false;
    p.innerHTML = `<span>Schedules paused until ${esc(when(state.paused_until))}</span><button class="btn small" id="resume">Resume</button>`;
  } else p.hidden = true;
}

function historyText(h) {
  const amount = h.litres != null ? `, ${h.litres} L` : "";
  switch (h.event) {
    case "ran": return `${h.zone} watered for ${h.minutes} min${amount} (${h.reason.charAt(0).toLowerCase() + h.reason.slice(1)})`;
    case "skipped": return `${h.zone} skipped: ${h.reason}`;
    case "stopped": return `${h.zone} stopped after ${h.minutes} min${amount}: ${h.reason}`;
    default: return h.detail ? `${h.reason}: ${h.detail}` : h.reason;
  }
}
function renderHistory() {
  $("#history").innerHTML = state.history.length ? state.history.map((h) =>
    `<li class="ev-${esc(h.event)}"><time datetime="${esc(h.time)}">${esc(shortTime(h.time))}</time><span>${esc(historyText(h))}</span></li>`
  ).join("") : `<li class="empty">Nothing has run yet.</li>`;
}

// ---------- greenhouse air temperature ----------
const CH = {W: 640, H: 230, L: 40, R: 10, T: 14, B: 30, SLOTS: 96};
const fmtT = (v) => `${v.toFixed(1)}\u00a0°C`;
const hourLabel = (t) => t.slice(11, 16);
const dayLabel = (t) => { const d = new Date(t); return `${DAYS[(d.getDay() + 6) % 7]} ${d.getDate()}`; };
const svgEl = (tag, attrs, text) => {
  const e = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text != null) e.textContent = text;
  return e;
};

async function fetchTemps() {
  try { temps = await api("/api/temperature"); renderClimate(true); } catch (_) { /* shown as stale */ }
}

function renderClimate(force = false) {
  if (state) $("#air-now").textContent = state.air_temp_c == null ? "No reading" : fmtT(state.air_temp_c);
  if (!temps) return;
  const rules = (state ? state.temp_rules : []).filter((r) => r.enabled);
  const key = JSON.stringify([temps.hours, rules.map((r) => [r.temp_c, r.when])]);
  if (!force && key === chartKey) return;
  chartKey = key;
  renderChart(rules); renderTempStats(); renderTempTable();
}

function renderChart(rules) {
  const svg = $("#temp-chart"), hours = temps.hours;
  CH.W = Math.round(Math.max(320, Math.min(900, svg.clientWidth || 640)));  // 1 unit = 1 px, so text stays readable on phones
  svg.setAttribute("viewBox", `0 0 ${CH.W} ${CH.H}`);
  const {W, H, L, R, T, B, SLOTS} = CH;
  svg.replaceChildren();
  const slot = (W - L - R) / SLOTS, x = (i) => L + (i + 0.5) * slot;
  const have = hours.filter((h) => h.avg != null);
  let lo = Math.min(...have.map((h) => h.min), ...rules.map((r) => r.temp_c));
  let hi = Math.max(...have.map((h) => h.max), ...rules.map((r) => r.temp_c));
  if (!isFinite(lo)) { lo = 10; hi = 30; }
  lo = Math.floor(lo) - 1; hi = Math.ceil(hi) + 1;
  if (hi - lo < 8) { const mid = (hi + lo) / 2; lo = Math.floor(mid - 4); hi = Math.ceil(mid + 4); }
  const step = hi - lo <= 14 ? 2 : hi - lo <= 35 ? 5 : 10;
  const y = (v) => T + (hi - v) / (hi - lo) * (H - T - B);
  for (let v = Math.ceil(lo / step) * step; v <= hi; v += step) {
    svg.append(svgEl("line", {x1: L, x2: W - R, y1: y(v), y2: y(v), class: "grid"}));
    svg.append(svgEl("text", {x: L - 6, y: y(v) + 4, "text-anchor": "end", class: "axis"}, `${v}°`));
  }
  for (let d = 0; d < SLOTS / 24; d++) {
    if (d) svg.append(svgEl("line", {x1: L + d * 24 * slot, x2: L + d * 24 * slot, y1: T, y2: H - B, class: "midnight"}));
    const first = hours[d * 24];
    if (first) svg.append(svgEl("text", {x: L + (d * 24 + 12) * slot, y: H - 9, "text-anchor": "middle", class: "day"}, dayLabel(first.t)));
  }
  // min-max band and average line, broken where hours are missing
  let seg = [];
  const flush = () => {
    if (!seg.length) return;
    if (seg.length === 1) {
      svg.append(svgEl("circle", {cx: x(seg[0].i), cy: y(seg[0].avg), r: 3, class: "cursor-dot"}));
    } else {
      const top = seg.map((p) => `${x(p.i)},${y(p.max)}`), bot = seg.slice().reverse().map((p) => `${x(p.i)},${y(p.min)}`);
      svg.append(svgEl("polygon", {points: [...top, ...bot].join(" "), class: "band"}));
      svg.append(svgEl("path", {d: "M" + seg.map((p) => `${x(p.i)} ${y(p.avg)}`).join(" L"), class: "line"}));
    }
    seg = [];
  };
  hours.forEach((h, i) => { if (h.avg == null) flush(); else seg.push({...h, i}); });
  flush();
  for (const r of rules) {
    svg.append(svgEl("line", {x1: L, x2: W - R, y1: y(r.temp_c), y2: y(r.temp_c), class: "rule"}));
    svg.append(svgEl("text", {x: W - R - 4, y: y(r.temp_c) - 5, "text-anchor": "end", class: "rule-label"},
      `${r.when === "above" ? "above" : "below"} ${r.temp_c}°: water`));
  }
  if (!have.length) {
    svg.append(svgEl("text", {x: (L + W - R) / 2, y: (T + H - B) / 2, "text-anchor": "middle", class: "empty-msg"},
      "No readings yet. Points appear as each hour fills in."));
  }
  const cur = svgEl("g", {class: "cursor-g", visibility: "hidden"});
  cur.append(svgEl("line", {y1: T, y2: H - B, class: "cursor"}), svgEl("circle", {r: 5, class: "cursor-dot"}));
  svg.append(cur);
  svg._geo = {x, y, cur};
  if (tempSel != null) selectHour(Math.min(tempSel, hours.length - 1));
}

function selectHour(i) {
  const hours = temps && temps.hours;
  if (!hours || !hours.length) return;
  tempSel = Math.max(0, Math.min(hours.length - 1, i));
  const h = hours[tempSel], g = $("#temp-chart")._geo;
  g.cur.setAttribute("visibility", "visible");
  g.cur.firstChild.setAttribute("x1", g.x(tempSel)); g.cur.firstChild.setAttribute("x2", g.x(tempSel));
  const dot = g.cur.lastChild;
  dot.setAttribute("visibility", h.avg == null ? "hidden" : "visible");
  if (h.avg != null) { dot.setAttribute("cx", g.x(tempSel)); dot.setAttribute("cy", g.y(h.avg)); }
  $("#temp-readout").innerHTML = `${esc(dayLabel(h.t))}, ${esc(hourLabel(h.t))} · ` + (h.avg == null ? "no reading"
    : `<b>${esc(fmtT(h.avg))}</b> (low ${esc(h.min.toFixed(1))}, high ${esc(h.max.toFixed(1))})`);
}

function extremes(list) {
  const have = list.filter((h) => h.avg != null);
  if (!have.length) return null;
  const lo = have.reduce((a, b) => (b.min < a.min ? b : a)), hi = have.reduce((a, b) => (b.max > a.max ? b : a));
  return {lo, hi};
}
function renderTempStats() {
  const today = temps.hours.filter((h) => h.t.slice(0, 10) === temps.now.slice(0, 10));
  const t = extremes(today), all = extremes(temps.hours);
  const parts = [];
  if (t) parts.push(`Today: low ${fmtT(t.lo.min)} at ${hourLabel(t.lo.t)}, high ${fmtT(t.hi.max)} at ${hourLabel(t.hi.t)}`);
  if (all) parts.push(`4 days: low ${fmtT(all.lo.min)} (${dayLabel(all.lo.t)}), high ${fmtT(all.hi.max)} (${dayLabel(all.hi.t)})`);
  $("#temp-stats").innerHTML = parts.map((p) => `<span>${esc(p)}</span>`).join("");
}

function renderTempTable() {
  const days = [];
  for (let d = 0; d * 24 < temps.hours.length; d++) days.push(temps.hours.slice(d * 24, d * 24 + 24));
  const head = `<thead><tr><th scope="col">Hour</th>${days.map((d) => `<th scope="col">${esc(dayLabel(d[0].t))}</th>`).join("")}</tr></thead>`;
  let rows = "";
  for (let h = 0; h < 24; h++) {
    rows += `<tr><th scope="row">${String(h).padStart(2, "0")}:00</th>` + days.map((d) => {
      const e = d[h];
      if (!e) return "<td></td>";
      return e.avg == null ? `<td class="none">–</td>` : `<td>${e.avg.toFixed(1)}</td>`;
    }).join("") + "</tr>";
  }
  $("#temp-table").innerHTML = head + `<tbody>${rows}</tbody>`;
}

const chart = $("#temp-chart");
function chartPoint(e) {
  if (!temps) return;
  const r = chart.getBoundingClientRect(), x = (e.clientX - r.left) / r.width * CH.W;
  selectHour(Math.floor((x - CH.L) / ((CH.W - CH.L - CH.R) / CH.SLOTS)));
}
chart.addEventListener("pointerdown", chartPoint);
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => temps && renderClimate(true), 150); });
chart.addEventListener("pointermove", (e) => { if (e.pointerType === "mouse" || e.buttons) chartPoint(e); });
chart.addEventListener("keydown", (e) => {
  if (!temps || !temps.hours.length) return;
  const last = temps.hours.length - 1, cur = tempSel ?? last;
  const next = {ArrowLeft: cur - 1, ArrowRight: cur + 1, Home: 0, End: last, PageUp: cur - 24, PageDown: cur + 24}[e.key];
  if (next == null) return;
  e.preventDefault(); selectHour(next);
});

// ---------- temperature rules ----------
const COOLDOWN = {30: "every 30 minutes", 60: "every hour", 120: "every 2 hours", 180: "every 3 hours",
  240: "every 4 hours", 360: "every 6 hours", 720: "every 12 hours", 1440: "once a day"};
function renderRules() {
  const zones = Object.fromEntries(state.zones.map((z) => [z.id, z.name]));
  const list = state.temp_rules || [];
  $("#rules").innerHTML = list.length ? list.map((r) => {
    const what = `${r.minutes}\u00a0min when ${r.when} ${r.temp_c}\u00a0°C`;
    const more = `${r.start} to ${r.end}, at most ${COOLDOWN[r.cooldown_minutes] || ""}`;
    const last = r.last_triggered ? `. Last triggered ${when(r.last_triggered)}` : "";
    return `<li class="sched${r.enabled ? "" : " off"}"><div class="sched-main"><b>${esc(zones[r.zone_id] || "Unknown zone")}: ${esc(what)}</b>
      <small>${esc(more)}${esc(last)}${r.enabled ? "" : ". Turned off"}</small></div>
      <button class="btn small ghost" data-rule="${r.id}">Edit</button></li>`;
  }).join("") : `<li class="empty">No rules yet. Add one to water when it gets hot (or cold).</li>`;
}
function openRule(r) {
  editingRule = r || null;
  const f = $("#rule-form");
  $("#rule-title").textContent = r ? "Edit temperature rule" : "Add temperature rule";
  $("#r-zone").innerHTML = state.zones.map((z) => `<option value="${z.id}">${esc(z.name)}</option>`).join("");
  const v = r || {zone_id: state.zones[state.zones.length - 1].id, when: "above", temp_c: 30, minutes: 5,
    cooldown_minutes: 120, start: "09:00", end: "17:00", enabled: true};
  f.zone_id.value = v.zone_id;
  f.querySelector(`input[name=when][value=${v.when}]`).checked = true;
  f.temp_c.value = v.temp_c; f.minutes.value = v.minutes; f.cooldown_minutes.value = v.cooldown_minutes;
  f.start.value = v.start; f.end.value = v.end; f.enabled.checked = v.enabled;
  $("#rule-delete").hidden = !r;
  $("#rule-error").textContent = "";
  $("#rule-dialog").showModal();
}
$("#rule-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const data = {zone_id: +f.zone_id.value, when: f.when.value, temp_c: +f.temp_c.value, minutes: +f.minutes.value,
    cooldown_minutes: +f.cooldown_minutes.value, start: f.start.value, end: f.end.value, enabled: f.enabled.checked};
  try {
    const r = editingRule ? await api(`/api/temp-rules/${editingRule.id}`, "PUT", data) : await api("/api/temp-rules", "POST", data);
    $("#rule-dialog").close(); toast(r.message); refresh();
  } catch (err) { $("#rule-error").textContent = err.message; }
});
$("#rule-cancel").addEventListener("click", () => $("#rule-dialog").close());
$("#rule-delete").addEventListener("click", async () => {
  if (!editingRule || !confirm("Delete this temperature rule?")) return;
  $("#rule-dialog").close();
  act(`/api/temp-rules/${editingRule.id}`, undefined, "DELETE").catch(() => {});
});

function renderSystem() {
  const c = state.controller, rows = [];
  rows.push(["Controller", c.mode === "simulated" ? "Simulated (no hardware)" : c.online ? `ESP32, firmware ${c.fw}` : (c.error ? "Offline" : "Offline")]);
  if (state.rssi != null && c.mode !== "simulated") {
    rows.push(["WiFi", state.rssi > -60 ? "Strong signal" : state.rssi > -72 ? "Good signal" : "Weak signal"]);
  }
  if (state.weather) rows.push(["Forecast", state.weather.summary]);
  if (c.uptime_s != null) {
    const h = Math.floor(c.uptime_s / 3600);
    rows.push(["Running for", h >= 24 ? `${Math.floor(h / 24)} days` : `${h} h ${Math.floor(c.uptime_s % 3600 / 60)} min`]);
  }
  $("#system").innerHTML = rows.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");
}

function renderRunbar() {
  const run = state.running, anyOn = state.zones.some((z) => z.on);
  const bar = $("#runbar");
  bar.hidden = !(run || anyOn || state.queue.length);
  if (bar.hidden) return;
  let text = "Water is on";
  if (run) {
    const left = Math.max(0, run.remaining_s - Math.floor((Date.now() - fetchedAt) / 1000));
    text = `${run.zone}: ${mmss(left)} left`;
    if (run.litres != null) text += `, ${run.litres}\u00a0L`;
  }
  if (state.queue.length) text += `. Then ${state.queue.map((q) => q.zone).join(", ")}`;
  $("#run-text").textContent = text;
}

function render() {
  renderLink();
  if (!state) return;
  renderTank(); renderZones(); renderClimate(); renderSchedules(); renderRules(); renderHistory(); renderSystem(); renderRunbar();
}

async function refresh() {
  try {
    state = await api("/api/status");
    fetchedAt = Date.now(); failures = 0;
  } catch (_) { failures++; }
  render();
}

// ---------- schedule dialog ----------
function openDialog(s) {
  editing = s || null;
  const f = $("#sched-form");
  $("#sched-title").textContent = s ? "Edit schedule" : "Add schedule";
  $("#f-zone").innerHTML = state.zones.map((z) => `<option value="${z.id}">${esc(z.name)}</option>`).join("");
  $("#f-days").innerHTML = DAYS.map((d, i) => `<label><input type="checkbox" name="days" value="${i}"><span>${d}</span></label>`).join("");
  const v = s || {zone_id: state.zones[0].id, kind: "daily", days: [0, 1, 2, 3, 4, 5, 6], start: "06:30", end: "18:00", every_minutes: 60, minutes: 10, enabled: true};
  f.zone_id.value = v.zone_id;
  f.querySelector(`input[name=kind][value=${v.kind}]`).checked = true;
  f.querySelectorAll("input[name=days]").forEach((c) => (c.checked = v.days.includes(+c.value)));
  f.start.value = v.start; f.end.value = v.end || "18:00"; f.every_minutes.value = v.every_minutes || 60;
  f.minutes.value = v.minutes; f.enabled.checked = v.enabled;
  $("#sched-delete").hidden = !s;
  $("#form-error").textContent = "";
  syncKind();
  $("#sched-dialog").showModal();
}
function syncKind() {
  const f = $("#sched-form"), cycle = f.kind.value === "cycle";
  f.classList.toggle("is-cycle", cycle);
  $("#start-label").textContent = cycle ? "From" : "Start at";
}

$("#sched-form").addEventListener("change", (e) => { if (e.target.name === "kind") syncKind(); });
$("#sched-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const data = {zone_id: +f.zone_id.value, kind: f.kind.value, start: f.start.value, minutes: +f.minutes.value,
    enabled: f.enabled.checked, days: [...f.querySelectorAll("input[name=days]:checked")].map((c) => +c.value)};
  if (data.kind === "cycle") { data.end = f.end.value; data.every_minutes = +f.every_minutes.value; }
  try {
    const r = editing ? await api(`/api/schedules/${editing.id}`, "PUT", data) : await api("/api/schedules", "POST", data);
    $("#sched-dialog").close(); toast(r.message); refresh();
  } catch (err) { $("#form-error").textContent = err.message; }
});
$("#sched-cancel").addEventListener("click", () => $("#sched-dialog").close());
$("#sched-delete").addEventListener("click", async () => {
  if (!editing || !confirm("Delete this schedule?")) return;
  $("#sched-dialog").close();
  act(`/api/schedules/${editing.id}`, undefined, "DELETE").catch(() => {});
});

// ---------- buttons ----------
document.addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.run) act(`/api/zones/${b.dataset.run}/run`, {minutes: +b.dataset.min}).catch(() => {});
  else if (b.dataset.edit) openDialog(state.schedules.find((s) => s.id === +b.dataset.edit));
  else if (b.dataset.pause) act("/api/pause", {days: +b.dataset.pause}).catch(() => {});
  else if (b.id === "resume") act("/api/resume").catch(() => {});
  else if (b.id === "stop") act("/api/stop").catch(() => {});
  else if (b.id === "add-schedule") state && openDialog();
  else if (b.dataset.rule) openRule(state.temp_rules.find((r) => r.id === +b.dataset.rule));
  else if (b.id === "add-rule") state && openRule();
  else if (b.id === "refill-reset") act("/api/refill", {action: "reset"}).catch(() => {});
  else if (b.id === "notify-test") act("/api/notify-test").catch(() => {});
});

// ---------- install (PWA) ----------
let installEvent = null;
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); installEvent = e; $("#install").hidden = false; });
$("#install").addEventListener("click", async () => {
  if (!installEvent) return;
  installEvent.prompt(); await installEvent.userChoice; installEvent = null; $("#install").hidden = true;
});
const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
if (/iphone|ipad|ipod/i.test(navigator.userAgent) && !standalone) $("#ios-hint").hidden = false;
if ("serviceWorker" in navigator && window.isSecureContext) navigator.serviceWorker.register("/sw.js").catch(() => {});

// ---------- loop ----------
refresh();
fetchTemps();
setInterval(() => { if (!document.hidden) refresh(); }, 3000);
setInterval(() => { if (!document.hidden) fetchTemps(); }, 5 * 60 * 1000);
setInterval(() => { if (state && (state.running)) { renderRunbar(); renderZones(); } }, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) { refresh(); fetchTemps(); } });
