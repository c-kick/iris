import { test } from "node:test";
import assert from "node:assert/strict";
import { createTabAlerts } from "../notify/admin_ui/tab-alerts.js";

function fixture(t, extra = {}) {
  const doc = new EventTarget();
  doc.defaultView = new EventTarget();
  doc.title = "Iris";
  const nodes = [];
  const node = () => ({
    isConnected: false,
    remove() { this.isConnected = false; },
  });
  const originals = [node(), node(), node(), node()];
  doc.head = { append(n) { if (!nodes.includes(n)) nodes.push(n); n.isConnected = true; } };
  originals.forEach((n) => doc.head.append(n));
  doc.querySelectorAll = () => originals;
  doc.createElement = node;
  let folders = [];
  let calls = 0;
  let visible = false;
  let received = null;
  const alerts = createTabAlerts({
    doc, interval: 5,
    loadFolders: async () => { calls += 1; return folders; },
    onFolders: (f) => { received = f; },
    chatVisible: () => visible,
    ...extra,
  });
  t.after(() => alerts.stop());
  return { doc, alerts, originals, nodes,
    set folders(f) { folders = f; },
    set visible(v) { visible = v; },
    get calls() { return calls; },
    get received() { return received; },
  };
}

const settle = () => new Promise((resolve) => setImmediate(resolve));

test("messages and bell, envelope and mention notices set the title and one favicon", (t) => {
  const f = fixture(t);
  for (const notice of [{ alert: true, alert_kind: "push" }, { alert: true, alert_kind: "email" }, { owner_mention: true }]) {
    f.alerts.update([{ unread: 0, ...notice }]);
    assert.equal(f.doc.title, "(1) Iris");
    assert.equal(f.nodes.filter((n) => n.isConnected).length, 1);
    assert.match(f.nodes.at(-1).href, /iris-favicon-unread.svg$/);
  }
  f.alerts.update([{ unread: 3, alert: true, owner_mention: true }, { unread: 2 }, { alert: true }]);
  assert.equal(f.doc.title, "(6) Iris");
  f.alerts.update([]);
  assert.equal(f.doc.title, "Iris");
  assert.ok(f.originals.every((n) => n.isConnected));
  assert.equal(f.nodes.filter((n) => n.isConnected).length, 4);
});

test("numbered icon follows the unread count and caps its label", (t) => {
  const labels = [];
  let renders = 0;
  const ctx = {
    beginPath() {}, moveTo() {}, lineTo() {}, stroke() {}, arc() {}, fill() {}, roundRect() {},
    measureText(label) { return { width: label.length * 25, actualBoundingBoxAscent: 45, actualBoundingBoxDescent: 5 }; },
    fillText(label) { labels.push(label); },
  };
  const f = fixture(t);
  const create = f.doc.createElement;
  f.doc.createElement = (tag) => tag === "canvas" ? {
    getContext: () => ctx,
    toDataURL: () => `data:image/png;render=${++renders}`,
  } : create();
  f.alerts.update([{ unread: 2 }]);
  assert.equal(labels.at(-1), "2");
  assert.match(f.nodes.at(-1).href, /^data:image\/png/);
  f.alerts.update([{ unread: 105 }]);
  assert.equal(labels.at(-1), "99+");
  assert.equal(f.doc.title, "(105) Iris");
  assert.equal(renders, 2);
  f.alerts.update([]);
  assert.ok(f.originals.every((n) => n.isConnected));
});

test("background polling works outside Chats and clears read notices", async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  const f = fixture(t);
  f.folders = [{ unread: 2 }];
  f.alerts.start();
  await settle();
  assert.equal(f.doc.title, "(2) Iris");
  assert.equal(f.received.length, 1);
  f.folders = [];
  t.mock.timers.tick(5);
  await settle();
  assert.equal(f.doc.title, "Iris");
  f.visible = true;
  t.mock.timers.tick(5);
  await settle();
  assert.equal(f.calls, 2, "the visible Chats page already polls");
  f.visible = false;
  f.folders = [{ owner_mention: true }];
  f.doc.defaultView.dispatchEvent(new Event("focus"));
  await settle();
  assert.equal(f.doc.title, "(1) Iris");
});

test("a pending confirmation marks the tab until decided", (t) => {
  const f = fixture(t);
  f.alerts.update([{ pending_confirm_requests: 1 }]);
  assert.equal(f.doc.title, "(1) Iris");
  f.alerts.update([{ pending_confirm_requests: 0 }]);
  assert.equal(f.doc.title, "Iris");
});

test("an in-flight response cannot restore badges after sign-out or overwrite foreground reads", async (t) => {
  let resolve;
  const f = fixture(t, { loadFolders: () => new Promise((r) => { resolve = r; }) });
  f.alerts.start();
  f.alerts.update([]); // the selected chat was read while the background request ran
  resolve([{ unread: 4 }]);
  await settle();
  assert.equal(f.doc.title, "Iris");
  f.doc.dispatchEvent(new Event("visibilitychange"));
  f.alerts.stop();
  resolve([{ alert: true }]);
  await settle();
  assert.equal(f.doc.title, "Iris");
  assert.equal(f.received, null);
});

test("failures retain the badge, retry, and requests do not overlap", async (t) => {
  t.mock.timers.enable({ apis: ["setInterval"] });
  let reject;
  let calls = 0;
  const f = fixture(t, { loadFolders: () => { calls += 1; return new Promise((_, r) => { reject = r; }); } });
  f.alerts.update([{ unread: 7 }]);
  f.alerts.start();
  f.alerts.start();
  t.mock.timers.tick(15);
  assert.equal(calls, 1);
  reject(new Error("offline"));
  await settle();
  assert.equal(f.doc.title, "(7) Iris");
  t.mock.timers.tick(5);
  assert.equal(calls, 2);
  f.alerts.stop();
  t.mock.timers.tick(20);
  assert.equal(calls, 2);
  assert.equal(f.doc.title, "Iris");
  reject(new Error("offline"));
  await settle();
});
