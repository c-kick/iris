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
  ["providers", "Providers"],
  ["mailbox", "Reply mailbox"],
  ["devices", "Devices"],
  ["people", "People & groups"],
  ["channels", "Channels"],
  ["access", "API access"],
  ["requests", "Access requests"],
  ["activity", "Activity"],
  ["settings", "Settings"],
];

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

const KINDS = [["", "Default devices"], ["push", "Push"], ["email", "Email"], ["push,email", "Push and email"]];

const SCOPE_HELP = {
  send: "send notifications",
  read: "see its own history, targets and events",
  manage: "retry or cancel its own notifications",
  admin: "everything, for every client",
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
    throw new Error(msg);
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

const logo = `<svg width="26" height="26" viewBox="0 0 32 32" aria-hidden="true"><circle cx="16" cy="16" r="12" fill="var(--action)"/><circle cx="16" cy="16" r="5" fill="var(--surface)"/></svg>`;

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
    const type = f.kind === "secret" ? "password" : f.kind === "int" ? "number" : f.kind === "url" ? "url" : "text";
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
  const badge = (k) => (k === "requests" && state.pending_registrations ? ` <span class="badge" aria-label="${state.pending_registrations} waiting">${state.pending_registrations}</span>` : "");
  const hasReplies = Object.values(cfg().providers).some((p) => p.imap_host); // a provider with a reply mailbox
  const nav = SECTIONS.filter(([k]) => k !== "mailbox" || hasReplies || section === "mailbox").map(([k, l]) => `<button data-nav="${k}" ${k === section ? 'aria-current="page"' : ""}>${esc(l)}${badge(k)}</button>`).join("");
  root.innerHTML = `
    <div class="shell">
      <aside class="side">
        <div class="top"><div class="brand">${logo}<h1>hnl-notify</h1></div><button class="btn quiet signout" data-signout>Sign out</button></div>
        <nav class="nav" aria-label="Sections">${nav}</nav>
        <div class="foot"><span>Version ${esc(state.version)}</span><button class="btn quiet" style="align-self:flex-start;padding-inline:0" data-signout>Sign out</button></div>
      </aside>
      <main class="main" id="main"></main>
    </div>`;
  root.querySelectorAll("[data-nav]").forEach((b) => (b.onclick = () => {
    section = b.dataset.nav;
    sessionStorage.setItem("notify.section", section);
    history.replaceState(null, "", `#${section}`);
    render();
  }));
  root.querySelectorAll("[data-signout]").forEach((b) => (b.onclick = async () => { await api("POST", "/logout"); boot(); }));
  root.querySelector('.nav [aria-current="page"]')?.scrollIntoView({ block: "nearest", inline: "center" });
  const main = document.getElementById("main");
  VIEWS[section](main);
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
      <div class="section-head"><h2>Delivery path</h2><p>Where a notification goes after a service sends it.</p></div>
      <div class="path">
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
        <p>A provider carries notifications to devices. Pushover delivers to the Pushover app on your phone: normal messages are reported as sent once Pushover accepts them, and only critical messages report delivery and acknowledgement back. Email goes out through your mail server.</p></div>
      <div class="rows">${list.length ? list.map(([n, p]) => {
        const t = state.provider_types[p.type] || {};
        const h = state.providers[n];
        return `<div class="row"><span class="lamp ${h?.configured ? "ok" : "bad"}"></span>
          <div class="grow"><div class="title">${esc(n)}</div>
            <div class="sub">${esc(t.label || p.type)}, delivers ${t.kind === "email" ? "email" : "push notifications"}${h?.configured ? "" : ", needs attention"}</div>
            ${p.type === "pushover" && p.app_token?.set ? `<div class="sub">Application token ${esc(p.app_token.hint)}</div>` : ""}
            ${h?.quota ? `<div class="sub">${h.quota.remaining.toLocaleString()} of ${h.quota.limit.toLocaleString()} messages left this month</div>` : ""}
            ${h?.problems?.length ? `<div class="problem">${esc(h.problems.join("; "))}</div>` : ""}
            <div class="sub" data-result></div></div>
          <div class="actions">${TESTABLE.has(p.type) ? `<button class="btn" data-test="${esc(n)}">Test connection</button>` : ""}<button class="btn" data-edit="${esc(n)}">Edit</button><button class="btn danger" data-del="${esc(n)}">Remove</button></div></div>`;
      }).join("") : `<div class="empty">No providers yet. Connect Pushover to reach phones, or add your mail server to send email.</div>`}</div>
      <p style="margin-top:1rem"><button class="btn primary" id="add">Add provider</button></p>`;
    main.querySelector("#add").onclick = () => addProviderForm();
    main.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => providerForm(b.dataset.edit)));
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
      <div class="section-head"><h2>Reply mailbox</h2>
        <p>hnl-notify moves every handled reply to one folder and every rejected mail to another, and keeps them there until it cleans up. Old mail is deleted for good, about once a day, by age first and by folder size as a safety net. Your inbox is never touched.</p></div>
      <div id="mb"><div class="empty">Reading the mailbox…</div></div>`;
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
            <div class="sub">device:${esc(n)} via ${esc(d.provider)}${info.opt_in ? `, only when an app asks for ${esc(info.kind)}` : ""}${d.owner ? `, belongs to ${esc(c.users[d.owner]?.display_name || d.owner)}` : ""}${d.enabled ? "" : ", turned off"}${d.min_severity !== "min" ? `, only ${esc(d.min_severity)} and above` : ""}</div>
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
          <div class="chips">${cl.scopes.map((s) => `<span class="chip" title="${esc(SCOPE_HELP[s])}">${esc(s)}</span>`).join("")}</div></div>
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
    main.querySelectorAll("[data-cl]").forEach((b) => (b.onclick = () => clientForm(b.dataset.cl)));
    main.querySelectorAll("[data-rot]").forEach((b) => (b.onclick = () => confirmAction("Replace token?",
      `The current token for <strong>${esc(b.dataset.rot)}</strong> stops working immediately.`, "Replace token", async () => {
        const res = await mutate("POST", `/clients/${b.dataset.rot}/rotate`);
        showToken("New token", b.dataset.rot, res.token);
      })));
    main.querySelectorAll("[data-delcl]").forEach((b) => (b.onclick = () => confirmAction("Revoke token?",
      `<strong>${esc(b.dataset.delcl)}</strong> can no longer send or read notifications.`, "Revoke",
      () => mutate("DELETE", `/clients/${b.dataset.delcl}`, undefined, "Token revoked"))));
  },

  activity(main) {
    main.innerHTML = `
      <div class="section-head"><h2>Activity</h2><p>The latest notifications from every service, with what each device reported back.</p></div>
      <div class="block" id="attention"></div>
      <div class="block" id="replies"></div>
      <div class="rows" id="recent"><div class="empty">Loading…</div></div>`;
    loadRecent(main.querySelector("#recent"), 50, main.querySelector("#attention"));
    loadReplies(main.querySelector("#replies"));
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
                                { name: "attachment_retention_days", kind: "int" }, { name: "attachment_url_hosts", kind: "textarea" }]);
      v.attachment_url_hosts = v.attachment_url_hosts.split("\n").map((x) => x.trim()).filter(Boolean);
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

function providerForm(name, newType) {
  const p = cfg().providers[name];
  const type = p?.type || newType;
  const t = state.provider_types[type];
  const pushover = type === "pushover";
  openForm({
    title: p ? `Edit ${name === type ? t.label : name}` : pushover ? "Connect Pushover" : `Add ${t.label}`,
    intro: pushover ? `Create an application at <a href="https://pushover.net/apps/build" target="_blank" rel="noopener">pushover.net/apps/build</a> (name it hnl-notify), then paste its API token.` : "",
    fields: Object.entries(t.settings).map(([k, s]) => ({ name: k, label: s.label, kind: s.kind, help: s.help, options: s.options,
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
      list: k === "device", button: k === "device" && CHECKABLE.has(ptype) ? { action: "check", label: "Check address" } : null,
    })),
    { name: "min_severity", label: "Receive from urgency", kind: "select", value: pick("min_severity", d?.min_severity || "min"),
      options: state.severities.map((s) => [s, `${s}: ${SEVERITY_HELP[s]}`]) },
    { name: "enabled", label: "Deliver to this device", kind: "check", value: pick("enabled", d ? d.enabled : true) },
    { name: "opt_in", label: "Only when an app asks for this kind explicitly", kind: "check", value: optIn,
      help: "Email devices are opt-in by default so existing apps keep sending push only." },
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
        enabled: v.enabled, min_severity: v.min_severity, opt_in: v.opt_in, address }, d ? "Device saved" : "Device added");
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
    ],
    submit: cl ? "Save" : "Create token",
    onSubmit: async (v) => {
      if (!v.scopes.length) throw new Error("Choose at least one permission.");
      if (cl) return mutate("PUT", `/clients/${v.name}`, { description: v.description, scopes: v.scopes }, "Token saved");
      const res = await mutate("POST", `/clients/${v.name}`, { description: v.description, scopes: v.scopes });
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

// ---------------------------------------------------------------- activity list

async function loadRecent(el, limit, attentionEl) {
  let data;
  try { data = await api("GET", `/activity?limit=${limit}`); }
  catch (ex) { el.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  if (attentionEl) {
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
    attentionEl.innerHTML = items.length || a.awaiting_acknowledgement.length ? `
      <h3>Needs attention</h3><div class="rows">
      ${a.awaiting_acknowledgement.map((x) => `<div class="row"><span class="lamp warn"></span><div class="grow"><div class="title">${esc(x.title || "(no title)")}</div><div class="sub">Critical alert on ${esc(x.device)} not acknowledged yet</div></div></div>`).join("")}
      ${items.map(({ kind, x, devices, at }) => `<div class="row"><span class="lamp bad"></span><div class="grow"><div class="title">${esc(x.title || "(no title)")}</div>
        <div class="sub">${kind === "failed" || kind === "expired" ? "" : "last change "}${esc(when(at))} (${esc(ago(at))})</div>
        <div class="sub">${esc(kind)} on ${esc(devices.join(", "))} after ${x.attempts} attempts: ${esc(x.last_error || "")}</div></div>
        ${kind === "failed" || kind === "expired" ? `<div class="actions"><button class="btn" data-dismiss="${esc(x.notification_id)}">Dismiss</button></div>` : ""}</div>`).join("")}
      </div>` : "";
    attentionEl.querySelectorAll("[data-dismiss]").forEach((b) => (b.onclick = async () => {
      b.disabled = true;
      try { await api("POST", `/notifications/${b.dataset.dismiss}/dismiss`); toast("Dismissed"); }
      catch (ex) { toast(ex.message); }
      loadRecent(el, limit, attentionEl); // also after a 409: another tab may have dismissed it already
    }));
  }
  if (!data.notifications.length) { el.innerHTML = `<div class="empty">Nothing sent yet.</div>`; return; }
  el.innerHTML = data.notifications.map((n) => `
    <div class="row" style="align-items:flex-start"><span class="lamp ${lampFor(n.status)}" style="margin-top:.45rem"></span>
      <div class="grow">
        <div class="title">${esc(n.title || n.message.slice(0, 60))}</div>
        <div class="sub">${esc(when(n.created_at))} from ${esc(n.source)} to ${esc(n.targets.join(", "))}, ${esc(n.severity)}</div>
        <div class="sub"><span class="status">${esc(n.status)}</span>${n.suppressed.length ? `: ${esc(n.suppressed[0].reason)}` : ""}</div>
        ${n.attachments?.length ? `<dl class="kv"><dt>Attachments</dt><dd>${n.attachments.map((a) =>
          `${esc(a.filename)} (${esc(a.content_type)}, ${esc(fmtSize(a.size))})${a.purged ? ` <span class="chip">purged</span>` : ""}`).join("<br>")}</dd></dl>` : ""}
        ${n.deliveries.map((d) => `
          <details class="delivery"><summary>${esc(cfg().devices[d.device]?.description || d.device)}: ${esc(d.state)}${d.final ? "" : " (may still change)"}</summary>
            <dl class="kv">
              ${d.timestamps.dispatched ? `<dt>Accepted by ${esc(d.provider)}</dt><dd>${esc(when(d.timestamps.dispatched))}</dd>` : ""}
              ${d.timestamps.delivered ? `<dt>Delivered</dt><dd>${esc(when(d.timestamps.delivered))}</dd>` : ""}
              ${d.timestamps.acknowledged ? `<dt>Acknowledged</dt><dd>${esc(when(d.timestamps.acknowledged))}${d.acknowledged_by ? ` on ${esc(d.acknowledged_by)}` : ""}</dd>` : ""}
              <dt>Can report</dt><dd>${esc(d.observable_states.filter((s) => ["dispatched", "delivered", "acknowledged"].includes(s)).join(", "))}</dd>
              <dt>Attempts</dt><dd>${d.attempts}</dd>
              ${d.last_error ? `<dt>Last error</dt><dd class="problem">${esc(d.last_error)}</dd>` : ""}
              ${d.capability_gaps.length ? `<dt>Not supported</dt><dd>${d.capability_gaps.map(esc).join("<br>")}</dd>` : ""}
            </dl></details>`).join("")}
      </div>
      <div class="actions">
        ${["failed", "partial"].includes(n.status) ? `<button class="btn" data-retry="${esc(n.id)}">Retry</button>` : ""}
        ${n.status === "pending" || n.deliveries.some((d) => !d.final && d.tracking === "receipt") ? `<button class="btn" data-cancel="${esc(n.id)}">Cancel</button>` : ""}
      </div></div>`).join("");
  el.querySelectorAll("[data-retry]").forEach((b) => (b.onclick = async () => {
    try { await api("POST", `/notifications/${b.dataset.retry}/retry`); toast("Queued again"); loadRecent(el, limit, attentionEl); }
    catch (ex) { toast(ex.message); }
  }));
  el.querySelectorAll("[data-cancel]").forEach((b) => (b.onclick = async () => {
    try { await api("POST", `/notifications/${b.dataset.cancel}/cancel`); toast("Cancelled"); loadRecent(el, limit, attentionEl); }
    catch (ex) { toast(ex.message); }
  }));
}

async function loadReplies(el) {
  let data;
  try { data = await api("GET", "/replies?limit=10"); }
  catch (ex) { el.innerHTML = `<div class="empty problem">${esc(ex.message)}</div>`; return; }
  const status = Object.entries(data.inbound).map(([name, s]) => `<div class="sub">${esc(name)}: ${s.last_error
    ? `<span class="problem">${esc(s.last_error)}</span>` : s.last_success ? `checked ${esc(when(s.last_success))}` : "not checked yet"}</div>`).join("");
  if (!data.replies.length && !data.quarantine.length && !status) { el.innerHTML = ""; return; }
  el.innerHTML = `<h3>Replies</h3>${status}<div class="rows">
    ${data.replies.map((r) => `<div class="row"><span class="lamp ${r.state === "unclaimed" ? "warn" : "ok"}"></span><div class="grow">
      <div class="title">${esc(r.notification_title || r.subject || "(no title)")}</div>
      <div class="sub">${esc(when(r.received_at))} from ${esc(r.from)}: ${esc(r.state)}${r.claimed_by ? ` by ${esc(r.claimed_by)}` : ""}${r.autoreply_notification_id ? ", autoreply sent" : ""}</div>
      <div class="sub">${esc((r.text || "").slice(0, 200))}</div>
      ${r.attachments.length ? `<div class="sub">${r.attachments.map((a) => esc(a.filename)).join(", ")}</div>` : ""}</div></div>`).join("")}
    ${data.quarantine.map((q) => `<div class="row"><span class="lamp bad"></span><div class="grow">
      <div class="title">Rejected: ${esc(q.subject || "(no subject)")}</div>
      <div class="sub">${esc(when(q.received_at))} from ${esc(q.from_address || "?")}: ${esc(q.reason)}</div></div></div>`).join("")}
  </div>`;
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
