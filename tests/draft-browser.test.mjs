import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../notify/admin_ui/app.js', import.meta.url), 'utf8');
const part = source.slice(source.indexOf('const draftBrowserId ='), source.indexOf('const draftLeaseQueues ='));
let counter = 0;
function documentId() {
  return vm.runInNewContext(`${part}\ndraftBrowserId`, {
    crypto: { getRandomValues: (bytes) => { bytes.fill(++counter); return bytes; } }, Uint8Array,
  });
}

test('duplicated and reloaded documents get distinct identities without a peer response', () => {
  const first = documentId();
  const duplicate = documentId();
  const reload = documentId();
  assert.notEqual(first, duplicate);
  assert.notEqual(first, reload);
  assert.notEqual(duplicate, reload);
});

test('queued edits retain their original deadline and a hung request is aborted before clear', async () => {
  const code = source.slice(source.indexOf('const draftBrowserId ='), source.indexOf('function chatDraftGet('));
  let now = 1_700_000_000_000;
  const requests = [], timers = [];
  class FakeDate extends Date { static now() { return now; } }
  const context = {
    crypto: { getRandomValues: (bytes) => { bytes.fill(++counter); return bytes; } },
    Uint8Array, Map, Set, Promise, Date: FakeDate, AbortController, serverClockOffset: 0,
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: () => {}, toast: () => {},
    api: (_method, _path, body, signal) => new Promise((resolve, reject) => {
      requests.push({ body, signal, resolve });
      signal.addEventListener('abort', () => reject(new Error('aborted')));
    }),
  };
  vm.runInNewContext(`${code}\nglobalThis.edit = chatDraftLease;`, context);
  context.edit('group', true);
  assert.equal(requests[0].body.expires_ms, now + 600_000);
  now += 500_000;
  context.edit('group', true);
  context.edit('group', false);
  timers[0]();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests[0].signal.aborted, true);
  assert.equal(requests[1].body.active, false);
  assert.ok(requests[1].body.sequence > requests[0].body.sequence);
  requests[1].resolve();
});

test('a delayed queued edit keeps the deadline captured at input time', async () => {
  const code = source.slice(source.indexOf('const draftBrowserId ='), source.indexOf('function chatDraftGet('));
  let now = 1_700_000_000_000;
  const requests = [], timers = [];
  class FakeDate extends Date { static now() { return now; } }
  const context = {
    crypto: { getRandomValues: (bytes) => { bytes.fill(++counter); return bytes; } },
    Uint8Array, Map, Set, Promise, Date: FakeDate, AbortController, serverClockOffset: 0,
    setTimeout: (fn) => { timers.push(fn); return timers.length; }, clearTimeout: () => {}, toast: () => {},
    api: (_method, _path, body, signal) => new Promise((resolve, reject) => {
      requests.push({ body, resolve }); signal.addEventListener('abort', () => reject(new Error('aborted')));
    }),
  };
  vm.runInNewContext(`${code}\nglobalThis.edit = chatDraftLease;`, context);
  context.edit('group', true);
  now += 1000;
  context.edit('group', true);
  const expected = now + 600_000;
  now += 700_000;
  timers[0]();
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(requests[1].body.expires_ms, expected);
  requests[1].resolve();
});

test('lease deadline uses server time when the browser clock is skewed', async () => {
  const apiSource = source.slice(source.indexOf('let serverClockOffset ='), source.indexOf('function toast('));
  const leaseSource = source.slice(source.indexOf('const draftBrowserId ='), source.indexOf('function chatDraftGet('));
  const serverTime = 1_700_000_000_000;
  class SkewedDate extends Date { static now() { return serverTime + 3_600_000; } }
  const requests = [];
  const context = {
    crypto: { getRandomValues: (bytes) => { bytes.fill(++counter); return bytes; } },
    Uint8Array, Map, Set, Promise, Date: SkewedDate, AbortController, setTimeout, clearTimeout,
    toast: () => {}, boot: async () => {},
    fetch: async (_url, opts) => {
      requests.push(opts);
      return { headers: { get: () => new Date(serverTime).toUTCString() }, status: 200, ok: true, json: async () => ({}) };
    },
  };
  vm.runInNewContext(`${apiSource}\n${leaseSource}\nglobalThis.api = api; globalThis.edit = chatDraftLease;`, context);
  await context.api('GET', '/chats');
  context.edit('group', true);
  await new Promise((resolve) => setImmediate(resolve));
  const body = JSON.parse(requests.at(-1).body);
  assert.equal(body.expires_ms, serverTime + 601_000);
});
