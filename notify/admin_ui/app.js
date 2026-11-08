// hnl-notify management UI. Vanilla JS, no build step.

const root = document.getElementById("root");
const dialog = document.getElementById("dialog");
const toastEl = document.getElementById("toast");

let state = null;
const hashSection = location.hash.slice(1);
let section = hashSection || sessionStorage.getItem("notify.section") || "overview";
if (section === "delivery") section = "providers"; // renamed page; old bookmarks still work

const SECTIONS = [
  ["overview", "Overview"],
  ["activity", "Activity"],
  ["scheduled", "Scheduled"],
  ["chats", "Chats"],
  ["devices", "Devices"],
  ["people", "People & groups"],
  ["channels", "Channels"],
  ["providers", "Providers"],
  ["access", "API access"],
  ["requests", "Access requests"],
  ["settings", "Settings"],
];

// The menu is grouped by what you are doing: watching, choosing who is reached, or connecting things.
const NAV_GROUPS = [
  ["Watch", ["overview", "activity", "scheduled", "chats"]],
  ["Reach", ["devices", "people", "channels"]],
  ["Connect", ["providers", "access", "requests", "settings"]],
];
const ICONS = {
  overview: '<circle cx="3" cy="8" r="1.6"/><circle cx="13" cy="8" r="1.6"/><path d="M4.6 8h6.8"/>',
  activity: '<path d="M2 8h2.5l1.8-4.5 3 9L11 8h3"/>',
  scheduled: '<circle cx="8" cy="8" r="5.5"/><path d="M8 4.8V8l2.2 1.4"/>',
  chats: '<path d="M2.5 3.5h11v7h-6l-3 2.5v-2.5h-2z"/>',
  devices: '<rect x="4.5" y="1.8" width="7" height="12.4" rx="1.6"/><path d="M7 12h2"/>',
  people: '<circle cx="6" cy="5.5" r="2.3"/><path d="M1.8 13c.3-2.4 2-3.7 4.2-3.7s3.9 1.300 4.200 3.700"/><path d="M10.5 3.600a2.200 2.200 0 0 1 0 3.800M12 9.600c1.200.5 2 1.600 2.200 3.400"/>',
  channels: '<path d="M2 5.500h12M2 10.500h12M6 2.500l-1 11M11 2.500l-1 11"/>',
  providers: '<path d="M2.500 10.500a3.500 3.500 0 0 1 .8-6.800 4.500 4.500 0 0 1 8.600 1A3 3 0 0 1 12.500 10.500z"/>',
  access: '<circle cx="5.500" cy="8" r="2.800"/><path d="M8.300 8H14M12 8v2.200M10.200 8v1.500"/>',
  requests: '<path d="M2.500 9.500l2-6h7l2 6v3h-11z"/><path d="M2.500 9.500h3.200l.8 1.500h3l.8-1.500h3.200"/>',
  settings: '<path d="M2.500 4.500h6M11.500 4.500h2M2.500 11.500h2M7.500 11.500h6"/><circle cx="10" cy="4.500" r="1.500"/><circle cx="6" cy="11.500" r="1.500"/>',
};
const icon = (k) => `<svg viewBox="0 0 16 16" aria-hidden="true">${ICONS[k] || ""}</svg>`;

// Pages without a menu entry of their own, reached from their parent (and by link): page -> parent
const SUBPAGES = { mailbox: "providers" };

function goTo(target) {
  section = target;
  sessionStorage.setItem("notify.section", section);
  history.replaceState(null, "", `#${section}`);
  render();
}

const SEVERITY_HELP = {
  min: "silent, no alert",
  low: "no sound or vibration",
  normal: "default alert",
  high: "bypasses quiet hours",
  critical: "repeats until acknowledged",
};

// Provider types whose /test (connection) and /check (address) endpoints do something.
const TESTABLE = new Set(["email"]);
const CHECKABLE = new Set(["pushover"]);
const BROWSER_FIELDS = new Set(["endpoint", "user_agent"]); // set by the enrollment, not typed

const KINDS = [["", "Default devices"], ["push", "Push"], ["email", "Email"], ["push,email", "Push and email"]];

const SCOPE_HELP = {
  send: "send notifications",
  read: "see its own history, targets and events",
  manage: "retry or cancel its own notifications",
  admin: "everything, for every client",
  chat: "take part in agent group chats",
};

// ---------------------------------------------------------------- helpers

const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

async function api(method, path, body) {
  const opts = { method, headers: { "X-Notify-Admin": "1" }, credentials: "same-origin" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(`/admin/api${path}`, opts);
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && path !== "/login" && path !== "/setup") {
    await boot();
    throw new Error("Signed out. Sign in again.");
  }
  if (!res.ok) {
    const d = data?.detail;
    const msg = typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => `${(e.loc || []).slice(1).join(".")}: ${e.msg}`).join("; ") : `Request failed (HTTP ${res.status})`;
    throw Object.assign(new Error(msg), { status: res.status });
  }
  return data;
}

function toast(msg) {
  toastEl.textContent = msg;
  toastEl.hidden = false;
  clearTimeout(toast.t);
  toast.t = setTimeout(() => (toastEl.hidden = true), 3200);
}

function ago(iso) {
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  if (!iso || Number.isNaN(s)) return "";
  if (s < 90) return "just now";
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function when(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  const same = d.toDateString() === new Date().toDateString();
  return same ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
              : d.toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

// ---- zone helpers (exercised by a node script outside the browser) ----
// A time the owner reads and types is in the service's zone, not the browser's.
const clock = (tz) => new Intl.DateTimeFormat([], { timeZone: tz, hour: "2-digit", minute: "2-digit" });

function zoneParts(ms, tz) {
  const p = {};
  for (const x of new Intl.DateTimeFormat("en-US", { timeZone: tz, hourCycle: "h23", year: "numeric", month: "2-digit",
    day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).formatToParts(new Date(ms))) p[x.type] = x.value;
  return p;
}

// UTC offset of the zone at an instant, in minutes.
function zoneOffset(ms, tz) {
  const p = zoneParts(ms, tz);
  const asUtc = Date.UTC(+p.year, +p.month - 1, +p.day, +p.hour, +p.minute, +p.second);
  return Math.round((asUtc - Math.floor(ms / 1000) * 1000) / 60000);
}

// "2026-10-25T02:30" typed in the zone -> ISO string with that zone's offset at that moment.
// A time that does not exist (clocks jump forward) throws; one that happens twice takes the first.
function zonedToIso(local, tz) {
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(local || "");
  if (!m) throw new Error("Pick a date and time");
  const guess = Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5]);
  const found = [];
  for (const o of new Set([zoneOffset(guess - 86400000, tz), zoneOffset(guess + 86400000, tz)])) {
    const t = guess - o * 60000;
    if (zoneOffset(t, tz) === o) found.push([t, o]);
  }
  if (!found.length) throw new Error("That time does not exist in " + tz + " (the clocks change then). Pick another.");
  const [, o] = found.sort((a, b) => a[0] - b[0])[0];
  const sign = o < 0 ? "-" : "+";
  const hh = String(Math.floor(Math.abs(o) / 60)).padStart(2, "0");
  const mm = String(Math.abs(o) % 60).padStart(2, "0");
  return `${local}:00${sign}${hh}:${mm}`;
}

// ISO instant -> value for a datetime-local input, in the zone.
function zoneInput(iso, tz) {
  const p = zoneParts(new Date(iso).getTime(), tz);
  return `${p.year}-${p.month}-${p.day}T${p.hour}:${p.minute}`;
}

// "tomorrow 07:30" / "today 18:05" / "Thu 3 Nov, 07:30", all in the zone.
function zoneWhen(iso, tz) {
  const t = new Date(iso);
  const day = (d) => zoneParts(d.getTime(), tz);
  const key = (p) => `${p.year}-${p.month}-${p.day}`;
  const now = Date.now();
  const hm = clock(tz).format(t);
  const k = key(day(t));
  if (k === key(day(new Date(now)))) return `today ${hm}`;
  if (k === key(day(new Date(now + 86400000)))) return `tomorrow ${hm}`;
  return `${new Intl.DateTimeFormat([], { timeZone: tz, weekday: "short", day: "numeric", month: "short" }).format(t)}, ${hm}`;
}

// The counterpart of ago() for times still ahead of us.
function until(iso) {
  const s = (new Date(iso) - Date.now()) / 1000;
  if (!iso || Number.isNaN(s)) return "";
  if (s < -60) return "overdue";
  if (s < 90) return "due now";
  if (s < 5400) return `in ${Math.round(s / 60)} min`;
  if (s < 129600) return `in ${Math.round(s / 3600)} h`;
  return `in ${Math.round(s / 86400)} days`;
}
// ---- end zone helpers ----

const logo = `<svg width="28" height="28" viewBox="0 0 32 32" aria-hidden="true"><rect width="32" height="32" rx="8" fill="var(--action)"/><path d="M7 16h4.500l2.500-6 4 12 2.500-6H25" fill="none" stroke="var(--action-ink)" stroke-width="2.200" stroke-linecap="round" stroke-linejoin="round"/></svg>`;

function fmtSize(bytes) {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1048576) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

function lampFor(status) {
  if (["acknowledged", "delivered", "dispatched"].includes(status)) return "ok";
  if (["pending", "partial"].includes(status)) return "warn";
  if (["failed", "expired"].includes(status)) return "bad";
  return "";
}

// ---------------------------------------------------------------- forms in a dialog

/**
 * fields: {name, label, kind: text|secret|int|select|checks|textarea|check|url, value, help, options:[[value,label]], placeholder}
 * onSubmit(values) -> Promise; throw to show an error.
 */
function openForm({ title, intro = "", fields, submit, onSubmit, extra, focus }) {
  dialog.innerHTML = `
    <form class="stack" method="dialog" novalidate>
      <h2>${esc(title)}</h2>
      ${intro ? `<p class="hint">${intro}</p>` : ""}
      ${fields.map(fieldHtml).join("")}
      <div class="form-error" role="alert"></div>
      <div class="form-actions">
        <button type="button" class="btn" data-close>Cancel</button>
        <button type="submit" class="btn primary">${esc(submit)}</button>
      </div>
    </form>`;
  const form = dialog.querySelector("form");
  const err = form.querySelector(".form-error");
  form.querySelector("[data-close]").onclick = () => dialog.close();
  if (extra) extra(form);
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    err.textContent = "";
    try {
      await onSubmit(readForm(form, fields));
      dialog.close();
    } catch (ex) {
      err.textContent = ex.message;
    } finally {
      btn.disabled = false;
    }
  };
  if (!dialog.open) dialog.showModal(); // re-rendered forms reuse the open dialog
  const first = (focus && form.querySelector(focus)) || form.querySelector("input:not([type=checkbox]):not([readonly]), select, textarea");
  if (first) first.focus();
}

function fieldHtml(f) {
  const id = `f-${f.name}`;
  const help = f.help ? `<span class="help" id="${id}-h">${f.help}</span>` : "";
  const aria = f.help ? `aria-describedby="${id}-h"` : "";
  if (f.kind === "checks") {
    const vals = new Set(f.value || []);
    return `<div class="field"><fieldset><legend>${esc(f.label)}</legend>${help}<div class="checks">
      ${f.options.map(([v, l]) => `<label><input type="checkbox" name="${esc(f.name)}" value="${esc(v)}" ${vals.has(v) ? "checked" : ""}> ${esc(l)}</label>`).join("")}
      ${f.options.length ? "" : `<span class="hint">Nothing to choose from yet.</span>`}
    </div></fieldset></div>`;
  }
  if (f.kind === "check") {
    return `<div class="field"><label><input type="checkbox" name="${esc(f.name)}" ${f.value ? "checked" : ""}> ${esc(f.label)}</label>${help}</div>`;
  }
  let input;
  if (f.kind === "select") {
    input = `<select id="${id}" name="${esc(f.name)}" ${aria}>${f.options.map(([v, l]) => `<option value="${esc(v)}" ${String(f.value ?? "") === String(v) ? "selected" : ""}>${esc(l)}</option>`).join("")}</select>`;
  } else if (f.kind === "textarea") {
    input = `<textarea id="${id}" name="${esc(f.name)}" ${aria}>${esc(f.value)}</textarea>`;
  } else {
    const type = f.kind === "secret" ? "password" : f.kind === "int" ? "number" : f.kind === "url" ? "url" : f.kind === "datetime" ? "datetime-local" : "text";
    const ph = f.kind === "secret" && f.stored ? `Stored (${f.stored}). Leave empty to keep it.` : (f.placeholder || "");
    input = `<input id="${id}" type="${type}" name="${esc(f.name)}" value="${f.kind === "secret" ? "" : esc(f.value)}"
      placeholder="${esc(ph)}" ${aria} autocomplete="off" ${f.readonly ? "readonly" : ""} ${f.list ? `list="${id}-list"` : ""}>`;
    if (f.list) input += `<datalist id="${id}-list"></datalist>`;
    if (f.button) input = `<div class="inline">${input}<button type="button" class="btn" data-action="${esc(f.button.action)}">${esc(f.button.label)}</button></div>`;
  }
  return `<div class="field"><label for="${id}">${esc(f.label)}</label>${input}${help}</div>`;
}

function readForm(form, fields) {
  const out = {};
  for (const f of fields) {
    if (f.kind === "checks") out[f.name] = [...form.querySelectorAll(`input[name="${f.name}"]:checked`)].map((i) => i.value);
    else if (f.kind === "check") out[f.name] = form.querySelector(`input[name="${f.name}"]`).checked;
    else {
      const v = form.elements[f.name].value.trim();
      out[f.name] = f.kind === "int" ? (v === "" ? null : Number(v)) : v;
    }
  }
  return out;
}

function confirmAction(title, body, label, fn) {
  openForm({ title, intro: body, fields: [], submit: label, onSubmit: fn });
  dialog.querySelector("button[type=submit]").classList.add("danger");
}

function showToken(title, name, token) {
  const base = location.origin;
  dialog.innerHTML = `
    <div class="stack" style="display:flex;flex-direction:column;gap:1rem">
      <h2>${esc(title)}</h2>
      <p>Copy this token for <strong>${esc(name)}</strong> now. It is stored only as a fingerprint and will not be shown again.</p>
      <div class="secret-box"><code id="tok">${esc(token)}</code><button class="btn" id="copy">Copy</button></div>
      <div><strong>Use it as a header</strong><pre class="snippet">Authorization: Bearer ${esc(token)}</pre></div>
      <div><strong>Connect an agent (MCP)</strong><pre class="snippet">claude mcp add --transport http hnl-notify ${esc(base)}/mcp \\
  --header "Authorization: Bearer ${esc(token)}"</pre></div>
      <div class="form-actions"><button class="btn primary" id="done">I've saved it</button></div>
    </div>`;
  dialog.querySelector("#copy").onclick = async () => {
    try { await navigator.clipboard.writeText(token); toast("Token copied"); }
    catch { toast("Copy isn't available here; select the token and copy it manually"); }
  };
  dialog.querySelector("#done").onclick = () => dialog.close();
  dialog.showModal();
}

// ---------------------------------------------------------------- auth screens

function renderGate(mode, message = "") {
  const setup = mode === "setup";
  root.innerHTML = `
    <main class="gate"><div class="panel">
      <div class="brand">${logo}<h1>hnl-notify</h1></div>
      <h2>${setup ? "Set up the admin password" : "Sign in"}</h2>
      ${setup ? `<p class="hint">Find the setup code in the container log:<br><code>docker logs hnl-notify 2>&1 | grep "setup code"</code></p>` : ""}
      <form class="stack" novalidate>
        ${setup ? fieldHtml({ name: "code", label: "Setup code", kind: "text", placeholder: "abc123-def456-789abc" }) : ""}
        ${fieldHtml({ name: "password", label: setup ? "New password" : "Password", kind: "secret",
                     help: setup ? "At least 10 characters." : "" })}
        <div class="form-error" role="alert">${esc(message)}</div>
        <div class="form-actions"><button class="btn primary" type="submit">${setup ? "Save password and continue" : "Sign in"}</button></div>
      </form>
    </div></main>`;
  const form = root.querySelector("form");
  form.querySelector("input").focus();
  form.onsubmit = async (e) => {
    e.preventDefault();
    const err = form.querySelector(".form-error");
    const btn = form.querySelector("button");
    btn.disabled = true;
    err.textContent = "";
    try {
      const body = { password: form.elements.password.value };
      if (setup) body.code = form.elements.code.value.trim();
      await api("POST", setup ? "/setup" : "/login", body);
      await boot();
    } catch (ex) {
      err.textContent = ex.message;
      btn.disabled = false;
    }
  };
}

// ---------------------------------------------------------------- app shell

async function boot() {
  const s = await (await fetch("/admin/api/session", { credentials: "same-origin" })).json();
  if (s.setup_required) return renderGate("setup");
  if (!s.signed_in) return renderGate("login");
  await refresh();
}

async function refresh() {
  state = await api("GET", "/state");
  render();
}

function render() {
  if (!VIEWS[section]) section = "overview";
  const badge = (k) => {
    const n = k === "requests" ? state.pending_registrations : k === "scheduled" ? state.scheduled_count : k === "chats" ? chatUnread() : 0;
    return n ? ` <span class="badge" aria-label="${n} ${k === "scheduled" ? "scheduled" : k === "chats" ? "unread" : "waiting"}">${n}</span>` : "";
  };
  const current = SUBPAGES[section] || section; // a sub-page keeps its parent highlighted
  const label = Object.fromEntries(SECTIONS);
  const nav = NAV_GROUPS.map(([g, keys]) => `<div class="nav-group" role="group" aria-label="${esc(g)}"><div class="nav-label" aria-hidden="true">${esc(g)}</div>${keys.map((k) =>
    `<button data-nav="${k}" ${k === current ? 'aria-current="page"' : ""}>${icon(k)}<span>${esc(label[k])}</span>${badge(k)}</button>`).join("")}</div>`).join("");
  root.innerHTML = `
    <div class="shell">
      <aside class="side">
        <div class="top"><div class="brand">${logo}<div><h1>hnl-notify</h1><span class="sub">notification desk</span></div></div><button class="btn quiet signout" data-signout>Sign out</button></div>
        <nav class="nav" aria-label="Sections">${nav}</nav>
        <div class="foot"><span>v${esc(state.version)}</span><button class="btn quiet" data-signout>Sign out</button></div>
      </aside>
      <main class="main" id="main"></main>
    </div>`;
  root.querySelectorAll("[data-nav]").forEach((b) => (b.onclick = () => goTo(b.dataset.nav)));
  root.querySelectorAll("[data-signout]").forEach((b) => (b.onclick = async () => { await api("POST", "/logout"); boot(); }));
  root.querySelector('.nav [aria-current="page"]')?.scrollIntoView({ block: "nearest", inline: "center" });
  const main = document.getElementById("main");
  VIEWS[section](main);
  addScope(main);
}

// The scope line under the page title: the exact slice of the system this page shows, in numbers.
function scopeLine() {
  const c = cfg();
  const n = (obj) => Object.keys(obj || {}).length;
  const b = (v, one, many) => `<b>${v}</b> ${v === 1 ? one : many}`;
  const ready = Object.keys(c.devices).filter((d) => c.devices[d].enabled && state.devices[d]?.ready).length;
  const parts = {
    overview: [b(n(c.clients), "sender", "senders"), b(n(c.providers), "provider", "providers"), b(ready, "device ready", "devices ready")],
    providers: [b(n(c.providers), "provider", "providers"), b(Object.keys(c.providers).filter((p) => state.providers[p]?.configured).length, "configured", "configured")],
    mailbox: [`reply mailbox`],
    devices: [b(n(c.devices), "device", "devices"), b(ready, "ready", "ready")],
    people: [b(n(c.users), "person", "people"), b(n(c.groups), "group", "groups")],
    channels: [b(n(c.channels), "channel", "channels")],
    access: [b(n(c.clients), "token", "tokens")],
    requests: [b(state.pending_registrations || 0, "waiting", "waiting")],
    scheduled: [b(state.scheduled_count || 0, "waiting to send", "waiting to send")],
    activity: ["newest first"],
    chats: ["agent group chats"],
    settings: [`version <b>${esc(state.version)}</b>`],
  }[section] || [];
  return parts.join(" · ");
}

function addScope(main) {
  const h = main.querySelector(".section-head h2");
  if (!h) return;
  const line = scopeLine();
  if (line) h.insertAdjacentHTML("afterend", `<div class="scope">${line}</div>`);
}

// Mutations return the new state; re-render with it.
async function mutate(method, path, body, done) {
  const res = await api(method, path, body);
  state = res;
  render();
  if (done) toast(done);
  return res;
}

// ---------------------------------------------------------------- derived info

const cfg = () => state.config;
const providerLabel = (n) => {
  const p = cfg().providers[n];
  const label = state.provider_types[p?.type]?.label || p?.type || n;
  return n === p?.type ? label : `${n} (${label})`;
};

function targetOptions() {
  const c = cfg();
  return [
    ...Object.entries(c.users).map(([n, u]) => [`user:${n}`, `${u.display_name || n} (person)`]),
    ...Object.keys(c.groups).map((n) => [`group:${n}`, `${n} (group)`]),
    ...Object.entries(c.devices).map(([n, d]) => [`device:${n}`, `${d.description || n} (device)`]),
  ];
}

function describeTarget(t) {
  const [kind, name] = t.split(":");
  const c = cfg();
  if (kind === "user") return c.users[name]?.display_name || name;
  if (kind === "device") return c.devices[name]?.description || name;
  return name;
}

// ---------------------------------------------------------------- views

const VIEWS = {
  overview(main) {
    const c = cfg();
    const provs = Object.entries(c.providers);
    const devices = Object.entries(c.devices);
    const readyDevices = devices.filter(([n, d]) => d.enabled && state.devices[n]?.ready);
    const clients = Object.keys(c.clients);
    const pending = state.stats.deliveries_by_state.queued || 0;

    const senderLamp = clients.length ? "ok" : "warn";
    const coreLamp = pending > 0 && state.stats.oldest_pending_age_seconds > 120 ? "warn" : "ok";
    const provLamp = provs.length && provs.every(([n]) => state.providers[n]?.configured) ? "ok" : "bad";
    const devLamp = readyDevices.length ? "ok" : devices.length ? "bad" : "warn";
    const wire = (l) => `--wire: var(--${l === "ok" ? "ok" : l === "bad" ? "bad" : l === "warn" ? "warn" : "idle"})`;
    const provNote = (n, p) => {
      const h = state.providers[n];
      if (!h?.configured) return h?.problems.join("; ") || "";
      if (h.quota) return `${h.quota.remaining.toLocaleString()} of ${h.quota.limit.toLocaleString()} messages left this month`;
      return p.type === "pushover" ? "Quota appears after the first message" : "Ready";
    };

    main.innerHTML = `
      ${state.pending_registrations ? `<div class="rows" style="margin-bottom:1.5rem"><div class="row"><span class="lamp warn"></span>
        <div class="grow"><div class="title">${state.pending_registrations === 1 ? "An app is" : `${state.pending_registrations} apps are`} waiting for access</div>
        <div class="sub">Approve or deny it before it can send notifications.</div></div>
        <div class="actions"><button class="btn primary" data-go="requests">Review</button></div></div></div>` : ""}
      <div class="section-head"><h2>Overview</h2><p>Where a notification goes after a service sends it, and how much has gone through.</p></div>
      <div class="path" role="group" aria-label="Delivery path">
        <div class="hop" style="${wire(senderLamp)}"><div class="kind">Senders</div>
          <div class="name"><span class="lamp ${senderLamp}"></span><span class="label">${clients.length} API ${clients.length === 1 ? "client" : "clients"}</span></div>
          <div class="note">${clients.length ? esc(clients.slice(0, 3).join(", ")) + (clients.length > 3 ? "…" : "") : `<button class="btn quiet" style="padding:0" data-go="access">Create a token</button>`}</div></div>
        <div class="hop" style="${wire(coreLamp)}"><div class="kind">Queue</div>
          <div class="name"><span class="lamp ${coreLamp}"></span><span class="label">hnl-notify</span></div>
          <div class="note">${pending ? `${pending} waiting` : "Nothing waiting"}</div></div>
        <div class="hop" style="${wire(provLamp)}"><div class="kind">${provs.length > 1 ? "Providers" : "Provider"}</div>
          <div class="hop-list">${provs.length ? provs.map(([n, p]) => `
            <div class="name"><span class="lamp ${state.providers[n]?.configured ? "ok" : "bad"}"></span><span class="label">${esc(providerLabel(n))}</span></div>
            <div class="note">${esc(provNote(n, p))}</div>`).join("")
          : `<div class="name"><span class="lamp bad"></span><span class="label">None yet</span></div><div class="note"><button class="btn quiet" style="padding:0" data-go="providers">Add a provider</button></div>`}</div></div>
        <div class="hop"><div class="kind">Devices</div>
          <div class="hop-list">${devices.length ? devices.map(([n, d]) => {
            const ok = d.enabled && state.devices[n]?.ready;
            return `<div class="name"><span class="lamp ${ok ? "ok" : d.enabled ? "bad" : ""}"></span><span class="label">${esc(d.description || n)}</span></div>`;
          }).join("") : `<div class="name"><span class="lamp ${devLamp}"></span><span class="label">None yet</span></div><div class="note"><button class="btn quiet" style="padding:0" data-go="devices">Add a device</button></div>`}</div></div>
      </div>

      <div class="kpis">
        <div class="kpi ${pending ? "go" : ""}"><div class="v">${pending + (state.stats.deliveries_by_state.sending || 0)}</div><div class="l">in the queue now</div></div>
        <div class="kpi"><div class="v">${["dispatched", "delivered", "acknowledged"].reduce((a, k) => a + (state.stats.deliveries_by_state[k] || 0), 0).toLocaleString()}</div><div class="l">accepted or delivered, all time</div></div>
        <div class="kpi ${(state.stats.deliveries_by_state.failed || 0) + (state.stats.deliveries_by_state.expired || 0) ? "bad" : ""}"><div class="v">${((state.stats.deliveries_by_state.failed || 0) + (state.stats.deliveries_by_state.expired || 0)).toLocaleString()}</div><div class="l">failed or expired, all time</div></div>
        <div class="kpi ${state.scheduled_count ? "warn" : ""}"><div class="v">${state.scheduled_count || 0}</div><div class="l">scheduled to send</div></div>
      </div>

      <div class="block">
        <h3>Send a test</h3>
        ${readyDevices.length ? `
        <form class="test-form" id="test">
          ${fieldHtml({ name: "target", label: "To", kind: "select", options: targetOptions(), value: Object.keys(c.users)[0] ? `user:${Object.keys(c.users)[0]}` : "" })}
          ${fieldHtml({ name: "severity", label: "Urgency", kind: "select", value: "normal",
                        options: state.severities.map((s) => [s, `${s}: ${SEVERITY_HELP[s]}`]),
                        help: "Only critical reports delivery back, and it repeats until acknowledged." })}
          ${fieldHtml({ name: "kinds", label: "Kinds", kind: "select", value: "", options: KINDS,
                        help: "Email devices only receive when email is chosen." })}
          <div class="form-error" role="alert"></div>
          <div class="form-actions"><button class="btn primary" type="submit">Send test notification</button></div>
        </form>` : `<div class="rows"><div class="empty">Add a provider and a device, then send a test from here.</div></div>`}
      </div>

      <div class="block"><h3>Recent notifications</h3><div id="recent" class="rows"><div class="empty">Loading…</div></div></div>`;

    main.querySelectorAll("[data-go]").forEach((b) => (b.onclick = () => { section = b.dataset.go; render(); }));
    const form = main.querySelector("#test");
    if (form) form.onsubmit = async (e) => {
      e.preventDefault();
      const err = form.querySelector(".form-error");
      err.textContent = "";
      try {
        const kinds = form.elements.kinds.value;
        const n = await api("POST", "/test", { targets: [form.elements.target.value], severity: form.elements.severity.value,
                                               kinds: kinds ? kinds.split(",") : undefined });
        const why = n.suppressed?.[0]?.reason;
        toast(n.status === "suppressed" ? `Not sent: ${why || "the target's severity floor filtered it out"}` : "Test sent. Watch its status below.");
        loadRecent(main.querySelector("#recent"), 5);
      } catch (ex) { err.textContent = ex.message; }
    };
    loadRecent(main.querySelector("#recent"), 5);
  },

  providers(main) {
    const c = cfg();
    const list = Object.entries(c.providers);
    main.innerHTML = `
      <div class="section-head"><h2>Providers</h2>
        <p>A provider carries notifications to devices. Browser push delivers to Chrome, Edge or Firefox on a computer or phone once you open an enrollment link in that browser; it reports when a message was shown and when it was clicked. Pushover delivers to the Pushover app on your phone: normal messages are reported as sent once Pushover accepts them, and only critical messages report delivery and acknowledgement back. Email goes out through your mail server.</p></div>
      <div class="rows">${list.length ? list.map(([n, p]) => {
        const t = state.provider_types[p.type] || {};
        const h = state.providers[n];
        return `<div class="row"><span class="lamp ${h?.configured ? "ok" : "bad"}"></span>
          <div class="grow"><div class="title">${esc(n)}</div>
            <div class="sub">${esc(t.label || p.type)}, delivers ${t.kind === "email" ? "email" : "push notifications"}${h?.configured ? "" : ", needs attention"}</div>
            ${p.type === "pushover" && p.app_token?.set ? `<div class="sub">Application token ${esc(p.app_token.hint)}</div>` : ""}
            ${h?.quota ? `<div class="sub">${h.quota.remaining.toLocaleString()} of ${h.quota.limit.toLocaleString()} messages left this month</div>` : ""}
            ${h?.problems?.length ? `<div class="problem">${esc(h.problems.join("; "))}</div>` : ""}
            ${p.imap_host ? `<div class="sub" data-mbox-sum="${esc(n)}">Reply mailbox: reading…</div>` : ""}
            <div class="sub" data-result></div></div>
          <div class="actions">${p.imap_host ? `<button class="btn" data-mbox aria-label="Manage reply mailbox">Manage…</button>` : ""}${TESTABLE.has(p.type) ? `<button class="btn" data-test="${esc(n)}" aria-label="Test connection">Test</button>` : ""}${p.type === "webpush" ? `<button class="btn" data-enroll="${esc(n)}">Enrollment link</button>` : ""}<button class="btn" data-edit="${esc(n)}">Edit</button><button class="btn danger" data-del="${esc(n)}">Remove</button></div></div>`;
      }).join("") : `<div class="empty">No providers yet. Connect Pushover to reach phones, or add your mail server to send email.</div>`}</div>
      <p style="margin-top:1rem"><button class="btn primary" id="add">Add provider</button></p>`;
    main.querySelector("#add").onclick = () => addProviderForm();
    main.querySelectorAll("[data-mbox]").forEach((b) => (b.onclick = () => goTo("mailbox")));
    if (main.querySelector("[data-mbox-sum]")) loadMailboxSummaries(main);
    main.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => providerForm(b.dataset.edit)));
    main.querySelectorAll("[data-enroll]").forEach((b) => (b.onclick = () => enrollmentForm(b.dataset.enroll)));
    main.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => confirmAction(
      "Remove provider?", `Remove <strong>${esc(b.dataset.del)}</strong>. Devices that use it must be removed first.`,
      "Remove provider", () => mutate("DELETE", `/providers/${b.dataset.del}`, undefined, "Provider removed"))));
    main.querySelectorAll("[data-test]").forEach((b) => (b.onclick = async () => {
      const out = b.closest(".row").querySelector("[data-result]");
      out.className = "sub";
      out.textContent = "Connecting…";
      b.disabled = true;
      try {
        const r = await api("POST", `/providers/${b.dataset.test}/test`);
        if (!r.supported) { b.hidden = true; out.textContent = ""; return; }
        if (!r.ok) throw new Error(r.error || "The connection failed");
        const tls = { starttls: "with STARTTLS", tls: "with TLS" }[r.tls] || "without encryption";
        let text = `Connection works, ${tls}. ${r.size_limit ? `The server accepts messages up to ${fmtSize(r.size_limit)}.` : "The server announces no size limit."}`;
        if (r.imap) {
          text += r.imap.ok
            ? ` Reply mailbox works: ${r.imap.messages} waiting, folders ${r.imap.folders.join(", ")}.${r.imap.authserv_ids.length ? ` Its newest message names ${r.imap.authserv_ids.join(", ")} in Authentication-Results.` : ""}`
            : ` Reply mailbox: ${r.imap.error}`;
        }
        out.textContent = text;
      } catch (ex) { out.className = "problem"; out.textContent = ex.message; } finally { b.disabled = false; }
    }));
  },

  mailbox(main) {
    main.innerHTML = `
      <p class="crumb"><button class="btn quiet" data-back>Back to Providers</button></p>
      <div class="section-head"><h2>Reply mailbox</h2>
        <p>hnl-notify moves every handled reply to one folder and every rejected mail to another, and keeps them there until it cleans up. Old mail is deleted for good, about once a day, by age first and by folder size as a safety net. Your inbox is never touched.</p></div>
      <div id="mb"><div class="empty">Reading the mailbox…</div></div>`;
    main.querySelector("[data-back]").onclick = () => goTo("providers");
    loadMailbox(main.querySelector("#mb"));
  },

  devices(main) {
    const c = cfg();
    const devices = Object.entries(c.devices);
    main.innerHTML = `
      <div class="section-head"><h2>Devices</h2><p>Each device is one place a notification can land, such as the Pushover app on a phone or an email address.</p></div>
      <div class="rows">${devices.length ? devices.map(([n, d]) => {
        const info = state.devices[n] || {};
        return `<div class="row"><span class="lamp ${d.enabled ? (info.ready ? "ok" : "bad") : ""}"></span>
          <div class="grow"><div class="title">${esc(d.description || n)}</div>
            <div class="sub">device:${esc(n)} via ${esc(d.provider)}${info.opt_in ? `, only when an app asks for ${esc(info.kind)}` : ""}${d.direct_only ? ", only when an app names this device" : ""}${d.owner ? `, belongs to ${esc(c.users[d.owner]?.display_name || d.owner)}` : ""}${d.enabled ? "" : ", turned off"}${d.min_severity !== "min" ? `, only ${esc(d.min_severity)} and above` : ""}</div>
            ${info.problems?.length ? `<div class="problem">${esc(info.problems.join("; "))}</div>` : ""}</div>
          <div class="actions"><button class="btn" data-edit="${esc(n)}">Edit</button><button class="btn danger" data-del="${esc(n)}">Remove</button></div></div>`;
      }).join("") : `<div class="empty">No devices yet.</div>`}</div>
      <p style="margin-top:1rem"><button class="btn primary" id="add">Add device</button></p>`;
    main.querySelector("#add").onclick = () => deviceForm();
    main.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => deviceForm(b.dataset.edit)));
    main.querySelectorAll("[data-del]").forEach((b) => (b.onclick = () => confirmAction(
      "Remove device?", `Notifications will no longer go to <strong>${esc(b.dataset.del)}</strong>. It is also removed from people, groups and channels.`,
      "Remove device", () => mutate("DELETE", `/devices/${b.dataset.del}`, undefined, "Device removed"))));
  },

  people(main) {
    const c = cfg();
    main.innerHTML = `
      <div class="section-head"><h2>People & groups</h2><p>Services send to a person or a group; hnl-notify picks the devices.</p></div>
      <div class="block"><h3>People</h3><div class="rows">${Object.entries(c.users).map(([n, u]) => `
        <div class="row"><div class="grow"><div class="title">${esc(u.display_name || n)}</div>
          <div class="sub">user:${esc(n)}</div>
          <div class="chips">${u.devices.map((d) => `<span class="chip">${esc(c.devices[d]?.description || d)}</span>`).join("") || `<span class="hint">No devices</span>`}</div></div>
          <div class="actions"><button class="btn" data-user="${esc(n)}">Edit</button><button class="btn danger" data-deluser="${esc(n)}">Remove</button></div></div>`).join("") || `<div class="empty">No people yet.</div>`}
      </div><p style="margin-top:.75rem"><button class="btn" id="adduser">Add person</button></p></div>
      <div class="block"><h3>Groups</h3><div class="rows">${Object.entries(c.groups).map(([n, g]) => `
        <div class="row"><div class="grow"><div class="title">${esc(n)}</div>
          <div class="sub">group:${esc(n)}${g.description ? `, ${esc(g.description)}` : ""}</div>
          <div class="chips">${g.members.map((m) => `<span class="chip">${esc(describeTarget(m))}</span>`).join("") || `<span class="hint">No members</span>`}</div></div>
          <div class="actions"><button class="btn" data-group="${esc(n)}">Edit</button><button class="btn danger" data-delgroup="${esc(n)}">Remove</button></div></div>`).join("") || `<div class="empty">No groups yet.</div>`}
      </div><p style="margin-top:.75rem"><button class="btn" id="addgroup">Add group</button></p></div>`;
    main.querySelector("#adduser").onclick = () => userForm();
    main.querySelector("#addgroup").onclick = () => groupForm();
    main.querySelectorAll("[data-user]").forEach((b) => (b.onclick = () => userForm(b.dataset.user)));
    main.querySelectorAll("[data-group]").forEach((b) => (b.onclick = () => groupForm(b.dataset.group)));
    main.querySelectorAll("[data-deluser]").forEach((b) => (b.onclick = () => confirmAction("Remove person?",
      `Services sending to user:${esc(b.dataset.deluser)} will get an error. Their devices stay.`, "Remove person",
      () => mutate("DELETE", `/users/${b.dataset.deluser}`, undefined, "Person removed"))));
    main.querySelectorAll("[data-delgroup]").forEach((b) => (b.onclick = () => confirmAction("Remove group?",
      `Services sending to group:${esc(b.dataset.delgroup)} will get an error.`, "Remove group",
      () => mutate("DELETE", `/groups/${b.dataset.delgroup}`, undefined, "Group removed"))));
  },

  channels(main) {
    const c = cfg();
    main.innerHTML = `
      <div class="section-head"><h2>Channels</h2><p>A channel is a topic such as security or server alerts. Services send to the channel; you decide here who receives it and from which urgency.</p></div>
      <div class="rows">${Object.entries(c.channels).map(([n, ch]) => `
        <div class="row"><div class="grow"><div class="title">${esc(n)}</div>
          <div class="sub">channel:${esc(n)}${ch.description ? `, ${esc(ch.description)}` : ""}${ch.min_severity !== "min" ? `. Only ${esc(ch.min_severity)} and above` : ""}</div>
          <div class="chips">${ch.targets.map((t) => `<span class="chip">${esc(describeTarget(t))}</span>`).join("") || `<span class="hint">Nobody receives this channel</span>`}</div></div>
          <div class="actions"><button class="btn" data-ch="${esc(n)}">Edit</button><button class="btn danger" data-delch="${esc(n)}">Remove</button></div></div>`).join("") || `<div class="empty">No channels yet.</div>`}
      </div><p style="margin-top:1rem"><button class="btn primary" id="addch">Add channel</button></p>`;
    main.querySelector("#addch").onclick = () => channelForm();
    main.querySelectorAll("[data-ch]").forEach((b) => (b.onclick = () => channelForm(b.dataset.ch)));
    main.querySelectorAll("[data-delch]").forEach((b) => (b.onclick = () => confirmAction("Remove channel?",
      `Services sending to channel:${esc(b.dataset.delch)} will get an error.`, "Remove channel",
      () => mutate("DELETE", `/channels/${b.dataset.delch}`, undefined, "Channel removed"))));
  },

  access(main) {
    const c = cfg();
    const firstUser = Object.keys(c.users)[0];
    const exampleTarget = firstUser ? `user:${firstUser}` : "user:NAME";
    main.innerHTML = `
      <div class="section-head"><h2>API access</h2><p>Every service or agent that sends notifications gets its own token, so you can see who sent what and revoke one without touching the others.</p></div>
      <div class="rows">${Object.entries(c.clients).map(([n, cl]) => `
        <div class="row"><div class="grow"><div class="title">${esc(n)}</div>
          ${cl.description ? `<div class="sub">${esc(cl.description)}</div>` : ""}
          <div class="chips">${cl.scopes.map((s) => `<span class="chip" title="${esc(SCOPE_HELP[s])}">${esc(s)}</span>`).join("")}</div>
          ${(cl.webhooks_off || []).map((w) => `<div class="sub"><span class="problem">Webhook to ${esc(w.host)} is turned off</span>${w.last_error ? ` (${esc(w.last_error)})` : ""} <button class="btn" data-whon="${esc(w.id)}">Turn on</button></div>`).join("")}</div>
          <div class="actions"><button class="btn" data-cl="${esc(n)}">Edit</button><button class="btn" data-rot="${esc(n)}">Replace token</button><button class="btn danger" data-delcl="${esc(n)}">Revoke</button></div></div>`).join("") || `<div class="empty">No tokens yet. Create one for each service that should send notifications, for example Home Assistant.</div>`}
      </div>
      <p style="margin-top:1rem"><button class="btn primary" id="addcl">Create token</button></p>
      <div class="block" style="margin-top:2rem"><h3>Let an app ask for access itself</h3>
        <p class="hint">${cfg().registration.enabled ? "The app gets a token right away; it starts working once you approve the request under Access requests." : "Self-registration is turned off in Settings."}</p>
        <pre class="snippet">curl -X POST ${esc(location.origin)}/v1/registrations \\
  -H "Content-Type: application/json" \\
  -d '{"name": "node-red", "description": "Flow alerts", "scopes": ["send", "read"]}'</pre></div>
      <div class="block"><h3>Send from a service</h3>
        <pre class="snippet">curl -X POST ${esc(location.origin)}/v1/notifications \\
  -H "Authorization: Bearer YOUR_TOKEN" -H "Content-Type: application/json" \\
  -d '{"targets": ["${esc(exampleTarget)}"], "title": "Washing machine", "message": "The laundry is done"}'</pre>
        <p class="hint" style="margin-top:.5rem">Full API reference: <a href="/docs" target="_blank" rel="noopener">/docs</a></p></div>`;
    main.querySelector("#addcl").onclick = () => clientForm();
    main.querySelectorAll("[data-whon]").forEach((b) => (b.onclick = () => confirmAction("Turn the webhook on again?",
      "It gets events from now on. Events from while it was off are not sent again; the app can catch up with GET /v1/events.",
      "Turn on", () => mutate("POST", `/subscriptions/${b.dataset.whon}/enable`, undefined, "Webhook turned on"))));
    main.querySelectorAll("[data-cl]").forEach((b) => (b.onclick = () => clientForm(b.dataset.cl)));
    main.querySelectorAll("[data-rot]").forEach((b) => (b.onclick = () => confirmAction("Replace token?",
      `The current token for <strong>${esc(b.dataset.rot)}</strong> stops working immediately.`, "Replace token", async () => {
        const res = await mutate("POST", `/clients/${b.dataset.rot}/rotate`);
        showToken("New token", b.dataset.rot, res.token);
      })));
    main.querySelectorAll("[data-delcl]").forEach((b) => (b.onclick = () => confirmAction("Revoke token?",
      `<strong>${esc(b.dataset.delcl)}</strong> can no longer send or read notifications.${scheduledNote(cfg().clients[b.dataset.delcl]?.scheduled)}`, "Revoke",
      () => mutate("DELETE", `/clients/${b.dataset.delcl}`, undefined, "Token revoked"))));
  },

  scheduled(main) {
    main.innerHTML = `
      <div class="section-head"><h2>Scheduled</h2><p>Notifications waiting for their send time. Who gets them is decided when they go out.</p></div>
      <div class="rows" id="sched"><div class="empty">Loading…</div></div>`;
    loadScheduled(main.querySelector("#sched"));
  },

  activity(main) {
    main.innerHTML = `
      <div class="section-head"><h2>Activity</h2><p>Everything sent and received, newest first, with what each device reported back.</p></div>
      <div class="block" id="attention"></div>
      <div class="tl-controls" id="tl-controls"></div>
      <div id="tl-inbound"></div>
      <div class="rows" id="tl-list"><div class="empty">Loading…</div></div>
      <div class="tl-pager" id="tl-pager" hidden></div>`;
    activityList(main);
  },

  chats(main) {
    chatsPage(main);
  },

  requests(main) {
    main.innerHTML = `
      <div class="section-head"><h2>Access requests</h2>
        <p>Apps that asked for their own token. Nothing they send is accepted until you approve them here.</p></div>
      <div id="reqs"><div class="rows"><div class="empty">Loading…</div></div></div>`;
    loadRequests(main.querySelector("#reqs"));
  },

  settings(main) {
    const s = cfg().server;
    const r = cfg().registration;
    main.innerHTML = `
      <div class="section-head"><h2>Settings</h2></div>
      <div class="block"><h3>Admin password</h3><p class="hint">Changing it signs out every other browser.</p>
        <button class="btn" id="pw">Change password</button></div>
      <div class="block"><h3>Access requests</h3>
        <form class="stack" id="reg">
          ${fieldHtml({ name: "enabled", label: "Let apps ask for access themselves", kind: "check", value: r.enabled })}
          ${fieldHtml({ name: "notify_targets", label: "Alert about new requests", kind: "checks", value: r.notify_targets,
                        options: [...targetOptions(), ...Object.keys(cfg().channels).map((n) => [`channel:${n}`, `${n} (channel)`])],
                        help: "Nothing ticked: every person is alerted." })}
          ${fieldHtml({ name: "auto_approve_subnets", label: "Approve automatically from these networks (optional)", kind: "text",
                        value: r.auto_approve_subnets.join(", "), placeholder: "192.168.1.0/24",
                        help: "Comma-separated. Leave empty to always ask. Only applies to requests that stay within the permissions below." })}
          ${fieldHtml({ name: "auto_approve_scopes", label: "Permissions that may be approved automatically", kind: "checks",
                        value: r.auto_approve_scopes, options: ["send", "read", "manage"].map((x) => [x, SCOPE_HELP[x]]) })}
          <div class="form-error" role="alert"></div>
          <div class="form-actions"><button class="btn primary" type="submit">Save access settings</button></div>
        </form></div>
      <div class="block"><h3>Delivery</h3>
        <form class="stack" id="server">
          ${fieldHtml({ name: "max_attempts", label: "Attempts before giving up", kind: "int", value: s.max_attempts,
                        help: "Only network problems and Pushover outages are retried. Rejected requests fail at once." })}
          ${fieldHtml({ name: "retention_days", label: "Keep history for (days)", kind: "int", value: s.retention_days })}
          ${fieldHtml({ name: "external_url", label: "Address of this service on your network", kind: "url", value: s.external_url || "",
                        placeholder: "http://notify.lan:8930",
                        help: "Used for links in notifications hnl-notify sends you, such as access requests." })}
          ${fieldHtml({ name: "public_base_url", label: "Public address for instant acknowledgements (optional)", kind: "url", value: s.public_base_url || "",
                        placeholder: "https://notify.example.org",
                        help: "Only if Pushover can reach this service from the internet. Without it, acknowledgements of critical alerts are checked every 30 seconds." })}
          ${fieldHtml({ name: "attachment_retention_days", label: "Keep attachments for (days)", kind: "int", value: s.attachment_retention_days,
                        help: "Attached files are deleted after this; the notification history stays." })}
          ${fieldHtml({ name: "attachment_url_hosts", label: "Hosts allowed for attachment URLs", kind: "textarea",
                        value: (s.attachment_url_hosts || []).join("\n"),
                        help: "One per line: a host, host:port or network, for example ha.lan:8123. Empty: apps can only attach files directly, not by URL." })}
          ${fieldHtml({ name: "chat_cap", label: "Group chat cap", kind: "int", value: s.chat_cap ?? 10,
                        help: "Agent messages in a row before a group pauses." })}
          ${fieldHtml({ name: "chat_paused_posts", label: "Messages while paused", kind: "int", value: s.chat_paused_posts ?? 5,
                        help: "How many messages each agent may still post while its group is paused (0–50; 0: none). A group can override it." })}
          ${fieldHtml({ name: "chat_resume_wakes", label: "Messages delivered on resume", kind: "int", value: s.chat_resume_wakes ?? 20,
                        help: "On resume each agent gets at most this many held messages one by one (1–200); older ones arrive as one notice and stay unread." })}
          ${fieldHtml({ name: "chat_alert_targets", label: "Group chat alerts", kind: "checks", value: s.chat_alert_targets || [],
                        options: [...targetOptions(), ...Object.keys(cfg().channels).map((n) => [`channel:${n}`, `${n} (channel)`])],
                        help: "Who is pushed when a group pauses, e.g. user:alex. Nothing ticked: nobody." })}
          ${fieldHtml({ name: "trusted_proxies", label: "Trusted reverse proxies", kind: "textarea",
                        value: (s.trusted_proxies || []).join("\n"),
                        help: "One address or network per line, for example 10.0.0.2. Only requests from these may pass on the client's address and https (X-Forwarded-For, X-Forwarded-Proto). Empty: none." })}
          <div class="form-error" role="alert"></div>
          <div class="form-actions"><button class="btn primary" type="submit">Save settings</button></div>
        </form></div>`;
    main.querySelector("#pw").onclick = () => openForm({
      title: "Change password",
      fields: [{ name: "current", label: "Current password", kind: "secret" },
               { name: "new", label: "New password", kind: "secret", help: "At least 10 characters." }],
      submit: "Change password",
      onSubmit: async (v) => { await api("POST", "/password", v); toast("Password changed"); },
    });
    const reg = main.querySelector("#reg");
    reg.onsubmit = async (e) => {
      e.preventDefault();
      const err = reg.querySelector(".form-error");
      err.textContent = "";
      const v = readForm(reg, [{ name: "enabled", kind: "check" }, { name: "notify_targets", kind: "checks" },
                               { name: "auto_approve_subnets", kind: "text" }, { name: "auto_approve_scopes", kind: "checks" }]);
      v.auto_approve_subnets = v.auto_approve_subnets.split(",").map((x) => x.trim()).filter(Boolean);
      try { await mutate("PUT", "/registration", v, "Access settings saved"); } catch (ex) { err.textContent = ex.message; }
    };
    const form = main.querySelector("#server");
    form.onsubmit = async (e) => {
      e.preventDefault();
      const err = form.querySelector(".form-error");
      err.textContent = "";
      const v = readForm(form, [{ name: "max_attempts", kind: "int" }, { name: "retention_days", kind: "int" },
                                { name: "external_url", kind: "url" }, { name: "public_base_url", kind: "url" },
                                { name: "attachment_retention_days", kind: "int" }, { name: "attachment_url_hosts", kind: "textarea" },
                                { name: "chat_cap", kind: "int" }, { name: "chat_alert_targets", kind: "checks" },
                                { name: "chat_paused_posts", kind: "int" }, { name: "chat_resume_wakes", kind: "int" },
                                { name: "trusted_proxies", kind: "textarea" }]);
      v.attachment_url_hosts = v.attachment_url_hosts.split("\n").map((x) => x.trim()).filter(Boolean);
      v.trusted_proxies = v.trusted_proxies.split("\n").map((x) => x.trim()).filter(Boolean);
      try { await mutate("PUT", "/server", v, "Settings saved"); } catch (ex) { err.textContent = ex.message; }
    };
  },
};

// ---------------------------------------------------------------- forms

function slugField(name, value, help) {
  return { name: "name", label: name, kind: "text", value, readonly: !!value, placeholder: "lowercase-name",
           help: value ? "Names can't be changed; remove and re-add instead." : help };
}

function addProviderForm() {
  const c = cfg();
  const types = Object.entries(state.provider_types);
  const freeName = (t) => { if (!c.providers[t]) return t; let i = 2; while (c.providers[`${t}-${i}`]) i++; return `${t}-${i}`; };
  const used = new Set(Object.values(c.providers).map((p) => p.type));
  const first = (types.find(([t]) => !used.has(t)) || types[0])[0];
  let auto = freeName(first);
  openForm({
    title: "Add provider",
    fields: [
      { name: "type", label: "Type", kind: "select", value: first, options: types.map(([t, x]) => [t, x.label]) },
      { name: "name", label: "Name", kind: "text", value: auto, placeholder: "lowercase-name",
        help: "Devices refer to the provider by this name. Names can't be changed later." },
    ],
    submit: "Next",
    extra: (form) => {
      form.elements.type.onchange = () => {
        if (form.elements.name.value.trim() !== auto) return; // keep a name the user typed
        auto = freeName(form.elements.type.value);
        form.elements.name.value = auto;
      };
    },
    onSubmit: async (v) => {
      if (!v.name) throw new Error("Enter a name.");
      if (c.providers[v.name]) throw new Error(`${v.name} already exists; edit it instead.`);
      setTimeout(() => providerForm(v.name, v.type), 0);
    },
  });
}

/** Creates a single-use link that enrolls one browser as a device of the chosen person. */
function enrollmentForm(name) {
  const users = Object.entries(cfg().users);
  if (!users.length) { toast("Add a person first"); section = "people"; render(); return; }
  openForm({
    title: "Enrollment link",
    intro: "Open the link in the browser you want to enroll (Chrome, Edge or Firefox), then press Enable notifications. The link works once and expires after 24 hours.",
    fields: [{ name: "owner", label: "Belongs to", kind: "select", value: users[0][0], options: users.map(([n, u]) => [n, u.display_name || n]) }],
    submit: "Create link",
    onSubmit: async (v) => {
      const r = await api("POST", `/providers/${name}/enrollments`, { owner: v.owner });
      setTimeout(() => showEnrollment(r.url, r.expires_at), 0);
    },
  });
}

function showEnrollment(url, expires) {
  dialog.innerHTML = `
    <div class="stack" style="display:flex;flex-direction:column;gap:1rem">
      <h2>Enrollment link</h2>
      <p>Open this link in the browser to enroll. It works once and expires ${esc(when(expires))}.</p>
      <div class="secret-box"><code id="lnk">${esc(url)}</code><button class="btn" id="copy">Copy</button></div>
      <div class="form-actions"><button class="btn primary" id="done">Done</button></div>
    </div>`;
  dialog.querySelector("#copy").onclick = async () => {
    try { await navigator.clipboard.writeText(url); toast("Link copied"); }
    catch { toast("Copy isn't available here; select the link and copy it manually"); }
  };
  dialog.querySelector("#done").onclick = () => dialog.close();
  dialog.showModal();
}

function providerForm(name, newType) {
  const p = cfg().providers[name];
  const type = p?.type || newType;
  const t = state.provider_types[type];
  const pushover = type === "pushover";
  openForm({
    title: p ? `Edit ${name === type ? t.label : name}` : pushover ? "Connect Pushover" : `Add ${t.label}`,
    intro: pushover ? `Create an application at <a href="https://pushover.net/apps/build" target="_blank" rel="noopener">pushover.net/apps/build</a> (name it hnl-notify), then paste its API token.` : "",
    fields: Object.entries(t.settings)
      .filter(([k]) => type !== "webpush" || (k !== "vapid_private_key" && (k !== "vapid_public_key" || p))) // the key pair is generated, never typed
      .map(([k, s]) => ({ name: k, label: s.label, kind: s.kind, help: s.help, options: s.options, readonly: k === "vapid_public_key",
      value: p?.[k] ?? s.default ?? (s.kind === "check" ? false : ""), stored: s.kind === "secret" && p?.[k]?.set ? p[k].hint : "" })),
    submit: "Save",
    onSubmit: (v) => mutate("PUT", `/providers/${name}`, { type, settings: v }, `${t.label} saved`),
  });
}

/** draft: values carried over when the provider select re-renders the form (address fields are dropped). */
function deviceForm(name, draft = null) {
  const c = cfg();
  const d = name ? c.devices[name] : null;
  const pnames = Object.keys(c.providers);
  if (!pnames.length) { toast("Add a provider first"); section = "providers"; render(); return; }
  const pname = draft?.provider || (c.providers[d?.provider] ? d.provider : pnames[0]);
  const ptype = c.providers[pname].type;
  const t = state.provider_types[ptype];
  const schema = t.address;
  const same = d && d.provider === pname; // stored address applies
  const pick = (k, dflt) => (draft && k in draft ? draft[k] : dflt);
  // effective value for the stored provider, otherwise the default for the chosen provider's kind
  const optIn = draft?.optInTouched ? draft.opt_in : same ? (state.devices[name]?.opt_in ?? d.opt_in ?? t.kind === "email") : t.kind === "email";
  const fields = [
    { ...slugField("Name", name, "Used by services as device:NAME, for example alex-phone."), value: name || pick("name", "") },
    { name: "description", label: "Description", kind: "text", value: pick("description", d?.description || ""), placeholder: "Alex's phone" },
    { name: "owner", label: "Belongs to", kind: "select", value: pick("owner", d?.owner || ""),
      options: [["", "Nobody"], ...Object.entries(c.users).map(([n, u]) => [n, u.display_name || n])] },
    { name: "provider", label: "Provider", kind: "select", value: pname, options: pnames.map((n) => [n, providerLabel(n)]) },
    ...Object.entries(schema).map(([k, s]) => ({
      name: `addr_${k}`, label: s.label, kind: s.kind, help: s.help, options: s.options,
      value: same ? d.address?.[k] ?? "" : "",
      stored: s.kind === "secret" && same && d.address?.[k]?.set ? d.address[k].hint : "",
      readonly: ptype === "webpush" && BROWSER_FIELDS.has(k),
      list: k === "device", button: k === "device" && CHECKABLE.has(ptype) ? { action: "check", label: "Check address" } : null,
    })),
    { name: "min_severity", label: "Receive from urgency", kind: "select", value: pick("min_severity", d?.min_severity || "min"),
      options: state.severities.map((s) => [s, `${s}: ${SEVERITY_HELP[s]}`]) },
    { name: "enabled", label: "Deliver to this device", kind: "check", value: pick("enabled", d ? d.enabled : true) },
    { name: "opt_in", label: "Only when an app asks for this kind explicitly", kind: "check", value: optIn,
      help: "Email devices are opt-in by default so existing apps keep sending push only." },
    { name: "direct_only", label: "Only when an app names this device", kind: "check", value: pick("direct_only", d?.direct_only ?? false),
      help: `Skipped when an app sends to its owner; reached only as device:${name || "NAME"} or through a group or channel that lists it.` },
  ];
  openForm({
    title: d ? `Edit ${d.description || name}` : "Add device",
    fields, submit: d ? "Save device" : "Add device",
    focus: draft ? "select[name=provider]" : null,
    extra: (form) => {
      let optInTouched = !!draft?.optInTouched;
      form.elements.opt_in.onchange = () => (optInTouched = true);
      form.elements.provider.onchange = () => deviceForm(name, { ...readForm(form, fields), optInTouched });
      const btn = form.querySelector('[data-action="check"]');
      if (!btn) return;
      btn.onclick = async () => {
        const err = form.querySelector(".form-error");
        err.textContent = "";
        btn.disabled = true;
        try {
          const address = {};
          for (const k of Object.keys(schema)) if (k !== "device") address[k] = form.elements[`addr_${k}`].value.trim();
          const res = await api("POST", `/providers/${pname}/check`, { address, device: name || null });
          if (!res.supported) { btn.hidden = true; toast("This provider can't check addresses"); return; }
          if (!res.valid) throw new Error(res.error || "Pushover did not accept this user key");
          const devs = res.devices || [];
          const list = form.querySelector("#f-addr_device-list");
          if (list) list.innerHTML = devs.map((x) => `<option value="${esc(x)}">`).join("");
          toast(devs.length ? `User key is valid. Devices: ${devs.join(", ")}` : "User key is valid, but no devices are registered yet");
          form.elements.addr_device?.focus();
        } catch (ex) { err.textContent = ex.message; } finally { btn.disabled = false; }
      };
    },
    onSubmit: (v) => {
      const address = {};
      for (const k of Object.keys(schema)) address[k] = v[`addr_${k}`];
      return mutate("PUT", `/devices/${v.name}`, { provider: pname, owner: v.owner || null, description: v.description,
        enabled: v.enabled, min_severity: v.min_severity, opt_in: v.opt_in, direct_only: v.direct_only, address }, d ? "Device saved" : "Device added");
    },
  });
}

function userForm(name) {
  const c = cfg();
  const u = name ? c.users[name] : null;
  openForm({
    title: u ? `Edit ${u.display_name || name}` : "Add person",
    fields: [
      slugField("Name", name, "Used by services as user:NAME, for example alex."),
      { name: "display_name", label: "Display name", kind: "text", value: u?.display_name || "" },
      { name: "devices", label: "Devices", kind: "checks", value: u?.devices || [],
        options: Object.entries(c.devices).map(([n, d]) => [n, d.description || n]) },
    ],
    submit: u ? "Save person" : "Add person",
    onSubmit: (v) => mutate("PUT", `/users/${v.name}`, { display_name: v.display_name, devices: v.devices }, u ? "Person saved" : "Person added"),
  });
}

function groupForm(name) {
  const g = name ? cfg().groups[name] : null;
  openForm({
    title: g ? `Edit ${name}` : "Add group",
    fields: [
      slugField("Name", name, "Used by services as group:NAME, for example household."),
      { name: "description", label: "Description", kind: "text", value: g?.description || "" },
      { name: "members", label: "Members", kind: "checks", value: g?.members || [],
        options: targetOptions().filter(([t]) => t !== `group:${name}`) },
    ],
    submit: g ? "Save group" : "Add group",
    onSubmit: (v) => mutate("PUT", `/groups/${v.name}`, { description: v.description, members: v.members }, g ? "Group saved" : "Group added"),
  });
}

function channelForm(name) {
  const ch = name ? cfg().channels[name] : null;
  openForm({
    title: ch ? `Edit ${name}` : "Add channel",
    fields: [
      slugField("Name", name, "Used by services as channel:NAME, for example security."),
      { name: "description", label: "Description", kind: "text", value: ch?.description || "" },
      { name: "targets", label: "Who receives it", kind: "checks", value: ch?.targets || [], options: targetOptions() },
      { name: "min_severity", label: "Only from urgency", kind: "select", value: ch?.min_severity || "min",
        options: state.severities.map((s) => [s, `${s}: ${SEVERITY_HELP[s]}`]),
        help: "Less urgent notifications to this channel are recorded but not sent." },
    ],
    submit: ch ? "Save channel" : "Add channel",
    onSubmit: (v) => mutate("PUT", `/channels/${v.name}`, { description: v.description, targets: v.targets, min_severity: v.min_severity }, ch ? "Channel saved" : "Channel added"),
  });
}

function clientForm(name) {
  const cl = name ? cfg().clients[name] : null;
  openForm({
    title: cl ? `Edit ${name}` : "Create token",
    fields: [
      slugField("Name", name, "The service or agent using it, for example home-assistant."),
      { name: "description", label: "Description", kind: "text", value: cl?.description || "" },
      { name: "scopes", label: "Allowed to", kind: "checks", value: cl?.scopes || ["send", "read"],
        options: state.scopes.map((s) => [s, `${SCOPE_HELP[s]}`]) },
      { name: "chat_host", label: "Chat host", kind: "text", value: cl?.chat_host || "",
        help: "host name for agent group chats, e.g. host-a; leave empty for none" },
    ],
    submit: cl ? "Save" : "Create token",
    onSubmit: async (v) => {
      if (!v.scopes.length) throw new Error("Choose at least one permission.");
      const body = { description: v.description, scopes: v.scopes };
      if ("chat_host" in v) body.chat_host = v.chat_host; // the field starts with the stored value; empty clears it
      if (cl) return mutate("PUT", `/clients/${v.name}`, body, "Token saved");
      const res = await mutate("POST", `/clients/${v.name}`, body);
      setTimeout(() => showToken("Token created", v.name, res.token), 0);
    },
  });
}

// ---------------------------------------------------------------- access requests

const REQUEST_STATUS = { approved: "ok", denied: "bad", expired: "", revoked: "" };

async function loadRequests(el) {
  let data;
  try { data = await api("GET", "/registrations"); }
  catch (ex) { el.innerHTML = `<div class="rows"><div class="empty problem">${esc(ex.message)}</div></div>`; return; }
  const from = (r) => [r.source_ip && `from ${r.source_ip}`, r.user_agent && `(${r.user_agent})`].filter(Boolean).join(" ");
  el.innerHTML = `
    <div class="block"><div class="rows">${data.pending.map((r) => `
      <div class="row"><span class="lamp warn"></span>
        <div class="grow"><div class="title">${esc(r.name)}</div>
          ${r.description ? `<div class="sub">${esc(r.description)}</div>` : ""}
          <div class="sub">Asked ${esc(when(r.created_at))} ${esc(from(r))}</div>
          <div class="chips">${r.requested_scopes.map((x) => `<span class="chip" title="${esc(SCOPE_HELP[x])}">${esc(x)}</span>`).join("")}</div></div>
        <div class="actions"><button class="btn primary" data-approve="${esc(r.id)}">Approve</button><button class="btn danger" data-deny="${esc(r.id)}">Deny</button></div>
      </div>`).join("") || `<div class="empty">No apps are waiting. Apps can ask for access with the snippet under API access.</div>`}
    </div></div>
    ${data.recent.length ? `<div class="block"><h3>Decided</h3><div class="rows">${data.recent.map((r) => `
      <div class="row"><span class="lamp ${REQUEST_STATUS[r.status] ?? ""}"></span>
        <div class="grow"><div class="title">${esc(r.name)}</div>
          <div class="sub">${esc(r.status)} ${esc(when(r.decided_at))}${r.decided_by === "auto-approve" ? " automatically" : ""}${r.granted_scopes ? `, may ${esc(r.granted_scopes.join(", "))}` : ""}</div></div></div>`).join("")}
    </div></div>` : ""}`;
  const byId = Object.fromEntries(data.pending.map((r) => [r.id, r]));
  el.querySelectorAll("[data-approve]").forEach((b) => (b.onclick = () => {
    const r = byId[b.dataset.approve];
    openForm({
      title: `Approve ${r.name}`,
      intro: esc(r.description || "No description given."),
      fields: [
        { name: "scopes", label: "Allowed to", kind: "checks", value: r.requested_scopes,
          options: r.requested_scopes.map((x) => [x, SCOPE_HELP[x]]),
          help: "Untick anything it shouldn't get. It keeps the token it already has." },
        { name: "description", label: "Description", kind: "text", value: r.description || "" },
      ],
      submit: "Approve",
      onSubmit: async (v) => {
        if (!v.scopes.length) throw new Error("Grant at least one permission, or deny the request.");
        const res = await api("POST", `/registrations/${r.id}/approve`, v);
        state = res.state; render(); toast(`${r.name} approved`);
      },
    });
  }));
  el.querySelectorAll("[data-deny]").forEach((b) => (b.onclick = () => {
    const r = byId[b.dataset.deny];
    confirmAction(`Deny ${r.name}?`, "Its token will never work. The app can ask again later.", "Deny", async () => {
      const res = await api("POST", `/registrations/${r.id}/deny`, {});
      state = res.state; render(); toast(`${r.name} denied`);
    });
  }));
}

// ---------------------------------------------------------------- scheduled list

const scheduledNote = (n) => (n ? ` ${n} scheduled notification${n === 1 ? "" : "s"} from this client will be cancelled.` : "");

// A 409 means someone got there first: say so, then show the list as it is now.
function raced(ex) {
  if (ex.status !== 409) return false;
  toast(/cancelled/.test(ex.message) ? "Already cancelled" : "Already sent");
  refresh();
  return true;
}

async function loadScheduled(el) {
  let data;
  try { data = await api("GET", "/scheduled"); }
  catch (ex) { el.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  const tz = data.timezone;
  const items = data.items;
  if (!items.length) {
    el.innerHTML = `<div class="empty">Nothing is scheduled. Send with <code>send_at</code> (a time) or <code>delay</code> (such as <code>2h</code>) to schedule one; see the <a href="/docs" target="_blank" rel="noopener">API docs</a>.</div>`;
    return;
  }
  const byId = Object.fromEntries(items.map((n) => [n.id, n]));
  el.innerHTML = items.map((n) => {
    const left = until(n.send_at);
    const markers = [n.reply ? "reply wanted" : "", n.attachments?.length ? `${n.attachments.length} attachment${n.attachments.length === 1 ? "" : "s"}` : "",
      n.in_reply_to ? "follow-up" : ""].filter(Boolean);
    return `<div class="row" style="align-items:flex-start"><span class="lamp ${left === "overdue" ? "warn" : ""}" style="margin-top:.45rem"></span>
      <div class="grow">
        <div class="title">${esc(n.title || n.message.split("\n")[0].slice(0, 60))}</div>
        <div class="sub">${esc(zoneWhen(n.send_at, tz))} (${esc(tz)}) · <span class="${left === "overdue" ? "problem" : ""}">${esc(left)}</span></div>
        <div class="sub">to ${esc(n.targets.join(", "))}, ${esc(n.severity)}, from ${esc(n.source)}</div>
        ${markers.length ? `<div class="chips">${markers.map((m) => `<span class="chip">${esc(m)}</span>`).join("")}</div>` : ""}
      </div>
      <div class="actions">
        <button class="btn" data-now="${esc(n.id)}">Send now</button>
        <button class="btn" data-resched="${esc(n.id)}">Reschedule</button>
        <button class="btn" data-edit="${esc(n.id)}">Edit</button>
        <button class="btn" data-cancel="${esc(n.id)}">Cancel</button>
      </div></div>`;
  }).join("");
  const label = (n) => esc(n.title || n.message.slice(0, 60));
  el.querySelectorAll("[data-now]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { await api("POST", `/notifications/${b.dataset.now}/send-now`); toast("Sending now"); }
    catch (ex) { if (!raced(ex)) toast(ex.message); }
    refresh();
  }));
  el.querySelectorAll("[data-resched]").forEach((b) => (b.onclick = () => {
    const n = byId[b.dataset.resched];
    openForm({
      title: "Reschedule", intro: `<strong>${label(n)}</strong>. Times are in ${esc(tz)}.`,
      fields: [{ name: "send_at", label: `New send time (${tz})`, kind: "datetime", value: zoneInput(n.send_at, tz) }],
      submit: "Reschedule",
      onSubmit: async (v) => {
        const send_at = zonedToIso(v.send_at, tz);
        try { await api("PATCH", `/notifications/${n.id}`, { send_at }); }
        catch (ex) { if (raced(ex)) return; throw ex; }
        toast("Rescheduled");
        refresh();
      },
    });
  }));
  el.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => editScheduled(byId[b.dataset.edit])));
  el.querySelectorAll("[data-cancel]").forEach((b) => (b.onclick = () => {
    const n = byId[b.dataset.cancel];
    confirmAction("Cancel this notification?", `<strong>${label(n)}</strong> will not be sent.`, "Cancel notification", async () => {
      try { await api("POST", `/notifications/${n.id}/cancel`); }
      catch (ex) { if (raced(ex)) return; throw ex; }
      toast("Cancelled");
      refresh();
    });
  }));
}

function editScheduled(n) {
  const known = new Set(targetOptions().map(([v]) => v));
  const options = [...targetOptions(), ...n.targets.filter((t) => !known.has(t)).map((t) => [t, t])];
  const fields = [
    { name: "title", label: "Title", kind: "text", value: n.title || "" },
    { name: "message", label: "Message", kind: "textarea", value: n.message },
    { name: "severity", label: "Severity", kind: "select", value: n.severity, options: state.severities.map((s) => [s, `${s} (${SEVERITY_HELP[s] || ""})`]) },
    { name: "targets", label: "Send to", kind: "checks", value: n.targets, options },
    { name: "kinds", label: "Delivery", kind: "select", value: (n.kinds || []).join(","), options: KINDS },
    { name: "url", label: "Link URL", kind: "url", value: n.url || "" },
    { name: "url_title", label: "Link title", kind: "text", value: n.url_title || "" },
    { name: "ttl_seconds", label: "Valid for (seconds after sending)", kind: "int", value: n.ttl_seconds ?? "", help: "Leave empty for no limit." },
    { name: "max_late_seconds", label: "May go out up to this late (seconds)", kind: "int", value: n.max_late_seconds, help: "If the service was down at the send time. 60 to 86400." },
  ];
  openForm({
    title: "Edit scheduled notification", fields, submit: "Save",
    extra: (form) => {
      // who would get it if it went out now, from the ordinary dry-run
      const box = document.createElement("div");
      box.className = "hint";
      form.querySelector(".form-error").before(box);
      let seq = 0;
      const refreshPreview = async () => {
        const mine = ++seq;
        const v = readForm(form, fields);
        if (!v.targets.length) { box.textContent = "Pick at least one target."; return; }
        try {
          const r = await api("POST", "/preview", { targets: v.targets, severity: v.severity, kinds: v.kinds ? v.kinds.split(",") : null,
            title: v.title || null, message: v.message || "preview" });
          if (mine !== seq) return;
          const devices = r.routing.devices.map((d) => cfg().devices[d.device]?.description || d.device);
          const skipped = r.routing.suppressed.map((x) => `${x.target}: ${x.reason}`);
          box.innerHTML = `If it went out now it would reach ${devices.length ? esc(devices.join(", ")) : "no device"}.${skipped.length ? ` Not reached: ${esc(skipped.join("; "))}.` : ""}`;
        } catch (ex) { if (mine === seq) box.textContent = ex.message; }
      };
      form.addEventListener("change", refreshPreview);
      refreshPreview();
    },
    onSubmit: async (v) => {
      const patch = {};
      const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
      for (const k of ["title", "url", "url_title"]) if (v[k] !== (n[k] || "")) patch[k] = v[k] || null;
      if (v.message !== n.message) patch.message = v.message;
      if (v.severity !== n.severity) patch.severity = v.severity;
      if (!same([...v.targets].sort(), [...n.targets].sort())) patch.targets = v.targets;
      const kinds = v.kinds ? v.kinds.split(",") : null;
      if (!same(kinds, n.kinds || null)) patch.kinds = kinds;
      if (v.ttl_seconds !== (n.ttl_seconds ?? null)) patch.ttl_seconds = v.ttl_seconds;
      if (v.max_late_seconds !== null && v.max_late_seconds !== n.max_late_seconds) patch.max_late_seconds = v.max_late_seconds;
      if (!Object.keys(patch).length) { toast("Nothing changed"); return; }
      try { await api("PATCH", `/notifications/${n.id}`, patch); }
      catch (ex) { if (raced(ex)) return; throw ex; }
      toast("Saved");
      refresh();
    },
  });
}

// ---------------------------------------------------------------- activity list

function attachmentList(n) {
  return n.attachments?.length ? `<dl class="kv"><dt>Attachments</dt><dd>${n.attachments.map((a) =>
    `${esc(a.filename)} (${esc(a.content_type)}, ${esc(fmtSize(a.size))})${a.purged ? ` <span class="chip">purged</span>` : ""}`).join("<br>")}</dd></dl>` : "";
}

function deliveryDetails(n) {
  return n.deliveries.map((d) => `
    <details class="delivery"><summary>${esc(cfg().devices[d.device]?.description || d.device)}: ${esc(d.state)}${d.final ? "" : " (may still change)"}</summary>
      <dl class="kv">
        ${d.timestamps.dispatched ? `<dt>Accepted by ${esc(d.provider)}</dt><dd>${esc(when(d.timestamps.dispatched))}</dd>` : ""}
        ${d.timestamps.delivered ? `<dt>Delivered</dt><dd>${esc(when(d.timestamps.delivered))}</dd>` : ""}
        ${d.timestamps.acknowledged ? `<dt>Acknowledged</dt><dd>${esc(when(d.timestamps.acknowledged))}${d.acknowledged_by ? ` on ${esc(d.acknowledged_by)}` : ""}</dd>` : ""}
        <dt>Can report</dt><dd>${esc(d.observable_states.filter((s) => ["dispatched", "delivered", "acknowledged"].includes(s)).join(", "))}</dd>
        <dt>Attempts</dt><dd>${d.attempts}</dd>
        ${d.last_error ? `<dt>Last error</dt><dd class="problem">${esc(d.last_error)}</dd>` : ""}
        ${d.capability_gaps.length ? `<dt>Not supported</dt><dd>${d.capability_gaps.map(esc).join("<br>")}</dd>` : ""}
      </dl></details>`).join("");
}

function notificationActions(n) {
  return `<div class="actions">
    ${["failed", "partial"].includes(n.status) ? `<button class="btn" data-retry="${esc(n.id)}">Retry</button>` : ""}
    ${n.status === "pending" || n.deliveries.some((d) => !d.final && d.tracking === "receipt") ? `<button class="btn" data-cancel="${esc(n.id)}">Cancel</button>` : ""}
  </div>`;
}

function bindNotificationActions(el, reload) {
  el.querySelectorAll("[data-retry]").forEach((b) => (b.onclick = async () => {
    try { await api("POST", `/notifications/${b.dataset.retry}/retry`); toast("Queued again"); reload(); }
    catch (ex) { toast(ex.message); }
  }));
  el.querySelectorAll("[data-cancel]").forEach((b) => (b.onclick = async () => {
    try { await api("POST", `/notifications/${b.dataset.cancel}/cancel`); toast("Cancelled"); reload(); }
    catch (ex) { toast(ex.message); }
  }));
}

async function loadAttention(attentionEl, reload) {
  let data;
  try { data = await api("GET", "/activity?limit=1"); }
  catch (ex) { attentionEl.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  const a = data.attention;
  const groups = new Map(); // one row per notification and kind, listing its devices
  for (const [kind, list] of [["failed", a.failed], ["expired", a.expired], ["stuck", a.stuck], ["retrying", a.retrying]]) {
    for (const x of list) {
      const key = `${kind}:${x.notification_id}`;
      const g = groups.get(key) || { kind, x, devices: [], at: x.at };
      g.devices.push(x.device);
      if (x.at > g.at) { g.at = x.at; g.x = x; } // attempts and error from the latest delivery
      groups.set(key, g);
    }
  }
  const items = [...groups.values()].sort((p, q) => (q.at || "").localeCompare(p.at || ""));
  const rp = a.release_problems || [];
  attentionEl.innerHTML = items.length || a.awaiting_acknowledgement.length || rp.length ? `
    <h3>Needs attention</h3><div class="rows">
    ${rp.map((x) => `<div class="row"><span class="lamp bad"></span><div class="grow"><div class="title">${esc(x.title || "(no title)")}</div>
      <div class="sub">Scheduled for ${esc(zoneWhen(x.send_at, data.timezone))} (${esc(data.timezone)}) · ${esc(x.status)}</div>
      <div class="sub">${esc(x.reason || "")}</div></div>
      <div class="actions">${x.status === "scheduled"
        ? `<button class="btn" data-relnow="${esc(x.notification_id)}">Send now</button>`
        : `<button class="btn" data-dismiss="${esc(x.notification_id)}">Dismiss</button>
      <button class="btn" data-relretry="${esc(x.notification_id)}">Retry</button>`}</div></div>`).join("")}
    ${a.awaiting_acknowledgement.map((x) => `<div class="row"><span class="lamp warn"></span><div class="grow"><div class="title">${esc(x.title || "(no title)")}</div><div class="sub">Critical alert on ${esc(x.device)} not acknowledged yet</div></div></div>`).join("")}
    ${items.map(({ kind, x, devices, at }) => `<div class="row"><span class="lamp bad"></span><div class="grow"><div class="title">${esc(x.title || "(no title)")}</div>
      <div class="sub">${kind === "failed" || kind === "expired" ? "" : "last change "}${esc(when(at))} (${esc(ago(at))})</div>
      <div class="sub">${esc(kind)} on ${esc(devices.join(", "))} after ${x.attempts} attempts: ${esc(x.last_error || "")}</div></div>
      ${kind === "failed" || kind === "expired" ? `<div class="actions"><button class="btn" data-dismiss="${esc(x.notification_id)}">Dismiss</button></div>` : ""}</div>`).join("")}
    </div>` : "";
  attentionEl.querySelectorAll("[data-relnow]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { await api("POST", `/notifications/${b.dataset.relnow}/send-now`); toast("Sending now"); }
    catch (ex) { toast(ex.status === 409 ? "Already sent" : ex.message); }
    reload();
  }));
  attentionEl.querySelectorAll("[data-relretry]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { await api("POST", `/notifications/${b.dataset.relretry}/retry`); toast("Sending now"); }
    catch (ex) { toast(ex.message); }
    reload();
  }));
  attentionEl.querySelectorAll("[data-dismiss]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try { await api("POST", `/notifications/${b.dataset.dismiss}/dismiss`); toast("Dismissed"); }
    catch (ex) { toast(ex.message); }
    reload(); // also after a 409: another tab may have dismissed it already
  }));
}

async function loadRecent(el, limit) {
  let data;
  try { data = await api("GET", `/activity?limit=${limit}`); }
  catch (ex) { el.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  if (!data.notifications.length) { el.innerHTML = `<div class="empty">Nothing sent yet.</div>`; return; }
  el.innerHTML = data.notifications.map((n) => `
    <div class="row" style="align-items:flex-start"><span class="lamp ${lampFor(n.status)}" style="margin-top:.45rem"></span>
      <div class="grow">
        <div class="title">${esc(n.title || n.message.slice(0, 60))}</div>
        <div class="sub">${esc(when(n.sent_at))} from ${esc(n.source)} to ${esc(n.targets.join(", "))}, ${esc(n.severity)}</div>
        <div class="sub"><span class="status">${esc(n.status)}</span>${n.suppressed.length ? `: ${esc(n.suppressed[0].reason)}` : ""}</div>
        ${attachmentList(n)}
        ${deliveryDetails(n)}
      </div>
      ${notificationActions(n)}</div>`).join("");
  bindNotificationActions(el, () => loadRecent(el, limit));
}

// ---------------------------------------------------------------- everything sent and received (Activity tab)

const DIRECTION_ICONS = {
  sent: `<span class="dir sent" role="img" aria-label="Sent" title="Sent"><svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M4 12 12 4M6 4h6v6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>`,
  received: `<span class="dir received" role="img" aria-label="Received" title="Received"><svg viewBox="0 0 16 16" width="16" height="16" aria-hidden="true"><path d="M12 4 4 12M10 12H4V6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg></span>`,
};

// Always plain text: a body can come from anyone who can mail us, and a sent message may be HTML for the provider.
function bodyBlock(text) {
  if (!text) return "";
  return `<div class="body" data-body>${esc(text)}</div>
    <button type="button" class="btn quiet more" data-more aria-expanded="false" hidden>Show more</button>`;
}

function idLine(id, extra = "") {
  return `<div class="ids"><code>${esc(id)}</code>
    <button type="button" class="btn quiet" data-copy="${esc(id)}" aria-label="Copy ID ${esc(id)}">Copy</button>${extra}</div>`;
}

function timelineRow(i) {
  if (i.kind === "rejected") {
    return `<div class="row">${DIRECTION_ICONS.received}<span class="lamp bad"></span><div class="grow">
      <div class="title">Rejected: ${esc(i.subject || "(no subject)")}</div>
      <div class="sub">${esc(when(i.at))} from ${esc(i.from_address || "?")}: ${esc(i.reason)}</div></div></div>`;
  }
  if (i.kind === "reply") {
    return `<div class="row">${DIRECTION_ICONS.received}<span class="lamp ${i.state === "unclaimed" ? "warn" : "ok"}"></span><div class="grow">
      <div class="title">${esc(i.notification_title || i.subject || "(no title)")}</div>
      <div class="sub">${esc(when(i.at))} from ${esc(i.from)}: ${esc(i.state)}${i.claimed_by ? ` by ${esc(i.claimed_by)}` : ""}${i.autoreply_notification_id ? ", autoreply sent" : ""}</div>
      ${bodyBlock(i.text)}
      ${i.truncated ? `<div class="sub">The stored text was cut off at the size limit.</div>` : ""}
      ${i.attachments.length ? `<div class="sub">${i.attachments.map((a) => esc(a.filename)).join(", ")}</div>` : ""}
      ${idLine(i.id, ` <span class="sub">answers <button type="button" class="btn quiet" data-find="${esc(i.notification_id)}">${esc(i.notification_id)}</button></span>`)}
    </div></div>`;
  }
  const line = !i.unsent ? `${esc(when(i.sent_at))} from ${esc(i.source)} to ${esc(i.targets.join(", "))}, ${esc(i.severity)}`
    : i.status === "scheduled" ? `Not sent yet: scheduled for ${esc(when(i.send_at))}, from ${esc(i.source)} to ${esc(i.targets.join(", "))}`
    : `Not sent: cancelled while waiting, from ${esc(i.source)} to ${esc(i.targets.join(", "))}`;
  return `<div class="row">${DIRECTION_ICONS.sent}<span class="lamp ${lampFor(i.status)}"></span><div class="grow">
      <div class="title">${esc(i.title || "(no title)")}</div>
      <div class="sub">${line}</div>
      <div class="sub"><span class="status">${esc(i.status)}</span>${i.suppressed.length ? `: ${esc(i.suppressed[0].reason)}` : ""}</div>
      ${bodyBlock(i.message)}
      ${attachmentList(i)}
      ${idLine(i.id)}
      ${deliveryDetails(i)}
    </div>${notificationActions(i)}</div>`;
}

async function copyText(text, shownIn) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* refused: fall through */ }
  // the admin page usually runs on plain HTTP, where browsers withhold the clipboard API
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.style.position = "fixed";
  area.style.opacity = "0";
  document.body.append(area);
  area.select();
  let copied = false;
  try { copied = document.execCommand("copy"); } catch { /* reported below */ }
  area.remove();
  if (!copied && shownIn) { // leave it selected, so the keyboard can finish the job
    const range = document.createRange();
    range.selectNodeContents(shownIn);
    const selection = window.getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
  }
  return copied;
}

function activityList(main) {
  const els = {
    controls: main.querySelector("#tl-controls"), inbound: main.querySelector("#tl-inbound"),
    list: main.querySelector("#tl-list"), pager: main.querySelector("#tl-pager"),
  };
  const state = { direction: "all", source: "", find: "", before: null, after: null, request: 0 };
  const firstPage = () => { state.before = null; state.after = null; };

  els.controls.innerHTML = `
    <div class="seg" role="group" aria-label="Show">
      ${[["all", "All"], ["sent", "Sent"], ["received", "Received"]].map(([v, label]) =>
        `<button type="button" class="btn" data-direction="${v}" aria-pressed="${v === "all"}">${label}</button>`).join("")}
    </div>
    <label class="tl-source">Sender <select id="tl-source"><option value="">Everyone</option></select></label>
    <form id="tl-find" class="tl-find">
      <input type="text" name="find" aria-label="Find by ID" placeholder="Find by ID: ntf_… or rpl_…" autocomplete="off" spellcheck="false">
      <button type="submit" class="btn">Find</button>
      <button type="button" class="btn quiet" id="tl-clear" hidden>Show all</button>
    </form>`;
  const sourceEl = els.controls.querySelector("#tl-source");
  const findForm = els.controls.querySelector("#tl-find");
  const clearEl = els.controls.querySelector("#tl-clear");

  const search = (needle) => {
    state.find = needle.trim();
    findForm.elements.find.value = state.find;
    firstPage();
    load();
  };
  els.controls.querySelectorAll("[data-direction]").forEach((b) => (b.onclick = () => {
    state.direction = b.dataset.direction;
    els.controls.querySelectorAll("[data-direction]").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
    firstPage();
    load();
  }));
  sourceEl.onchange = () => { state.source = sourceEl.value; firstPage(); load(); };
  findForm.onsubmit = (e) => { e.preventDefault(); search(findForm.elements.find.value); };
  clearEl.onclick = () => search("");

  function renderBodies() {
    els.list.querySelectorAll("[data-body]").forEach((body) => {
      const more = body.nextElementSibling;
      more.hidden = body.scrollHeight <= body.clientHeight + 1;
      more.onclick = () => {
        const open = body.classList.toggle("open");
        more.textContent = open ? "Show less" : "Show more";
        more.setAttribute("aria-expanded", String(open));
      };
    });
  }

  async function load({ scroll = false } = {}) {
    const request = ++state.request; // a slow answer must not overwrite a newer choice
    const q = new URLSearchParams({ limit: "25" });
    if (state.find) q.set("find", state.find);
    else {
      if (state.direction !== "all") q.set("direction", state.direction);
      if (state.source) q.set("source", state.source);
      if (state.before) q.set("before", state.before);
      if (state.after) q.set("after", state.after);
    }
    let data;
    try { data = await api("GET", `/timeline?${q}`); }
    catch (ex) {
      if (request === state.request) { els.list.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; els.pager.hidden = true; }
      return;
    }
    if (request !== state.request) return;
    if (!data.items.length && (state.before || state.after)) { firstPage(); load(); return; } // that page was purged

    els.inbound.innerHTML = Object.entries(data.inbound).map(([name, s]) => `<div class="sub">${esc(name)}: ${s.last_error
      ? `<span class="problem">${esc(s.last_error)}</span>` : s.last_success ? `checked ${esc(when(s.last_success))}` : "not checked yet"}${deletedLeftText(s)}</div>`).join("");
    const known = [...new Set([...data.sources, ...(state.source ? [state.source] : [])])].sort();
    sourceEl.innerHTML = `<option value="">Everyone</option>${known.map((s) => `<option value="${esc(s)}">${esc(s)}</option>`).join("")}`;
    sourceEl.value = state.source;
    clearEl.hidden = !state.find;
    els.controls.querySelectorAll("[data-direction]").forEach((b) => (b.disabled = Boolean(state.find)));
    sourceEl.disabled = Boolean(state.find);

    els.list.innerHTML = data.items.length ? data.items.map(timelineRow).join("")
      : `<div class="empty">${state.find ? "Nothing with exactly that ID. It may have passed the retention period."
        : state.direction === "all" && !state.source ? "Nothing sent or received yet." : "Nothing matches these filters."}</div>`;
    renderBodies();
    bindNotificationActions(els.list, () => reloadAll());
    els.list.querySelectorAll("[data-find]").forEach((b) => (b.onclick = () => search(b.dataset.find)));
    els.list.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = async () => {
      toast(await copyText(b.dataset.copy, b.previousElementSibling) ? "ID copied" : "Could not copy: press Ctrl+C or Cmd+C");
    }));

    els.pager.hidden = !data.older && !data.newer;
    els.pager.innerHTML = `
      <button type="button" class="btn" data-page="newer" ${data.newer ? "" : "disabled"}>Newer</button>
      <button type="button" class="btn" data-page="older" ${data.older ? "" : "disabled"}>Older</button>`;
    els.pager.querySelector("[data-page=newer]").onclick = () => { state.before = null; state.after = data.newer; load({ scroll: true }); };
    els.pager.querySelector("[data-page=older]").onclick = () => { state.after = null; state.before = data.older; load({ scroll: true }); };
    if (scroll) els.controls.scrollIntoView({ block: "start" });
  }

  const attentionEl = main.querySelector("#attention");
  const reloadAll = () => { loadAttention(attentionEl, reloadAll); load(); };
  reloadAll();
}

// ---------------------------------------------------------------- reply mailbox (GET /admin/api/mailbox)

const MIB = 1048576;
const mib = (bytes) => `${(bytes / MIB).toFixed(1)} MiB`;
const mailboxNotes = {}; // provider -> result line of the last manual cleanup, shown after the panel reloads

function mailboxFields(name, st) {
  const f = (n, label) => ({ name: `${name}__${n}`, label, kind: "int", value: st[n] });
  return [
    { name: `${name}__enabled`, label: "Clean up automatically", kind: "check", value: st.enabled,
      help: "Once a day. Off stops the daily run; “Clean up now” still works." },
    f("processed_days", "Keep handled replies (days)"),
    f("quarantine_days", "Keep rejected mail (days)"),
    f("processed_max_mib", "Max size handled replies (MiB)"),
    f("quarantine_max_mib", "Max size rejected mail (MiB)"),
    { name: `${name}__alert_targets`, label: "Send size alerts to", kind: "checks", value: st.alert_targets,
      options: [...targetOptions(), ...Object.keys(cfg().channels).map((n) => [`channel:${n}`, `${n} (channel)`])],
      help: "Sent when a folder outgrows its size limit. Nothing ticked: the people who hear about access requests." },
  ];
}

function mailboxResultText(r) {
  if (r.error) return `The cleanup could not run: ${r.error}`;
  if (r.refused) return `Nothing was deleted: ${r.refused}.`;
  const parts = Object.entries(r.folders).filter(([, f]) => f.removed_age + f.removed_cap)
    .map(([n, f]) => `${f.removed_age + f.removed_cap} from ${n} (${mib(f.removed_bytes)})`);
  const problems = Object.entries(r.folders).filter(([, f]) => f.error).map(([n, f]) => `${n}: ${f.error}`);
  const unfinished = Object.values(r.folders).some((f) => f.unfinished);
  return `${parts.length ? `Deleted ${parts.join(", ")}.` : "Nothing was past the limits."}${unfinished ? " It stopped early; run it again to continue." : ""}${problems.length ? ` Problems: ${problems.join("; ")}` : ""}`;
}

const DAY = 86400000;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

// One folder: what is in it, a size gauge against its limit, and the two rules that act on it, side by side.
function mailboxFolder(p, role, folderName, f) {
  const st = p.settings, days = st[`${role}_days`], capMib = st[`${role}_max_mib`];
  const label = role === "processed" ? "Handled replies" : "Rejected mail";
  const why = role === "processed" ? "Copies of replies agents already have." : "The only copy of mail that failed the checks.";
  const field = (key, aria, unit) => `<label class="rule"><span>${key === "days" ? "Keep" : "At most"}</span>
      <input type="number" min="0" inputmode="numeric" name="${esc(`${p.name}__${role}_${key === "days" ? "days" : "max_mib"}`)}"
        value="${esc(key === "days" ? days : capMib)}" aria-label="${esc(aria)}"><span>${unit}</span></label>`;
  const rules = `<div class="rules">${field("days", role === "processed" ? "Keep handled replies (days)" : "Keep rejected mail (days)", "days")}
      ${field("cap", role === "processed" ? "Max size handled replies (MiB)" : "Max size rejected mail (MiB)", "MiB")}</div>`;
  if (!f) {
    return `<div class="mbox-folder"><div class="mbox-head"><div><div class="title">${esc(label)}</div>
      <div class="sub">${esc(folderName)} folder, not read</div></div></div>${rules}</div>`;
  }
  const cap = capMib * MIB, going = f.removed_bytes, kept = Math.max(0, f.bytes - going);
  const scale = Math.max(cap, f.bytes, 1);
  const pct = (b) => `${Math.min(100, (b / scale) * 100).toFixed(2)}%`;
  const gone = f.removed_age + f.removed_cap;
  const sizeLine = cap ? `${esc(mib(f.bytes))} of ${esc(capMib)} MiB` : `${esc(mib(f.bytes))}, no size limit`;
  let ageLine = "Empty.";
  if (f.oldest) {
    const age = Math.floor((Date.now() - new Date(f.oldest)) / DAY);
    if (!days) ageLine = `Oldest mail is ${plural(age, "day", "days")} old; no age limit.`;
    else if (age >= days) ageLine = `${plural(f.removed_age, "mail is", "mails are")} older than ${days} days.`;
    else ageLine = `Oldest mail is ${plural(age, "day", "days")} old; the first goes on ${esc(new Date(new Date(f.oldest).getTime() + days * DAY).toLocaleDateString([], { day: "numeric", month: "short" }))}.`;
  }
  const meterLabel = `${mib(f.bytes)} used${cap ? ` of a ${mib(cap)} limit` : ""}${gone ? `, ${mib(going)} would be deleted` : ""}`;
  return `<div class="mbox-folder${f.error ? " has-error" : ""}">
    <div class="mbox-head">
      <div><div class="title">${esc(label)}</div><div class="sub">${esc(folderName)} folder. ${esc(why)}</div></div>
      <div class="mbox-size"><strong>${plural(Number(f.messages) || 0, "mail", "mails")}</strong><span>${sizeLine}</span></div>
    </div>
    <div class="gauge" role="img" aria-label="${esc(meterLabel)}">
      <span class="track"><span class="kept" style="width:${pct(kept)}"></span><span class="going" style="width:${pct(going)}"></span></span>
      ${cap ? `<span class="cap" style="left:${pct(cap)}"></span>` : ""}
    </div>
    <div class="sub mbox-age">${ageLine}${gone ? ` <span class="going-note">${plural(gone, "mail", "mails")} (${esc(mib(going))}) would be deleted now.</span>` : ""}</div>
    ${f.error ? `<div class="problem">Reading or cleaning this folder failed: ${esc(f.error)}</div>` : ""}
    ${rules}
  </div>`;
}

function mailboxPanel(p) {
  const pv = p.preview, st = p.status || {};
  const prov = cfg().providers[p.name] || {};
  const names = { processed: prov.reply_processed || "Processed", quarantine: prov.reply_quarantine || "Quarantine" };
  const folders = pv.folders || {};
  const would = Object.values(folders).reduce((a, f) => ({ n: a.n + f.removed_age + f.removed_cap, b: a.b + f.removed_bytes }), { n: 0, b: 0 });
  const blocked = pv.error || pv.refused;
  const lamp = blocked ? "bad" : would.n ? "warn" : "ok";
  const headline = pv.error ? "The mailbox could not be read."
    : pv.refused ? "Cleanup can't run for this mailbox."
    : would.n ? `${plural(would.n, "mail", "mails")} (${esc(mib(would.b))}) ${would.n === 1 ? "is" : "are"} past the limits below.`
    : "Nothing is past the limits below.";
  const detail = pv.error ? esc(pv.error) : pv.refused ? `${esc(pv.refused)}.`
    : would.n ? (st.last_run && p.settings.enabled ? "The daily cleanup deletes them, or clean up now." : "Clean up now to delete them.")
    : "Mail is deleted once it passes them.";
  const last = st.last_run
    ? `Last cleanup ${esc(when(st.last_run))}${st.last_by && st.last_by !== "hnl-notify" ? `, started from ${esc(st.last_by)}` : ", automatic"}: ${st.last_error ? `<span class="problem">${esc(st.last_error)}</span>` : esc(mailboxResultText(st.last_result))}`
    : "No cleanup has run since the service started.";
  const fields = mailboxFields(p.name, p.settings);
  const [enabled, , , , , targets] = fields;
  return `<form class="block mbox" data-provider="${esc(p.name)}" data-settings novalidate>
    <h3>${esc(p.name)}</h3>
    <div class="mbox-status"><span class="lamp ${lamp}"></span>
      <div class="grow"><div class="title">${headline}</div><div class="sub">${detail}</div></div>
      <button class="btn${would.n && !blocked ? " primary" : ""}" type="button" data-clean ${blocked ? "disabled" : ""}>Clean up now</button>
    </div>
    ${mailboxNotes[p.name] ? `<p class="hint mbox-note" role="status">${esc(mailboxNotes[p.name])}</p>` : ""}
    <div class="rows mbox-folders">
      ${mailboxFolder(p, "processed", names.processed, folders[names.processed])}
      ${mailboxFolder(p, "quarantine", names.quarantine, folders[names.quarantine])}
    </div>
    <p class="hint">Deleting is permanent. 0 switches the rule off. Over a size limit, the oldest mail goes first.</p>
    ${p.alert_warning ? `<p class="problem">${esc(p.alert_warning)}</p>` : ""}
    <div class="mbox-settings">${fieldHtml(enabled)}${fieldHtml(targets)}</div>
    <p class="hint">${last}</p>
    <div class="form-error" role="alert"></div>
    <div class="form-actions"><span class="hint mbox-dirty" hidden>Unsaved changes. The numbers above use the saved limits.</span>
      <button class="btn primary" type="submit">Save cleanup settings</button></div>
  </form>`;
}

// One line per reply mailbox on the Providers page; the server caches the folder reading for 30 s.
async function loadMailboxSummaries(main) {
  let data;
  try { data = await api("GET", "/mailbox"); } catch { data = { providers: [] }; }
  main.querySelectorAll("[data-mbox-sum]").forEach((el) => {
    const p = data.providers.find((x) => x.name === el.dataset.mboxSum);
    const pv = p?.preview;
    if (!pv || pv.error) { el.textContent = "Reply mailbox: could not be read. Open it for details."; return; }
    if (pv.refused) { el.textContent = "Reply mailbox: cleanup can't run. Open it for details."; return; }
    const fs = Object.values(pv.folders || {});
    const n = fs.reduce((a, f) => a + (Number(f.messages) || 0), 0), b = fs.reduce((a, f) => a + f.bytes, 0);
    const past = fs.reduce((a, f) => a + f.removed_age + f.removed_cap, 0);
    el.textContent = `Reply mailbox: ${plural(n, "mail", "mails")} (${mib(b)}), ${past ? `${plural(past, "mail", "mails")} past the limits` : "nothing past the limits"}.`;
  });
}

async function loadMailbox(el) {
  let data;
  el.innerHTML = `<div class="empty">Reading the mailbox…</div>`;
  try { data = await api("GET", "/mailbox"); }
  catch (ex) { el.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  if (!data.providers.length) { el.innerHTML = `<div class="empty">No provider has a reply mailbox yet. Add the IMAP settings to an email provider first.</div>`; return; }
  el.innerHTML = data.providers.map(mailboxPanel).join("");
  data.providers.forEach((p) => {
    const form = el.querySelector(`[data-provider="${CSS.escape(p.name)}"]`);
    const fields = mailboxFields(p.name, p.settings);
    const dirty = form.querySelector(".mbox-dirty");
    form.addEventListener("input", () => { dirty.hidden = false; });
    form.onsubmit = async (e) => {
      e.preventDefault();
      const err = form.querySelector(".form-error");
      err.textContent = "";
      const raw = readForm(form, fields);
      const v = Object.fromEntries(Object.entries(raw).map(([k, x]) => [k.slice(p.name.length + 2), x]));
      try { state = await api("PUT", `/mailbox/${encodeURIComponent(p.name)}`, v); toast("Cleanup settings saved"); loadMailbox(el); }
      catch (ex) { err.textContent = ex.message; }
    };
    const would = Object.values(p.preview.folders || {}).reduce((a, f) => ({ n: a.n + f.removed_age + f.removed_cap, b: a.b + f.removed_bytes }), { n: 0, b: 0 });
    form.querySelector("[data-clean]").onclick = () => confirmAction(
      "Clean up the reply mailbox?",
      would.n ? `This permanently deletes about <strong>${esc(would.n)} mails (${esc(mib(would.b))})</strong> that are past the saved limits. It cannot be undone.`
              : "Nothing is past the saved limits, so nothing will be deleted.",
      "Clean up now",
      async () => {
        const r = await api("POST", `/mailbox/${encodeURIComponent(p.name)}/cleanup`);
        mailboxNotes[p.name] = mailboxResultText(r);
        toast("Cleanup finished");
        loadMailbox(el);
      });
  });
}

boot().catch((ex) => { root.innerHTML = `<main class="gate"><div class="panel"><p class="problem">${esc(ex.message)}</p></div></main>`; });


// ---------------------------------------------------------------- chats

const chats = { folders: [], selected: null, currentId: null, lastId: null, timer: null, composeDraft: "", replyTo: null, msgs: {} };
// The Chats page is exactly as tall as what the viewer can see. visualViewport shrinks when a phone
// keyboard opens (100dvh does not on iOS), so the composer stays in view without the page scrolling.
if (window.visualViewport) {
  const fit = () => {
    if (window.visualViewport.scale > 1.01) return; // pinch zoom: keep the layout as it was
    document.documentElement.style.setProperty("--app-h", `${Math.round(window.visualViewport.height)}px`);
    if (document.querySelector(".chats")) window.scrollTo(0, 0);
  };
  window.visualViewport.addEventListener("resize", fit);
  fit();
}
const chatUnread = () => chats.folders.reduce((n, g) => n + (g.unread || 0), 0);
const CHAT_WHO = { claude: "Claude", codex: "Codex", owner: "You" };
const CHAT_ACTIVE_MS = 10 * 60 * 1000; // the owner counts as watching this long after the last input
let chatLastInput = Date.now();
["mousemove", "keydown", "scroll", "touchstart", "click"].forEach((t) =>
  document.addEventListener(t, () => { chatLastInput = Date.now(); }, { passive: true, capture: true }));
const chatWatchingNow = () => document.visibilityState === "visible" && Date.now() - chatLastInput < CHAT_ACTIVE_MS;
const CHAT_LIVE_SECONDS = 180; // matches the server's stale sweep

// one thread per host and folder; the key is what the list rows and the selection carry
const chatKey = (g) => JSON.stringify([g.host, g.folder]);
const chatFolderOf = (key) => chats.folders.find((f) => chatKey(f) === key);

function chatPresent(g) {
  return g.state === "open" && g.members.some((m) => !m.left_at);
}

// Mail the poller moved but could not expunge (the server has no UIDPLUS) stays flagged \Deleted in the folder.
function deletedLeftText(s) {
  const n = s.deleted_left || 0;
  if (!n) return "";
  const text = `${n} ${n === 1 ? "message" : "messages"} left flagged deleted in the reply folder (the server cannot expunge single messages); remove them with a mail client`;
  return ` · <span class="${n >= 500 ? "problem" : "hint"}">${esc(text)}</span>`;
}

function chatMembersText(g) {
  const here = g.members.filter((m) => !m.left_at).map(chatMemberLabel);
  if (g.state === "closed") return `Closed ${when(g.closed_at)}`;
  return here.length ? `Here now: ${here.join(", ")}` : "Nobody here";
}

// ---- members of the open thread: status dot, name, kind, what it said it would do, claims held
const CHAT_STATUS = {
  busy: ["busy", "busy"],
  "busy?": ["busyq", "busy?"],
  waiting: ["waiting", "waiting for you"],
  free: ["free", "free"],
  unknown: ["unknown", "no status yet"],
};
function chatStatusOf(m) {
  const [cls, text] = CHAT_STATUS[m.status] || CHAT_STATUS.unknown;
  const quiet = m.status === "busy?" && m.quiet_minutes ? ` (quiet ${m.quiet_minutes} min)` : "";
  return { cls, text: text + quiet };
}
// members still in the chat; one who left only shows as its "left the chat" line in the thread
const chatMembersShown = (g) => g.members.filter((m) => !m.left_at);
function chatMembersHtml(g) {
  if (g.state !== "open") return "";
  const rows = chatMembersShown(g).map((m) => {
    const st = chatStatusOf(m);
    const kind = CHAT_WHO[m.kind] || m.kind;
    const name = m.name || kind;
    const sub = [m.status_since ? `${esc(st.text)} since ${esc(when(m.status_since))}` : esc(st.text),
      m.intent ? `<span class="intent" title="${esc(m.intent)}">${esc(m.intent)}</span>` : ""].filter(Boolean).join(" · ");
    const claims = m.claims ? `<button type="button" class="btn quiet cmem-claims" data-claim="${esc(m.oldest_claim)}" title="Show the oldest">${m.claims} claim${m.claims === 1 ? "" : "s"}</button>` : "";
    const rename = `<button type="button" class="btn quiet cmem-rename" data-rename="${esc(m.id)}" data-name="${esc(name)}" aria-label="Rename ${esc(name)}" title="Rename">✎</button>`;
    return `<li class="cmem"><button type="button" class="cmem-main" data-mention="${esc(m.name || "")}" title="${m.name ? `Mention ${esc(name)} in your message` : ""}">
      <span class="sdot ${esc(st.cls)}" role="img" aria-label="${esc(st.text)}"></span><span class="nm">${esc(name)}</span> <small>${esc(kind)}</small>
      <span class="sub">${sub}</span></button>${claims}${rename}</li>`;
  }).join("");
  const others = (g.presences || []).map((p) => `<li class="cmem away"><span class="sdot unknown" aria-hidden="true"></span><span class="nm">${esc(CHAT_WHO[p.kind] || p.kind)}</span> <small>not in the chat</small></li>`).join("");
  return `<div class="chat-members" aria-label="Members"><ul>${rows || `<li class="cmem empty-row">Nobody here</li>`}</ul>${others ? `<div class="also">Also here</div><ul>${others}</ul>` : ""}</div>`;
}
// the same members folded into the thread header on a narrow screen: dots and names
function chatMembersMini(g) {
  if (g.state !== "open") return "";
  return g.members.filter((m) => !m.left_at).map((m) => {
    const st = chatStatusOf(m);
    return `<span class="mini"><span class="sdot ${esc(st.cls)}" role="img" aria-label="${esc(st.text)}"></span>${esc(m.name || CHAT_WHO[m.kind] || m.kind)}</span>`;
  }).join("");
}

function chatGroupRow(g) {
  const lm = g.last_message;
  const preview = lm ? `${CHAT_WHO[lm.author] || "Notice"}: ${lm.text_preview ?? ""}` : "No messages yet";
  return `<button type="button" class="chat-row${chatKey(g) === chats.selected ? " on" : ""}" data-key="${esc(chatKey(g))}">
    <span class="t"><span class="name"><span class="dot${chatPresent(g) ? "" : " off"}" aria-hidden="true"></span>${esc(g.folder)}</span>
      ${g.unread ? `<span class="badge" aria-label="${g.unread} unread">${g.unread}</span>` : `<small>${esc(lm ? when(lm.created_at) : "")}</small>`}</span>
    <span class="p">${esc(g.host)} · ${g.state === "open" ? (g.adhoc ? "open on demand" : "open") : "closed"}${g.paused ? ` · <span class="paused-tag">paused</span>` : ""}${g.pending_history_requests ? ` · <span class="paused-tag">history request</span>` : ""}</span>
    <span class="p">${esc(preview)}</span></button>`;
}

const CHAT_EXCERPT = 120;
const chatExcerpt = (t) => {
  const x = String(t ?? "").replace(/\s+/g, " ").trim();
  return x.length > CHAT_EXCERPT ? `${x.slice(0, CHAT_EXCERPT)}…` : x;
};
const chatMemberLabel = (m) => m.name ? `${m.name} (${CHAT_WHO[m.kind] || m.kind})` : (CHAT_WHO[m.kind] || m.kind);
const chatWhoLabel = (author, memberId, name) => author === "owner" ? "you"
  : name ? `${name} (${CHAT_WHO[author] || author})` : `${CHAT_WHO[author] || author} ${memberId || ""}`.trim();
// the author line of a message: the member's name with its kind small, "You" for the owner
const chatAuthorHtml = (author, name) => author === "owner" || !name ? esc(CHAT_WHO[author] || author)
  : `${esc(name)} <span class="kind">${esc(CHAT_WHO[author] || author)}</span>`;

// one dot on the owner's own messages: orange until an agent has read or claimed it, then green
// ("read" = it reached the agent's session and the agent made a group call since; hnl-notify cannot see more)
function chatTicksHtml(id, r, author) {
  if (author !== "owner" || !r || r.state === "none") return `<span class="ticks" data-ticks="${esc(id)}"></span>`;
  const done = r.claimed || r.read > 0;
  const label = r.claimed ? "Claimed by an agent" : r.read > 0 ? `Read by ${r.read} of ${r.recipients}` : "Not read or claimed yet";
  return `<span class="ticks ${done ? "seen" : "unseen"}" data-ticks="${esc(id)}" role="img" aria-label="${esc(label)}" title="${esc(label)}"><i></i></span>`;
}

function chatMessageHtml(m, canReply = false) {
  if (m.author === "system") {
    const hr = m.history_request;
    const ask = hr && hr.state === "pending"
      ? `<span class="req-reason">Reason: ${esc(hr.reason ?? "hidden")}</span><span class="req-actions"><button type="button" class="btn primary" data-hreq="${esc(hr.id)}" data-act="approve">Approve</button> <button type="button" class="btn" data-hreq="${esc(hr.id)}" data-act="deny">Deny</button></span>`
      : "";
    return `<div class="cm sys${ask ? " ask" : ""}" data-id="${esc(m.id)}"${ask ? ` data-req="${esc(hr.id)}"` : ""}>${esc(when(m.created_at))} · ${esc(m.text ?? "notice")}${ask}</div>`;
  }
  chats.msgs[m.id] = { author: m.author, member_id: m.member_id, name: m.member_name, text: m.text };
  const quote = m.in_reply_to
    ? `<div class="quote" data-quote="${esc(m.in_reply_to)}">${m.reply_excerpt == null ? `Reply to ${esc(m.in_reply_to)}` : `<b>${chatAuthorHtml(m.reply_author, m.reply_member_name)}</b> ${esc(m.reply_excerpt)}`}</div>`
    : "";
  const tag = m.kind === "fyi" ? `<span class="kind-tag fyi" title="Everyone reads it, nobody acts on it">heads-up</span>`
    : m.kind === "all" ? `<span class="kind-tag all" title="Each member acts on it">for everyone</span>`
    : m.mentions && m.mentions.length ? `<span class="kind-tag for" title="Only they act; the others read it">for ${esc(m.mentions.join(", "))}</span>` : "";
  const w = m.wakes || {};
  const notes = [];
  if (w.skipped) notes.push(`not delivered to ${w.skipped}`);
  return `<div class="cm ${esc(m.author)}" data-id="${esc(m.id)}"><div class="who">${chatAuthorHtml(m.author, m.member_name)} <small>${esc(when(m.created_at))}</small>${tag}${chatTicksHtml(m.id, m.receipt, m.author)}</div>
    ${quote}<div class="body">${m.text == null ? `<em class="hint">hidden</em>` : esc(m.text)}</div>
    ${notes.length ? `<div class="act">${esc(notes.join(" · "))}</div>` : ""}${m.claimed_by ? `<div class="act claimed">claimed by ${esc(m.claimed_by)}${canReply ? ` <button type="button" class="btn quiet release-btn" data-release="${esc(m.id)}">release</button>` : ""}</div>` : ""}${canReply && m.text != null ? `<button type="button" class="btn quiet reply-btn" data-reply="${esc(m.id)}">Reply</button>` : ""}</div>`;
}

// an earlier session of the folder: one bar (expandable), or one small line when nobody said anything
function chatEarlierHtml(g) {
  const span = `${when(g.opened_at)}–${when(g.closed_at)}`;
  if (!g.message_count) return `<div class="cm sys chat-earlier-empty" data-earlier="${esc(g.id)}">Earlier session ${esc(span)} · opened and closed, no messages</div>`;
  const n = `${g.message_count} message${g.message_count === 1 ? "" : "s"}`;
  return `<div class="chat-earlier" data-earlier="${esc(g.id)}">
    <button type="button" class="chat-earlier-bar" aria-expanded="false">Earlier session ${esc(span)} · ${esc(n)} · agents can read it only on request, as history</button>
    <div class="chat-earlier-body" hidden></div></div>`;
}

async function chatsPage(main) {
  clearInterval(chats.timer);
  main.innerHTML = `
    <div class="section-head"><h2>Chats</h2><p>Agent sessions in the same folder talk here, one thread per folder. Read along, and post into an open group to steer it.</p></div>
    <div class="chats${chats.selected ? " thread" : ""}" id="chats">
      <div class="chat-list" id="chat-list"><div class="empty">Loading…</div></div>
      <div class="chat-pane" id="chat-pane"></div>
    </div>`;
  const el = main.querySelector("#chats");
  try { await chatsLoadList(el); } catch (ex) { el.querySelector("#chat-list").innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  if (chats.foldersOpen) await chatFolders(el);
  else if (chats.selected && chatFolderOf(chats.selected)) await chatOpen(el, chats.selected, true);
  else { chats.selected = null; el.classList.remove("thread"); chatPaneEmpty(el); }
  chats.timer = setInterval(() => chatTick(el), 5000);
}

function chatPaneEmpty(el) {
  el.querySelector("#chat-pane").innerHTML = `<div class="empty">${chats.folders.length ? "Pick a folder on the left." : "No group chats yet. Switch chats on for a folder under Folders, or an agent opens one when it helps."}<br><span class="hint">Agent sessions in the same folder talk here, one thread per folder. Read along, and post into an open group to steer it.</span></div>`;
}

async function chatsLoadList(el) {
  const data = await api("GET", "/chats");
  chats.folders = data.folders;
  chatsRenderList(el);
}

function chatsRenderList(el) {
  const list = el.querySelector("#chat-list");
  const top = `<div class="chat-list-top"><button type="button" class="btn quiet${chats.foldersOpen ? " on" : ""}" id="chat-folders-btn">Folders · chats on or off</button></div>`;
  list.innerHTML = top + (chats.folders.map((f) => chatGroupRow(f) + (chatKey(f) === chats.selected ? chatMembersHtml(f) : "")).join("")
    || `<div class="empty">No chats yet.</div>`);
  list.querySelectorAll("[data-key]").forEach((b) => (b.onclick = () => chatOpen(el, b.dataset.key)));
  list.querySelectorAll("[data-mention]").forEach((b) => (b.onclick = () => chatMention(el, b.dataset.mention)));
  list.querySelectorAll("[data-claim]").forEach((b) => (b.onclick = () => chatShowMessage(el, b.dataset.claim)));
  list.querySelectorAll("[data-rename]").forEach((b) => (b.onclick = () => chatRename(el, b.dataset.rename, b.dataset.name)));
  const fb = list.querySelector("#chat-folders-btn");
  if (fb) fb.onclick = () => chatFolders(el);
  const nav = document.querySelector('[data-nav="chats"]');
  if (nav) nav.querySelector(".badge")?.remove();
  const n = chatUnread();
  if (nav && n) nav.insertAdjacentHTML("beforeend", ` <span class="badge" aria-label="${n} unread">${n}</span>`);
}

// "@" in the composer: a list of the members' names (and all) to pick from, filtered as you type
function chatAtPicker(el, ta, list) {
  let hits = [], pick = 0, start = -1;
  const close = () => { list.hidden = true; hits = []; start = -1; ta.removeAttribute("aria-activedescendant"); };
  const names = () => {
    const f = chatFolderOf(chats.selected);
    const here = f ? f.members.filter((m) => !m.left_at && m.name) : [];
    return [...here.map((m) => ({ name: m.name, kind: CHAT_WHO[m.kind] || m.kind })), { name: "all", kind: "everyone acts" }];
  };
  const draw = () => {
    list.innerHTML = hits.map((h, i) => `<li role="option" id="chat-at-${i}" data-i="${i}" aria-selected="${i === pick}" class="${i === pick ? "on" : ""}">@${esc(h.name)} <small>${esc(h.kind)}</small></li>`).join("");
    list.hidden = !hits.length;
    if (hits.length) ta.setAttribute("aria-activedescendant", `chat-at-${pick}`);
  };
  const choose = (i) => {
    const h = hits[i];
    if (!h) return;
    const end = ta.selectionStart;
    ta.value = `${ta.value.slice(0, start)}@${h.name} ${ta.value.slice(end)}`;
    const pos = start + h.name.length + 2;
    close();
    ta.focus();
    ta.setSelectionRange(pos, pos);
    ta.dispatchEvent(new Event("input"));
  };
  const update = () => {
    const before = ta.value.slice(0, ta.selectionStart ?? 0);
    const m = /(^|[\s(\[{<"'])@([A-Za-z0-9]*)$/.exec(before);
    if (!m) { close(); return; }
    start = before.length - m[2].length - 1;
    const q = m[2].toLowerCase();
    hits = names().filter((h) => h.name.toLowerCase().startsWith(q)).slice(0, 8);
    pick = Math.min(pick, Math.max(0, hits.length - 1));
    draw();
  };
  ta.addEventListener("input", update);
  ta.addEventListener("click", update);
  ta.addEventListener("blur", () => setTimeout(close, 150));
  list.onmousedown = (e) => { const li = e.target.closest("[data-i]"); if (li) { e.preventDefault(); choose(+li.dataset.i); } };
  return {
    key(e) {
      if (list.hidden || !hits.length) return false;
      if (e.key === "ArrowDown" || e.key === "ArrowUp") { e.preventDefault(); pick = (pick + (e.key === "ArrowDown" ? 1 : hits.length - 1)) % hits.length; draw(); return true; }
      if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); choose(pick); return true; }
      if (e.key === "Escape") { e.preventDefault(); close(); return true; }
      return false;
    },
  };
}

function chatRename(el, id, name) {
  openForm({
    title: `Rename ${name}`,
    intro: "The other members see a line in the chat. Names are unique in the group.",
    fields: [{ name: "name", label: "Name", kind: "text", value: name, help: "One word, 2–20 letters or digits, starting with a letter." }],
    submit: "Rename",
    onSubmit: async (v) => { await api("PUT", `/chats/members/${encodeURIComponent(id)}`, { name: v.name }); await chatTick(el); },
  });
}

// ---- Folders: where chats are on (sessions join when they start) and where a chat can be opened now
async function chatFolders(el) {
  chats.selected = null;
  chats.currentId = null;
  chats.foldersOpen = true;
  el.classList.add("thread");
  chatsRenderList(el);
  const pane = el.querySelector("#chat-pane");
  pane.innerHTML = `<div class="empty">Loading…</div>`;
  let data;
  try { data = await api("GET", "/chats/folders"); } catch (ex) { pane.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  if (!chats.foldersOpen) return;
  const row = (f) => {
    const bits = [f.host];
    if (f.present) bits.push(`${f.present} session${f.present === 1 ? "" : "s"} here, not in a chat`);
    if (f.open_group_id) bits.push(`chat open${f.adhoc ? " (opened on demand)" : ""} · ${f.members} member${f.members === 1 ? "" : "s"}`);
    const key = esc(JSON.stringify([f.host, f.folder]));
    return `<li class="cf-row"><div class="cf-name"><span class="mono">${esc(f.folder)}</span><small>${esc(bits.join(" · "))}</small></div>
      <label class="fyi-toggle cf-on" title="Agent sessions that start here join the folder's chat"><input type="checkbox" data-fen="${key}" ${f.enabled ? "checked" : ""}> Chats on</label>
      ${f.open_group_id ? `<button type="button" class="btn quiet" data-fshow="${key}">Show chat</button>`
        : `<button type="button" class="btn" data-fopen="${key}" title="Open a chat now and invite the sessions here">Open chat</button>`}
      ${f.editable || f.deletable ? `<span class="cf-edit">${f.editable ? `<button type="button" class="btn quiet" data-fedit="${key}">Edit</button>` : ""}<button type="button" class="btn quiet danger" data-fdel="${key}" data-chats="${f.closed_chats}" data-msgs="${f.closed_messages}">Delete</button></span>` : `<span class="cf-edit" aria-hidden="true"></span>`}</li>`;
  };
  pane.innerHTML = `
    <div class="chat-head"><button type="button" class="btn quiet back" id="chat-back" aria-label="All folders">‹ Chats</button>
      <div class="grow"><b>Folders</b><span class="hint">Chats on: agent sessions that start in the folder join its chat and report in. Off: they stay out, and an agent (or you) can still open a chat there when it helps.</span></div>
      <button type="button" class="btn" id="cf-add">Add folder…</button></div>
    <div class="chat-folders">${data.folders.length ? `<ul>${data.folders.map(row).join("")}</ul>` : `<div class="empty">No folders yet. They appear when an agent session starts, or add one.</div>`}</div>`;
  const back = () => { chats.foldersOpen = false; el.classList.remove("thread"); chatsRenderList(el); chatPaneEmpty(el); };
  pane.querySelector("#chat-back").onclick = back;
  const parse = (k) => { const [host, folder] = JSON.parse(k); return { host, folder }; };
  pane.querySelectorAll("[data-fen]").forEach((cb) => (cb.onchange = async () => {
    cb.disabled = true;
    try { await api("PUT", "/chats/folders", { ...parse(cb.dataset.fen), enabled: cb.checked }); toast(cb.checked ? "Chats on: new sessions there join" : "Chats off: an open chat stays until it empties"); }
    catch (ex) { cb.checked = !cb.checked; toast(ex.message); }
    finally { cb.disabled = false; }
  }));
  pane.querySelectorAll("[data-fopen]").forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    try {
      const r = await api("POST", "/chats/folders/open", parse(b.dataset.fopen));
      toast(r.invited ? `Chat opened; ${r.invited} session${r.invited === 1 ? "" : "s"} invited` : "Chat opened; no session is here to invite yet");
      await chatsLoadList(el);
      await chatFolders(el);
    } catch (ex) { toast(ex.message); b.disabled = false; }
  }));
  pane.querySelectorAll("[data-fshow]").forEach((b) => (b.onclick = async () => {
    chats.foldersOpen = false;
    await chatsLoadList(el);
    const k = JSON.stringify(JSON.parse(b.dataset.fshow));
    if (chatFolderOf(k)) chatOpen(el, k); else back();
  }));
  pane.querySelectorAll("[data-fedit]").forEach((b) => (b.onclick = () => {
    const f = parse(b.dataset.fedit);
    openForm({
      title: "Edit folder",
      intro: "Correct the host or path. Chats stay on or off as they were.",
      fields: [
        { name: "host", label: "Host", kind: "select", value: f.host, options: data.hosts.map((h) => [h, h]) },
        { name: "folder", label: "Folder", kind: "text", value: f.folder, help: "Absolute path of the git checkout (or folder) the agents work in." },
      ],
      submit: "Save",
      onSubmit: async (v) => { await api("POST", "/chats/folders/move", { ...f, new_host: v.host, new_folder: v.folder }); await chatFolders(el); },
    });
  }));
  pane.querySelectorAll("[data-fdel]").forEach((b) => (b.onclick = () => {
    const f = parse(b.dataset.fdel);
    const n = +b.dataset.chats || 0, m = +b.dataset.msgs || 0;
    const what = n ? ` This also deletes its ${n} earlier chat${n === 1 ? "" : "s"} (${m} message${m === 1 ? "" : "s"}) now, instead of after the retention period. This cannot be undone.` : "";
    confirmAction("Delete folder", `Remove <span class="mono">${esc(f.folder)}</span> on ${esc(f.host)} from the list? Sessions that start there stay out of chats.${what}`, n ? "Delete with history" : "Delete",
      async () => { await api("POST", "/chats/folders/delete", { ...f, with_history: n > 0 }); await chatsLoadList(el); await chatFolders(el); });
  }));
  pane.querySelector("#cf-add").onclick = () => openForm({
    title: "Add a folder",
    intro: "Switch chats on for a folder before any agent has started there.",
    fields: [
      { name: "host", label: "Host", kind: "select", value: data.hosts[0] || "", options: data.hosts.map((h) => [h, h]), help: "The chat host of the API client the agents there use." },
      { name: "folder", label: "Folder", kind: "text", value: "", placeholder: "/home/alex/project", help: "Absolute path of the git checkout (or folder) the agents work in." },
    ],
    submit: "Switch chats on",
    onSubmit: async (v) => { await api("PUT", "/chats/folders", { host: v.host, folder: v.folder, enabled: true }); await chatFolders(el); },
  });
}

// a click on a member: "@Name " at the cursor in the composer
function chatMention(el, name) {
  const ta = el.querySelector("#chat-text");
  if (!name || !ta || el.querySelector("#chat-compose")?.hidden) return;
  const at = ta.selectionStart ?? ta.value.length;
  const before = ta.value.slice(0, at);
  const pad = before && !/\s$/.test(before) ? " " : "";
  ta.value = `${before}${pad}@${name} ${ta.value.slice(ta.selectionEnd ?? at)}`;
  const pos = before.length + pad.length + name.length + 2;
  ta.focus();
  ta.setSelectionRange(pos, pos);
  ta.dispatchEvent(new Event("input"));
}

function chatShowMessage(el, id) {
  const m = id && el.querySelector(`#chat-msgs > [data-id="${CSS.escape(id)}"]`);
  if (!m) return;
  m.scrollIntoView({ block: "center", behavior: "smooth" });
  m.classList.remove("flash");
  void m.offsetWidth;
  m.classList.add("flash");
}

async function chatOpen(el, key, keep = false) {
  const f = chatFolderOf(key);
  if (!f) return;
  chats.foldersOpen = false;
  chats.selected = key;
  chats.lastId = null;
  chats.currentId = null;
  chats.replyTo = null;
  chats.msgs = {};
  el.classList.add("thread");
  chatsRenderList(el);
  const pane = el.querySelector("#chat-pane");
  pane.innerHTML = `<div class="empty">Loading…</div>`;
  let data, info;
  try {
    info = await api("GET", `/chats/folder?host=${encodeURIComponent(f.host)}&folder=${encodeURIComponent(f.folder)}`);
    const id = info.groups[info.groups.length - 1].id;
    data = await api("GET", `/chats/${encodeURIComponent(id)}/messages`);
  } catch (ex) { pane.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  if (chats.selected !== key) return;
  const g = data.group;
  const id = g.id;
  chats.currentId = id;
  pane.innerHTML = `
    <div class="chat-head"><button type="button" class="btn quiet back" id="chat-back" aria-label="All folders">‹ Folders</button>
      <div class="grow"><b id="chat-title"></b><span class="hint" id="chat-sub"></span><span class="chat-mini" id="chat-mini"></span>
        <span class="hint chat-watch" id="chat-watch" hidden>You're watching: no auto-pause</span></div>
      <button type="button" class="btn quiet" id="chat-limitbtn" hidden title="How many messages each agent may post while this group is paused"></button>
      <button type="button" class="btn" id="chat-pausebtn" hidden></button></div>
    <div class="chat-paused" id="chat-paused" hidden></div>
    <div class="chat-msgs" id="chat-msgs" tabindex="0" aria-live="polite" aria-label="Messages"></div>
    <form class="chat-compose" id="chat-compose" hidden>
      <div class="chat-replying" id="chat-replying" hidden><span class="rq"></span><button type="button" class="btn quiet" id="chat-reply-cancel" aria-label="Cancel reply">✕</button></div>
      <label class="sr" for="chat-text">Message this group</label>
      <textarea id="chat-text" rows="2" placeholder="Message this group… @Name hands it to one member, @all to everyone" aria-autocomplete="list" aria-controls="chat-at"></textarea>
      <ul class="chat-at" id="chat-at" role="listbox" aria-label="Members to mention" hidden></ul>
      <label class="fyi-toggle" title="A heads-up: everyone reads it, nobody acts on it"><input type="checkbox" id="chat-fyi"> FYI</label>
      <button type="submit" class="btn primary">Send</button>
      <div class="form-error" role="alert"></div>
    </form>
    <div class="chat-readonly hint" id="chat-readonly" hidden>This group is closed. It is read-only.</div>`;
  pane.querySelector("#chat-pausebtn").onclick = async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try { await api("POST", `/chats/${encodeURIComponent(chats.currentId)}/${btn.dataset.act}`); await chatTick(el); }
    catch (ex) { toast(ex.message); }
    finally { btn.disabled = false; }
  };
  pane.querySelector("#chat-back").onclick = () => { chats.selected = null; el.classList.remove("thread"); chatsRenderList(el); chatPaneEmpty(el); };
  pane.querySelector("#chat-limitbtn").onclick = () => {
    const cur = chats.group || {};
    openForm({
      title: "Messages while paused",
      intro: `How many messages each agent may still post while this group is paused. Empty: the setting (${esc(String(cur.paused_posts_effective ?? 5))} now, unless overridden here).`,
      fields: [{ name: "paused_posts", label: "Per agent", kind: "int", value: cur.paused_posts ?? "", help: "0–50; 0: none. Beyond it an agent is told to wait for you." }],
      submit: "Save",
      onSubmit: async (v) => { await api("PUT", `/chats/${encodeURIComponent(chats.currentId)}/settings`, { paused_posts: v.paused_posts }); await chatTick(el); },
    });
  };
  const box = pane.querySelector("#chat-msgs");
  const earlier = info.groups.slice(0, -1); // oldest first, above the current group
  box.innerHTML = earlier.map(chatEarlierHtml).join("") + (data.messages.map((m) => chatMessageHtml(m, g.state === "open")).join("") || `<div class="empty">No messages yet.</div>`);
  const replying = pane.querySelector("#chat-replying");
  const setReply = (id) => {
    const m = id ? chats.msgs[id] : null;
    chats.replyTo = m ? id : null;
    replying.hidden = !m;
    replying.querySelector(".rq").textContent = m ? `Replying to ${chatWhoLabel(m.author, m.member_id, m.name)}: ${chatExcerpt(m.text)}` : "";
  };
  replying.querySelector("#chat-reply-cancel").onclick = () => { setReply(null); pane.querySelector("#chat-text").focus(); };
  box.onclick = async (e) => {
    const rel = e.target.closest("[data-release]");
    if (rel) {
      rel.disabled = true;
      try { await api("POST", `/chats/messages/${encodeURIComponent(rel.dataset.release)}/release`); await chatTick(el); }
      catch (ex) { toast(ex.message); rel.disabled = false; }
      return;
    }
    const rb = e.target.closest("[data-reply]");
    if (rb) { setReply(rb.dataset.reply); pane.querySelector("#chat-text").focus(); return; }
    const bar = e.target.closest(".chat-earlier-bar");
    if (bar) { chatToggleEarlier(bar); return; }
    const b = e.target.closest("[data-hreq]");
    if (!b) return;
    box.querySelectorAll(`[data-hreq="${CSS.escape(b.dataset.hreq)}"]`).forEach((x) => { x.disabled = true; });
    try { await api("POST", `/chats/history-requests/${encodeURIComponent(b.dataset.hreq)}/${b.dataset.act}`); await chatTick(el); }
    catch (ex) { toast(ex.message); await chatTick(el); }
  };
  chats.lastId = data.messages.length ? data.messages[data.messages.length - 1].id : null;
  chatHeader(el, g, f);
  box.scrollTop = box.scrollHeight;
  const form = pane.querySelector("#chat-compose");
  const ta = form.querySelector("textarea");
  ta.value = chats.composeDraft;
  const grow = () => { ta.style.height = "auto"; if (ta.value) ta.style.height = `${ta.scrollHeight + 2}px`; };
  ta.oninput = () => { chats.composeDraft = ta.value; grow(); };
  grow();
  const at = chatAtPicker(el, ta, form.querySelector("#chat-at"));
  const fyi = form.querySelector("#chat-fyi");
  ta.onkeydown = (e) => {
    if (at.key(e)) return;
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  };
  form.onsubmit = async (e) => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text) return;
    const err = form.querySelector(".form-error");
    const btn = form.querySelector("button[type=submit]");
    btn.disabled = true;
    err.textContent = "";
    try {
      const payload = chats.replyTo ? { text, in_reply_to: chats.replyTo } : { text };
      if (fyi.checked) payload.fyi = true;
      const res = await api("POST", `/chats/${encodeURIComponent(chats.currentId)}/messages`, payload);
      if (res && res.unknown_mentions && res.unknown_mentions.length) toast(`Nobody here is called ${res.unknown_mentions.map((n) => `@${n}`).join(", ")}: sent as plain text`);
      setReply(null);
      fyi.checked = false;
      ta.value = "";
      chats.composeDraft = "";
      grow();
      await chatTick(el);
    } catch (ex) { err.textContent = ex.message; }
    finally { btn.disabled = false; ta.focus(); }
  };
  if (g.unread) { try { await api("POST", `/chats/${encodeURIComponent(id)}/seen`); await chatsLoadList(el); } catch { /* the badge stays; harmless */ } }
}

async function chatToggleEarlier(bar) {
  const wrap = bar.closest(".chat-earlier");
  const body = wrap.querySelector(".chat-earlier-body");
  const open = bar.getAttribute("aria-expanded") !== "true";
  bar.setAttribute("aria-expanded", String(open));
  body.hidden = !open;
  if (!open || body.dataset.loaded) return;
  body.innerHTML = `<div class="empty">Loading…</div>`;
  try {
    const d = await api("GET", `/chats/${encodeURIComponent(wrap.dataset.earlier)}/messages`);
    body.innerHTML = d.messages.map(chatMessageHtml).join("") || `<div class="empty">No messages.</div>`;
    body.querySelectorAll("[data-hreq]").forEach((x) => x.remove()); // an old session cannot be decided any more
    body.dataset.loaded = "1";
  } catch (ex) { body.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; }
}

function chatHeader(el, g, f) {
  chats.group = g;
  const lb = el.querySelector("#chat-limitbtn");
  lb.hidden = g.state !== "open";
  lb.textContent = `While paused: ${g.paused_posts_effective ?? "?"} each${g.paused_posts == null ? "" : " (this chat)"}`;
  el.querySelector("#chat-title").textContent = `${g.folder}`;
  // open: the members are listed under the thread (or as dots in the header on a phone); closed: when it closed
  el.querySelector("#chat-sub").textContent = g.state === "open" ? ` ${g.host}` : ` ${g.host} · ${chatMembersText(g)}`;
  el.querySelector("#chat-mini").innerHTML = chatMembersMini(g);
  const p = el.querySelector("#chat-paused");
  p.hidden = !(g.paused && g.state === "open");
  p.textContent = `Paused. Each agent may post ${g.paused_posts_effective ?? "a few"} more message${g.paused_posts_effective === 1 ? "" : "s"}; post here or press Resume to let them continue.`;
  const pb = el.querySelector("#chat-pausebtn");
  pb.hidden = g.state !== "open";
  pb.dataset.act = g.paused ? "resume" : "pause";
  pb.textContent = g.paused ? "Resume" : "Pause now";
  if (g.paused) el.querySelector("#chat-watch").hidden = true;
  el.querySelector("#chat-compose").hidden = g.state !== "open";
  el.querySelector("#chat-readonly").hidden = g.state === "open";
}

async function chatTick(el) {
  if (section !== "chats" || !el.isConnected) { clearInterval(chats.timer); return; }
  if (document.visibilityState !== "visible") return;
  try {
    await chatsLoadList(el);
    const key = chats.selected;
    if (!key) return;
    const f = chatFolderOf(key);
    if (!f) return;
    if (f.id !== chats.currentId) { await chatOpen(el, key, true); return; } // a new session opened in this folder
    const id = chats.currentId;
    const watch = el.querySelector("#chat-watch");
    const open = f.state === "open";
    if (open && chatWatchingNow()) {
      try { await api("POST", `/chats/${encodeURIComponent(id)}/watching`); if (watch) watch.hidden = !!f.paused; }
      catch { if (watch) watch.hidden = true; }
    } else if (watch) watch.hidden = true;
    const path = `/chats/${encodeURIComponent(id)}/messages${chats.lastId ? `?after=${encodeURIComponent(chats.lastId)}` : ""}`;
    const data = await api("GET", path);
    if (chats.selected !== key || chats.currentId !== id) return;
    chatHeader(el, data.group, f);
    const box = el.querySelector("#chat-msgs");
    // a request decided elsewhere (phone link, expiry) loses its buttons
    box.querySelectorAll("[data-req]").forEach((n) => {
      if (!data.pending_history_requests.includes(n.dataset.req)) { n.querySelector(".req-actions")?.remove(); n.removeAttribute("data-req"); n.classList.remove("ask"); }
    });
    for (const [mid, r] of Object.entries(data.receipts || {})) {
      const t = box.querySelector(`:scope > [data-id="${CSS.escape(mid)}"] [data-ticks]`);
      if (t) t.outerHTML = chatTicksHtml(mid, r, chats.msgs[mid]?.author);
    }
    if (data.messages.length) {
      const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
      box.querySelector(":scope > .empty")?.remove();
      for (const m of data.messages) {
        const old = box.querySelector(`:scope > [data-id="${CSS.escape(m.id)}"]`);
        if (old) old.outerHTML = chatMessageHtml(m, data.group.state === "open"); else box.insertAdjacentHTML("beforeend", chatMessageHtml(m, data.group.state === "open"));
      }
      chats.lastId = data.messages[data.messages.length - 1].id;
      if (stick) box.scrollTop = box.scrollHeight;
      if (data.group.unread) { await api("POST", `/chats/${encodeURIComponent(id)}/seen`); await chatsLoadList(el); }
    }
  } catch { /* a missed poll is retried in 5 s; sign-out is handled by api() */ }
}
