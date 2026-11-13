import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const source = readFileSync(new URL('../notify/admin_ui/app.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('../notify/admin_ui/app.css', import.meta.url), 'utf8');

test('only newest pending confirmation sticks in the transcript', () => {
  const start = source.indexOf('function chatSyncConfirmations(');
  assert.ok(start >= 0, 'pending confirmations must be reconciled in the transcript');
  const end = source.indexOf('\n}\n', start) + 2;
  const makeButton=()=>({dataset:{},remove(){this.owner.more=null}});
  const sync = vm.runInNewContext(source.slice(start, end) + '\nchatSyncConfirmations', {document:{createElement:makeButton}});
  const card = id => ({dataset:{confirm:id,id:'message-'+id},more:null,classList:{set:new Set(),toggle(name,on){on?this.set.add(name):this.set.delete(name)}}, querySelector(s){return s === '.confirm-more' ? this.more : {textContent:''}},prepend(button){this.more=button;button.owner=this}});
  const a=card('a'), b=card('b'); const box={querySelectorAll:()=>[a,b]};
  sync(box,['a','b']);
  assert.equal(a.classList.set.has('confirm-sticky'),false);
  assert.equal(b.classList.set.has('confirm-sticky'),true);
  assert.equal(b.more.textContent,'+1 more pending');
  assert.equal(b.more.dataset.confirmJump,'message-a');
  const preserved=b.more; sync(box,['a','b']); assert.equal(b.more,preserved);
  sync(box,['a']);
  assert.equal(a.classList.set.has('confirm-sticky'),true);
  assert.equal(b.classList.set.has('confirm-sticky'),false);
  assert.equal(b.more,null);
  sync(box,[]);
  assert.equal(a.classList.set.has('confirm-sticky'),false);
});

test('sticky card is bounded and no separate transcript pin exists', () => {
  assert.ok(!source.includes('id="chat-confirm-pins"'));
  assert.match(css,/\.cm\.sys\.confirm-sticky\s*\{[^}]*position:\s*sticky/s);
  assert.match(css,/\.cm\.sys\.confirm-sticky\s*\{[^}]*max-height:/s);
  assert.match(css,/\.cm\.sys\.confirm-sticky\s*\{[^}]*align-self:\s*stretch/s);
  assert.match(css,/\.cm\.sys\.confirm-sticky\s*\{[^}]*max-width:\s*none/s);
  assert.match(css,/\.cm\.sys\.confirm-sticky\s*\{[^}]*overflow:\s*auto/s);
});


test('pending-count button navigates to the older message', async () => {
  const start=source.indexOf('  box.onclick = async (e) => {');
  const end=source.indexOf('  chatSyncConfirmations(box, data.pending_confirm_requests || []);',start);
  const box={}, el={}; let target;
  vm.runInNewContext(source.slice(start,end),{box,el,chatShowMessage:(_el,id)=>{target=id}});
  await box.onclick({target:{closest:s=>s==='[data-confirm-jump]'?{dataset:{confirmJump:'message-a'}}:null}});
  assert.equal(target,'message-a');
});
