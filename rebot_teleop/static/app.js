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
const state = { arms: {}, teleop: {}, sequencer: {}, lift: {}, cameras: {}, links: [], tab: 'home', camsPaused: false, seq: null };
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
  if (tab === 'seq') loadSequences().catch(e => toast(e.message));
  for (const t of Object.values(twins)) t.resize();
}

$('stop').onclick = () => act('/api/stop', {}, 'Holding');
document.addEventListener('keydown', e => { if (e.code === 'Space' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) { e.preventDefault(); act('/api/stop', {}, 'Holding'); } });
document.querySelectorAll('.heavy-switch').forEach(box => box.onchange = () => act('/api/limits', { heavy: box.checked }, box.checked ? 'Heavy poses on: no reach limit or sequence rests; the arm relaxes only at 95% of the budget' : 'Heavy poses off: normal protection'));
document.querySelectorAll('.grip-slider').forEach(bar => { bar.oninput = () => showGrip(bar.value / 100); bar.onchange = () => act('/api/limits', { grip: bar.value / 100 }, `Grip strength ${bar.value}%: ${bar.value < 100 ? 'the grippers close slower and hold lighter' : 'full strength'}`); });
function showGrip(value) { document.querySelectorAll('.grip-value').forEach(node => { node.textContent = Math.round(value * 100) + '%'; }); }
$('motor-report').onclick = () => window.open('/api/motorlog/report?format=text&hours=24', '_blank');
$('hold-here').onclick = () => act('/api/hold', {}).then(r => toast('Holding: ' + (r.holding.join(', ') || 'no arm') + (r.notes.length ? '. ' + r.notes.join('; ') : ''), r.holding.length > 0));
$('power-all').onclick = () => act('/api/power', { on: true }).then(r => toast('Powered: ' + (r.powered.join(', ') || 'none') + (r.notes.length ? '. ' + r.notes.join('; ') : ''), r.notes.length === 0));
for (const arm of ['left', 'right']) $('power-' + arm).onclick = () => act('/api/power', { arm, on: true }, arm + ' arm holding');
$('off-all').onclick = () => { if (confirm('Turn torque off on both arms? They will drop under gravity, so hold them.')) act('/api/power', { on: false }, 'Torque off'); };
$('clear').onclick = () => act('/api/clear', {}, 'Cleared');
$('save-pose').onclick = () => { const name = prompt('Pose name'); if (name) act('/api/pose/save', { name }, 'Saved ' + name).then(loadPoses); };
document.querySelectorAll('[data-view]').forEach(b => b.onclick = () => twins.home && twins.home.view(b.dataset.view));

async function loadPoses() {
  const poses = await api('/api/poses');
  const names = [...new Set([...Object.keys(poses.builtin), ...Object.keys(poses.saved)])];
  $('pose-buttons').replaceChildren(...names.map(n => el('button', { class: 'small', onclick: () => act('/api/move', { pose: n, wait: false }) }, n)));
}

function budgetLevel(a) {
  if (a.rest) return 'rest';
  const h = a.heat;
  if (!h) return 'ok';
  return h.pct >= 80 ? 'rest' : h.pct >= 50 && h.seconds !== null ? 'warm' : 'ok';
}

function budgetText(a) {
  if (a.rest) {
    const r = a.rest;
    if (r.phase === 'returning') return 'recovered, going back';
    if (r.phase === 'recovering') return r.comes_back ? `resting, back in about ${r.eta_s} s` : 'cannot pull in further from here; bring the hand in or down';
    return 'pulling the hand in until the load is light';
  }
  const h = a.heat;
  if (!h) return a.powered ? 'motor budget full' : '';
  if (h.seconds !== null) return `joint ${h.joint} has used ${h.pct}% here, ${h.seconds} s before the arm relaxes itself`;
  return h.pct > 2 ? `joint ${h.joint} at ${h.pct}%, recovering` : 'motor budget full';
}

function pills() {
  const out = [];
  for (const [name, a] of Object.entries(state.arms)) {
    const cls = a.fault ? 'bad' : a.powered ? 'ok' : a.connected ? 'blue' : 'warn';
    const text = a.cooling ? 'over temperature, cooling at home' : a.resting ? 'pulling in' : a.fault ? 'fault' : a.powered ? 'powered' : a.connected ? 'connected, torque off' : 'offline';
    out.push(el('span', { class: 'pill ' + cls, title: a.note || '' }, `${name}: ${text}`));
    for (const tag of a.alert_tags || []) out.push(el('span', { class: 'pill warn', title: a.alert || '' }, `${name}: ${tag}`));
  }
  const t = state.teleop;
  if (t.active) out.push(el('span', { class: 'pill blue' }, `teleop ${t.source || ''} ${t.input_hz} Hz`));
  if (state.sequencer.playing) out.push(el('span', { class: 'pill blue' }, 'playing ' + state.sequencer.playing));
  if (state.sequencer.recording) out.push(el('span', { class: 'pill bad' }, 'recording ' + state.sequencer.recording.seconds + ' s'));
  if (state.heavy) out.push(el('span', { class: 'pill warn', title: 'No reach limit or sequence rests; the arm relaxes only at 95% of the budget' }, 'heavy poses on'));
  if (state.sim) out.push(el('span', { class: 'pill' }, 'simulated arms'));
  $('pills').replaceChildren(...out);
}

const TORQUE = { series: '#3987e5', text: '#e6e8eb', muted: '#8b93a0', grid: '#2a2f38', surface: '#171a1f', cols: 3, rows: 2, gap: 14, top: 16 };
const torqueData = {};
const torqueHover = {};
function torquePanel(canvas, j) {
  const pw = (canvas.clientWidth - TORQUE.gap * (TORQUE.cols - 1)) / TORQUE.cols, ph = (canvas.clientHeight - TORQUE.gap * (TORQUE.rows - 1)) / TORQUE.rows;
  return { x0: (j % TORQUE.cols) * (pw + TORQUE.gap), y0: Math.floor(j / TORQUE.cols) * (ph + TORQUE.gap), pw, ph };
}
function drawTorque(name) {
  const canvas = $(`torque-${name}`), d = torqueData[name];
  if (!canvas || !d || canvas.offsetParent === null) return;
  const ratio = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) { canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio); }
  const g = canvas.getContext('2d');
  g.setTransform(ratio, 0, 0, ratio, 0, 0);
  g.clearRect(0, 0, w, h);
  g.font = '11px system-ui, sans-serif';
  const hover = torqueHover[name];
  for (let j = 0; j < 6; j++) {
    const { x0, y0, pw, ph } = torquePanel(canvas, j), plot = ph - TORQUE.top, peak = d.peak[j], rated = d.rated[j];
    const X = t => x0 + pw * (1 - (d.now - t) / d.seconds), Y = v => y0 + TORQUE.top + plot * (1 - Math.min(v, peak) / peak);
    const shown = d.t.map((t, i) => [t, d.nm[i][j]]).filter(([t]) => d.now - t <= d.seconds);
    const last = shown.length ? shown[shown.length - 1][1] : null, high = shown.length ? Math.max(...shown.map(p => p[1])) : null;
    g.textBaseline = 'top'; g.textAlign = 'left'; g.fillStyle = TORQUE.text;
    g.fillText(`J${j + 1}   ${last === null ? 'torque off' : last.toFixed(1) + ' Nm now'}`, x0, y0);
    if (high !== null) { g.textAlign = 'right'; g.fillStyle = TORQUE.muted; g.fillText(`highest ${high.toFixed(1)}`, x0 + pw, y0); }
    g.setLineDash([]); g.lineWidth = 1; g.strokeStyle = TORQUE.grid;
    g.beginPath(); g.moveTo(x0, Math.round(Y(0)) + 0.5); g.lineTo(x0 + pw, Math.round(Y(0)) + 0.5); g.moveTo(x0, Math.round(Y(peak)) + 0.5); g.lineTo(x0 + pw, Math.round(Y(peak)) + 0.5); g.stroke();
    g.setLineDash([4, 4]); g.strokeStyle = TORQUE.muted; g.beginPath(); g.moveTo(x0, Math.round(Y(rated)) + 0.5); g.lineTo(x0 + pw, Math.round(Y(rated)) + 0.5); g.stroke(); g.setLineDash([]);
    g.textAlign = 'left'; g.fillStyle = TORQUE.muted; g.textBaseline = 'bottom'; g.fillText(`${rated} Nm`, x0 + 2, Y(rated) - 2);
    g.textBaseline = 'top'; g.fillText(`${peak} Nm max`, x0 + 2, Y(peak) + 3);
    g.strokeStyle = TORQUE.series; g.lineWidth = 2; g.lineJoin = 'round'; g.beginPath();
    let prev = null;
    for (const [t, v] of shown) { if (prev === null || t - prev > 0.5) g.moveTo(X(t), Y(v)); else g.lineTo(X(t), Y(v)); prev = t; }
    g.stroke();
    if (hover && hover.t !== null) {
      const near = shown.reduce((best, p) => best === null || Math.abs(p[0] - hover.t) < Math.abs(best[0] - hover.t) ? p : best, null);
      if (near && Math.abs(near[0] - hover.t) < 0.5) {
        g.strokeStyle = TORQUE.muted; g.lineWidth = 1; g.beginPath(); g.moveTo(X(near[0]), y0 + TORQUE.top); g.lineTo(X(near[0]), y0 + ph); g.stroke();
        g.fillStyle = TORQUE.series; g.strokeStyle = TORQUE.surface; g.lineWidth = 2; g.beginPath(); g.arc(X(near[0]), Y(near[1]), 4, 0, 6.3); g.fill(); g.stroke();
      }
    }
  }
}
function torqueHoverEvents(name) {
  const canvas = $(`torque-${name}`), tip = $(`torque-tip-${name}`);
  canvas.addEventListener('mousemove', e => {
    const d = torqueData[name], box = canvas.getBoundingClientRect(), x = e.clientX - box.left, y = e.clientY - box.top;
    if (!d) return;
    const j = Math.min(TORQUE.cols - 1, Math.floor(x / (box.width / TORQUE.cols))) + TORQUE.cols * Math.min(TORQUE.rows - 1, Math.floor(y / (box.height / TORQUE.rows)));
    const { x0, pw } = torquePanel(canvas, j), t = d.now - d.seconds * (1 - Math.min(Math.max((x - x0) / pw, 0), 1));
    let best = -1;
    d.t.forEach((v, i) => { if (best < 0 || Math.abs(v - t) < Math.abs(d.t[best] - t)) best = i; });
    if (best < 0 || Math.abs(d.t[best] - t) > 0.5) { torqueHover[name] = null; tip.style.display = 'none'; drawTorque(name); return; }
    torqueHover[name] = { t: d.t[best] };
    tip.textContent = `${Math.round(d.now - d.t[best])} s ago\n` + d.nm[best].map((v, i) => `J${i + 1}  ${v.toFixed(1)} Nm`).join('\n');
    tip.style.display = 'block';
    tip.style.left = Math.min(x + 14, box.width - 110) + 'px';
    tip.style.top = Math.max(y - 20, 0) + 'px';
    drawTorque(name);
  });
  canvas.addEventListener('mouseleave', () => { torqueHover[name] = null; tip.style.display = 'none'; drawTorque(name); });
}
setInterval(() => {
  if (state.tab !== 'home' || document.hidden) return;
  fetch('/api/torque').then(r => r.ok ? r.json() : null).then(r => {
    if (!r) return;
    for (const [name, d] of Object.entries(r.arms)) { torqueData[name] = { ...d, now: r.now, seconds: r.seconds }; drawTorque(name); }
  }).catch(() => {});
}, 500);

const armCards = {};
function armCard(name) {
  if (armCards[name]) return armCards[name];
  const body = el('tbody');
  const card = el('div', { class: 'panel' },
    el('div', { class: 'arm-head' }, el('h3', {}, name[0].toUpperCase() + name.slice(1) + ' arm'), el('span', { class: 'pill', id: `pill-${name}` }), el('span', { class: 'grow' }),
      el('button', { class: 'small go', onclick: () => act('/api/power', { arm: name, on: true }, name + ' holding') }, 'Power on'),
      el('button', { class: 'small', onclick: () => { if (confirm(`Torque off the ${name} arm? Hold it first.`)) act('/api/power', { arm: name, on: false }, name + ' torque off; move it by hand'); } }, 'Power off')),
    el('div', { class: 'note', id: `note-${name}` }),
    el('div', { class: 'budget', id: `budget-${name}` }),
    el('table', {}, el('thead', {}, el('tr', {}, el('th', {}, 'Joint'), el('th', {}, 'deg'), el('th', {}, ''), el('th', {}, 'C'), el('th', {}, 'Nm'), el('th', {}, 'grav'), el('th', { title: 'How much of what the motor can hold it has used, from the RobStride overload tables' }, 'budget'), el('th', {}, 'jog'))), body),
    el('div', { class: 'muted small-text torque-title' }, 'Torque on each joint over the last minute. The dashed line is what the motor can hold for long while still'),
    el('div', { class: 'torque-wrap' }, el('canvas', { class: 'torque', id: `torque-${name}` }), el('div', { class: 'torque-tip', id: `torque-tip-${name}` })),
    el('div', { class: 'buttons' }, el('span', { class: 'muted' }, 'Gripper'), el('span', { class: 'mono', id: `grip-${name}` }),
      el('button', { class: 'small', onclick: () => act('/api/gripper', { arm: name, state: 'open' }) }, 'Open'),
      el('button', { class: 'small', onclick: () => act('/api/gripper', { arm: name, state: 'closed' }) }, 'Close'),
      el('button', { class: 'small', title: 'Use when the reading is a whole turn off', onclick: () => act('/api/gripper', { arm: name, calibrate: 'open' }, 'Gripper reading set to open') }, 'It is open'),
      el('button', { class: 'small', title: 'Use when the closed reading is a whole turn off', onclick: () => act('/api/gripper', { arm: name, calibrate: 'closed' }, 'Gripper reading set to closed') }, 'It is closed'),
      el('span', { class: 'grow' }), el('span', { class: 'muted mono', id: `tip-${name}` })));
  for (let i = 0; i < 6; i++) {
    body.append(el('tr', {}, el('td', {}, 'J' + (i + 1)), el('td', { id: `j-${name}-${i}` }), el('td', {}, el('div', { class: 'bar' }, el('i', { id: `b-${name}-${i}` }))), el('td', { id: `t-${name}-${i}`, class: 'temp' }), el('td', { id: `e-${name}-${i}` }), el('td', { id: `g-${name}-${i}` }), el('td', {}, el('div', { class: 'bar heat' }, el('i', { id: `h-${name}-${i}` })), el('small', { class: 'mono', id: `hs-${name}-${i}` })),
      el('td', {}, el('button', { class: 'small', onclick: () => act('/api/jog', { arm: name, joint: i, delta_deg: -5 }) }, '-'), el('button', { class: 'small', onclick: () => act('/api/jog', { arm: name, joint: i, delta_deg: 5 }) }, '+'))));
  }
  $('arm-cards').append(card);
  armCards[name] = card;
  torqueHoverEvents(name);
  return card;
}

function renderArms() {
  for (const [name, a] of Object.entries(state.arms)) {
    armCard(name);
    const pill = $(`pill-${name}`);
    pill.className = 'pill ' + (a.fault ? 'bad' : a.powered ? 'ok' : a.connected ? 'blue' : 'warn');
    pill.textContent = a.cooling ? 'over temperature' : a.resting ? 'pulling in' : a.fault ? 'fault' : a.powered ? `powered ${a.loop_hz} Hz` + ((a.supply_v || []).some(v => v != null) ? ', ' + Math.min(...a.supply_v.filter(v => v != null)).toFixed(1) + ' V' : '') : a.connected ? 'torque off, hand guide' : 'offline';
    $(`note-${name}`).textContent = [a.alert, a.fault, a.note, a.warm].filter(Boolean).join('. ');
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
      const pct = a.heat ? a.heat.pct_all[i] : 0, left = a.heat ? a.heat.seconds_all[i] : null;
      const bar = $(`h-${name}-${i}`);
      bar.style.width = Math.min(pct, 100) + '%';
      bar.className = pct >= 80 ? 'rest' : pct >= 50 ? 'warm' : '';
      $(`hs-${name}-${i}`).textContent = left !== null ? ` ${left}s` : '';
    }
    $(`budget-${name}`).textContent = budgetText(a) ? 'Motor budget: ' + budgetText(a) : '';
    $(`budget-${name}`).className = 'budget ' + budgetLevel(a);
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

const CAMERA_ORDER = ['head', 'left', 'right'];
function cameraList() {
  const names = Object.keys(state.cameras);
  return CAMERA_ORDER.filter(n => names.includes(n)).concat(names.filter(n => !CAMERA_ORDER.includes(n)));
}
function renderCameras() {
  const targets = [['home', 'cams-home', n => n === 'head'], ['home', 'cams-wrists', n => n !== 'head'], ['teleop', 'cams-tele', n => n !== 'head'], ['policy', 'cams-policy', () => true]];
  for (const [tab, id, wanted] of targets) {
    const box = $(id);
    const live = state.tab === tab && !state.camsPaused;
    const names = cameraList().filter(wanted);
    if (box.dataset.names !== names.join() + live) {
      box.dataset.names = names.join() + live;
      box.replaceChildren(...(names.length ? names.map((n, i) => el('div', { class: 'cam' + (i === 0 && id !== 'cams-wrists' && id !== 'cams-tele' ? ' big' : '') }, el('img', { src: live ? `/api/camera/${n}.mjpg` : '', alt: '' }), el('span', {}, n))) : [el('div', { class: 'muted' }, 'No cameras configured')]));
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

function renderRecord() {
  const r = state.record;
  if (!r) return;
  $('tele-record').textContent = r.recording ? 'Stop recording' : 'Record teleop';
  $('tele-record-status').textContent = r.error ? 'Recording failed: ' + r.error : r.recording ? `Recording ${Math.round(r.seconds)} s` : r.name ? `Saved ${r.name}, ${Math.round(r.seconds)} s` : '';
}
$('tele-record').onclick = () => act(state.record && state.record.recording ? '/api/record/stop' : '/api/record/start', {}).then(r => { state.record = r; renderRecord(); });

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
  if (state.usb) $('usb-status').textContent = '(' + state.usb.state + ')';
  if (state.gist) { if (state.gist.url) $('gist-link').href = state.gist.url; $('gist-status').textContent = state.gist.status || ''; }
  if (!t.settings) return;
  const s = t.settings;
  if (document.activeElement !== $('tele-view')) $('tele-view').value = s.view;
  if (document.activeElement !== $('tele-scale')) $('tele-scale').value = s.scale;
  $('tele-scale-v').textContent = Number(s.scale).toFixed(1) + 'x';
  $('tele-rot').checked = !!s.rotation;
  $('tele-gaze').checked = !!s.follow_gaze;
  const lines = [`teleop ${t.active ? 'on (' + t.source + ')' : 'off'}   input ${t.input_hz} Hz   loop ${t.loop_hz} Hz   round trip ${t.latency_ms ?? '?'} ms   frames ${t.frames || 0}, dropped ${t.dropped || 0}`];
  for (const [n, h] of Object.entries(t.hands || {})) {
    const a = state.arms[n] || {};
    lines.push(`${n.padEnd(6)} ${a.rest ? 'relaxing' : a.ready ? 'ready' : a.powered ? 'fault' : a.connected ? 'torque off' : 'offline'}  ${h.engaged ? 'MOVING' : 'holding'}${h.reach_limited ? '  REACH LIMITED (joint budget low, bring the hand in)' : ''}  reach error ${h.reach_error_cm} cm  input age ${h.age_ms ?? '-'} ms`);
  }
  $('tele-status').textContent = lines.join('\n');
  $('tele-budget').replaceChildren(...Object.entries(state.arms).map(([n, a]) => {
    const pct = a.heat ? Math.min(a.heat.pct, 100) : 0;
    return el('div', { class: 'budget-row ' + budgetLevel(a) }, el('b', {}, n), el('div', { class: 'bar heat big' }, el('i', { class: budgetLevel(a), style: `width:${a.rest ? 100 : pct}%` })), el('span', {}, a.connected ? (budgetText(a) || 'torque off') : 'offline'));
  }));
}
$('tele-start').onclick = () => act('/api/teleop/start', { source: 'dashboard' }).then(r => toast('Ready: ' + (r.powered.join(', ') || 'no arm') + (r.offline.length ? '. Offline: ' + r.offline.join(', ') : ''), !r.offline.length));
$('tele-stop').onclick = () => act('/api/teleop/stop', {}, 'Teleop ended; arms holding');
$('tele-view').onchange = e => act('/api/teleop/settings', { view: e.target.value });
$('tele-scale').oninput = e => { $('tele-scale-v').textContent = Number(e.target.value).toFixed(1) + 'x'; };
$('tele-scale').onchange = e => act('/api/teleop/settings', { scale: +e.target.value });
$('tele-rot').onchange = e => act('/api/teleop/settings', { rotation: e.target.checked });
$('tele-gaze').onchange = e => act('/api/teleop/settings', { follow_gaze: e.target.checked });

const enc = encodeURIComponent;
const KIND = { pose: 'point', track: 'motion', gripper: 'gripper', wait: 'wait', lift: 'table' };
const SPEEDS = [['slow', 30], ['normal', 90], ['fast', 150]];
const OPEN_M = 0.05;
let seqQueue = Promise.resolve(), seqPending = 0, seqTimer = null, seqStale = false, dragFrom = null;

function seqSend(request) {
  seqPending++;
  seqQueue = seqQueue.then(request).then(result => {
    seqPending--;
    const seq = result && (result.sequence || (Array.isArray(result.steps) ? result : null));
    if (seq && seqPending === 0) { state.seq = seq; state.seqName = seq.name; renderSequence(); }
    seqSoon();
    return result;
  }).catch(error => { seqPending--; toast(error.message); return loadSequence().catch(() => {}); });
  return seqQueue;
}

function seqSoon() {
  clearTimeout(seqTimer);
  seqTimer = setTimeout(() => { loadList().catch(() => {}); loadPlan(); }, 350);
}

async function loadList() {
  const data = await api('/api/seq');
  state.seqList = data.sequences;
  if (!state.seqName) state.seqName = data.sequences.length ? data.sequences[0].name : 'Recording 1';
  $('seq-items').replaceChildren(...data.sequences.map(s => el('div', { class: s.name === state.seqName ? 'on' : '', onclick: () => loadSequences(s.name) }, el('span', {}, s.name), el('small', {}, `${s.steps} ${s.steps === 1 ? 'step' : 'steps'}, ${Math.round(s.duration_s)} s`))));
  if (!data.sequences.length) $('seq-items').append(el('div', { class: 'muted' }, 'Nothing saved yet'));
  fillTogether(data.sequences);
}

function fillTogether(sequences) {
  for (const side of ['left', 'right']) {
    const select = $('seq-' + side);
    let keep = select.value;
    try { if (!keep) keep = localStorage.getItem('seq-' + side) || ''; } catch {}
    const options = [];
    for (const s of sequences) {
      const motions = s.motions || [];
      const moves = list => list.length ? ` (moves ${list.join(' and ')} arm)` : ' (no arm moves)';
      if (motions.length !== 1 || s.steps !== 1) options.push(el('option', { value: JSON.stringify({ name: s.name }) }, `${s.name}, all ${s.steps} ${s.steps === 1 ? 'step' : 'steps'}`));
      for (const m of motions) options.push(el('option', { value: JSON.stringify({ name: s.name, step: m.step }) }, (motions.length === 1 && s.steps === 1 ? s.name : `${s.name} > ${m.label}`) + moves(m.moves)));
    }
    select.replaceChildren(el('option', { value: '' }, 'Pick a recording'), ...options);
    select.value = options.some(o => o.value === keep) ? keep : '';
  }
  $('seq-both').disabled = !($('seq-left').value && $('seq-right').value);
}
for (const side of ['left', 'right']) $('seq-' + side).onchange = e => { try { localStorage.setItem('seq-' + side, e.target.value); } catch {} $('seq-both').disabled = !($('seq-left').value && $('seq-right').value); };
$('seq-both').onclick = () => seqQueue.then(() => act('/api/seq-together', { left: JSON.parse($('seq-left').value), right: JSON.parse($('seq-right').value), loop: $('seq-loop').checked, speed: +$('seq-speed').value, smooth: +$('seq-smooth').value, power_off: $('seq-poweroff').checked })).catch(() => {});

async function loadSequence() {
  state.seq = await api(`/api/seq/${enc(state.seqName)}?light=1`);
  state.plan = null;
  renderSequence(true);
  loadPlan();
}

async function loadSequences(select) {
  if (select) state.seqName = select;
  await loadList();
  await loadSequence();
}

async function loadPlan() {
  if (!state.seqName) return;
  const name = state.seqName;
  let plan = null;
  try { plan = await api(`/api/seq/${enc(name)}/plan?speed=${+$('seq-speed').value}`); } catch (error) {}
  if (name !== state.seqName) return;
  state.plan = plan;
  renderPlan();
}

function renderPlan() {
  const p = state.plan;
  document.querySelectorAll('#seq-steps li[data-i]').forEach(li => {
    const planned = p && p.steps[+li.dataset.i], box = li.querySelector('.plan-note');
    const notes = [];
    if (planned && planned.problem) notes.push(el('span', { class: 'fault', title: planned.problem }, 'too heavy'));
    else if (planned && planned.rest_s) notes.push(el('span', { class: 'note' }, `rests ${planned.rest_s} s first`));
    if (planned && planned.over) notes.push(el('span', { class: 'note', title: planned.over.map(o => `${o.arm} joint ${o.joint}: ${o.deg} deg, playback stops at ${o.limit}`).join('\n') }, 'cut short'));
    box.replaceChildren(...notes);
  });
  if (!p || !p.steps.length) { $('seq-plan').textContent = ''; return; }
  const parts = [`Playing this takes about ${Math.round(p.seconds)} s at ${p.speed.toFixed(2)}x`];
  if (p.heavy) parts.push('heavy poses are on, so it plays every step without rests');
  else if (p.rest_s) parts.push(`including ${p.rest_s} s of rests so the shoulder motors can recover`);
  $('seq-plan').replaceChildren(el('div', { class: p.ok ? 'muted' : 'fault' }, parts.join(', ') + '.'), ...p.problems.map(t => el('div', { class: 'fault' }, t)), ...(p.warnings || []).map(t => el('div', { class: 'note' }, t)));
}

function describe(step) {
  if (step.type === 'pose') return 'go to ' + Object.entries(step.arms).map(([a, q]) => `${a} ${q.map(v => v.toFixed(0)).join(' ')}`).join('   ');
  if (step.type === 'track') return `${(step.arm_names || []).join(' and ')} motion, ${Number(step.seconds || 0).toFixed(1)} s`;
  if (step.type === 'gripper') return `${step.width > 0.04 ? 'open' : step.width < 0.005 ? 'close' : 'set'} ${step.arm === 'both' || !step.arm ? 'both grippers' : 'the ' + step.arm + ' gripper'}`;
  if (step.type === 'wait') return 'wait';
  if (step.type === 'lift') return `table ${step.action}`;
  return step.type;
}

function seqEdit(change, local) {
  if (local && state.seq) { local(state.seq.steps); renderSequence(true); }
  const name = state.seqName;
  return seqSend(() => api(`/api/seq/${enc(name)}/edit`, change));
}

function setField(i, key, value) {
  if (state.seq && state.seq.steps[i]) state.seq.steps[i][key] = value;
  seqEdit({ op: 'set', index: i, fields: { [key]: value } });
}

function pick(options, value, onpick) {
  const known = options.some(([, v]) => String(v) === String(value));
  return el('select', { onchange: e => onpick(e.target.value) }, ...(known ? options : [...options, [String(value), value]]).map(([text, v]) => el('option', { value: v, selected: String(v) === String(value) }, text)));
}

function amount(step, i, key, before, after, fallback) {
  return el('label', {}, before, el('input', { type: 'number', step: '0.1', min: 0, value: step[key] ?? fallback, onchange: e => setField(i, key, +e.target.value) }), after);
}

function stepRow(step, i, count) {
  const fields = [];
  if (step.type === 'pose') fields.push(el('label', {}, 'speed', pick(SPEEDS, step.speed_deg_s ?? 90, v => setField(i, 'speed_deg_s', +v))), amount(step, i, 'pause_s', 'then wait', 's', 0));
  if (step.type === 'track') fields.push(amount(step, i, 'speed', 'speed', 'x', 1), amount(step, i, 'pause_s', 'then wait', 's', 0));
  if (step.type === 'wait') fields.push(amount(step, i, 'seconds', '', 's', 1));
  if (step.type === 'lift') fields.push(pick([['up', 'up'], ['down', 'down']], step.action, v => setField(i, 'action', v)), amount(step, i, 'seconds', 'for', 's', 1));
  if (step.type === 'gripper') fields.push(pick([...Object.keys(state.arms).map(n => [n, n]), ['both', 'both']], step.arm || 'both', v => setField(i, 'arm', v)), pick([['open', OPEN_M], ['close', 0]], step.width, v => setField(i, 'width', +v)));
  const move = to => seqEdit({ op: 'move', index: i, to }, steps => steps.splice(to, 0, steps.splice(i, 1)[0]));
  const li = el('li', { draggable: true, 'data-i': i },
    el('span', { class: 'handle', title: 'Drag to reorder' }, '⋮⋮'), el('span', { class: 'num' }, i + 1), el('span', { class: 'kind ' + step.type }, KIND[step.type] || step.type),
    el('input', { class: 'label', value: step.label || '', placeholder: describe(step), title: describe(step), onchange: e => setField(i, 'label', e.target.value) }),
    el('span', { class: 'fields' }, ...fields), el('span', { class: 'plan-note small-text' }),
    el('span', { class: 'row-buttons' },
      el('button', { class: 'small', title: 'Play from this step', onclick: () => seqQueue.then(() => act(`/api/seq/${enc(state.seqName)}/play`, { start: i, loop: false, speed: +$('seq-speed').value })) }, '▶'),
      el('button', { class: 'small', title: 'Move up', disabled: i === 0, onclick: () => move(i - 1) }, '↑'),
      el('button', { class: 'small', title: 'Move down', disabled: i === count - 1, onclick: () => move(i + 1) }, '↓'),
      el('button', { class: 'small', title: 'Make a copy', onclick: () => seqEdit({ op: 'duplicate', index: i }, steps => steps.splice(i + 1, 0, JSON.parse(JSON.stringify(step)))) }, '⧉'),
      el('button', { class: 'small danger', title: 'Remove this step', onclick: () => seqEdit({ op: 'delete', index: i }, steps => steps.splice(i, 1)) }, '✕')));
  li.addEventListener('dragstart', e => { if (['INPUT', 'SELECT'].includes(document.activeElement.tagName)) { e.preventDefault(); return; } dragFrom = i; li.classList.add('drag'); });
  li.addEventListener('dragend', () => li.classList.remove('drag'));
  li.addEventListener('dragover', e => { e.preventDefault(); li.classList.add('over'); });
  li.addEventListener('dragleave', () => li.classList.remove('over'));
  li.addEventListener('drop', e => { e.preventDefault(); li.classList.remove('over'); const from = dragFrom; dragFrom = null; if (from === null || from === i) return; seqEdit({ op: 'move', index: from, to: i }, steps => steps.splice(i, 0, steps.splice(from, 1)[0])); });
  return li;
}

function renderSequence(force) {
  const seq = state.seq;
  if (!seq) return;
  const list = $('seq-steps');
  if (!force && list.contains(document.activeElement) && ['INPUT', 'SELECT'].includes(document.activeElement.tagName)) { seqStale = true; return; }
  seqStale = false;
  if (document.activeElement !== $('seq-name')) $('seq-name').value = seq.name;
  $('seq-loop').checked = !!seq.loop;
  list.replaceChildren(...seq.steps.map((step, i) => stepRow(step, i, seq.steps.length)));
  if (!seq.steps.length) list.append(el('li', { class: 'muted empty' }, 'No steps yet. Move the arms where you want them and press Save point, or press Record motion.'));
  $('seq-play').disabled = !seq.steps.length;
  renderPlan();
  state.lastSeqKey = null;
  renderSeqStatus();
}
$('seq-steps').addEventListener('focusout', () => setTimeout(() => { if (seqStale && !$('seq-steps').contains(document.activeElement)) renderSequence(); }, 0));

function renderSeqStatus() {
  const s = state.sequencer || {};
  const rec = s.recording;
  const button = $('seq-record');
  button.textContent = rec ? `Stop recording (${Number(rec.seconds).toFixed(1)} s)` : 'Record motion';
  button.classList.toggle('on', !!rec);
  const over = rec && rec.over && rec.over.length ? rec.over : null;
  const banner = $('seq-banner');
  banner.hidden = !rec;
  if (rec) {
    banner.className = over ? 'warn' : '';
    banner.textContent = `Recording the ${rec.arms.join(' and ')} ${rec.arms.length > 1 ? 'arms' : 'arm'}. Move ${rec.arms.length > 1 ? 'them' : 'it'} now, by teleop or by hand with the power off, then press Stop recording.` + (over ? ' Past what playback can do: ' + over.map(o => `${o.arm} joint ${o.joint} at ${o.deg} deg (stops at ${o.limit})`).join(', ') + '.' : '');
  }
  $('seq-capture').disabled = !!rec;
  $('seq-progress').textContent = s.playing ? (s.progress && s.progress.lanes ? `Playing ${s.playing}. ${s.progress.label}` : `Playing ${s.playing}: step ${s.progress ? s.progress.index + 1 : ''} of ${s.progress ? s.progress.of : ''} ${s.progress && s.progress.label ? '(' + s.progress.label + ')' : ''}`) : (s.progress && s.progress.error ? 'Stopped: ' + s.progress.error : '');
  const own = Object.entries(state.arms || {}).filter(([, a]) => a && a.powered && (a.rest || a.fault)).map(([name, a]) => { const arm = name[0].toUpperCase() + name.slice(1) + ' arm'; return a.rest ? `${arm} is moving on its own to protect its motors: ${a.fault || a.rest.reason}` : `${arm}: ${a.fault}`; });
  const safety = own.join('|');
  if (state.lastSeqSafety !== safety) { state.lastSeqSafety = safety; $('seq-safety').replaceChildren(...own.map(t => el('div', { class: 'note' }, t))); }
  const now = state.seq && s.playing === state.seq.name && s.progress && s.progress.index !== undefined ? s.progress.index : -1;
  if (state.lastSeqKey !== now) {
    state.lastSeqKey = now;
    document.querySelectorAll('#seq-steps li[data-i]').forEach(li => li.classList.toggle('now', +li.dataset.i === now));
  }
}

const addStep = step => { const name = state.seqName; return seqSend(() => api(`/api/seq/${enc(name)}/step`, { step, light: true })); };
function gripNow(what) {
  const arm = $('seq-arm').value || null;
  const recording = !!state.sequencer.recording;
  api('/api/gripper', { arm, state: what === 'open' ? 'open' : 'closed' }).then(() => { if (recording) toast(`Gripper ${what === 'open' ? 'opening' : 'closing'}; the recording keeps it`, true); }).catch(error => toast(`The gripper did not move: ${error.message}`));
  if (!recording) addStep({ type: 'gripper', arm: arm || 'both', width: what === 'open' ? OPEN_M : 0 });
}
$('seq-open').onclick = () => gripNow('open');
$('seq-close').onclick = () => gripNow('close');
$('seq-new').onclick = async () => {
  const taken = new Set((state.seqList || []).map(s => s.name));
  let n = 1;
  while (taken.has(`Recording ${n}`)) n++;
  const name = `Recording ${n}`;
  await seqQueue;
  try { await api('/api/seq/' + enc(name), { name, steps: [] }, 'PUT'); } catch (error) { toast(error.message); return; }
  await loadSequences(name);
  $('seq-name').focus();
  $('seq-name').select();
};
$('seq-name').onchange = () => {
  const old = state.seqName, name = $('seq-name').value.trim();
  if (!name || name === old) { $('seq-name').value = old; return; }
  seqSend(() => api(`/api/seq/${enc(old)}/rename`, { name }));
};
$('seq-name').onkeydown = e => { if (e.key === 'Enter') e.target.blur(); };
$('seq-capture').onclick = () => { const name = state.seqName; seqSend(() => api(`/api/seq/${enc(name)}/capture`, { arm: $('seq-arm').value || null, speed_deg_s: 90, light: true })).then(r => { if (r && r.steps) toast('Point saved', true); }); };
function limp(arm) {
  return api('/api/power', { arm, on: false }).catch(error => toast(`Could not switch the motors off: ${error.message}`));
}
$('seq-record').onclick = async () => {
  const name = state.seqName, arm = $('seq-arm').value || null, byHand = $('seq-byhand').checked;
  if (state.sequencer.recording) {
    const r = await seqSend(() => api('/api/seq/record/stop', { name, light: true }));
    if (byHand) await limp(arm);
    if (r && r.sequence) toast(`Motion added, ${Number(r.seconds).toFixed(1)} s` + (byHand ? '. The arms are off' : ''), true);
    return;
  }
  if (byHand) {
    const live = Object.entries(state.arms).filter(([n, a]) => a.powered && (!arm || n === arm)).map(([n]) => n);
    if (live.length) {
      if (!confirm(`Recording by hand switches the ${live.join(' and ')} ${live.length > 1 ? 'arms' : 'arm'} off. ${live.length > 1 ? 'They' : 'It'} will drop if not resting or held. Hold ${live.length > 1 ? 'them' : 'it'}, then press OK.`)) return;
      await limp(arm);
    }
  }
  act('/api/seq/record/start', { arm }).catch(() => {});
};
try { $('seq-byhand').checked = localStorage.getItem('seq-byhand') !== '0'; } catch {}
$('seq-byhand').onchange = e => { try { localStorage.setItem('seq-byhand', e.target.checked ? '1' : '0'); } catch {} };
$('seq-add-wait').onclick = () => addStep({ type: 'wait', seconds: 1 });
$('seq-add-lift').onclick = () => addStep({ type: 'lift', action: 'up', seconds: 2 });
$('seq-loop').onchange = e => seqEdit({ op: 'meta', loop: e.target.checked });
$('seq-speed').oninput = e => { $('seq-speed-v').textContent = Number(e.target.value).toFixed(2) + 'x'; };
$('seq-speed').onchange = () => loadPlan();
try { const saved = localStorage.getItem('seq-smooth'); if (saved) $('seq-smooth').value = saved; } catch {}
$('seq-smooth-v').textContent = Number($('seq-smooth').value).toFixed(2) + ' s';
try { $('seq-poweroff').checked = localStorage.getItem('seq-poweroff') !== '0'; } catch {}
$('seq-poweroff').onchange = e => { try { localStorage.setItem('seq-poweroff', e.target.checked ? '1' : '0'); } catch {} };
$('seq-smooth').oninput = e => { $('seq-smooth-v').textContent = Number(e.target.value).toFixed(2) + ' s'; try { localStorage.setItem('seq-smooth', e.target.value); } catch {} };
$('seq-play').onclick = () => seqQueue.then(() => act(`/api/seq/${enc(state.seqName)}/play`, { loop: $('seq-loop').checked, speed: +$('seq-speed').value, smooth: +$('seq-smooth').value, power_off: $('seq-poweroff').checked })).catch(() => {});
$('seq-stop').onclick = () => act('/api/seq/stop', {}, 'Stopped');
$('seq-delete').onclick = () => { if (confirm(`Delete ${state.seqName}?`)) seqQueue.then(() => api('/api/seq/' + enc(state.seqName), undefined, 'DELETE')).then(() => { state.seqName = null; return loadSequences(); }).catch(e => toast(e.message)); };

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
    state.arms = m.arms; state.teleop = m.teleop; state.sequencer = m.sequencer; state.lift = m.lift; state.sim = m.sim; state.heavy = !!m.heavy; state.grip = m.grip === undefined ? 1 : m.grip;
    document.querySelectorAll('.heavy-switch').forEach(box => { if (document.activeElement !== box) box.checked = state.heavy; });
    if (!document.querySelector('.grip-slider:active') && ![...document.querySelectorAll('.grip-slider')].includes(document.activeElement)) { document.querySelectorAll('.grip-slider').forEach(bar => { bar.value = Math.round(state.grip * 100); }); showGrip(state.grip); }
    if (m.cameras) state.cameras = m.cameras;
    if (m.links) state.links = m.links;
    if (m.gist) state.gist = m.gist;
    if (m.usb) state.usb = m.usb;
    if (m.record) { state.record = m.record; renderRecord(); }
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
