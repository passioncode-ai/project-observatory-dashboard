// Space is a bounded same-origin maintenance surface, never a command console.
(() => {
  if (PAGE !== 'space') return;
  const byId = id => document.getElementById(id);
  const live = location.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(location.hostname);
  let state = D.space || {}, preview = null, pending = false, message = '', timer;
  const bytes = n => typeof n === 'number' ? (n / 1e9).toLocaleString(LOCALE, {maximumFractionDigits: 2}) + ' GB' : '—';
  const reason = id => ({
    'inventory-only': T('Protected: no approved cleanup operation'),
    'linked-path': T('Protected: linked cache path'),
    'outside-home': T('Protected: outside the user home'),
    'different-volume': T('Protected: on another volume'),
    'cache-unavailable': T('Protected: cache could not be inspected'),
    'not-present': T('Not installed or no cache yet'),
    'tool-missing': T('Cleanup tool is unavailable'),
    'activity-unknown': T('Protected: active consumers could not be checked'),
    'in-use': T('Protected: a related process is running'),
    'local-daemon-unverified': T('Protected: local Docker daemon could not be verified'),
    'native-lock-unverified': T('Protected: tool locking could not be verified'),
    'native-cache-maintenance': T('Available through the cache owner'),
    'cache-moved': T('Skipped: cache location changed'),
    'not-in-preview': T('Skipped: not eligible in the preview'),
    'command-failed': T('The cleanup command failed or timed out'),
  }[id] || T('Not measured'));
  const result = id => ({completed:T('Completed'),partial:T('Some operations failed'),
    skipped:T('Skipped'),failed:T('Failed'),running:T('Working…'),interrupted:T('Interrupted')}[id] || T('Not measured'));
  const notice = id => ({critical:T('Disk space is critically low'),recovered:T('Disk space has recovered'),
    'still-critical':T('Space remains low. Protected data was left in place.'),
    'cleanup-interrupted':T('Cleanup was interrupted. Its result is unknown; create a new preview.'),
    'cleanup-finished':T('Cleanup finished; inspect the measured result')}[id] || T('Not measured'));
  const table = (heads, rows) => rows.length ? '<table><thead><tr>' + heads.map(h=>`<th scope="col">${E(h)}</th>`).join('') +
    '</tr></thead><tbody>' + rows.map(row=>'<tr>'+row.map((v,i)=>`<td data-label="${E(heads[i])}">${v}</td>`).join('')+'</tr>').join('')+'</tbody></table>' :
    `<p class="empty">${E(T('Nothing recorded yet. Measure caches to start.'))}</p>`;
  function draw() {
    const busy = pending || state.busy;
    byId('space-status').innerHTML = `<section class="card panel${state.pressure === 'critical' ? ' space-critical' : ''}"><b class="space-free">${E(bytes(state.free_bytes))}</b> ${E(T('Free disk'))}` +
      `<p>${E(typeof state.free_bytes !== 'number' ? T('Not measured') : state.pressure === 'critical' ? T('Disk space is critically low') : T('Disk space is above the cleanup threshold'))}</p>` +
      (state.observed_at ? `<p class="machine-at">${E(T('Measured {at}',{at:state.observed_at}))}</p>` : '') +
      (busy ? `<p>${E(T('Working…'))}</p>` : '') + (message ? `<p>${E(message)}</p>` : '') +
      (!live ? `<p>${E(T('Open the local server to run cleanup. This file is a snapshot.'))} <code>observatory full open --serve</code></p>` : '') + '</section>';
    byId('space-preview').disabled = !live || busy;
    byId('space-scan').disabled = !live || busy;
    byId('space-auto').disabled = !live || pending;
    byId('space-auto').checked = !!state.auto_enabled;
    byId('space-threshold').textContent = T('Start below {start}; stop at {target}. Retry at most once every {minutes} minutes.',
      {start:bytes(state.trigger_bytes),target:bytes(state.target_bytes),minutes:(state.cooldown_seconds || 1800)/60});
    const rows = [...(state.caches || [])].sort((a,b)=>(b.size_bytes ?? -1)-(a.size_bytes ?? -1));
    byId('space-caches').innerHTML = (state.measured_at ? `<p class="machine-at">${E(T('Measured {at}',{at:state.measured_at}))}</p>` : '') +
      table([T('Cache owner'),T('Location'),T('Occupied'),T('Cleanup availability')],rows.map(r=>[
        E(r.owner),`<span class="mono">${E(r.path)}</span>`, E(r.size_display || bytes(r.size_bytes)),
        E(reason(r.reason)) + (r.automatic && r.eligible ? `<br>${E(T('Eligible for automatic maintenance'))}` : '') +
        (r.measurement_error ? `<br>${E(T('Size could not be measured; retry measurement.'))}` : '')]));
    byId('space-history').innerHTML = table([T('Time'),T('Mode'),T('Result'),T('Free-space change')],
      [...(state.runs || [])].reverse().map(r=>[E(r.finished_at || r.started_at),E(r.mode === 'automatic' ? T('Automatic') : T('Manual')),
        E(result(r.status)) + '<ul>' + (r.actions||[]).map(a=>`<li>${E(a.cache_id)}: ${E(result(a.result))}${a.reason ? ' · '+E(reason(a.reason)) : ''}</li>`).join('')+'</ul>',
        E(bytes(r.free_delta_bytes))]));
    byId('space-notifications').innerHTML = (state.notifications || []).length ? '<ul>' + [...state.notifications].reverse().map(n=>
      `<li>${E(n.at)} · ${E(notice(n.kind))}${n.desktop ? ' · '+E(n.desktop === 'requested' ? T('Desktop notification requested') : T('Desktop notification unavailable')) : ''}</li>`).join('') + '</ul>' : `<p class="empty">${E(T('No space notifications yet.'))}</p>`;
  }
  async function api(body) {
    const controller = new AbortController(), timeout = setTimeout(()=>controller.abort(),35000);
    try {
      const options = body ? {method:'POST',headers:{'Content-Type':'application/json','X-Observatory-Action':'space'},body:JSON.stringify(body)} : {cache:'no-store'};
      const r = await fetch('/api/space', {...options,signal:controller.signal});
      const data = await r.json();
      if (!r.ok) throw new Error(data.error || 'request-failed');
      return data;
    } finally { clearTimeout(timeout); }
  }
  async function refresh() {
    if (!live) return;
    try {
      state = await api();
      if (state.error) message = T('The operation could not finish. Refresh the preview before trying again.');
      draw();
    } catch (_) {
      message = T('The local server could not be reached. Check the server and retry.'); draw();
    }
    clearTimeout(timer); timer = setTimeout(refresh, state.busy ? 1500 : 15000);
  }
  async function act(body) {
    pending = true; message = ''; draw();
    try { return await api(body); }
    catch (_) { message = T('The operation could not finish. Refresh the preview before trying again.'); return null; }
    finally { pending = false; draw(); }
  }
  byId('space-scan').onclick = async () => { const r = await act({action:'scan'}); if(r) await refresh(); };
  byId('space-auto').onchange = async ev => {
    const enabled = ev.target.checked;
    const r = await act({action:'policy',enabled});
    if(r) state.auto_enabled = r.auto_enabled;
    draw();
  };
  byId('space-preview').onclick = async () => {
    preview = await act({action:'plan'});
    if (!preview) return;
    const panel = byId('space-plan');
    panel.hidden = false;
    panel.innerHTML = `<h2 class="machine-h">${E(T('Review cache cleanup'))}</h2><p>${E(T('These cache entries may need downloading or rebuilding later. Cleanup cannot be undone. Busy caches are checked again before each operation.'))}</p>` +
      table([T('Cache owner'),T('Cleanup availability')],preview.actions.map(r=>[E(r.owner),E(reason(r.reason))])) +
      `<div class="space-actions"><button id="space-confirm" class="chip-btn" type="button"${preview.actions.some(r=>r.eligible)?'':' disabled'}>${E(T('Clean listed caches'))}</button>` +
      `<button id="space-cancel" class="chip-btn" type="button">${E(T('Cancel'))}</button></div>`;
    panel.focus();
    byId('space-cancel').onclick = async () => {
      const id = preview && preview.id;
      preview = null; panel.hidden = true;
      if (id) await act({action:'cancel',plan_id:id});
      byId('space-preview').focus();
    };
    byId('space-confirm').onclick = async () => {
      const id = preview && preview.id;
      preview = null; panel.hidden = true;
      if(id) await act({action:'clean',plan_id:id});
      byId('space-preview').focus(); await refresh();
    };
  };
  draw();refresh();
})();
