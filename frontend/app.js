const element = id => document.getElementById(id);
const labels = {queued:'Afventer godkendelse',approved:'Klar til Pi',dispatched:'Sendt til Pi',simulated:'Simulering gennemført',unknown:'Kræver gennemgang',resolved:'Gennemgået og lukket'};
async function api(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || 'Handlingen kunne ikke gennemføres');
  return body;
}
async function approve(job) {
  element('approvalName').textContent = job.name;
  const dialog = element('approval');
  dialog.returnValue = 'cancel';
  dialog.showModal();
  dialog.addEventListener('close', async () => {
    if (dialog.returnValue !== 'approve') return;
    try {
      await api(`/api/jobs/${job.id}/approve`, {method:'POST', headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_job_id:job.id})});
      await refresh();
    } catch (error) { element('message').textContent = error.message; }
  }, {once:true});
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
      if (job.state === 'queued') { const button = document.createElement('button'); button.textContent = 'Godkend simulering'; button.onclick = () => approve(job); row.append(button); }
      if (['unknown','dispatched'].includes(job.state)) {
        const button = document.createElement('button'); button.className = 'secondary'; button.textContent = 'Luk efter kontrol';
        button.onclick = async () => {
          if (!window.confirm('Har du kontrolleret jobbet på Pi’en? Lukning frigiver køen og genstarter eller stopper ikke jobbet.')) return;
          try {
            await api(`/api/jobs/${job.id}/resolve`, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({confirm_job_id:job.id})});
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
