const element = id => document.getElementById(id);
const labels = {queued:'Afventer godkendelse',approved:'Klar til Pi',dispatched:'Sendt til Pi',submitted:'Driver har accepteret printkommandoen – afventer kontrol',simulated:'Simulering gennemført',unknown:'Kræver gennemgang',resolved:'Gennemgået og lukket'};
let latestPrinter = null;
let liveEnabled = false;
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Handlingen kunne ikke gennemføres');
  return body;
}
async function approve(job) {
  element('approvalName').textContent = job.name;
  const dialog = element('approval');
  const form = dialog.querySelector('form');
  const cancel = dialog.querySelector('[value="cancel"]');
  cancel.formNoValidate = true;
  let submitting = false;
  let profileMismatch = false;
  const oldControls = document.getElementById('print-choice');
  if (oldControls) oldControls.remove();
  const controls = document.createElement('div'); controls.id = 'print-choice';
  const modeLabel = document.createElement('label'); modeLabel.textContent = 'Handling'; modeLabel.htmlFor = 'execution-mode';
  const mode = document.createElement('select'); mode.id = 'execution-mode';
  for (const [value,text] of [['simulate','Simulér – ingen fysisk printstart'],['live','Start fysisk print']]) {
    const option = document.createElement('option'); option.value=value; option.textContent=text; option.disabled=value==='live' && !liveEnabled; mode.append(option);
  }
  const physical = document.createElement('input'); physical.type='text'; physical.id='physical-confirmation'; physical.autocomplete='off'; physical.pattern='START PRINT';
  physical.oninput = () => physical.setCustomValidity(physical.value && physical.value !== 'START PRINT' ? 'Skriv hele teksten START PRINT med mellemrum.' : '');
  const physicalLabel = document.createElement('label'); physicalLabel.htmlFor=physical.id; physicalLabel.textContent='Skriv START PRINT for at godkende fysisk printstart';
  physical.hidden=true; physicalLabel.hidden=true;
  const submit = dialog.querySelector('[value="approve"]');
  mode.onchange = () => {
    const live = mode.value==='live'; physical.required=live; physical.hidden=!live; physicalLabel.hidden=!live; physical.value=''; physical.setCustomValidity('');
    submit.textContent=live?'Start fysisk print':'Godkend simulering';
    dialog.querySelector('h2').textContent=live?'Bekræft fysisk printstart':'Godkend simulering';
    dialog.querySelector('form > p:last-of-type').textContent=live?'Printeren kan begynde klargøring og print straks efter godkendelsen.':'Pi’en tester forløbet uden fysisk printstart.';
  };
  mode.onchange();
  const filament = job.analysis.filaments[0];
  const requirement = document.createElement('p');
  requirement.textContent = `Filen bruger 1 filament: ${filament.material} · ${filament.profile || 'Ukendt profil'} · ${filament.color || 'Ukendt farve'}`;
  const label = document.createElement('label'); label.textContent = 'Vælg AMS-plads'; label.htmlFor = 'ams-choice';
  const choice = document.createElement('select'); choice.id = 'ams-choice'; choice.required = true;
  const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = 'Vælg filament…'; choice.append(placeholder);
  for (const slot of latestPrinter?.ams_slots || []) {
    if (slot.stale || slot.present !== true || slot.material?.toUpperCase() !== filament.material.toUpperCase()) continue;
    const option = document.createElement('option'); option.value = JSON.stringify({ams_id:slot.ams_id,slot:slot.slot});
    option.textContent = `AMS ${slot.ams_id + 1} · Plads ${slot.slot}: ${slot.name || slot.material} · #${slot.color || '?'} · ${slot.remaining_percent ?? '?'} %`;
    choice.append(option);
  }
  const warning = document.createElement('p'); warning.className = 'plan-warning';
  const differences = document.createElement('input'); differences.type = 'checkbox'; differences.id = 'accept-differences';
  const differencesLabel = document.createElement('label'); differencesLabel.className = 'check-label'; differencesLabel.append(differences, document.createTextNode('Jeg accepterer de viste forskelle i farve eller profil.'));
  choice.onchange = () => {
    differences.checked = false;
    const selected = choice.value ? JSON.parse(choice.value) : null;
    const slot = latestPrinter?.ams_slots.find(item => item.ams_id === selected?.ams_id && item.slot === selected?.slot);
    const warnings = [];
    if (slot && filament.color?.replace('#','').toUpperCase().slice(0,6) !== slot.color?.toUpperCase().slice(0,6)) warnings.push('Farven afviger fra filen.');
    profileMismatch = Boolean(slot && (!slot.name || !filament.profile.toUpperCase().includes(slot.name.toUpperCase())));
    if (profileMismatch) warnings.push(`Profilkonflikt: filen bruger ${filament.profile || 'ukendt profil'}, men pladsen melder ${slot.name || 'ukendt filament'}. Fysisk print kræver en matchende profil.`);
    warning.textContent = warnings.join(' ') || 'Materiale, profil og farve matcher de tilgængelige oplysninger.';
    differences.required = warnings.length > 0; differencesLabel.hidden = !warnings.length;
    submit.disabled = mode.value === 'live' && profileMismatch;
  };
  const modeChanged = mode.onchange;
  mode.onchange = () => { modeChanged(); choice.onchange(); };
  const bed = document.createElement('input'); bed.type = 'checkbox'; bed.required = true;
  const bedLabel = document.createElement('label'); bedLabel.className = 'check-label'; bedLabel.append(bed, document.createTextNode('Jeg har kontrolleret, at byggepladen er tom.'));
  const hint = document.createElement('p'); hint.textContent = 'Kun samme materialetype kan vælges. Tomme eller ukendte pladser vises ikke. Kontrollér den valgte plads før godkendelse.';
  differencesLabel.hidden = true;
  const errorMessage = document.createElement('div'); errorMessage.id = 'approval-error'; errorMessage.className = 'plan-warning'; errorMessage.setAttribute('role','alert'); errorMessage.tabIndex=-1;
  controls.append(modeLabel,mode,requirement, label, choice, hint, warning, differencesLabel, bedLabel,physicalLabel,physical,errorMessage);
  element('approvalName').after(controls);
  dialog.returnValue = 'cancel';
  dialog.showModal();
  dialog.oncancel = event => { if (submitting) event.preventDefault(); };
  form.onsubmit = async event => {
    event.preventDefault();
    if (submitting) return;
    if (event.submitter?.value === 'cancel') { dialog.close('cancel'); return; }
    if (!form.reportValidity()) return;
    submitting=true; submit.disabled=true; cancel.disabled=true; errorMessage.textContent='Godkendelsen sendes…';
    try {
      await api(`/api/jobs/${job.id}/approve`, {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_job_id:job.id,...JSON.parse(choice.value),bed_clear:bed.checked,accept_differences:differences.checked,execution_mode:mode.value,confirm_physical:physical.value})});
      dialog.close('approve');
      await refresh();
    } catch (error) {
      errorMessage.textContent = error.message;
      errorMessage.focus();
    } finally {
      submitting=false; submit.disabled=mode.value==='live' && profileMismatch; cancel.disabled=false;
    }
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
async function refresh() {
  try {
    const data = await api('/api/status');
    const printer = data.pi?.printer;
    liveEnabled = data.live_enabled === true;
    document.querySelector('.notice').textContent = liveEnabled ? 'Fysisk print er aktiveret. Hvert job kræver valg af handling og særskilt bekræftelse.' : 'Simulering er aktiv. Godkendte job gennemløber køen uden at starte fysiske print.';
    latestPrinter = data.agent_online ? printer : null;
    renderAMS(printer, data.agent_online);
    const fresh = data.agent_online && printer?.connected && !printer.stale;
    element('connection').textContent = data.agent_online ? 'Pi online' : 'Pi offline';
    element('connection').className = `badge ${data.agent_online ? 'online' : ''}`;
    element('state').textContent = fresh ? printer.state : 'Afventer frisk printerstatus';
    element('updated').textContent = data.last_seen ? `Pi sidst set ${new Date(data.last_seen * 1000).toLocaleString('da-DK')}` : 'Forbind Pi-agenten for at se printerstatus';
    for (const [id, key, unit] of [['nozzle','nozzle_c',' °C'],['bed','bed_c',' °C'],['progress','progress_percent',' %']]) {
      element(id).textContent = fresh && printer[key] != null ? printer[key] + unit : '—';
    }
    element('count').textContent = `${data.jobs.length} job`;
    const rows = data.jobs.map(job => {
      const row = document.createElement('div'); row.className = 'job';
      const details = document.createElement('div');
      const name = document.createElement('strong'); name.textContent = job.name;
      const state = document.createElement('small'); state.textContent = `${labels[job.state] || job.state} · ${new Date(job.created * 1000).toLocaleString('da-DK')}`;
      details.append(name, state); row.append(details);
      const analysis = document.createElement('small');
      analysis.textContent = job.analysis?.supported ? `1 anvendt filament · ${job.analysis.filaments[0].material} · ${job.analysis.filaments[0].profile || 'Ukendt profil'}` : job.analysis?.reason || 'Filanalyse ukendt';
      details.append(analysis);
      if (job.selection) { const selection = document.createElement('small'); selection.textContent = `Valgt: AMS ${job.selection.ams_id + 1}, plads ${job.selection.slot} · ${job.selection.name || job.selection.material}`; details.append(selection); }
      if (job.execution_mode) { const mode = document.createElement('small'); mode.textContent=job.execution_mode==='live'?'Handling: fysisk print':'Handling: simulering'; details.append(mode); }
      if (job.receipt) {
        const receipt=JSON.parse(job.receipt); const diagnostic=receipt.diagnostics;
        if (diagnostic && (diagnostic.exit_code != null || diagnostic.error_type || diagnostic.events?.length)) {
          const report=document.createElement('details'); const heading=document.createElement('summary'); heading.textContent='Tekniske fejlkoder';
          const text=document.createElement('pre'); text.textContent=JSON.stringify(diagnostic,null,2); report.append(heading,text); details.append(report);
        }
      }
      if (job.state === 'queued') { const button = document.createElement('button'); button.textContent = 'Vælg filament og godkend'; button.disabled = !job.analysis?.supported || !fresh; button.onclick = () => approve(job); row.append(button); }
      if (['unknown','dispatched','submitted'].includes(job.state)) {
        const button = document.createElement('button'); button.className = 'secondary'; button.textContent = 'Luk efter kontrol';
        button.onclick = async () => {
          if (!window.confirm('Har du kontrolleret jobbet på Pi’en? Lukning frigiver køen og genstarter eller stopper ikke jobbet.')) return;
          try {
            await api(`/api/jobs/${job.id}/resolve`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_job_id:job.id,printer_checked_idle:true})});
            await refresh();
          } catch (error) { element('message').textContent = error.message; }
        };
        row.append(button);
      }
      return row;
    });
    element('jobs').replaceChildren(...rows);
    if (!rows.length) element('jobs').textContent = 'Køen er tom.';
  } catch (error) {
    renderAMS(null, false);
    element('connection').textContent = 'Server utilgængelig';
    element('connection').className = 'badge';
    element('state').textContent = 'Status ukendt';
    for (const id of ['nozzle','bed','progress']) element(id).textContent = '—';
  }
}
element('upload').addEventListener('submit', async event => {
  event.preventDefault(); const file = element('file').files[0];
  if (!file || !file.name.endsWith('.gcode.3mf') || file.size > 100 * 1024 ** 2) { element('message').textContent = 'Vælg en .gcode.3mf-fil på højst 100 MiB.'; return; }
  element('uploadButton').disabled = true; element('message').textContent = 'Uploader…';
  try {
    await api('/api/jobs', {method:'POST', headers:{'X-Filename':file.name},body:file});
    element('message').textContent = 'Filen er i køen og afventer godkendelse.'; element('upload').reset(); await refresh();
  } catch (error) { element('message').textContent = error.message; }
  finally { element('uploadButton').disabled = false; }
});
refresh(); setInterval(refresh, 5000);
