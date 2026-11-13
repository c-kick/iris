import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(new URL('../notify/admin_ui/app.js', import.meta.url), 'utf8');
const block = source.slice(source.indexOf('function chatMessageHtml('), source.indexOf('// an earlier session'));
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const render = vm.runInNewContext(`${block}\nchatMessageHtml`, {esc, when: () => 'now', chats: { names: {} }});
test('owner card shows full exact details safely and offers a one-request decision', () => {
  const details = '<script>alert(1)</script>\n' + 'a'.repeat(4000) + 'END';
  const html = render({author:'system', id:'msg', text:'Agent requests owner confirmation', created_at:'now',
    confirm_request:{id:'rqc_123', state:'pending', action:'Deploy & restart', target:'host-a', details}});
  assert.ok(html.includes(esc(details)));
  assert.ok(!html.includes('<script>'));
  assert.ok(html.includes('data-creq="rqc_123"'));
  assert.ok(html.includes('data-act="approve"') && html.includes('data-act="deny"'));
});
test('decided request has no decision buttons', () => {
  const html = render({author:'system', id:'msg', text:'notice', created_at:'now',
    confirm_request:{id:'rqc_123', state:'approved', action:'Deploy', target:'host', details:'version'}});
  assert.ok(!html.includes('data-creq='));
  assert.ok(html.includes('Status: approved'));
});

test('decision failure re-enables retry and refreshes authoritative state', async () => {
  const buttons = [{disabled:false,dataset:{creq:'request',act:'approve'}},{disabled:false}];
  const box = { querySelectorAll: () => buttons };
  let refreshed = 0;
  const context = {box, pane: box, el:{}, CSS:{escape:x=>x}, api:async()=>{throw Error('connection failed');},
    chatTick:async()=>{refreshed += 1;}, toast() {}};
  const start = source.indexOf('  box.onclick = async (e) => {');
  const end = source.indexOf('  pane.querySelector("#chat-confirm-pins").onclick', start);
  vm.runInNewContext(source.slice(start, end), context);
  await box.onclick({target:{closest:selector=>selector==='[data-creq]'?buttons[0]:null}});
  assert.equal(buttons[0].disabled, false);
  assert.equal(buttons[1].disabled, false);
  assert.equal(refreshed, 1);
});

test('successful decision keeps buttons disabled until refresh reconciles', async () => {
  const buttons = [{disabled:false,dataset:{creq:'request',act:'approve'}},{disabled:false}];
  const box = { querySelectorAll: () => buttons };
  let refreshed = 0;
  const context = {box, pane: box, el:{}, CSS:{escape:x=>x}, api:async()=>{},
    chatTick:async()=>{refreshed += 1;}, toast() {}};
  const start = source.indexOf('  box.onclick = async (e) => {');
  const end = source.indexOf('  pane.querySelector("#chat-confirm-pins").onclick', start);
  vm.runInNewContext(source.slice(start, end), context);
  await box.onclick({target:{closest:selector=>selector==='[data-creq]'?buttons[0]:null}});
  assert.equal(buttons[0].disabled, true);
  assert.equal(refreshed, 1);
});
