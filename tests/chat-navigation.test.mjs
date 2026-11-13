import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../notify/admin_ui/app.js', import.meta.url), 'utf8');

test('a poll finishing after a selection does not start a second open', async () => {
  let release;
  const opened = [];
  const chats = { opening: 0, selected: 'A', currentId: 'A' };
  const context = {
    chats, section: 'chats', document: { visibilityState: 'visible' }, console,
    clearInterval() {},
    chatsLoadList: () => new Promise(resolve => { release = resolve; }),
    chatFolderOf: () => ({ id: 'B' }),
    chatOpen: async (_el, key) => opened.push(key),
  };
  vm.runInNewContext(source.slice(source.indexOf('async function chatTickOnce')) + '; globalThis.tick = chatTickOnce;', context);
  const done = context.tick({ isConnected: true });
  chats.opening = 1; chats.selected = 'B'; chats.currentId = null;
  release();
  await done;
  assert.deepEqual(opened, []);
});

test('an older open failure cannot replace the latest open result, even for the same chat', async () => {
  const requests = [];
  const pane = { innerHTML: '' };
  const el = { isConnected: true, classList: { add() {} }, querySelector: () => pane };
  const context = {
    chats: {}, chatRosterToggle() {}, chatsRenderList() {}, esc: text => text,
    chatFolderOf: () => ({ host: 'host', folder: '/folder' }),
    api: () => new Promise((_resolve, reject) => requests.push(reject)),
  };
  const code = source.slice(source.indexOf('async function chatOpen('), source.indexOf('function chatAttach('));
  vm.runInNewContext(code + '; globalThis.open = chatOpen;', context);
  const first = context.open(el, 'A');
  const latest = context.open(el, 'A');
  requests[1](new Error('latest failure')); await latest;
  requests[0](new Error('old failure')); await first;
  assert.match(pane.innerHTML, /latest failure/);
});

test('refresh waits through pointer and keyboard release until the native click task finishes', () => {
  const document = new EventTarget(), window = new EventTarget(), list = new EventTarget();
  const timers = [];
  const renders = [];
  list.isConnected = true;
  const el = { isConnected: true };
  const context = {
    document, window, WeakMap, AbortController,
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {},
    chatsRenderList: target => renders.push(target),
  };
  const code = source.slice(source.indexOf('const chatListPresses'), source.indexOf('function chatsRenderList'));
  vm.runInNewContext(code + '; globalThis.state = chatListPressState;', context);
  const state = context.state(list, el);
  for (const type of ['pointerdown', 'keydown']) {
    const start = new Event(type);
    Object.defineProperty(start, 'target', { value: { closest: () => ({}) } });
    Object.assign(start, { button: 0, pointerId: 1, key: ' ' });
    list.dispatchEvent(start);
    state.pending = true;
    const end = new Event(type === 'pointerdown' ? 'pointerup' : 'keyup');
    Object.assign(end, { pointerId: 1, key: ' ' });
    document.dispatchEvent(end);
    assert.equal(state.held, true);
    timers.shift()();
    assert.equal(state.held, false);
  }
  assert.equal(renders.length, 2);
});
