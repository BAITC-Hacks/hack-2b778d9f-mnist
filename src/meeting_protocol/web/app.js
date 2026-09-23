const $ = id => document.getElementById(id);
let selected = null, audioURL = null, audioMeeting = null, polling = false, catalog = null, generation = 0, detailRequest = 0;
$('key').value = sessionStorage.getItem('meetingKey') || '';
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fail() { $('error').textContent = 'The request could not be completed. Check your connection and API key, then try again.'; }
async function api(path, options = {}) {
  const headers = {'X-Agent-API-Key': sessionStorage.getItem('meetingKey') || '', ...(options.headers || {})};
  const response = await fetch('/api/v1' + path, {...options, headers});
  if (!response.ok) throw Error(`Request failed (${response.status})`);
  return response;
}
const json = body => ({headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
const operations = ['asr','diarization','extract_tasks','verify_tasks','resolve_speakers','generate_summary'];
const labels = {asr:'Speech recognition',diarization:'Speaker detection',extract_tasks:'Task extraction',verify_tasks:'Task verification',resolve_speakers:'Speaker matching',generate_summary:'Summary'};
function selection(root) { const result={}; root.querySelectorAll('[data-model]').forEach(el=>{if(el.value)result[el.dataset.model]=el.value;}); return result; }
function controlsReady(root) { return !!catalog && operations.every(op=>root.querySelector(`[data-model="${op}"]`)); }
function changedSelection(root, saved) { const next=selection(root); return operations.some(op=>(next[op]||'')!==(saved[op]||''))?next:null; }
function modelControl(operation, saved={}) {
  const profiles=(catalog?.profiles||[]).filter(p=>p.operations?.includes(operation));
  const fallback=catalog?.defaults?.[operation]||'';
  const defaultProfile=(catalog?.profiles||[]).find(p=>p.id===fallback);
  const value=saved[operation]||'';
  const current=profiles.find(p=>p.id===value);
  const removed=value&&!current;
  const stateText={configured:'Configured · not verified',missing_files:'Model files missing',server_unavailable:'Server unavailable',not_verified:'Not verified',incompatible:'Incompatible'};
  const statusProfile=current||defaultProfile;
  const status=statusProfile?`${escapeHTML(statusProfile.label)} · ${stateText[statusProfile.availability]||'State unknown'}`:'No default profile reported';
  return `<label class="operation">${labels[operation]}<select data-model="${operation}"><option value="">${fallback?`Server default${defaultProfile?` · ${escapeHTML(defaultProfile.label)}`:''}`:'Server default'}</option>${removed?`<option value="${escapeHTML(value)}" selected>${escapeHTML(value)} · no longer listed</option>`:''}${profiles.map(p=>`<option value="${escapeHTML(p.id)}" ${p.id===value?'selected':''}>${escapeHTML(p.label)} · ${stateText[p.availability]||'State unknown'}</option>`).join('')}</select><span class="model-status">${removed?'Saved profile no longer listed; processing may be rejected.':value&&current?`${escapeHTML(current.label)} · ${stateText[current.availability]||'State unknown'}`:`Default: ${status}`}</span></label>`;
}
function modelPanel(saved={}) { return `<div class="models"><h3>Model choices</h3><p>Configured means set up, not verified on real audio.</p>${modelControl('asr',saved)}${modelControl('diarization',saved)}<details><summary>Advanced · language model steps</summary><div class="model-grid">${['extract_tasks','verify_tasks','resolve_speakers','generate_summary'].map(op=>modelControl(op,saved)).join('')}</div></details></div>`; }
async function loadCatalog(version=generation) { let result=null; try { const data=await(await api('/model-profiles')).json(); result=data?.schema_version===1&&Array.isArray(data.profiles)&&data.defaults?data:null; } catch {} if(version!==generation)return; catalog=result; $('createModels').innerHTML=catalog?modelPanel():'<div class="models"><h3>Model choices</h3><p>Model options unavailable. The server will use its defaults.</p></div>'; }
async function list(version=generation) {
  const items = await (await api('/meetings')).json();
  if(version!==generation)return;
  $('meetings').replaceChildren();
  for (const item of items) { const button = document.createElement('button'); button.textContent = `${item.title} · ${item.status}`; button.onclick = () => open(item.id,false,version).catch(e=>{if(version===generation)fail(e);}); $('meetings').append(button); }
}
$('connect').onclick = async () => {const previous=selected;const version=++generation;detailRequest++;selected=null;polling=false;catalog=null;$('detail').innerHTML='<p class="empty">Connecting…</p>';sessionStorage.setItem('meetingKey', $('key').value);$('error').textContent='';await loadCatalog(version);if(version!==generation)return;try{await list(version);if(previous)await open(previous,false,version);else $('detail').innerHTML='<p class="empty">Select or create a meeting.</p>';}catch(e){if(version===generation){$('detail').innerHTML='<p class="empty">Select or create a meeting.</p>';fail(e);}}};
$('create').onsubmit = async event => {
  event.preventDefault();
  const version=generation;
  try { const body={title:$('title').value,meeting_date:$('date').value || null,participants:$('participants').value.split('\n').map(s=>s.trim()).filter(Boolean).map(name=>({name}))}; if(controlsReady($('createModels')))body.model_selection=selection($('createModels')); const meeting = await (await api('/meetings', {method:'POST', ...json(body)})).json(); if(version!==generation)return; await list(version); await open(meeting.id,false,version); } catch(e) {if(version===generation)fail(e);}
};
function time(value) { return new Date(value * 1000).toISOString().slice(11,19); }
async function open(id, refresh=false, version=generation) {
  const request=++detailRequest;
  const m = await (await api('/meetings/' + id)).json();
  if(version!==generation||request!==detailRequest)return;
  selected = id;
  const active = ['queued','normalizing','transcribing','diarizing','extracting'].includes(m.status);
  const oldAudio = $('player'); const position = oldAudio && audioMeeting === id ? oldAudio.currentTime : 0;
  if (!refresh) $('error').textContent = '';
  const saved=m.model_selection||{};
  const attempts=Array.isArray(m.attempts)?m.attempts:[];
  const latest=attempts.length?attempts[attempts.length-1]:null;
  const bindingMarkup=bindings=>bindings&&typeof bindings==='object'?`<div class="binding-list">${Object.entries(bindings).map(([key,value])=>`<span class="binding"><strong>${escapeHTML(labels[key]||key)}</strong>${escapeHTML(typeof value==='string'?value:JSON.stringify(value))}</span>`).join('')}</div>`:'<p class="hint">No binding details reported.</p>';
  $('detail').innerHTML = `<h2>${escapeHTML(m.title)}</h2><p>${escapeHTML(m.meeting_date || 'Date not supplied')} · <strong>${escapeHTML(m.status)}</strong></p>
  <section><h3>Processing models</h3>${catalog?modelPanel(saved):`<div class="models"><h3>Model choices</h3><p>Model options unavailable. Saved choices will be used for processing.</p>${operations.filter(op=>saved[op]).map(op=>`<p class="model-status">${labels[op]}: ${escapeHTML(saved[op])}</p>`).join('')}</div>`}<button id="saveModels" ${active||!catalog?'disabled':''}>Save model choices</button><p class="hint">${catalog?'Changes replace saved choices. Clear selections to use defaults.':'Reconnect to load model options before changing choices.'}</p>${latest?`<div class="attempt"><strong>Latest attempt · ${escapeHTML(latest.attempt_id||latest.id||'')}</strong><p>${escapeHTML(latest.status||'')}</p>${bindingMarkup(latest.bindings)}</div>`:'<p class="hint">No processing attempt yet.</p>'}</section>
  <input type="file" id="audioFile" accept="audio/*,video/*"><button id="upload" ${active?'disabled':''}>Upload</button><button id="process" ${active||!m.has_audio?'disabled':''}>Start processing</button>
  <audio id="player" controls></audio><section><h3>Summary</h3><p>${escapeHTML(m.summary.text)}</p><h3>Key decisions</h3><ul>${m.decisions.map(d=>`<li>${escapeHTML(d.text)}</li>`).join('')}</ul></section>
  <section><h3>Tasks</h3><div class="scroll"><table><thead><tr><th>Task</th><th>Assignee</th><th>Deadline</th><th>Status</th><th>Confidence</th><th>Review</th><th>Evidence</th></tr></thead><tbody>${m.tasks.map(t=>`<tr><td>${escapeHTML(t.action)}</td><td>${escapeHTML(t.assignee_display || 'Unknown')}</td><td>${escapeHTML(t.deadline_normalized || t.deadline_raw || 'Not stated')}</td><td><select data-task="${t.id}" ${active?'disabled':''}>${['new','in_progress','completed'].map(s=>`<option ${s===t.status?'selected':''}>${s}</option>`).join('')}</select></td><td>${Math.round(t.confidence*100)}%</td><td class="review">${t.needs_review?'Needs review':''}</td><td><button data-seek="${t.evidence_start}" title="${escapeHTML(t.evidence_text)}">Source ${time(t.evidence_start)}</button></td></tr>`).join('')}</tbody></table></div></section>
  <section><h3>Speakers</h3>${m.speaker_mappings.map(s=>`<label>${escapeHTML(s.speaker)} (${Math.round(s.confidence*100)}%) <input data-speaker="${escapeHTML(s.speaker)}" value="${escapeHTML(s.name || '')}" placeholder="Participant name" ${active?'disabled':''}></label>`).join('')}<button id="saveSpeakers" ${active?'disabled':''}>Save names</button></section>
  <section><h3>Transcript</h3><div class="transcript">${m.transcript.map(s=>`<p><button data-seek="${s.start}">${time(s.start)}</button> <strong>${escapeHTML(s.speaker)} / ${escapeHTML(s.speaker_name)}</strong><br>${escapeHTML(s.text)}</p>`).join('')}</div></section>
  <button id="docx" ${m.status!=='completed'?'disabled':''}>Export DOCX</button><button id="pdf" ${m.status!=='completed'?'disabled':''}>Export PDF</button>`;
  if (m.has_audio && !active) {
    if (audioMeeting !== id || !audioURL || !refresh) {const blob=await (await api(`/meetings/${id}/audio`)).blob();if(version!==generation||request!==detailRequest)return;if(audioURL) URL.revokeObjectURL(audioURL); audioURL = URL.createObjectURL(blob); audioMeeting=id;}
    $('player').src=audioURL; $('player').currentTime=position;
  }
  $('upload').onclick=async()=>{try{const file=$('audioFile').files[0];if(!file)throw Error('Select an audio file');const form=new FormData();form.append('file',file);await api(`/meetings/${id}/audio`,{method:'POST',body:form});audioMeeting=null;await open(id);await list();}catch(e){fail(e);}};
  $('saveModels').onclick=async()=>{if(!controlsReady($('detail')))return;try{const body=selection($('detail'));await api(`/meetings/${id}/model-selection`,{method:'PATCH',...json({model_selection:body})});if(version!==generation)return;await open(id);await list();}catch(e){if(version===generation)fail(e);}};
  $('process').onclick=async()=>{try{const changed=controlsReady($('detail'))?changedSelection($('detail'),saved):null;const options=changed===null?{method:'POST'}:{method:'POST',...json({model_selection:changed})};await api(`/meetings/${id}/process`,options);if(version!==generation)return;await open(id);await list();}catch(e){if(version===generation)fail(e);}};
  document.querySelectorAll('[data-seek]').forEach(b=>b.onclick=()=>{$('player').currentTime=Number(b.dataset.seek);$('player').play().catch(fail);});
  document.querySelectorAll('[data-task]').forEach(s=>s.onchange=async()=>{try{await api('/tasks/'+s.dataset.task,{method:'PATCH',...json({status:s.value})});}catch(e){fail(e);}});
  $('saveSpeakers').onclick=async()=>{try{const body=[...document.querySelectorAll('[data-speaker]')].map(s=>({speaker:s.dataset.speaker,name:s.value||null}));await api(`/meetings/${id}/speaker-mappings`,{method:'PATCH',...json(body)});await open(id,true);}catch(e){fail(e);}};
  for(const kind of ['docx','pdf']) $(kind).onclick=async()=>{try{const blob=await(await api(`/meetings/${id}/export.${kind}`)).blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`protocol.${kind}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(e){fail(e);}};
  polling=active;
}
setInterval(async()=>{if(selected&&polling){const version=generation;try{await open(selected,true,version);if(version===generation)await list(version);}catch(e){if(version===generation){polling=false;fail(e);}}}},2500);
loadCatalog().then(()=>list()).catch(fail);
