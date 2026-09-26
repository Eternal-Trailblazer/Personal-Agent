/* Personal Agent — dashboard front-end. Vanilla JS, no build step. */
'use strict';

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  view: 'dashboard',
  taskScope: 'open',
  day: new Date().toISOString().slice(0, 10),
  history: [],
  engine: 'local',
};

/* ── api ─────────────────────────────────────────────── */
async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({ error: 'bad response' }));
  if (!res.ok || data.error) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

/* For endpoints that report failure *inside* a 200 body (the connection test
   returns {ok:false, error:"..."} as data, not as an exception). */
async function apiRaw(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  return res.json().catch(() => ({ ok: false, error: 'bad response' }));
}

/* ── helpers ─────────────────────────────────────────── */
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function toast(msg, isErr = false) {
  const el = $('#toast');
  el.textContent = msg;
  el.className = 'toast' + (isErr ? ' err' : '');
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, 2600);
}

function empty(text) { return `<div class="empty">${esc(text)}</div>`; }

function dueChip(t) {
  if (!t.due_at) return '';
  if (t.overdue)  return `<span class="chip late">${esc(t.due_human)}</span>`;
  if (t.due_today) return `<span class="chip soon">today ${esc((t.due_at || '').slice(11, 16))}</span>`;
  return `<span class="chip">${esc(t.due_human)}</span>`;
}

function taskRow(t) {
  const done = t.status === 'done';
  return `<div class="row ${done ? 'is-done' : ''}" data-id="${t.id}">
    <button class="tick ${done ? 'on' : ''}" data-act="toggle" title="Toggle done">✓</button>
    <div class="grow">
      <div class="title">${esc(t.title)}</div>
      <div class="meta">
        <span class="chip p${t.priority}">${esc(t.priority_label)}</span>
        ${dueChip(t)}
        ${t.project ? `<span>${esc(t.project)}</span>` : ''}
      </div>
    </div>
    <button class="btn btn-icon" data-act="del" title="Delete">✕</button>
  </div>`;
}

function eventRow(e) {
  return `<div class="tl-item" data-id="${e.id}">
    <div class="tl-time">${esc(e.time_label.split(' – ')[0] || '')}</div>
    <div class="tl-rail"><div class="tl-dot" style="background:var(--${kindVar(e.kind)})"></div><div class="tl-line"></div></div>
    <div class="tl-card">
      <div class="title">${esc(e.title)}</div>
      <div class="meta">
        <span class="chip ${esc(e.kind)}">${esc(e.kind)}</span>
        ${e.location ? `<span>${esc(e.location)}</span>` : ''}
        <span>${esc(e.minutes)} min</span>
      </div>
      <button class="btn btn-icon" data-act="delevent" style="margin-top:5px">✕ remove</button>
    </div>
  </div>`;
}

const kindVar = k => ({
  class: 'accent', meeting: 'violet', study: 'green',
  personal: 'amber', deadline: 'red', travel: 'text-dim',
}[k] || 'accent');

/* ── views ───────────────────────────────────────────── */
const TITLES = {
  dashboard: ['Dashboard', 'Your day at a glance'],
  tasks: ['Tasks', 'Everything you are tracking'],
  schedule: ['Schedule', 'Agenda, conflicts and free time'],
  drafts: ['Drafts', 'Ready for your review before sending'],
  memory: ['Memory', 'Durable facts that persist across sessions'],
  settings: ['Settings', 'Intelligence layer and personal defaults'],
};

function show(view) {
  state.view = view;
  $$('.view').forEach(v => v.classList.toggle('is-active', v.id === `view-${view}`));
  $$('.nav-item').forEach(n => n.classList.toggle('is-active', n.dataset.view === view));
  $('#viewTitle').textContent = TITLES[view][0];
  $('#viewSub').textContent = TITLES[view][1];
  load(view);
}

/* ── loaders ─────────────────────────────────────────── */
async function load(view = state.view) {
  try {
    if (view === 'dashboard') await loadDashboard();
    if (view === 'tasks')     await loadTasks();
    if (view === 'schedule')  await loadSchedule();
    if (view === 'drafts')    await loadDrafts();
    if (view === 'memory')    await loadMemory();
    if (view === 'settings')  await loadSettings();
  } catch (e) { toast(e.message, true); }
}

async function loadDashboard() {
  const d = await api('/api/overview');
  $('#tzLabel').textContent = `${d.tz} · ${d.tz_label}`;
  $('#viewSub').textContent = `${d.greeting} · ${d.tz_label}`;

  const c = d.counts;
  $('#stats').innerHTML = [
    ['Open', c.open, ''],
    ['Due today', c.due_today, c.due_today ? 'warn' : ''],
    ['Overdue', c.overdue, c.overdue ? 'alert' : ''],
    ['Doing', c.doing, ''],
    ['Done', c.done, 'good'],
    ['Events today', d.today.length, ''],
  ].map(([k, v, cls]) => `<div class="stat ${cls}"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

  $('#navTaskCount').textContent = c.open;
  $('#navDraftCount').textContent = d.drafts;
  $('#todayDate').textContent = d.today.length
    ? `${d.today.length} event(s)`
    : 'Clear';

  $('#focus').innerHTML = d.focus_task ? `
    <div class="focus-body">
      <span class="chip p${d.focus_task.priority}">${esc(d.focus_task.priority_label)}</span>
      <div>
        <div class="big">${esc(d.focus_task.title)}</div>
        <div class="focus-note">
          ${d.focus_task.project ? esc(d.focus_task.project) : 'Unassigned'} ·
          ${d.focus_task.due_at ? `due ${esc(d.focus_task.due_human)}` : 'no deadline'}
        </div>
      </div>
    </div>` : empty('Nothing open. Clean slate.');

  $('#todayList').innerHTML = d.today.length
    ? d.today.map(eventRow).join('') : empty('No events today.');

  const tasks = await api('/api/tasks?scope=open').then(r => r.tasks.slice(0, 7));
  $('#dueList').innerHTML = tasks.length
    ? tasks.map(taskRow).join('') : empty('No open tasks.');

  $('#conflictList').innerHTML = d.conflicts.length
    ? d.conflicts.map(x => `<div class="row">
        <span class="chip late">${x.overlap_min}m</span>
        <div class="grow">
          <div class="title">${esc(x.a.title)} ↔ ${esc(x.b.title)}</div>
          <div class="meta"><span>${esc(x.day)}</span><span>${esc(x.a.label)}</span></div>
        </div></div>`).join('')
    : empty('No conflicts.');
}

async function loadTasks() {
  const { tasks } = await api(`/api/tasks?scope=${state.taskScope}`);
  $('#taskList').innerHTML = tasks.length ? tasks.map(taskRow).join('') : empty('Nothing here.');
  $('#navTaskCount').textContent = tasks.filter(t => t.status !== 'done').length;
}

async function loadSchedule() {
  const d = await api(`/api/events?day=${state.day}`);
  $('#dayPicker').value = state.day;
  $('#agendaCount').textContent = `${d.events.length} event(s)`;
  $('#agenda').innerHTML = d.events.length ? d.events.map(eventRow).join('') : empty('Nothing scheduled.');
  $('#dayConflicts').innerHTML = d.conflicts && d.conflicts.length
    ? d.conflicts.map(c => `<div class="row">
        <span class="chip late">${c.overlap_min}m</span>
        <div class="grow">
          <div class="title">${esc(c.a.title)} ↔ ${esc(c.b.title)}</div>
          <div class="meta"><span>${esc(c.a.label)}</span></div>
        </div></div>`).join('')
    : empty('No conflicts.');
  $('#freeSlots').innerHTML = d.free_slots.length
    ? d.free_slots.map(s => `<div class="row"><div class="grow">
        <div class="title">${esc(s.start.slice(11, 16))} – ${esc(s.end.slice(11, 16))}</div>
        <div class="meta"><span>${s.minutes} min free</span></div></div></div>`).join('')
    : empty('Day is fully booked.');
}

async function loadDrafts() {
  const { drafts } = await api('/api/drafts');
  $('#draftList').innerHTML = drafts.length ? drafts.map(d => `
    <div class="row" data-id="${d.id}">
      <div class="grow">
        <div class="title">${esc(d.subject || '(no subject)')}</div>
        <div class="meta">
          <span class="chip">${esc(d.channel)}</span>
          <span>to ${esc(d.audience || '—')}</span>
          <span>${esc((d.body || '').slice(0, 90))}…</span>
        </div>
      </div>
      <button class="btn btn-mini" data-act="viewdraft">View</button>
      <button class="btn btn-icon" data-act="deldraft">✕</button>
    </div>`).join('') : empty('No drafts. Ask the assistant to write one.');
}

async function loadMemory() {
  const { memory } = await api('/api/memory');
  $('#memList').innerHTML = memory.length ? memory.map(m => `
    <div class="row" data-id="${m.id}">
      <span class="chip">${esc(m.kind)}</span>
      <div class="grow"><div class="title">${esc(m.text)}</div>
        <div class="meta"><span>${esc(m.created_at || '')}</span></div></div>
      <button class="btn btn-icon" data-act="delmem">✕</button>
    </div>`).join('') : empty('Nothing remembered yet.');
}

let PRESETS = {}, PRESET_MODELS = {};

async function loadSettings() {
  const d = await api('/api/settings');
  PRESETS = Object.fromEntries(d.presets.map(p => [p.id, p]));
  PRESET_MODELS = d.preset_models || {};

  $('#setProvider').value = d.provider || 'hybrid';
  $('#setPreset').innerHTML = d.presets.map(p =>
    `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');
  $('#setPreset').value = d.llm_preset || (d.llm.api_key_set ? 'custom' : 'gemini');
  onPresetChange(false);

  $('#setBaseUrl').value = d.llm.base_url || '';
  $('#setApiKey').value = '';
  $('#setApiKey').placeholder = d.llm.api_key_set ? '•••••• (saved)' : 'Paste your key';
  $('#clearKey').hidden = !d.llm.api_key_set;

  fillModels(d.llm.model);

  const provider = $('#setProvider').value;
  const llmOn = d.llm.api_key_set;
  const bits = [];
  if (!llmOn) bits.push('running fully local');
  else if (provider === 'hybrid') bits.push('hybrid — local first, cloud when needed');
  else if (provider === 'openai') bits.push('cloud for everything');
  else bits.push('local only — cloud configured but unused');
  $('#llmStatus').textContent = `${d.engine} · ${bits.join('')}`;
  $('#llmStatus').className = 'status';

  $('#setOwner').value = d.settings.owner;
  $('#setTz').value = d.settings.tz;
  $('#setPrompt').value = d.settings.custom_prompt || '';

  const { tools } = await api('/api/tools');
  $('#toolList').innerHTML = tools.map(t =>
    `<div class="tool-row"><code>${esc(t.name)}</code><span>${esc(t.description)}</span></div>`).join('');
}

function fillModels(current) {
  const models = PRESET_MODELS[$('#setPreset').value] || [];
  const sel = $('#setModel');
  if (!models.length) {
    sel.innerHTML = '';
    sel.placeholder = 'model id';
    return;
  }
  sel.innerHTML = models.map(([id, label]) =>
    `<option value="${esc(id)}">${esc(label)}</option>`).join('');
  if (current) sel.value = current;
}

function onPresetChange(save) {
  const p = PRESETS[$('#setPreset').value];
  if (!p) return;
  $('#setBaseUrl').value = p.base_url;
  $('#presetNote').textContent = p.note || '';
  $('#keyHint').textContent = p.key_hint ? `· get one at ${p.key_hint}` : '';
  fillModels(p.model);
  if (save) saveLlm();
}

async function saveLlm() {
  const body = {
    provider: $('#setProvider').value,
    llm_preset: $('#setPreset').value,
    base_url: $('#setBaseUrl').value,
    model: $('#setModel').value || $('#setModel').placeholder,
  };
  const key = $('#setApiKey').value.trim();
  if (key) body.api_key = key;
  try {
    const r = await api('/api/settings', { method: 'POST', body });
    await loadSettings();
    await setEngine(r.engine);
    toast('Saved.');
  } catch (e) { toast(e.message, true); }
}

$('#setPreset').addEventListener('change', () => onPresetChange(true));

/* Live connection test: reachability, auth and — crucially — tool-calling. */
$('#testLlm').addEventListener('click', async () => {
  const btn = $('#testLlm'), status = $('#llmStatus');
  btn.disabled = true; btn.textContent = 'Testing…';
  status.className = 'status';
  // The check makes two model round-trips, which on a free tier can take a
  // while. Show elapsed time so a slow-but-working provider doesn't look hung.
  const started = Date.now();
  status.innerHTML = '<div class="diag"><div class="diag-row warn">' +
    '<span class="mark">⋯</span><span class="k">Contacting the provider</span>' +
    '<span class="v" id="testElapsed">0.0s</span></div></div>';
  const ticker = setInterval(() => {
    const el = $('#testElapsed');
    if (el) el.textContent = ((Date.now() - started) / 1000).toFixed(1) + 's';
  }, 100);
  try {
    const r = await apiRaw('/api/test_llm', { method: 'POST' });
    renderDiagnosis(r);
  } catch (e) {
    status.className = 'status err';
    status.textContent = e.message;
  } finally {
    clearInterval(ticker);
    btn.disabled = false; btn.textContent = 'Test connection';
  }
});

function renderDiagnosis(r) {
  const rows = [];
  const add = (ok, k, v, warn) => rows.push(
    `<div class="diag-row ${ok ? 'pass' : warn ? 'warn' : 'fail'}">
       <span class="mark">${ok ? '✓' : warn ? '!' : '✕'}</span>
       <span class="k">${esc(k)}</span><span class="v">${esc(v)}</span></div>`);

  if (r.error) {
    $('#llmStatus').className = 'status err';
    $('#llmStatus').innerHTML =
      `<div class="diag">
         <div class="diag-row fail"><span class="mark">✕</span>
           <span class="k">${esc(r.error)}</span></div>
         <div class="diag-row"><span class="k">Endpoint</span>
           <span class="v">${esc(r.base_url || '')}</span></div>
       </div>`;
    return;
  }
  add(true, 'Reachable', `${r.latency_ms} ms`);
  add(true, 'Key accepted', r.model);
  add(true, 'Reply', r.sample || '—');
  add(r.tools_ok, 'Tool calling', r.tools_ok
      ? `works (${r.tool_probe})`
      : (r.tool_error ? 'untested — see below' : 'not supported — can chat but cannot act'));
  if (!r.tools_ok && r.tool_error) add(false, 'Tool error', r.tool_error);
  if (r.warning) add(true, 'Note', r.warning, true);
  const cls = r.tools_ok ? 'status' : 'status err';
  $('#llmStatus').className = cls;
  $('#llmStatus').innerHTML = `<div class="diag">${rows.join('')}</div>`;
}

$('#clearKey').addEventListener('click', async () => {
  if (!confirm('Remove the stored API key?')) return;
  await api('/api/llm_key', { method: 'DELETE' });
  await loadSettings();
  toast('Key removed.');
});

async function setEngine(name) {
  state.engine = name;
  const remote = name === 'openai' || name === 'hybrid';
  ['#engineChip', '#engineChip2'].forEach(sel => $(sel).classList.toggle('remote', remote));
  const label = name === 'hybrid' ? 'hybrid' : name;
  $('#engineName').textContent = label;
  $('#engineName2').textContent = label;
  $('#brandMeta').textContent =
    name === 'hybrid' ? 'local-first · cloud fallback'
    : name === 'openai' ? 'cloud reasoning'
    : 'local-first · offline';
}

/* ── chat ────────────────────────────────────────────── */
function addMsg(role, text, acts = [], reason = '', degraded = false) {
  const el = document.createElement('div');
  el.className = `msg ${role === 'user' ? 'me' : 'bot'}`;
  const bits = [];
  if (reason) bits.push(reason);
  if (acts.length) bits.push('⚙ ' + acts.map(a => esc(a.tool)).join(' · '));
  const foot = bits.length
    ? `<div class="acts">${esc(bits[0])}${acts.length
        ? ` <span style="opacity:.75">· ⚙ ${acts.map(a => esc(a.tool)).join(' · ')}</span>` : ''}</div>`
    : '';
  el.innerHTML = `<div class="bubble">${esc(text)}${foot}</div>`;
  if (degraded) el.querySelector('.bubble').classList.add('degraded');
  $('#chat').appendChild(el);
  $('#chat').scrollTop = $('#chat').scrollHeight;
}

$('#chatForm').addEventListener('submit', async e => {
  e.preventDefault();
  const input = $('#chatInput');
  const text = input.value.trim();
  if (!text) return;
  addMsg('user', text);
  state.history.push({ role: 'user', content: text });
  input.value = '';
  input.style.height = 'auto';

  const typing = document.createElement('div');
  typing.className = 'msg bot';
  typing.innerHTML = '<div class="bubble"><div class="typing"><i></i><i></i><i></i></div></div>';
  $('#chat').appendChild(typing);
  $('#chat').scrollTop = $('#chat').scrollHeight;

  try {
    const r = await api('/api/chat', { method: 'POST', body: { message: text, history: state.history.slice(-10) } });
    typing.remove();
    if (r.engine) await setEngine(r.engine);
    addMsg('assistant', r.reply, r.actions || [], r.routing_reason, r.degraded);
    state.history.push({ role: 'assistant', content: r.reply });
    if ((r.actions || []).some(a => !['daily_brief', 'list_tasks', 'recall', 'list_events', 'free_slots', 'find_conflicts'].includes(a.tool))) {
      load(); loadDashboard();
    }
  } catch (err) {
    typing.remove();
    addMsg('assistant', `Error: ${err.message}`);
  }
});

/* ── modal ───────────────────────────────────────────── */
const FIELDS = {
  add: [
    ['title', 'text', 'Task title', true],
    ['due_at', 'text', 'Due — e.g. "friday 5pm", "tomorrow 9am", "26 sep 14:00"'],
    ['priority', 'select', null, false, [1, 2, 3], ['P1 · high', 'P2 · normal', 'P3 · low']],
    ['project', 'text', 'Project (optional)'],
    ['notes', 'textarea', 'Notes'],
  ],
  addevent: [
    ['title', 'text', 'Title', true],
    ['start_at', 'text', 'Start — e.g. "tomorrow 4pm"'],
    ['end_at', 'text', 'End — e.g. "tomorrow 6pm"'],
    ['kind', 'select', null, false, ['class', 'meeting', 'study', 'personal', 'deadline', 'travel']],
    ['location', 'text', 'Location'],
  ],
  adddraft: [
    ['audience', 'text', 'To', true],
    ['subject', 'text', 'Subject'],
    ['channel', 'select', null, false, ['email', 'message', 'note']],
    ['body', 'textarea', 'Body', true],
  ],
};

let modalMode = null;

function openModal(mode) {
  modalMode = mode;
  const titles = { add: 'New task', addevent: 'Block time', adddraft: 'New draft' };
  $('#modalTitle').textContent = titles[mode];
  $('#modalFields').innerHTML = FIELDS[mode].map(([name, type, label, req, opts, optLabels]) => {
    const lab = label || name;
    const control = type === 'select'
      ? `<select name="${name}">${(opts || []).map((o, i) =>
          `<option value="${esc(o)}" ${i === 1 ? 'selected' : ''}>${esc((optLabels || opts)[i])}</option>`).join('')}</select>`
      : type === 'textarea'
        ? `<textarea name="${name}" rows="4" ${req ? 'required' : ''}></textarea>`
        : `<input name="${name}" type="${type}" ${req ? 'required' : ''}>`;
    return `<label>${esc(lab)}${req ? ' *' : ''}</label>${control}`;
  }).join('');
  $('#modalNote').textContent = mode === 'add' ? 'Leave due blank for no deadline' : '';
  $('#modal').hidden = false;
  setTimeout(() => $('#modalFields input, #modalFields textarea')?.focus(), 30);
}

function closeModal() { $('#modal').hidden = true; modalMode = null; }

$('#modalForm').addEventListener('submit', async e => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(e.target).entries());
  if (data.priority) data.priority = Number(data.priority);
  const routes = { add: ['/api/tasks', 'POST'], addevent: ['/api/events', 'POST'],
                   adddraft: ['/api/drafts', 'POST'], editdraft: ['/api/drafts', 'PATCH'] };
  let path, method;
  if (modalMode === 'editdraft') { path = `/api/drafts/${data.id}`; method = 'PATCH'; }
  else { [path, method] = routes[modalMode]; }
  try {
    const r = await api(path, { method, body: data });
    // Overlaps are advisory, not blocking: save it, then flag it loudly.
    toast(r.warning || 'Saved.', !!r.warning);
    closeModal();
    load(); loadDashboard();
  } catch (err) { toast(err.message, true); }
});

$('#modalClose').addEventListener('click', closeModal);
$('#modalCancel').addEventListener('click', closeModal);
$('#modal').addEventListener('click', e => { if (e.target.id === 'modal') closeModal(); });

// Escape always closes, so the dialog can never trap you.
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('#modal').hidden) { closeModal(); return; }
  if (!$('#chatInput')) return;
  $('#chatInput').value = '';           // Esc in the composer clears a draft
  $('#chatInput').style.height = 'auto';
});

/* ── row actions (delegated) ─────────────────────────── */
document.addEventListener('click', async e => {
  const tick = e.target.closest('[data-act="toggle"]');
  if (tick) {
    const id = e.target.closest('.row').dataset.id;
    const row = e.target.closest('.row');
    const done = tick.classList.contains('on');
    await api(`/api/tasks/${id}`, { method: 'PATCH', body: { status: done ? 'open' : 'done' } });
    return load(), loadDashboard();
  }
  const del = e.target.closest('[data-act="del"]');
  if (del) {
    const row = del.closest('.row');
    if (confirm(`Delete "${row.querySelector('.title').textContent.trim()}"? This cannot be undone.`)) {
      await api(`/api/tasks/${row.dataset.id}`, { method: 'DELETE' });
      toast('Task deleted.'); load(); loadDashboard();
    }
    return;
  }
  const de = e.target.closest('[data-act="delevent"]');
  if (de) {
    if (confirm('Remove this event?')) {
      await api(`/api/events/${de.closest('.tl-item').dataset.id}`, { method: 'DELETE' });
      toast('Event removed.'); load(); loadDashboard();
    }
    return;
  }
  const dd = e.target.closest('[data-act="deldraft"]');
  if (dd) {
    await api(`/api/drafts/${dd.closest('.row').dataset.id}`, { method: 'DELETE' });
    toast('Draft deleted.'); load();
    return;
  }
  const dm = e.target.closest('[data-act="delmem"]');
  if (dm) {
    await api(`/api/memory/${dm.closest('.row').dataset.id}`, { method: 'DELETE' });
    toast('Forgotten.'); load();
    return;
  }
  const vd = e.target.closest('[data-act="viewdraft"]');
  if (vd) {
    const { drafts } = await api('/api/drafts');
    const d = drafts.find(x => x.id == vd.closest('.row').dataset.id);
    if (d) {
      openModal('adddraft');
      $('#modalTitle').textContent = `Draft → ${d.audience || 'recipient'}`;
      $('#modalFields').innerHTML = [
        `<input type="hidden" name="id" value="${esc(d.id)}">`,
        `<label>To</label><input name="audience" value="${esc(d.audience)}">`,
        `<label>Subject</label><input name="subject" value="${esc(d.subject)}">`,
        `<label>Channel</label><select name="channel">${['email', 'message', 'note'].map(c =>
          `<option value="${c}" ${c === d.channel ? 'selected' : ''}>${c}</option>`).join('')}</select>`,
        `<label>Body</label><textarea name="body" rows="10">${esc(d.body)}</textarea>`,
      ].join('');
      modalMode = 'editdraft';
    }
  }
});

/* ── nav, tabs, day nav ──────────────────────────────── */
$('#nav').addEventListener('click', e => {
  const b = e.target.closest('.nav-item');
  if (b) show(b.dataset.view);
});
$('#taskTabs').addEventListener('click', e => {
  const t = e.target.closest('.tab');
  if (!t) return;
  $$('.tab').forEach(x => x.classList.remove('is-active'));
  t.classList.add('is-active');
  state.taskScope = t.dataset.scope;
  loadTasks();
});
$('#dayPicker').addEventListener('change', e => { state.day = e.target.value; loadSchedule(); });
$('#dayNext').addEventListener('click', () => { state.day = shiftDay(1); loadSchedule(); });
$('#dayPrev').addEventListener('click', () => { state.day = shiftDay(-1); loadSchedule(); });
function shiftDay(n) {
  const d = new Date(state.day + 'T12:00:00');
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
}
document.addEventListener('click', e => {
  const q = e.target.closest('[data-quick]');
  if (q) openModal(q.dataset.quick);
});

/* ── settings ────────────────────────────────────────── */
$('#saveLlm').addEventListener('click', saveLlm);

$('#saveSelf').addEventListener('click', async () => {
  await api('/api/settings', { method: 'POST', body: {
    owner: $('#setOwner').value, tz: $('#setTz').value, custom_prompt: $('#setPrompt').value,
  }});
  toast('Saved.');
});

$('#memForm').addEventListener('submit', async e => {
  e.preventDefault();
  await api('/api/memory', { method: 'POST', body: { text: $('#memText').value, kind: $('#memKind').value } });
  $('#memText').value = '';
  loadMemory();
  toast('Remembered.');
});

/* ── theme ───────────────────────────────────────────── */
const savedTheme = localStorage.getItem('pa-theme');
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
$('#themeBtn').addEventListener('click', () => {
  const next = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('pa-theme', next);
});

/* ── search ──────────────────────────────────────────── */
let searchT;
$('#search').addEventListener('input', e => {
  clearTimeout(searchT);
  const q = e.target.value.trim();
  if (!q) return load();
  searchT = setTimeout(async () => {
    const r = await api(`/api/search?q=${encodeURIComponent(q)}`);
    show('tasks');
    const all = [
      ...r.tasks.map(t => ({ ...t, due_human: '', priority_label: 'P2', _hit: 1 })),
      ...r.events.map(e => ({ id: -e.id, title: e.title, status: 'open', priority: 3, _hit: 1 })),
      ...r.memory.map(m => ({ id: -m.id, title: m.text, status: 'open', priority: 3, _hit: 1 })),
    ];
    $('#taskList').innerHTML = all.length
      ? all.map(t => `<div class="row"><div class="grow">
          <div class="title">${esc(t.title)}</div>
          <div class="meta"><span>${t._hit && t.due_at ? esc(t.due_at) : 'match'}</span></div>
        </div></div>`).join('')
      : empty('No matches.');
  }, 260);
});

/* ── boot ────────────────────────────────────────────── */
(async function init() {
  const h = await api('/api/health').catch(() => ({ engine: 'local' }));
  await setEngine(h.engine);
  await loadDashboard();
  if (window.innerWidth < 1180) $('#chatInput').focus();
})();
