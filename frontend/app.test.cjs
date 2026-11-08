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
    constructor(tag) { this.tag=tag; this.children=[]; this.value=''; this.open=false; this.dataset={}; this.style={}; this.listeners={}; all.push(this); }
    get childNodes() { return this.children; }
    set textContent(value) { this.text=value; this.children=[]; }
    get textContent() { return this.text || ''; }
    insertBefore(child,before) { child.remove?.(); const index=before?this.children.indexOf(before):this.children.length;this.children.splice(index,0,child);child.parentNode=this; }
    replaceChildren(...children) { for(const child of this.children)child.parentNode=null;this.children=[];this.append(...children); }
    reset() { this.wasReset=true; }
    set id(value) { this.identifier=value; nodes.set(value,this); }
    get id() { return this.identifier; }
    append(...children) { this.children.push(...children); for(const child of children)child.parentNode=this; if (this.tag==='select' && this.children.length===children.length) this.value=children[0].value; }
    after() {}
    remove() { nodes.delete(this.id); if(this.parentNode){const index=this.parentNode.children.indexOf(this);if(index>=0)this.parentNode.children.splice(index,1);this.parentNode=null;} }
    setAttribute() {}
    addEventListener(name,listener) { this.listeners[name]=listener; }
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
  const notice=new Node('div');
  for(const id of ['jobs','count','history-jobs','history-count','ams-slots','connection','state','updated','nozzle','bed','progress','file','uploadButton','message']){const node=new Node('div');node.id=id;}
  const context={document:{getElementById:id=>nodes.get(id),createElement:tag=>new Node(tag),createTextNode:text=>({textContent:text}),querySelector:selector=>selector==='.notice'?notice:null,querySelectorAll:()=>all.filter(node=>node.dataset.requiresPrinter)},fetch};
  vm.createContext(context);
  const source=readFileSync(join(__dirname,'app.js'),'utf8').replace(/\nrefresh\(\); setInterval\(refresh, 5000\);\s*$/, '');
  vm.runInContext(source,context);
  context.refresh=async()=>{ context.refreshed=true; };
  vm.runInContext('latestPrinter={ams_slots:[{ams_id:0,slot:1,present:true,stale:false,material:"PLA",name:"PLA Basic",color:"000000FF"}]}; liveEnabled=true;',context);
  const job={id:'test-job',name:'model.gcode.3mf',analysis:{filaments:[{material:'PLA',profile:'PLA Basic',color:'#000000'}]}};
  const event=button=>({preventDefault(){},submitter:button});
  return {context,nodes,dialog,form,cancel,submit,job,event,all,Node};
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


test('slicing filament is preselected and color differences still need acceptance',async()=>{
  const view=setup(async()=>{throw new Error('No approval');});
  view.job.slice_options={ams_id:0,slot:1};
  view.job.analysis.filaments[0].color='#FFFFFF';
  await view.context.approve(view.job);
  assert.equal(view.nodes.get('ams-choice').value,'{"ams_id":0,"slot":1}');
  assert.equal(view.nodes.get('accept-differences').required,true);
  assert.notEqual(view.nodes.get('accept-differences').checked,true);
});

test('unknown profile is handled without throwing and blocks physical printing',async()=>{
  const view=setup(async()=>{throw new Error('No approval');});
  view.job.analysis.filaments[0].profile=null;
  await choose(view);
  const mode=view.nodes.get('execution-mode');mode.value='live';mode.onchange();
  assert.equal(view.submit.disabled,true);
});

function queuedJob(extra={}) {
  return {id:'queued-job',name:'model.gcode.3mf',created:100,state:'queued',analysis:{supported:true,filaments:[{material:'PLA',profile:'PLA Basic'}]},...extra};
}

test('unchanged polling preserves the row, focused control and expanded diagnostics',()=>{
  const view=setup(async()=>{});
  const job=queuedJob({receipt:JSON.stringify({diagnostics:{exit_code:0}})});
  const data={jobs:[job],agent_online:true};
  view.context.renderJobs(data,true);
  const row=view.nodes.get('jobs').children[0];
  const diagnostic=row.children[0].children.find(node=>node.tag==='details');
  diagnostic.open=true;const button=row.children.find(node=>node.tag==='button');button.focus();
  view.context.renderJobs(JSON.parse(JSON.stringify(data)),true);
  assert.equal(view.nodes.get('jobs').children[0],row);
  assert.equal(diagnostic.open,true);
  assert.equal(button.focused,true);
});

test('completed jobs move to history while dispatched jobs cannot be closed',()=>{
  const view=setup(async()=>{});
  view.context.renderJobs({jobs:[queuedJob({state:'dispatched'}),queuedJob({id:'done',state:'resolved'})],agent_online:true},true);
  assert.equal(view.nodes.get('jobs').children.length,1);
  assert.equal(view.nodes.get('history-jobs').children.length,1);
  assert.equal(view.nodes.get('jobs').children[0].children.filter(node=>node.tag==='button').length,0);
  assert.equal(view.nodes.get('count').textContent,'1 job');
});

test('offline updates disable approval and recovery enables it again',async()=>{
  let offline=false;
  const data={jobs:[queuedJob()],agent_online:true,live_enabled:false,last_seen:100,pi:{printer:{connected:true,stale:false,state:'IDLE',ams_slots:[]}}};
  const view=setup(async()=>{if(offline)throw new Error('offline');return {ok:true,json:async()=>data};});
  await view.context.refreshStatus();
  const original=view.nodes.get('jobs').children[0].children.find(node=>node.tag==='button');
  assert.equal(original.disabled,false);
  offline=true;await view.context.refreshStatus();assert.equal(original.disabled,true);
  offline=false;await view.context.refreshStatus();
  assert.equal(view.nodes.get('jobs').children[0].children.find(node=>node.tag==='button').disabled,false);
});

test('upload sends UTF-8 filenames in an ASCII-safe header',async()=>{
  let sent;
  const view=setup(async(path,options)=>{sent={path,...options};return {ok:true,json:async()=>({id:'upload-id'})};});
  const name='Jason & Jamie 🐼.GCODE.3MF';
  view.nodes.get('file').files=[{name,size:100}];
  await view.nodes.get('upload').listeners.submit({preventDefault(){}});
  assert.equal(sent.headers['X-Filename-Encoded'],encodeURIComponent(name));
  assert.equal(view.nodes.get('upload').wasReset,true);
});

test('overlapping status polls coalesce and forced refresh waits for a new response',async()=>{
  const view=setup(async()=>{});
  // setup replaces refresh for dialog tests; restore the actual polling wrapper.
  const source=readFileSync(join(__dirname,'app.js'),'utf8');
  const start=source.indexOf('function refresh(force=false)');
  const end=source.indexOf('\nasync function refreshStatus()',start);
  vm.runInContext(source.slice(start,end),view.context);
  let calls=0;const completions=[];
  view.context.refreshStatus=()=>{calls++;return new Promise(resolve=>completions.push(resolve));};
  const first=view.context.refresh();
  assert.equal(view.context.refresh(),first);
  const forced=view.context.refresh(true);assert.equal(calls,1);
  completions.shift()();await first;await Promise.resolve();
  assert.equal(calls,2);
  completions.shift()();await forced;
});
