import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(new URL('../notify/admin_ui/app.js', import.meta.url), 'utf8');

test('quoted replies use a native keyboard-accessible button', () => {
  assert.ok(source.includes('<button type="button" class="quote" data-quote='), "reply quote must use a button");
});

test('message navigation finds nested earlier messages and reports unavailable targets', () => {
  const start = source.indexOf('function chatShowMessage(');
  const end = source.indexOf('\n}\n', start) + 2;
  const context = { CSS: { escape: value => value } };
  const navigate = vm.runInNewContext(`${source.slice(start, end)}\nchatShowMessage`, context);
  let selector, scrolled = false;
  const message = { scrollIntoView() { scrolled = true; }, classList: { remove() {}, add() {} }, offsetWidth: 1 };
  const el = { querySelector(s) { selector = s; return message; } };
  assert.equal(navigate(el, 'original'), true);
  assert.match(selector, /^#chat-msgs \[data-id=/, 'search inside expanded earlier sessions too');
  assert.equal(scrolled, true);
  assert.equal(navigate({ querySelector() { return null; } }, 'missing'), false);
});

test('delegated quote activation jumps or explains a missing original', async () => {
  const start = source.indexOf('  box.onclick = async (e) => {');
  const end = source.indexOf('  pane.querySelector("#chat-confirm-pins").onclick', start);
  const box = {}, el = {};
  let seen, message, found = true;
  const context = { box, el, chatShowMessage: (_el, id) => { seen = id; return found; }, toast: text => { message = text; } };
  vm.runInNewContext(source.slice(start, end), context);
  await box.onclick({target:{closest: selector => selector === '[data-quote]' ? {dataset:{quote:'original'}} : null}});
  assert.equal(seen, 'original');
  assert.equal(message, undefined);
  found = false;
  await box.onclick({target:{closest: selector => selector === '[data-quote]' ? {dataset:{quote:'original'}} : null}});
  assert.match(message, /not available/i);
});
