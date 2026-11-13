import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(new URL('../notify/admin_ui/app.js', import.meta.url), 'utf8');
const start = source.indexOf('function chatPinnedConfirmations(');
const end = source.indexOf('\n}\n', start) + 2;
const nodes = [];
const list = {
  querySelectorAll: () => [...nodes],
  querySelector: (selector) => nodes.find((n) => selector.includes(`"${n.dataset.confirm}"`)) || null,
  insertAdjacentHTML: (_where, html) => {
    const id = /data-confirm="([^"]+)"/.exec(html)[1];
    const node = { dataset: { confirm: id }, remove() { nodes.splice(nodes.indexOf(node), 1); } };
    nodes.push(node);
  },
};
const title = {textContent:''};
const region = {hidden:false, querySelector: (s) => s === '.pinned-confirm-list' ? list : title};
const pane = {querySelector: () => region};
const fn = vm.runInNewContext(`${source.slice(start,end)}\nchatPinnedConfirmations`, {
  CSS:{escape:x=>x}, chatMessageHtml: (card) => `<div data-confirm="${card.confirm_request.id}"></div>`, Set,
});
const card = (id) => ({confirm_request:{id}});
test('pending cards remain pinned and existing button nodes survive refresh', () => {
  fn(pane, [card('a'), card('b')]);
  assert.equal(region.hidden, false);
  assert.equal(nodes.length, 2);
  const first = nodes[0];
  fn(pane, [card('a'), card('b')]);
  assert.equal(nodes[0], first, 'poll must not replace a button during a press');
  fn(pane, [card('a')]);
  assert.equal(nodes.length, 1);
  assert.equal(nodes[0], first);
  assert.equal(title.textContent, 'Confirmation awaiting your decision');
  fn(pane, []);
  assert.equal(region.hidden, true);
});
