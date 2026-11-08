const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {join} = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

test('approved jobs explain the blocking inspection and offline waiting',()=>{
  const source=readFileSync(join(__dirname,'app.js'),'utf8');
  const start=source.indexOf('function queueWaitMessage(');
  const end=source.indexOf('\nasync function api(',start);
  const context={}; vm.createContext(context); vm.runInContext(source.slice(start,end),context);
  const waiting={state:'approved'};
  const blocker={state:'submitted',name:'Tidligere print'};
  assert.match(context.queueWaitMessage(waiting,[blocker],true),/Tidligere print/);
  assert.match(context.queueWaitMessage(waiting,[blocker],true),/byggepladen/);
  assert.match(context.queueWaitMessage(waiting,[],false),/online/);
  assert.match(context.queueWaitMessage(waiting,[{...blocker,state:'dispatched'}],true),/sendt til Pi/);
  assert.equal(context.queueWaitMessage({state:'resolved'},[blocker],true),'');
});

function setup(fetch) {
  const nodes = new Map();
  const all = [];
  class Node {
    constructor(tag) { this.tag=tag; this.children=[]; this.value=''; this.open=false; all.push(this); }
    set id(value) { this.identifier=value; nodes.set(value,this); }
    get id() { return this.identifier; }
    append(...children) { this.children.push(...children); if (this.tag==='select' && this.children.length===children.length) this.value=children[0].value; }
    after() {}
    remove() { nodes.delete(this.id); }
    setAttribute() {}
    addEventListener() {}
    setCustomValidity(message) { this.validationMessage=message; }
    showModal() { this.open=true; }
    close(value) { this.open=false; this.returnValue=value; }
    focus() { this.focused=true; }
  }
  const dialog=new Node('dialog'); dialog.id='approval';
  const form=new Node('form'); const cancel=new Node('button'); cancel.value='cancel';
  const submit=new Node('button'); submit.value='approve';
  const title=new Node('h2'); const description=new Node('p');
  const name=new Node('p'); name.id='approvalName';
  const upload=new Node('form'); upload.id='upload';
  const selectors={'form':form,'[value="cancel"]':cancel,'[value="approve"]':submit,'h2':title,'form > p:last-of-type':description};
  dialog.querySelector=selector=>selectors[selector];
  form.reportValidity=()=>true;
  const context={document:{getElementById:id=>nodes.get(id),createElement:tag=>new Node(tag),createTextNode:text=>({textContent:text})},fetch};
  vm.createContext(context);
  const source=readFileSync(join(__dirname,'app.js'),'utf8').replace(/\nrefresh\(\); setInterval\(refresh, 5000\);\s*$/, '');
  vm.runInContext(source,context);
  context.refresh=async()=>{ context.refreshed=true; };
  vm.runInContext('latestPrinter={ams_slots:[{ams_id:0,slot:1,present:true,stale:false,material:"PLA",name:"PLA Basic",color:"000000FF"}]}; liveEnabled=true;',context);
  const job={id:'test-job',name:'model.gcode.3mf',analysis:{filaments:[{material:'PLA',profile:'PLA Basic',color:'#000000'}]}};
  const event=button=>({preventDefault(){},submitter:button});
  return {context,nodes,dialog,form,cancel,submit,job,event,all};
}

async function choose(view) {
  await view.context.approve(view.job);
  view.nodes.get('ams-choice').value=JSON.stringify({ams_id:0,slot:1});
  view.all.find(node=>node.type==='checkbox' && !node.id).checked=true;
}

test('server rejection stays visible in dialog and preserves selection',async()=>{
  let requests=0;
  const view=setup(async()=>{ requests++; return {ok:false,json:async()=>({error:'Printerstatus er for gammel.'})}; });
  await choose(view);
  await view.form.onsubmit(view.event(view.submit));
  assert.equal(requests,1);
  assert.equal(view.dialog.open,true);
  assert.equal(view.nodes.get('approval-error').textContent,'Printerstatus er for gammel.');
  assert.equal(view.nodes.get('ams-choice').value,'{"ams_id":0,"slot":1}');
  assert.equal(view.submit.disabled,false);
});

test('dialog closes only after acknowledgement and prevents duplicate submissions',async()=>{
  let acknowledge; let requests=0;
  const view=setup(()=>{ requests++; return new Promise(resolve=>{acknowledge=resolve;}); });
  await choose(view);
  const first=view.form.onsubmit(view.event(view.submit));
  await view.form.onsubmit(view.event(view.submit));
  assert.equal(view.dialog.open,true);
  assert.equal(view.submit.disabled,true);
  assert.equal(requests,1);
  acknowledge({ok:true,json:async()=>({saved:true})});
  await first;
  assert.equal(view.dialog.open,false);
  assert.equal(view.context.refreshed,true);
});

test('cancel and invalid forms never send approval',async()=>{
  let requests=0;
  const view=setup(async()=>{requests++;});
  await choose(view);
  view.form.reportValidity=()=>false;
  await view.form.onsubmit(view.event(view.submit));
  assert.equal(view.dialog.open,true);
  await view.form.onsubmit(view.event(view.cancel));
  assert.equal(view.dialog.open,false);
  assert.equal(requests,0);
});

test('physical confirmation requires full START PRINT text',async()=>{
  const view=setup(async()=>{throw new Error('No approval should be sent');});
  await choose(view);
  const physical=view.nodes.get('physical-confirmation');
  physical.value='START'; physical.oninput();
  assert.match(physical.validationMessage,/START PRINT/);
  physical.value='START PRINT'; physical.oninput();
  assert.equal(physical.validationMessage,'');
});

test('live button blocks conflicting profiles but simulation remains possible',async()=>{
  const view=setup(async()=>{throw new Error('No approval should be sent');});
  view.job.analysis.filaments[0].profile='Bambu PLA Matte';
  await choose(view);
  const mode=view.nodes.get('execution-mode');
  mode.value='live'; mode.onchange();
  assert.equal(view.submit.disabled,true);
  mode.value='simulate'; mode.onchange();
  assert.equal(view.submit.disabled,false);
});

test('STL jobs send the separate preview confirmation',async()=>{
  let sent;
  const view=setup(async(path,options)=>{sent=JSON.parse(options.body);return {ok:true,json:async()=>({saved:true})};});
  view.job.model_info={dimensions_mm:[10,10,10]};
  await choose(view);
  for(const node of view.all)if(node.type==='checkbox' && node.required)node.checked=true;
  await view.form.onsubmit(view.event(view.submit));
  assert.equal(sent.preview_checked,true);
  assert.equal(sent.bed_clear,true);
  assert.equal(sent.execution_mode,'simulate');
});
