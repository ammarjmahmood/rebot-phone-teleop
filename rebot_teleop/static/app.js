import { Twin } from './twin.js';

const $ = id => document.getElementById(id);
const el = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid !== null && kid !== undefined) e.append(kid.nodeType ? kid : document.createTextNode(kid));
  return e;
};
const state = { arms: {}, teleop: {}, sequencer: {}, lift: {}, cameras: {}, links: [], tab: 'home', camsPaused: false, seq: null, seqMode: 'point' };
const twins = {};

function toast(text, ok = false) {
  const t = el('div', { class: ok ? 'ok' : '' }, text);
  $('toast').append(t);
  setTimeout(() => t.remove(), ok ? 2500 : 6000);
}

async function api(path, body, method) {
  const response = await fetch(path, { method: method || (body === undefined ? 'GET' : 'POST'), headers: body === undefined ? {} : { 'Content-Type': 'application/json' }, body: body === undefined ? undefined : JSON.stringify(body) });
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(data.detail || response.statusText);
  return data;
}
const act = (path, body, okText) => api(path, body).then(r => { if (okText) toast(okText, true); return r; }).catch(e => { toast(e.message); throw e; });

document.querySelectorAll('#tabs button').forEach(b => b.onclick = () => showTab(b.dataset.tab));
function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b.dataset.tab === tab));
  document.querySelectorAll('.tab').forEach(s => s.classList.toggle('on', s.id === 'tab-' + tab));
  try { localStorage.setItem('tab', tab); } catch {}
  renderCameras();
  if (tab === 'seq') loadSequences();
  for (const t of Object.values(twins)) t.resize();
}

$('stop').onclick = () => act('/api/stop', {}, 'Holding');
document.addEventListener('keydown', e => { if (e.code === 'Space' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) { e.preventDefault(); act('/api/stop', {}, 'Holding'); } });
$('power-all').onclick = () => act('/api/power', { on: true }).then(r => toast('Powered: ' + (r.powered.join(', ') || 'none') + (r.notes.length ? '. ' + r.notes.join('; ') : ''), r.notes.length === 0));
$('off-all').onclick = () => { if (confirm('Turn torque off on both arms? They will drop under gravity, so hold them.')) act('/api/power', { on: false }, 'Torque off'); };
$('clear').onclick = () => act('/api/clear', {}, 'Cleared');
$('save-pose').onclick = () => { const name = prompt('Pose name'); if (name) act('/api/pose/save', { name }, 'Saved ' + name).then(loadPoses); };
document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => twins.home && twins.home.view(b.dataset.view));

async function loadPoses() {
  const poses = await api('/api/poses');
  const names = [...new Set([...Object.keys(poses.builtin), ...Object.keys(poses.saved)])];
  $('pose-buttons').replaceChildren(...names.map(n => el('button', { class: 'small', onclick: () => act('/api/move', { pose: n, wait: false }) }, n)));
}

function pills() {
  const out = [];
  for (const [name, a] of Object.entries(state.arms)) {
    const cls = a.fault ? 'bad' : a.powered ? 'ok' : a.connected ? 'blue' : 'warn';
    const text = a.fault ? 'fault' : a.powered ? 'powered' : a.connected ? 'connected, torque off' : 'offline';
    out.push(el('span', { class: 'pill ' + cls, title: a.note || '' }, `${name}: ${text}`));
  }
  const t = state.teleop;
  if (t.active) out.push(el('span', { class: 'pill blue' }, `teleop ${t.source || ''} ${t.input_hz} Hz`));
  if (state.sequencer.playing) out.push(el('span', { class: 'pill blue' }, 'playing ' + state.sequencer.playing));
  if (state.sequencer.recording) out.push(el('span', { class: 'pill bad' }, 'recording ' + state.sequencer.recording.seconds + ' s'));
  if (state.sim) out.push(el('span', { class: 'pill' }, 'simulated arms'));
  $('pills').replaceChildren(...out);
}

const armCards = {};
function armCard(name) {
  if (armCards[name]) return armCards[name];
  const body = el('tbody');
  const card = el('div', { class: 'panel' },
    el('div', { class: 'arm-head' }, el('h3', {}, name[0].toUpperCase() + name.slice(1) + ' arm'), el('span', { class: 'pill', id: `pill-${name}` }), el('span', { class: 'grow' }),
      el('button', { class: 'small go', onclick: () => act('/api/power', { arm: name, on: true }, name + ' holding') }, 'Power on'),
      el('button', { class: 'small', onclick: () => { if (confirm(`Torque off the ${name} arm? Hold it first.`)) act('/api/power', { arm: name, on: false }, name + ' torque off; move it by hand'); } }, 'Power off')),
    el('div', { class: 'note', id: `note-${name}` }),
    el('table', {}, el('thead', {}, el('tr', {}, el('th', {}, 'Joint'), el('th', {}, 'deg'), el('th', {}, ''), el('th', {}, 'C'), el('th', {}, 'Nm'), el('th', {}, 'grav'), el('th', {}, 'jog'))), body),
    el('div', { class: 'buttons' }, el('span', { class: 'muted' }, 'Gripper'), el('span', { class: 'mono', id: `grip-${name}` }),
      el('button', { class: 'small', onclick: () => act('/api/gripper', { arm: name, state: 'open' }) }, 'Open'),
      el('button', { class: 'small', onclick: () => act('/api/gripper', { arm: name, state: 'closed' }) }, 'Close'),
      el('button', { class: 'small', title: 'Use when the reading is a whole turn off', onclick: () => act('/api/gripper', { arm: name, calibrate: 'open' }, 'Gripper reading set to open') }, 'It is open'),
      el('span', { class: 'grow' }), el('span', { class: 'muted mono', id: `tip-${name}` })));
  for (let i = 0; i < 6; i++) {
    body.append(el('tr', {}, el('td', {}, 'J' + (i + 1)), el('td', { id: `j-${name}-${i}` }), el('td', {}, el('div', { class: 'bar' }, el('i', { id: `b-${name}-${i}` }))), el('td', { id: `t-${name}-${i}`, class: 'temp' }), el('td', { id: `e-${name}-${i}` }), el('td', { id: `g-${name}-${i}` }),
      el('td', {}, el('button', { class: 'small', onclick: () => act('/api/jog', { arm: name, joint: i, delta_deg: -5 }) }, '-'), el('button', { class: 'small', onclick: () => act('/api/jog', { arm: name, joint: i, delta_deg: 5 }) }, '+'))));
  }
  $('arm-cards').append(card);
  armCards[name] = card;
  return card;
}

function renderArms() {
  for (const [name, a] of Object.entries(state.arms)) {
    armCard(name);
    const pill = $(`pill-${name}`);
    pill.className = 'pill ' + (a.fault ? 'bad' : a.powered ? 'ok' : a.connected ? 'blue' : 'warn');
    pill.textContent = a.fault ? 'fault' : a.powered ? `powered ${a.loop_hz} Hz` : a.connected ? 'torque off, hand guide' : 'offline';
    $(`note-${name}`).textContent = [a.fault, a.note, a.warm].filter(Boolean).join('. ');
    $(`note-${name}`).className = a.fault ? 'fault' : 'note';
    for (let i = 0; i < 6; i++) {
      const [lo, hi] = a.limits_deg[i];
      $(`j-${name}-${i}`).textContent = a.joints_deg[i].toFixed(1);
      $(`b-${name}-${i}`).style.width = (100 * (a.joints_deg[i] - lo) / (hi - lo)).toFixed(0) + '%';
      const t = a.temperatures_c[i];
      const cell = $(`t-${name}-${i}`);
      cell.textContent = t == null ? '' : t.toFixed(0);
      cell.className = 'temp' + (t >= 85 ? ' hot' : t >= 70 ? ' warm' : '');
      $(`e-${name}-${i}`).textContent = a.effort_nm[i].toFixed(1);
      $(`g-${name}-${i}`).textContent = a.gravity_nm[i].toFixed(1);
    }
    $(`grip-${name}`).textContent = (a.gripper_m * 100).toFixed(1) + ' cm';
    $(`tip-${name}`).textContent = 'tip ' + a.tip_xyz.map(v => v.toFixed(3)).join(', ');
  }
}

function liftWidget(id) {
  const box = $(id);
  if (!box.dataset.built) {
    box.dataset.built = '1';
    const hold = dir => {
      let timer = null;
      const b = el('button', { class: 'hold' }, dir === 'up' ? 'Table up' : 'Table down');
      const go = e => { e.preventDefault(); api('/api/lift', { action: dir, hold: true }).catch(err => toast(err.message)); timer = setInterval(() => api('/api/lift', { action: dir, hold: true }).catch(() => {}), 200); };
      const end = () => { if (timer) { clearInterval(timer); timer = null; api('/api/lift', { action: 'stop' }).catch(() => {}); } };
      b.addEventListener('pointerdown', go);
      ['pointerup', 'pointerleave', 'pointercancel'].forEach(n => b.addEventListener(n, end));
      return b;
    };
    box.append(el('b', {}, 'Table lift'), hold('up'), hold('down'), el('button', { onclick: () => act('/api/lift', { action: 'stop' }) }, 'Stop'),
      el('label', {}, 'Speed', el('input', { type: 'range', min: 10, max: 100, step: 5, class: 'lift-speed', onchange: e => act('/api/lift', { speed: +e.target.value }) })), el('span', { class: 'muted mono lift-state' }));
  }
  const l = state.lift;
  box.querySelector('.lift-state').textContent = (l.connected ? `${l.state}, ticks ${l.ticks ?? ''}` : (l.note || 'not connected'));
  const speed = box.querySelector('.lift-speed');
  if (document.activeElement !== speed && l.speed) speed.value = l.speed;
}

const CAMERA_ORDER = ['torso', 'left', 'right'];
function cameraList() {
  const names = Object.keys(state.cameras);
  return CAMERA_ORDER.filter(n => names.includes(n)).concat(names.filter(n => !CAMERA_ORDER.includes(n)));
}
function renderCameras() {
  const targets = { home: 'cams-home', policy: 'cams-policy' };
  for (const [tab, id] of Object.entries(targets)) {
    const box = $(id);
    const live = state.tab === tab && !state.camsPaused;
    const names = cameraList();
    if (box.dataset.names !== names.join() + live) {
      box.dataset.names = names.join() + live;
      box.replaceChildren(...(names.length ? names.map((n, i) => el('div', { class: 'cam' + (i === 0 ? ' big' : '') }, el('img', { src: live ? `/api/camera/${n}.mjpg` : '', alt: '' }), el('span', {}, n))) : [el('div', { class: 'muted' }, 'No cameras configured')]));
    }
  }
  const tele = $('tele-cam');
  const chosen = state.teleCam || cameraList()[0];
  const want = state.tab === 'teleop' && chosen ? `/api/camera/${chosen}.mjpg` : '';
  if (tele.dataset.src !== want) { tele.dataset.src = want; tele.src = want; }
  const buttons = $('tele-cam-buttons');
  if (buttons.dataset.names !== cameraList().join()) {
    buttons.dataset.names = cameraList().join();
    buttons.replaceChildren(...cameraList().map(n => el('button', { class: 'small', onclick: () => { state.teleCam = n; renderCameras(); } }, n)));
  }
}
$('cams-toggle').onclick = () => { state.camsPaused = !state.camsPaused; $('cams-toggle').textContent = state.camsPaused ? 'Resume' : 'Pause'; renderCameras(); };

function renderTeleop() {
  const t = state.teleop;
  const link = state.links[0];
  if (link) {
    if ($('quest-link').href !== link.quest) {
      $('quest-link').href = link.quest; $('quest-link').textContent = link.quest;
      $('phone-link').href = link.phone; $('phone-link').textContent = link.phone;
      $('quest-qr').replaceChildren(el('img', { src: '/api/qr.svg?text=' + encodeURIComponent(link.quest) }));
      $('phone-qr').replaceChildren(el('img', { src: '/api/qr.svg?text=' + encodeURIComponent(link.phone) }));
    }
  }
  if (state.gist) { if (state.gist.url) $('gist-link').href = state.gist.url; $('gist-status').textContent = state.gist.status || ''; }
  if (!t.settings) return;
  const s = t.settings;
  if (document.activeElement !== $('tele-view')) $('tele-view').value = s.view;
  if (document.activeElement !== $('tele-scale')) $('tele-scale').value = s.scale;
  $('tele-scale-v').textContent = Number(s.scale).toFixed(1) + 'x';
  $('tele-rot').checked = !!s.rotation;
  $('tele-gaze').checked = !!s.follow_gaze;
  const lines = [`teleop ${t.active ? 'on (' + t.source + ')' : 'off'}   input ${t.input_hz} Hz   loop ${t.loop_hz} Hz`];
  for (const [n, h] of Object.entries(t.hands || {})) {
    const a = state.arms[n] || {};
    lines.push(`${n.padEnd(6)} ${a.powered ? 'ready' : a.connected ? 'torque off' : 'offline'}  ${h.engaged ? 'MOVING' : 'holding'}  reach error ${h.reach_error_cm} cm  input age ${h.age_ms ?? '-'} ms`);
  }
  $('tele-status').textContent = lines.join('\n');
}
$('tele-start').onclick = () => act('/api/teleop/start', { source: 'dashboard' }).then(r => toast('Ready: ' + (r.powered.join(', ') || 'no arm') + (r.offline.length ? '. Offline: ' + r.offline.join(', ') : ''), !r.offline.length));
$('tele-stop').onclick = () => act('/api/teleop/stop', {}, 'Teleop ended; arms holding');
$('tele-view').onchange = e => act('/api/teleop/settings', { view: e.target.value });
$('tele-scale').oninput = e => { $('tele-scale-v').textContent = Number(e.target.value).toFixed(1) + 'x'; };
$('tele-scale').onchange = e => act('/api/teleop/settings', { scale: +e.target.value });
$('tele-rot').onchange = e => act('/api/teleop/settings', { rotation: e.target.checked });
$('tele-gaze').onchange = e => act('/api/teleop/settings', { follow_gaze: e.target.checked });

async function loadSequences(select) {
  const data = await api('/api/seq');
  const items = data.sequences;
  if (select) state.seqName = select;
  if (!state.seqName && items.length) state.seqName = items[0].name;
  if (!state.seqName) state.seqName = 'Sequence 1';
  $('seq-items').replaceChildren(...items.map(s => el('div', { class: s.name === state.seqName ? 'on' : '', onclick: () => { state.seqName = s.name; loadSequences(); } }, el('span', {}, s.name), el('small', {}, `${s.steps} steps, ${s.duration_s}s`))));
  state.seq = await api('/api/seq/' + encodeURIComponent(state.seqName));
  renderSequence();
}

function describe(step) {
  if (step.type === 'pose') return Object.entries(step.arms).map(([a, q]) => `${a} ${q.map(v => v.toFixed(0)).join(' ')}`).join('   ');
  if (step.type === 'track') { const d = (step.t[step.t.length - 1] - step.t[0]).toFixed(1); return `${Object.keys(step.arms).join(' + ')} motion, ${d} s, ${step.t.length} samples`; }
  if (step.type === 'gripper') return `${step.arm} gripper to ${(step.width * 100).toFixed(1)} cm`;
  if (step.type === 'wait') return `wait`;
  if (step.type === 'lift') return `table ${step.action}`;
  return step.type;
}

async function saveSequence() {
  state.seq.name = $('seq-name').value.trim() || state.seq.name;
  const old = state.seqName;
  const saved = await api('/api/seq/' + encodeURIComponent(old), state.seq, 'PUT');
  state.seqName = saved.name;
  if (old !== saved.name) loadSequences(saved.name);
}

let dragFrom = null;
function renderSequence() {
  const seq = state.seq;
  if (!seq) return;
  if (document.activeElement !== $('seq-name')) $('seq-name').value = seq.name;
  $('seq-loop').checked = !!seq.loop;
  const progress = state.sequencer.progress;
  const playingThis = state.sequencer.playing === seq.name;
  $('seq-steps').replaceChildren(...seq.steps.map((step, i) => {
    const field = (key, label, unit, fallback) => el('label', {}, label, el('input', { type: 'number', step: '0.1', value: step[key] ?? fallback, onchange: e => { step[key] = +e.target.value; saveSequence(); } }), unit);
    const extra = [];
    if (step.type === 'pose') extra.push(field('speed_deg_s', 'speed', 'deg/s', 45), field('pause_s', 'pause', 's', 0));
    if (step.type === 'track') extra.push(field('speed', 'speed', 'x', 1), field('pause_s', 'pause', 's', 0));
    if (step.type === 'wait' || step.type === 'lift') extra.push(field('seconds', '', 's', 1));
    if (step.type === 'lift') extra.push(el('select', { onchange: e => { step.action = e.target.value; saveSequence(); } }, ...['up', 'down'].map(v => el('option', { value: v, selected: step.action === v }, v))));
    if (step.type === 'gripper') extra.push(el('select', { onchange: e => { step.arm = e.target.value; saveSequence(); } }, ...Object.keys(state.arms).map(v => el('option', { value: v, selected: step.arm === v }, v))), field('width', 'width', 'm', 0));
    const li = el('li', { draggable: true, class: playingThis && progress && progress.index === i ? 'now' : '' },
      el('span', { class: 'handle' }, '⋮⋮'), el('span', { class: 'kind ' + step.type }, step.type === 'track' ? 'motion' : step.type === 'pose' ? 'point' : step.type),
      el('input', { class: 'label', value: step.label || '', placeholder: describe(step), onchange: e => { step.label = e.target.value; saveSequence(); } }),
      el('span', { class: 'muted small-text' }, step.label ? describe(step) : ''), ...extra,
      el('button', { class: 'small', title: 'Play from here', onclick: () => act(`/api/seq/${encodeURIComponent(seq.name)}/play`, { start: i, loop: false, speed: +$('seq-speed').value }) }, '▶'),
      el('button', { class: 'small', title: 'Move up', onclick: () => { if (i > 0) { seq.steps.splice(i - 1, 0, seq.steps.splice(i, 1)[0]); saveSequence(); renderSequence(); } } }, '↑'),
      el('button', { class: 'small', title: 'Move down', onclick: () => { if (i < seq.steps.length - 1) { seq.steps.splice(i + 1, 0, seq.steps.splice(i, 1)[0]); saveSequence(); renderSequence(); } } }, '↓'),
      el('button', { class: 'small', title: 'Duplicate', onclick: () => { seq.steps.splice(i + 1, 0, JSON.parse(JSON.stringify(step))); saveSequence(); renderSequence(); } }, '⧉'),
      el('button', { class: 'small danger', title: 'Delete step', onclick: () => { seq.steps.splice(i, 1); saveSequence(); renderSequence(); } }, '✕'));
    li.addEventListener('dragstart', () => { dragFrom = i; li.classList.add('drag'); });
    li.addEventListener('dragend', () => li.classList.remove('drag'));
    li.addEventListener('dragover', e => { e.preventDefault(); li.classList.add('over'); });
    li.addEventListener('dragleave', () => li.classList.remove('over'));
    li.addEventListener('drop', e => { e.preventDefault(); li.classList.remove('over'); if (dragFrom === null || dragFrom === i) return; seq.steps.splice(i, 0, seq.steps.splice(dragFrom, 1)[0]); dragFrom = null; saveSequence(); renderSequence(); });
    return li;
  }));
  if (!seq.steps.length) $('seq-steps').append(el('li', { class: 'muted' }, 'No steps yet. Save a point or record a motion.'));
}

function renderSeqStatus() {
  const s = state.sequencer;
  const rec = s.recording;
  $('seq-record').textContent = rec ? `Stop recording (${rec.seconds} s)` : 'Start recording';
  $('seq-rec-note').textContent = rec ? `${rec.samples} samples of ${rec.arms.join(' and ')}` : 'Move the arms by hand (torque off) or with teleop while recording.';
  $('seq-progress').textContent = s.playing ? `Playing ${s.playing}: step ${s.progress ? s.progress.index + 1 : ''} of ${s.progress ? s.progress.of : ''} ${s.progress && s.progress.label ? '(' + s.progress.label + ')' : ''}` : (s.progress && s.progress.error ? 'Stopped: ' + s.progress.error : '');
  const key = (s.playing || '') + (s.progress ? s.progress.index : '');
  if (state.lastSeqKey !== key) { state.lastSeqKey = key; renderSequence(); }
}

document.querySelectorAll('#seq-mode button').forEach(b => b.onclick = () => {
  state.seqMode = b.dataset.mode;
  document.querySelectorAll('#seq-mode button').forEach(x => x.classList.toggle('on', x === b));
  $('seq-point-tools').hidden = state.seqMode !== 'point';
  $('seq-record-tools').hidden = state.seqMode !== 'record';
});
$('seq-new').onclick = () => { const name = prompt('Name for the new sequence'); if (name) { state.seqName = name; api('/api/seq/' + encodeURIComponent(name), { name, steps: [] }, 'PUT').then(() => loadSequences(name)); } };
$('seq-name').onchange = () => saveSequence().then(() => loadSequences());
$('seq-capture').onclick = async () => { await saveSequence(); state.seq = await act(`/api/seq/${encodeURIComponent(state.seqName)}/capture`, { arm: $('seq-arm').value || null, speed_deg_s: +$('seq-step-speed').value }); renderSequence(); loadSequences(); };
$('seq-record').onclick = async () => {
  if (state.sequencer.recording) { await saveSequence(); await act('/api/seq/record/stop', { name: state.seqName }, 'Motion added'); loadSequences(); }
  else act('/api/seq/record/start', { arm: $('seq-arm').value || null });
};
const addStep = step => saveSequence().then(() => act(`/api/seq/${encodeURIComponent(state.seqName)}/step`, { step })).then(s => { state.seq = s; renderSequence(); });
$('seq-add-wait').onclick = () => addStep({ type: 'wait', seconds: 1 });
$('seq-add-open').onclick = () => addStep({ type: 'gripper', arm: $('seq-arm').value || Object.keys(state.arms)[0], width: 0.05 });
$('seq-add-close').onclick = () => addStep({ type: 'gripper', arm: $('seq-arm').value || Object.keys(state.arms)[0], width: 0 });
$('seq-add-lift').onclick = () => addStep({ type: 'lift', action: 'up', seconds: 2 });
$('seq-loop').onchange = e => { state.seq.loop = e.target.checked; saveSequence(); };
$('seq-speed').oninput = e => { $('seq-speed-v').textContent = Number(e.target.value).toFixed(2) + 'x'; };
$('seq-play').onclick = () => saveSequence().then(() => act(`/api/seq/${encodeURIComponent(state.seqName)}/play`, { loop: $('seq-loop').checked, speed: +$('seq-speed').value }));
$('seq-stop').onclick = () => act('/api/seq/stop', {}, 'Stopped');
$('seq-delete').onclick = () => { if (confirm(`Delete ${state.seqName}?`)) api('/api/seq/' + encodeURIComponent(state.seqName), undefined, 'DELETE').then(() => { state.seqName = null; loadSequences(); }); };

let capAfter = 0, capTimer = null;
function capEvent(e) {
  const log = $('cap-log');
  let node;
  if (e.kind === 'image') node = el('div', { class: 'ev' }, el('div', { class: 'muted' }, 'looked: ' + e.text), el('img', { src: 'data:image/jpeg;base64,' + e.image }));
  else node = el('div', { class: 'ev ' + e.kind + (e.ok === false || e.kind === 'error' ? ' bad' : '') }, e.kind === 'done' ? 'Done: ' + e.text : e.text);
  log.append(node);
  log.scrollTop = log.scrollHeight;
}
async function pollCap() {
  try {
    const r = await api('/api/cap?after=' + capAfter);
    for (const e of r.events) { capAfter = e.id; capEvent(e); }
    if (!r.running && capTimer) { clearInterval(capTimer); capTimer = null; $('cap-run').disabled = false; }
  } catch {}
}
$('cap-run').onclick = async () => {
  $('cap-log').replaceChildren();
  capAfter = 0;
  await act('/api/cap', { instruction: $('cap-text').value, model: $('cap-model').value.trim() || null, effort: $('cap-effort').value });
  $('cap-run').disabled = true;
  capTimer = capTimer || setInterval(pollCap, 700);
};
$('cap-stop').onclick = () => act('/api/cap/stop', {}, 'Stopped');
$('py-run').onclick = async () => {
  $('py-out').textContent = 'Running...';
  try { const r = await api('/api/policy/run', { code: $('py-code').value }); $('py-out').textContent = (r.ok ? '' : 'FAILED\n') + r.output + `\n(${r.seconds} s)`; }
  catch (e) { $('py-out').textContent = e.message; }
};

function connect() {
  const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/state');
  ws.onmessage = event => {
    const m = JSON.parse(event.data);
    state.arms = m.arms; state.teleop = m.teleop; state.sequencer = m.sequencer; state.lift = m.lift; state.sim = m.sim;
    if (m.cameras) state.cameras = m.cameras;
    if (m.links) state.links = m.links;
    if (m.gist) state.gist = m.gist;
    for (const t of Object.values(twins)) t.update(m.twin, m.arms);
    pills();
    if (state.tab === 'home') { renderArms(); liftWidget('lift-home'); }
    if (state.tab === 'teleop') { renderTeleop(); liftWidget('lift-tele'); }
    if (state.tab === 'seq') renderSeqStatus();
    if (m.cameras) renderCameras();
    $('mode-note').textContent = Object.values(state.arms).some(a => a.connected && !a.powered) ? 'Torque off arms can be moved by hand; the twin follows them' : '';
  };
  ws.onclose = () => { $('pills').replaceChildren(el('span', { class: 'pill bad' }, 'server not reachable, retrying')); setTimeout(connect, 1000); };
}

(async () => {
  try { const t = localStorage.getItem('tab'); if (t) showTab(t); } catch {}
  connect();
  loadPoses().catch(() => {});
  try {
    const scene = await api('/api/scene');
    twins.home = new Twin($('twin-home'), scene);
    twins.tele = new Twin($('twin-tele'), scene);
    twins.seq = new Twin($('twin-seq'), scene);
  } catch (e) { $('twin-note').textContent = 'Twin unavailable: ' + e.message; }
  pollCap();
})();
