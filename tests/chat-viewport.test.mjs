import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
const source=readFileSync(process.env.IRIS_VIEWPORT_SOURCE || new URL('../notify/admin_ui/app.js',import.meta.url),'utf8');
function fit({height, innerHeight, offsetTop=0, scrollY=0, focused=true, onChats=true, scale=1, userAgent='iPhone'}={}) {
  const start=source.indexOf('if (window.visualViewport) {');
  const end=source.indexOf('  // ?vvdebug=1',start);
  const vars={}; let reset=0;
  const window={visualViewport:{height,offsetTop,scale},scrollY,scrollTo(){reset++}};
  const document={documentElement:{style:{setProperty(k,v){vars[k]=v}}},activeElement:{matches:()=>focused},getElementById:()=>onChats?{}:null};
  vm.runInNewContext(source.slice(start,end)+'\nlet debug=null; fit(); }',{window,document,navigator:{userAgent},innerHeight,chatRosterSize(){}});
  return {vars,reset};
}
test('measured iPhone keyboard viewport is not reduced by an extra toolbar estimate',()=>{
  const {vars}=fit({height:384,innerHeight:714,offsetTop:0});
  assert.equal(vars['--app-h'],'384px');
  assert.equal(vars['--app-top'],'0px');
});
test('viewport height is preserved on docked iPhone and Android keyboards',()=>{
  assert.equal(fit({height:384,innerHeight:384,offsetTop:330}).vars['--app-h'],'384px');
  assert.equal(fit({height:384,innerHeight:714,userAgent:'Android'}).vars['--app-h'],'384px');
  assert.equal(fit({height:384,innerHeight:384,offsetTop:330}).vars['--app-top'],'330px');
});
test('viewport pinch zoom does not alter layout',()=>{
  assert.deepEqual(fit({height:384,innerHeight:714,scale:1.5}).vars,{});
});
test('closing the keyboard restores the full viewport without following a stale offset',()=>{
  const {vars,reset}=fit({height:714,innerHeight:714,offsetTop:330,focused:false,scrollY:330});
  assert.equal(vars['--app-h'],'714px');
  assert.equal(vars['--app-top'],'0px');
  assert.equal(reset,1);
});
test('typing follows a retained viewport offset and clears page scroll on Chats',()=>{
  const {vars,reset}=fit({height:384,innerHeight:384,offsetTop:330,scrollY:330});
  assert.equal(vars['--app-h'],'384px');
  assert.equal(vars['--app-top'],'330px');
  assert.equal(reset,1);
});
test('typing on other admin pages does not reset their document scroll',()=>{
  const {vars,reset}=fit({height:384,innerHeight:714,offsetTop:330,scrollY:330,onChats:false});
  assert.equal(vars['--app-top'],'0px');
  assert.equal(reset,0);
});
test('desktop and iPad views also retain their visible viewport height',()=>{
  assert.equal(fit({height:800,innerHeight:800,focused:false,userAgent:'desktop'}).vars['--app-h'],'800px');
  assert.equal(fit({height:384,innerHeight:714,userAgent:'iPad'}).vars['--app-h'],'384px');
});
