const element = id => document.getElementById(id);
const labels = {queued:'Afventer godkendelse',approved:'Klar til Pi',dispatched:'Sendt til Pi',submitted:'Driver har accepteret printkommandoen – afventer kontrol',simulated:'Simulering gennemført',unknown:'Kræver gennemgang',resolved:'Gennemgået og lukket'};
Object.assign(labels,{model:'Klar til slicing',slice_queued:'Afventer slicer',slicing:'Slicer modellen',slice_failed:'Slicing fejlede'});
let latestPrinter = null;
let liveEnabled = false;
let modelJobs = [];
let selectedModel = null;
let previewWidget = null;
let previewKey = '';
let previewRequest = 0;
let previewController = null;
let latestStatus = null;
let refreshing = null;
let sliceSlotsKey = '';
let sliceSlotControls=[];
const jobRows = new Map();
const printerLabels = {IDLE:'Klar til print',RUNNING:'Printer',PREPARE:'Klargør printer',PAUSE:'Print sat på pause',FINISH:'Print afsluttet – kontrollér byggepladen',FAILED:'Print afbrudt – kontrollér printeren',SLICING:'Klargør print',unknown:'Status ukendt'};
function queueWaitMessage(job, jobs, online) {
  if (job.state !== 'approved') return '';
  const blocker = jobs.find(item => ['submitted','unknown','dispatched'].includes(item.state));
  if (blocker?.state === 'dispatched') return `Venter på jobbet “${blocker.name}”, som er sendt til Pi’en.`;
  if (blocker) return `Venter på kontrol af “${blocker.name}”. Når printeren er færdig, skal printet fjernes og byggepladen kontrolleres. Bekræft derefter på det tidligere job for at frigive køen.`;
  return online ? 'Godkendt – afventer afhentning fra Pi’en.' : 'Godkendt – venter på, at Pi’en kommer online.';
}
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Handlingen kunne ikke gennemføres');
  return body;
}
async function approve(job) {
  element('approvalName').textContent = job.name;
  const dialog=element('approval'), form=dialog.querySelector('form');
  const cancel=dialog.querySelector('[value="cancel"]'), submit=dialog.querySelector('[value="approve"]');
  cancel.formNoValidate=true;
  let submitting=false, profileMismatch=false, versionSupported=true;
  document.getElementById('print-choice')?.remove();
  const controls=document.createElement('div');controls.id='print-choice';
  const modeLabel=document.createElement('label');modeLabel.textContent='Handling';modeLabel.htmlFor='execution-mode';
  const mode=document.createElement('select');mode.id='execution-mode';
  for(const [value,text] of [['simulate','Simulér – ingen fysisk printstart'],['live','Start fysisk print']]) {
    const option=document.createElement('option');option.value=value;option.textContent=text;option.disabled=value==='live'&&!liveEnabled;mode.append(option);
  }
  const physical=document.createElement('input');physical.type='text';physical.id='physical-confirmation';physical.autocomplete='off';physical.pattern='START PRINT';
  physical.oninput=()=>physical.setCustomValidity(physical.value&&physical.value!=='START PRINT'?'Skriv hele teksten START PRINT med mellemrum.':'');
  const physicalLabel=document.createElement('label');physicalLabel.htmlFor=physical.id;physicalLabel.textContent='Skriv START PRINT for at godkende fysisk printstart';
  const heading=document.createElement('p');
  const filaments=job.analysis.filaments.map((item,index)=>({...item,id:item.id??index+1}));
  heading.textContent=`Filen bruger ${filaments.length} ${filaments.length===1?'filament':'filamenter'}. Vælg AMS-plads for hver farve.`;
  const fields=[];
  controls.append(modeLabel,mode,heading);
  const previous=job.selection||job.slice_options;
  const preferred=previous?.filaments|| (previous?[{...previous,filament_id:previous.filament_id??filaments[0].id}]:[]);
  for(const filament of filaments) {
    const label=document.createElement('label');const choice=document.createElement('select');
    choice.id=filaments.length===1?'ams-choice':`ams-choice-${filament.id}`;choice.required=true;label.htmlFor=choice.id;
    label.textContent=`Filament ${filament.id}: ${filament.material} · ${filament.profile||'Ukendt profil'} · ${filament.color||'Ukendt farve'}`;
    const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='Vælg filament…';choice.append(placeholder);
    const eligible=[];
    for(const slot of latestPrinter?.ams_slots||[]) {
      if(slot.stale||slot.present!==true||slot.material?.toUpperCase()!==filament.material.toUpperCase())continue;
      const option=document.createElement('option');option.value=JSON.stringify({ams_id:slot.ams_id,slot:slot.slot});
      option.textContent=`AMS ${slot.ams_id+1} · Plads ${slot.slot}: ${slot.name||slot.material} · #${slot.color||'?'} · ${slot.remaining_percent??'?'} %`;choice.append(option);eligible.push(slot);
    }
    const saved=preferred.find(item=>item.filament_id===filament.id);
    const exact=eligible.filter(slot=>slot.color?.slice(0,6).toUpperCase()===filament.color?.replace('#','').slice(0,6).toUpperCase()&&slot.name&&(filament.profile||'').toUpperCase().includes(slot.name.toUpperCase()));
    const suggestion=saved||(exact.length===1?exact[0]:null);
    if(suggestion) {
      const value=JSON.stringify({ams_id:suggestion.ams_id,slot:suggestion.slot});
      if(Array.from(choice.children).some(option=>option.value===value))choice.value=value;
    }
    fields.push({filament,choice});controls.append(label,choice);
  }
  const hint=document.createElement('p');hint.textContent='Alle anvendte filamenter skal være valgt. Materialet skal passe til filen; Pi’en kontrollerer valgene igen før start.';
  const warning=document.createElement('p');warning.className='plan-warning';
  const differences=document.createElement('input');differences.type='checkbox';differences.id='accept-differences';
  const differencesLabel=document.createElement('label');differencesLabel.className='check-label';differencesLabel.append(differences,document.createTextNode('Jeg accepterer de viste farve- eller profilforskelle for alle filamenter.'));
  const previewChecked=document.createElement('input');previewChecked.type='checkbox';previewChecked.required=Boolean(job.model_info||job.preview_ready);
  const previewLabel=document.createElement('label');previewLabel.className='check-label';previewLabel.hidden=!previewChecked.required;
  previewLabel.append(previewChecked,document.createTextNode('Jeg har kontrolleret det slicede preview, farvefordelingen og indstillingerne.'));
  const bed=document.createElement('input');bed.type='checkbox';bed.required=true;
  const bedLabel=document.createElement('label');bedLabel.className='check-label';bedLabel.append(bed,document.createTextNode('Jeg har kontrolleret, at byggepladen er tom.'));
  const errorMessage=document.createElement('div');errorMessage.id='approval-error';errorMessage.className='plan-warning';errorMessage.setAttribute('role','alert');errorMessage.tabIndex=-1;
  const check=()=>{
    differences.checked=false;profileMismatch=false;
    const warnings=[];
    for(const {filament,choice} of fields) {
      const selected=choice.value?JSON.parse(choice.value):null;
      const slot=latestPrinter?.ams_slots?.find(item=>item.ams_id===selected?.ams_id&&item.slot===selected?.slot);
      if(slot&&filament.color?.replace('#','').toUpperCase().slice(0,6)!==slot.color?.toUpperCase().slice(0,6))warnings.push(`Filament ${filament.id}: farven afviger fra filen.`);
      if(slot&&(!slot.name||!(filament.profile||'').toUpperCase().includes(slot.name.toUpperCase()))) {
        profileMismatch=true;warnings.push(`Filament ${filament.id}: profilkonflikt mellem ${filament.profile||'ukendt profil'} og ${slot.name||'ukendt filament'}.`);
      }
    }
    const needed=filaments.length>1||filaments[0].id!==1?3:1;
    versionSupported=(latestStatus?.pi?.plan_version??2)>=needed;
    warning.textContent=(!versionSupported?'Opdatér Pi-agent og printservice, før dette flerfarvejob kan godkendes. ': '')+(warnings.join(' ')||'Materiale, profil og farve matcher de tilgængelige oplysninger.');
    differences.required=warnings.length>0;differencesLabel.hidden=!warnings.length;
    submit.disabled=!versionSupported||mode.value==='live'&&profileMismatch;
  };
  for(const field of fields)field.choice.onchange=check;
  mode.onchange=()=>{
    const live=mode.value==='live';physical.required=live;physical.hidden=!live;physicalLabel.hidden=!live;physical.value='';physical.setCustomValidity('');
    submit.textContent=live?'Start fysisk print':'Godkend simulering';
    dialog.querySelector('h2').textContent=live?'Bekræft fysisk printstart':'Godkend simulering';
    dialog.querySelector('form > p:last-of-type').textContent=live?'Printeren kan begynde klargøring og print straks efter godkendelsen.':'Pi’en tester forløbet uden fysisk printstart.';
    check();
  };
  controls.append(hint,warning,differencesLabel,previewLabel,bedLabel,physicalLabel,physical,errorMessage);
  element('approvalName').after(controls);
  mode.onchange();dialog.returnValue='cancel';dialog.showModal();
  dialog.oncancel=event=>{if(submitting)event.preventDefault();};
  form.onsubmit=async event=>{
    event.preventDefault();if(submitting)return;
    if(event.submitter?.value==='cancel'){dialog.close('cancel');return;}
    if(!versionSupported||mode.value==='live'&&profileMismatch)return;
    if(!form.reportValidity())return;
    const selections=fields.map(({filament,choice})=>({filament_id:filament.id,...JSON.parse(choice.value)}));
    submitting=true;submit.disabled=true;cancel.disabled=true;errorMessage.textContent='Godkendelsen sendes…';
    try {
      await api(`/api/jobs/${job.id}/approve`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
        confirm_job_id:job.id,...(selections.length===1?selections[0]:{filaments:selections}),bed_clear:bed.checked,
        preview_checked:previewChecked.checked,accept_differences:differences.checked,execution_mode:mode.value,confirm_physical:physical.value
      })});
      dialog.close('approve');await refresh(true);
    }catch(error){errorMessage.textContent=error.message;errorMessage.focus();}
    finally{submitting=false;submit.disabled=!versionSupported||mode.value==='live'&&profileMismatch;cancel.disabled=false;}
  };
}
function renderAMS(printer, online) {
  const slots = Array.isArray(printer?.ams_slots) ? printer.ams_slots : [];
  const cards = slots.map(slot => {
    const fresh = online && printer.connected && !slot.stale;
    const card = document.createElement('article'); card.className = 'ams-card';
    const heading = document.createElement('h3'); heading.textContent = `AMS ${slot.ams_id + 1} · Plads ${slot.slot}`;
    const swatch = document.createElement('div'); swatch.className = 'filament-swatch';
    const color = fresh && slot.present !== false && /^[0-9A-F]{8}$/i.test(slot.color || '') ? slot.color : null;
    if (color) swatch.style.backgroundColor = `#${color}`;
    swatch.setAttribute('aria-label', color ? `Farvekode #${color}` : 'Farve ukendt');
    const name = document.createElement('strong');
    name.textContent = !fresh ? 'Status ukendt' : slot.present === false ? 'Tom plads' : slot.name || slot.material || 'Ukendt filament';
    const details = document.createElement('p');
    details.textContent = !fresh ? 'Afventer friske oplysninger' : slot.present === false ? 'Intet filament registreret' : `${slot.material || 'Ukendt materiale'} · ${color ? '#' + color : 'Ukendt farve'}`;
    const remain = document.createElement('span'); remain.className = 'ams-remaining';
    remain.textContent = fresh && slot.present !== false && slot.remaining_percent != null ? `Ca. ${slot.remaining_percent} % tilbage` : 'Restmængde ukendt';
    card.append(heading, swatch, name, details, remain); return card;
  });
  element('ams-slots').replaceChildren(...cards);
  if (!cards.length) element('ams-slots').textContent = online ? 'Afventer AMS-oplysninger fra printeren.' : 'AMS-status utilgængelig – Pi offline.';
}
function renderJobRow(job, data, fresh) {
  const row = document.createElement('div'); row.className = 'job';
  const details = document.createElement('div');
  const name = document.createElement('strong'); name.textContent = job.name;
  const state = document.createElement('small'); state.textContent = `${labels[job.state] || job.state} · ${new Date(job.created * 1000).toLocaleString('da-DK')}`;
  details.append(name, state); row.append(details);
  const waiting = queueWaitMessage(job, data.jobs, data.agent_online);
  if (waiting) {
    state.textContent = `${waiting.startsWith('Venter på') ? 'Godkendt · venter i kø' : 'Godkendt'} · ${new Date(job.created * 1000).toLocaleString('da-DK')}`;
    const notice = document.createElement('p'); notice.className = 'queue-notice'; notice.textContent = waiting; details.append(notice);
  }
  if (job.model_info || job.preview_ready) {
    const modelButton=document.createElement('button'); modelButton.className='secondary'; modelButton.textContent=job.preview_ready?'Vis model og slicet preview':'Åbn model og slice'; modelButton.onclick=()=>openModel(job.id); row.append(modelButton);
    const size=document.createElement('small'); size.textContent=job.model_info?.dimensions_mm?`Model: ${job.model_info.dimensions_mm.join(' × ')} mm`:job.model_info?'3MF-projekt med farver og dele':'Færdigslicet 3MF'; details.append(size);
  }
  const analysis = document.createElement('small');
  analysis.textContent = job.analysis?.supported ? `${job.analysis.filaments.length} anvendte filamenter · ${job.analysis.filaments.map(item=>`#${item.id} ${item.material} ${item.color||''}`).join(' · ')}` : job.analysis?.reason || 'Filanalyse ukendt';
  details.append(analysis);
  if (job.selection) { const selection = document.createElement('small'); selection.textContent = `Valgt: ${(job.selection.filaments||[job.selection]).map(item=>`filament ${item.filament_id} → AMS ${item.ams_id+1}, plads ${item.slot}`).join(' · ')}`; details.append(selection); }
  if (job.execution_mode) { const mode = document.createElement('small'); mode.textContent=job.execution_mode==='live'?'Handling: fysisk print':'Handling: simulering'; details.append(mode); }
  if (job.receipt) {
    const receipt=JSON.parse(job.receipt); const diagnostic=receipt.diagnostics;
    if (diagnostic && (diagnostic.exit_code != null || diagnostic.error_type || diagnostic.events?.length)) {
      const report=document.createElement('details'); const heading=document.createElement('summary'); heading.textContent='Tekniske fejlkoder';
      const text=document.createElement('pre'); text.textContent=JSON.stringify(diagnostic,null,2); report.append(heading,text); details.append(report);
    }
  }
  if (job.state === 'queued') { const button = document.createElement('button'); button.textContent = 'Vælg filament og godkend'; button.disabled = !job.analysis?.supported || !fresh; button.dataset.requiresPrinter='true'; button.onclick = () => approve(job); row.append(button); }
  if (['unknown','submitted'].includes(job.state)) {
    const button = document.createElement('button'); button.className = 'secondary'; button.textContent = job.state === 'submitted' ? 'Bekræft tom byggeplade og frigiv kø' : 'Luk efter kontrol';
    const notice = document.createElement('p'); notice.className = 'queue-notice';
    notice.textContent = job.state === 'submitted' ? 'Køen er sat på pause til kontrol af byggepladen. Vent til printeren er færdig, fjern printet, og kontrollér at pladen er tom. Allerede godkendte job kan starte automatisk, når du frigiver køen.' : job.state === 'unknown' ? 'Dette job kræver kontrol på printeren og holder køen tilbage. Kontrollér printeren og byggepladen, før du lukker jobbet.' : 'Jobbet er sendt til Pi’en. Afvent resultatet; luk kun manuelt efter kontrol på printeren.';
    details.append(notice);
    button.onclick = async () => {
      if (!window.confirm('Er printeren færdig eller stoppet, og har du fjernet printet og kontrolleret, at byggepladen er tom? Når du frigiver køen, kan det næste allerede godkendte job starte automatisk.')) return;
      try {
        await api(`/api/jobs/${job.id}/resolve`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_job_id:job.id,printer_checked_idle:true})});
        await refresh(true);
      } catch (error) { element('message').textContent = error.message; }
    };
    row.append(button);
  }
  if (job.state === 'dispatched') { const notice=document.createElement('p'); notice.className='queue-notice'; notice.textContent='Pi’en behandler jobbet. Afvent kvitteringen, før jobbet kan lukkes.'; details.append(notice); }
  return row;
}

function syncJobRows(container, rows, emptyText) {
  const wanted = new Set(rows);
  for (const child of Array.from(container.childNodes)) if (!wanted.has(child)) child.remove();
  rows.forEach((row,index) => { if (container.children[index] !== row) container.insertBefore(row,container.children[index] || null); });
  if (!rows.length && container.textContent !== emptyText) container.textContent = emptyText;
}

function renderJobs(data, fresh) {
  const active=[], history=[];
  const present = new Set(data.jobs.map(job=>job.id));
  for (const id of jobRows.keys()) if (!present.has(id)) jobRows.delete(id);
  for (const job of data.jobs) {
    const signature=JSON.stringify([job,fresh,queueWaitMessage(job,data.jobs,data.agent_online)]);
    let cached=jobRows.get(job.id);
    if (!cached || cached.signature!==signature) {
      cached={signature,row:renderJobRow(job,data,fresh)};jobRows.set(job.id,cached);
    }
    (['resolved','simulated'].includes(job.state)?history:active).push(cached.row);
  }
  element('count').textContent=`${active.length} job`;
  syncJobRows(element('jobs'),active,'Køen er tom. Upload en model for at komme i gang.');
  if (element('history-jobs')) {
    element('history-count').textContent=`${history.length} job`;
    syncJobRows(element('history-jobs'),history,'Ingen afsluttede job endnu.');
  }
}

function refresh(force=false) {
  if (refreshing) return force ? refreshing.then(()=>refresh(true)) : refreshing;
  refreshing=refreshStatus().finally(()=>{refreshing=null;});
  return refreshing;
}

async function refreshStatus() {
  try {
    const data = await api('/api/status');
    latestStatus=data;
    const printer = data.pi?.printer;
    modelJobs = data.jobs;
    liveEnabled = data.live_enabled === true;
    document.querySelector('.notice').textContent = liveEnabled ? 'Fysisk print er aktiveret. Hvert job kræver valg af handling og særskilt bekræftelse.' : 'Simulering er aktiv. Godkendte job gennemløber køen uden at starte fysiske print.';
    latestPrinter = data.agent_online ? printer : null;
    renderAMS(printer, data.agent_online);
    const fresh = data.agent_online && printer?.connected && !printer.stale;
    if (element('model-workspace')) refreshModels(data);
    element('connection').textContent = data.agent_online ? 'Pi online' : 'Pi offline';
    element('connection').className = `badge ${data.agent_online ? 'online' : ''}`;
    element('state').textContent = fresh ? (printerLabels[printer.state] || 'Status ukendt') : 'Afventer frisk printerstatus';
    element('updated').textContent = data.last_seen ? `Pi sidst set ${new Date(data.last_seen * 1000).toLocaleString('da-DK')}` : 'Forbind Pi-agenten for at se printerstatus';
    for (const [id, key, unit] of [['nozzle','nozzle_c',' °C'],['bed','bed_c',' °C'],['progress','progress_percent',' %']]) {
      element(id).textContent = fresh && printer[key] != null ? printer[key] + unit : '—';
    }
    element('count').textContent = `${data.jobs.length} job`;
    renderJobs(data, fresh);
  } catch (error) {
    latestPrinter=null; liveEnabled=false; latestStatus=null;
    for(const cached of jobRows.values()) cached.signature=null;
    renderAMS(null, false);
    for (const button of document.querySelectorAll('[data-requires-printer], #approve-model, #slice-button, #retry-slice')) button.disabled=true;
    element('connection').textContent = 'Server utilgængelig';
    element('connection').className = 'badge';
    element('state').textContent = 'Status ukendt';
    for (const id of ['nozzle','bed','progress']) element(id).textContent = '—';
  }
}
element('upload').addEventListener('submit', async event => {
  event.preventDefault(); const file = element('file').files[0];
  const isStl=file?.name.toLowerCase().endsWith('.stl');
  const isModel=isStl||file?.name.toLowerCase().endsWith('.3mf')&&!file?.name.toLowerCase().endsWith('.gcode.3mf');
  if (!file || (!isModel && !file.name.toLowerCase().endsWith('.gcode.3mf')) || file.size > (isStl?50:100) * 1024 ** 2) { element('message').textContent = 'Vælg STL (maks. 50 MiB), 3MF-projekt eller .gcode.3mf (maks. 100 MiB).'; return; }
  element('uploadButton').disabled = true; element('message').textContent = 'Uploader…';
  try {
    const uploaded=await api('/api/jobs', {method:'POST', headers:{'X-Filename-Encoded':encodeURIComponent(file.name)},body:file});
    element('message').textContent = uploaded.state==='model'?'Modellen er uploadet. Vælg filament og klargør den.':'Filen er i køen og afventer godkendelse.'; element('upload').reset(); await refresh(true);
    if (uploaded.state==='model'||modelJobs.find(item=>item.id===uploaded.id)?.preview_ready) await openModel(uploaded.id);
  } catch (error) { element('message').textContent = error.message; }
  finally { element('uploadButton').disabled = false; }
});

async function showGeometry(job,kind) {
  const requestId=++previewRequest;
  previewController?.abort(); previewController=new AbortController();
  const controller=previewController;
  if (previewWidget) {previewWidget.dispose();previewWidget=null;}
  element('model-preview').textContent='Indlæser 3D-preview…';
  element('layer-controls').hidden=true;
  try {
    const module=await import('/preview.js');
    const widget=await module.mount(element('model-preview'),`/api/jobs/${job.id}/${kind==='sliced'?'preview':'model'}`,kind,controller.signal);
    if (requestId!==previewRequest) {widget.dispose();return;}
    previewWidget=widget;
    if (kind==='sliced') {
      const data=widget.data;
      element('layer-controls').hidden=false;
      const slider=element('preview-layer'); slider.max=data.layers.length; slider.value=data.layers.length;
      const update=()=>{const index=Number(slider.value)-1;widget.setLayer(index);element('layer-label').textContent=`Lag ${index+1} / ${data.layers.length} · Z ${data.layers[index].z} mm`;};
      slider.oninput=update; update();
      element('slice-estimate').textContent=`Ca. ${Math.ceil(data.estimated_seconds/60)} min · ${data.filament_grams} g filament${data.omitted_segments?' · Preview er forenklet; enkelte baner er udeladt.':''}`;
    }
  } catch (error) {if(requestId===previewRequest)element('model-preview').textContent='3D-preview kunne ikke vises: '+error.message;}
}

async function openModel(jobId) {
  const job=modelJobs.find(item=>item.id===jobId); if(!job)return;
  selectedModel=jobId; previewKey=''; element('model-workspace').hidden=false;
  element('model-name').textContent=`${job.name}${job.model_info?.dimensions_mm?' · '+job.model_info.dimensions_mm.join(' × ')+' mm':job.model_info?' · 3MF-projekt':' · færdigslicet 3MF'}`;
  sliceSlotsKey=''; updateSliceSlots(job.slice_options);
  if(job.slice_options){element('slice-layer').value=job.slice_options.layer_height;element('slice-infill').value=job.slice_options.infill;}
  if(element('slice-quality')) element('slice-quality').value=element('slice-layer').value;
  previewKey=job.preview_ready?job.sha256:'model';
  if(latestStatus) refreshModels(latestStatus);
  await showGeometry(job,job.preview_ready?'sliced':job.model_info?.kind==='3mf'?'project':'model');
  element('model-workspace').scrollIntoView({behavior:'smooth',block:'start'});
}

function updateSliceSlots(preferred) {
  const job=modelJobs.find(item=>item.id===selectedModel);
  const container=element('slice-filaments');if(!container||!job?.model_info)return;
  const required=job.model_info.filaments||[{id:1,material:'PLA'}];
  const slots=(latestPrinter?.connected&&!latestPrinter.stale?latestPrinter.ams_slots:[])||[];
  const available=slots.filter(slot=>slot.present===true&&!slot.stale&&slot.material==='PLA'&&['PLA Basic','PLA Matte','PLA Translucent'].includes(slot.name));
  const key=JSON.stringify([job.id,required,available.map(slot=>[slot.ams_id,slot.slot,slot.name,slot.color])]);
  if(key===sliceSlotsKey&&!preferred)return;
  sliceSlotsKey=key;
  const previous=preferred?.filaments||(preferred?[{...preferred,filament_id:1}]:sliceSlotControls.filter(item=>item.choice.value).map(item=>({filament_id:item.id,...JSON.parse(item.choice.value)})));
  container.replaceChildren();sliceSlotControls=[];
  for(const filament of required) {
    const label=document.createElement('label'),choice=document.createElement('select');
    choice.id=sliceSlotControls.length===0?'slice-slot':`slice-slot-${filament.id}`;choice.required=true;label.htmlFor=choice.id;
    label.textContent=`Filament ${filament.id} · ${filament.material}${filament.color?' · '+filament.color:''}`;
    const placeholder=document.createElement('option');placeholder.value='';placeholder.textContent='Vælg AMS-plads…';choice.append(placeholder);
    const eligible=available.filter(slot=>slot.material===filament.material);
    for(const slot of eligible) {
      const option=document.createElement('option');option.value=JSON.stringify({ams_id:slot.ams_id,slot:slot.slot});
      option.textContent=`AMS ${slot.ams_id+1}, plads ${slot.slot} · ${slot.name} · #${slot.color?.slice(0,6)||'?'}`;choice.append(option);
    }
    const saved=previous.find(item=>item.filament_id===filament.id);
    const exact=eligible.filter(slot=>slot.color?.slice(0,6).toUpperCase()===filament.color?.replace('#','').slice(0,6).toUpperCase());
    const suggestion=saved||(exact.length===1?exact[0]:required.length===1?eligible[0]:null);
    if(suggestion) {
      const value=JSON.stringify({ams_id:suggestion.ams_id,slot:suggestion.slot});
      if(Array.from(choice.children).some(option=>option.value===value))choice.value=value;
    }
    choice.onchange=()=>{if(latestStatus)refreshModels(latestStatus);};
    container.append(label,choice);sliceSlotControls.push({id:filament.id,choice});
  }
}

function readSliceChoices() {
  return sliceSlotControls.filter(item=>item.choice.value).map(item=>({filament_id:item.id,...JSON.parse(item.choice.value)}));
}

function refreshModels(data) {
  element('slicer-status').textContent=data.slicer_online?'Slicer online':'Slicer offline / ikke konfigureret';
  const job=data.jobs.find(item=>item.id===selectedModel);if(!job)return;
  updateSliceSlots();
  if(element('slice-settings'))element('slice-settings').hidden=!job.model_info;
  element('show-original').hidden=!job.model_info;
  const fresh=data.agent_online && latestPrinter?.connected && !latestPrinter.stale;
  const busy=['slicing','slice_queued'].includes(job.state);
  const needsPi3=job.analysis?.filaments?.length>1||job.analysis?.filaments?.[0]?.id>1;
  const piCompatible=!needsPi3||data.pi?.plan_version>=3;
  const ready=fresh && data.slicer_online && (job.model_info?.kind!=='3mf'||data.slicer_version>=2) && readSliceChoices().length===(job.model_info?.filaments?.length||1);
  element('slice-button').disabled=busy || !ready || !['model','slice_failed','queued'].includes(job.state);
  if(element('retry-slice')) {element('retry-slice').hidden=!busy;element('retry-slice').disabled=!ready;}
  if(element('show-sliced')) element('show-sliced').disabled=!job.preview_ready;
  const errors={worker_interrupted:'Forbindelsen til sliceren blev afbrudt, eller forsøget udløb. Klargør modellen igen.',timeout:'Klargøringen tog for lang tid. Prøv igen eller brug en enklere model.',invalid_result:'Sliceren leverede ikke en brugbar printfil. Kontrollér modellen og prøv igen.',slicer_failed:'Modellen kunne ikke klargøres. Kontrollér størrelse og orientering, og prøv igen.'};
  element('slice-message').textContent=job.state==='slicing'?'Klargør modellen…':job.state==='slice_queued'?'Venter på slicer…':job.state==='slice_failed'?(errors[job.slice_error] || errors.slicer_failed):job.preview_ready?'Modellen er klar. Kontrollér printbanerne og gennemse jobbet nedenfor.':!fresh?'Afventer frisk printerstatus og filamentoplysninger.':!data.slicer_online?'Sliceren er offline. Prøv igen, når forbindelsen er tilbage.':'';
  element('download-sliced').hidden=!job.preview_ready;
  element('download-sliced').href=`/api/jobs/${job.id}/sliced-file`;element('download-sliced').download=job.name.toLowerCase().endsWith('.gcode.3mf')?job.name:job.name.replace(/\.(stl|3mf)$/i,'.gcode.3mf');
  if(element('review-print')) {
    element('review-print').hidden=job.state!=='queued' || !job.preview_ready;
    element('approve-model').disabled=!fresh || !job.analysis?.supported || !piCompatible;
    element('approve-model-hint').textContent=!piCompatible?'Opdatér Pi-agent og printservice til flerfarveprint.':!fresh?'Afventer frisk printerstatus før godkendelse.':!job.analysis?.supported?(job.analysis?.reason || 'Filen kan ikke godkendes.'):'';
  }
  const key=job.preview_ready?job.sha256:'model';
  if(key!==previewKey){previewKey=key;showGeometry(job,job.preview_ready?'sliced':job.model_info?.kind==='3mf'?'project':'model');}
}

if(element('slice-form')) {
  element('file').accept='.stl,.3mf';
  element('upload').previousElementSibling.textContent='Upload STL, et Bambu Studio 3MF-projekt med farver og dele, eller en færdigslicet .gcode.3mf. Upload starter aldrig print.';
  document.querySelector('.model-layout > div > p').textContent='Drej visningen med fingeren eller musen · zoom med to fingre eller scroll. Modellens printorientering ændres ikke, når du drejer visningen.';
  document.querySelector('label[for="file"]').textContent='STL (maks. 50 MiB) eller 3MF / .gcode.3mf (maks. 100 MiB)';
  if(element('slice-quality')) {
    element('slice-quality').onchange=()=>{element('slice-layer').value=element('slice-quality').value;};
    element('slice-layer').onchange=()=>{element('slice-quality').value=element('slice-layer').value;};
  }
  if(element('approve-model')) element('approve-model').onclick=()=>{const job=modelJobs.find(item=>item.id===selectedModel);if(job?.state==='queued')approve(job);};
  element('slice-form').onsubmit=async event=>{
    event.preventDefault(); const job=modelJobs.find(item=>item.id===selectedModel);if(!job)return;
    element('slice-button').disabled=true;
    try {
      await api(`/api/jobs/${job.id}/slice`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({filaments:readSliceChoices(),layer_height:Number(element('slice-layer').value),infill:Number(element('slice-infill').value)})});
      await refresh(true);
    } catch(error){element('slice-message').textContent=error.message;element('slice-button').disabled=false;}
  };
  element('show-original').onclick=()=>{const job=modelJobs.find(item=>item.id===selectedModel);if(job){previewKey=job.preview_ready?job.sha256:'model';showGeometry(job,job.model_info?.kind==='3mf'?'project':'model');}};
  const slicedButton=document.createElement('button');slicedButton.type='button';slicedButton.id='show-sliced';slicedButton.className='secondary';slicedButton.textContent='Vis printbaner';
  slicedButton.onclick=()=>{const job=modelJobs.find(item=>item.id===selectedModel);if(job?.preview_ready)showGeometry(job,'sliced');};element('show-original').after(slicedButton);
  const retry=document.createElement('button');retry.type='button';retry.id='retry-slice';retry.className='secondary';retry.textContent='Genstart slice-forsøg';retry.hidden=true;
  retry.onclick=async()=>{if(window.confirm('Genstart slicing? Et ældre resultat ignoreres. Det starter ikke et print.'))await element('slice-form').onsubmit({preventDefault(){}});};element('slice-button').after(retry);
}
refresh(); setInterval(refresh, 5000);

