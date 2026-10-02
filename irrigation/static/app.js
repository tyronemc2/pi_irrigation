"use strict";
const $ = (s) => document.querySelector(s);
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
let state = null, fetchedAt = 0, editing = null, failures = 0;

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
  $("#temp").textContent = state.water_temp_c == null ? "No reading" : `${state.water_temp_c.toFixed(1)} °C`;
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
    case "ran": return `${h.zone} watered for ${h.minutes} min${amount} (${h.reason.toLowerCase()})`;
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
    if (run.litres != null) text += `, ${run.litres} L`;
  }
  if (state.queue.length) text += `. Then ${state.queue.map((q) => q.zone).join(", ")}`;
  $("#run-text").textContent = text;
}

function render() {
  renderLink();
  if (!state) return;
  renderTank(); renderZones(); renderSchedules(); renderHistory(); renderSystem(); renderRunbar();
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
setInterval(() => { if (!document.hidden) refresh(); }, 3000);
setInterval(() => { if (state && (state.running)) { renderRunbar(); renderZones(); } }, 1000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
