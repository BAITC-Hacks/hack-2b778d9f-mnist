const $ = id => document.getElementById(id);
let selected = null, audioURL = null, audioMeeting = null, polling = false;
$('key').value = sessionStorage.getItem('meetingKey') || '';
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fail(error) { $('error').textContent = error.message || String(error); }
async function api(path, options = {}) {
  const headers = {'X-Agent-API-Key': sessionStorage.getItem('meetingKey') || '', ...(options.headers || {})};
  const response = await fetch('/api/v1' + path, {...options, headers});
  if (!response.ok) { let detail; try { detail = (await response.json()).detail; } catch {} throw Error(typeof detail === 'string' ? detail : `Request failed (${response.status})`); }
  return response;
}
const json = body => ({headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)});
async function list() {
  const items = await (await api('/meetings')).json();
  $('meetings').replaceChildren();
  for (const item of items) { const button = document.createElement('button'); button.textContent = `${item.title} · ${item.status}`; button.onclick = () => open(item.id).catch(fail); $('meetings').append(button); }
}
$('connect').onclick = () => {sessionStorage.setItem('meetingKey', $('key').value); $('error').textContent = ''; list().catch(fail);};
$('create').onsubmit = async event => {
  event.preventDefault();
  try { const meeting = await (await api('/meetings', {method:'POST', ...json({title:$('title').value,meeting_date:$('date').value || null,participants:$('participants').value.split('\n').map(s=>s.trim()).filter(Boolean).map(name=>({name}))})})).json(); await list(); await open(meeting.id); } catch(e) {fail(e);}
};
function time(value) { return new Date(value * 1000).toISOString().slice(11,19); }
async function open(id, refresh=false) {
  const m = await (await api('/meetings/' + id)).json();
  selected = id;
  const active = ['queued','normalizing','transcribing','diarizing','extracting'].includes(m.status);
  const oldAudio = $('player'); const position = oldAudio && audioMeeting === id ? oldAudio.currentTime : 0;
  if (!refresh) $('error').textContent = '';
  $('detail').innerHTML = `<h2>${escapeHTML(m.title)}</h2><p>${escapeHTML(m.meeting_date || 'Date not supplied')} · <strong>${m.status}</strong></p><p class="review">${escapeHTML(m.error || '')}</p>
  <input type="file" id="audioFile" accept="audio/*,video/*"><button id="upload" ${active?'disabled':''}>Upload</button><button id="process" ${active||!m.has_audio?'disabled':''}>Start processing</button>
  <audio id="player" controls></audio><section><h3>Summary</h3><p>${escapeHTML(m.summary.text)}</p><h3>Key decisions</h3><ul>${m.decisions.map(d=>`<li>${escapeHTML(d.text)}</li>`).join('')}</ul></section>
  <section><h3>Tasks</h3><div class="scroll"><table><thead><tr><th>Task</th><th>Assignee</th><th>Deadline</th><th>Status</th><th>Confidence</th><th>Review</th><th>Evidence</th></tr></thead><tbody>${m.tasks.map(t=>`<tr><td>${escapeHTML(t.action)}</td><td>${escapeHTML(t.assignee_display || 'Unknown')}</td><td>${escapeHTML(t.deadline_normalized || t.deadline_raw || 'Not stated')}</td><td><select data-task="${t.id}" ${active?'disabled':''}>${['new','in_progress','completed'].map(s=>`<option ${s===t.status?'selected':''}>${s}</option>`).join('')}</select></td><td>${Math.round(t.confidence*100)}%</td><td class="review">${t.needs_review?'Needs review':''}</td><td><button data-seek="${t.evidence_start}" title="${escapeHTML(t.evidence_text)}">Source ${time(t.evidence_start)}</button></td></tr>`).join('')}</tbody></table></div></section>
  <section><h3>Speakers</h3>${m.speaker_mappings.map(s=>`<label>${escapeHTML(s.speaker)} (${Math.round(s.confidence*100)}%) <input data-speaker="${escapeHTML(s.speaker)}" value="${escapeHTML(s.name || '')}" placeholder="Participant name" ${active?'disabled':''}></label>`).join('')}<button id="saveSpeakers" ${active?'disabled':''}>Save names</button></section>
  <section><h3>Transcript</h3><div class="transcript">${m.transcript.map(s=>`<p><button data-seek="${s.start}">${time(s.start)}</button> <strong>${escapeHTML(s.speaker)} / ${escapeHTML(s.speaker_name)}</strong><br>${escapeHTML(s.text)}</p>`).join('')}</div></section>
  <button id="docx" ${m.status!=='completed'?'disabled':''}>Export DOCX</button><button id="pdf" ${m.status!=='completed'?'disabled':''}>Export PDF</button>`;
  if (m.has_audio && !active) {
    if (audioMeeting !== id || !audioURL || !refresh) {if(audioURL) URL.revokeObjectURL(audioURL); audioURL = URL.createObjectURL(await (await api(`/meetings/${id}/audio`)).blob()); audioMeeting=id;}
    $('player').src=audioURL; $('player').currentTime=position;
  }
  $('upload').onclick=async()=>{try{const file=$('audioFile').files[0];if(!file)throw Error('Select an audio file');const form=new FormData();form.append('file',file);await api(`/meetings/${id}/audio`,{method:'POST',body:form});audioMeeting=null;await open(id);await list();}catch(e){fail(e);}};
  $('process').onclick=async()=>{try{await api(`/meetings/${id}/process`,{method:'POST'});await open(id);await list();}catch(e){fail(e);}};
  document.querySelectorAll('[data-seek]').forEach(b=>b.onclick=()=>{$('player').currentTime=Number(b.dataset.seek);$('player').play().catch(fail);});
  document.querySelectorAll('[data-task]').forEach(s=>s.onchange=async()=>{try{await api('/tasks/'+s.dataset.task,{method:'PATCH',...json({status:s.value})});}catch(e){fail(e);}});
  $('saveSpeakers').onclick=async()=>{try{const body=[...document.querySelectorAll('[data-speaker]')].map(s=>({speaker:s.dataset.speaker,name:s.value||null}));await api(`/meetings/${id}/speaker-mappings`,{method:'PATCH',...json(body)});await open(id,true);}catch(e){fail(e);}};
  for(const kind of ['docx','pdf']) $(kind).onclick=async()=>{try{const blob=await(await api(`/meetings/${id}/export.${kind}`)).blob();const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download=`protocol.${kind}`;a.click();setTimeout(()=>URL.revokeObjectURL(url),10000);}catch(e){fail(e);}};
  polling=active;
}
setInterval(async()=>{if(selected&&polling){try{await open(selected,true);await list();}catch(e){polling=false;fail(e);}}},2500);
list().catch(fail);
