const $ = id => document.getElementById(id);
let state = null, timer;
const esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
function toast(text, error = false) { $('toast').textContent = text; $('toast').className = error ? 'error' : ''; $('toast').hidden = false; clearTimeout(timer); timer = setTimeout(() => $('toast').hidden = true, 4000); }
async function api(path, body) {
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
  return data;
}
const act = fn => async () => { try { await fn(); await poll(); } catch (error) { toast(error.message, true); } };
const releaseArmed = {};
const armedRecently = name => releaseArmed[name] && Date.now() - releaseArmed[name] < 4000;
function armCard(name, a, local) {
  const status = !a.connected ? 'Arm not answering. Check its 48 V supply and CAN adapter, then press Power on.' : a.hand_guide ? 'Hand guiding: motors off, move the joints by hand and watch the numbers and the twin.' : a.estopped ? 'Stop is latched. Check the arm, then Reset stop.' : a.arm_fault ? 'Stopped by a fault. Fix the cause, press Reset stop, then Power on and hold here.' : !a.torque ? 'Motors off.' : a.homing ? 'Moving to zero.' : a.owner ? 'Under teleop control.' : 'Motors on and holding.';
  const joints = a.joints_deg.map((v, i) => `<div>J${i + 1} ${v.toFixed(1)}°</div>`).join('') + `<div>Grip ${a.gripper_deg}°</div>`;
  const temps = (a.temperatures || []).map((t, i) => `<div class="${t !== null && t >= a.temperature_limits_c[0] ? 'warm' : ''}">${i < 6 ? 'J' + (i + 1) : 'Grip'} ${t === null ? '?' : t.toFixed(0) + ' °C'}</div>`).join('');
  const guide = a.connected && !a.torque ? `<div class="row"><button data-act="${a.hand_guide ? 'guide-off' : 'guide-on'}" data-arm="${esc(name)}">${a.hand_guide ? 'Stop hand guiding' : 'Hand guide (motors stay off)'}</button></div>` : '';
  const model = a.hand_guide ? `<p class="fine">Model tip x ${a.tip_xyz[0].toFixed(3)} y ${a.tip_xyz[1].toFixed(3)} z ${a.tip_xyz[2].toFixed(3)} m. Difference from home: ${a.joints_deg.map((v, i) => 'J' + (i + 1) + ' ' + (v - a.home_deg[i]).toFixed(0) + '°').join(', ')}</p>` : '';
  const buttons = guide + model + (local ? `<div class="row">${a.torque ? '' : `<button class="primary" data-act="power-home" data-arm="${esc(name)}">Power on and go to zero</button><button data-act="power-hold" data-arm="${esc(name)}">Power on and hold here</button>`}${a.torque ? `<button data-act="home" data-arm="${esc(name)}">Home</button>` : ''}${a.estopped || a.arm_fault ? `<button data-act="reset" data-arm="${esc(name)}">Reset stop</button>` : ''}${a.torque ? `<button class="danger" data-act="release" data-arm="${esc(name)}">${armedRecently(name) ? 'Click again: arm may drop' : 'Release torque'}</button>` : ''}</div><div class="row"><button data-act="save-closed" data-arm="${esc(name)}">Save gripper as fully closed</button><button data-act="save-open" data-arm="${esc(name)}">Save gripper as fully open</button><button data-act="is-open" data-arm="${esc(name)}">Gripper is open now</button><button data-act="is-closed" data-arm="${esc(name)}">Gripper is closed now</button></div><p class="fine">Gripper range ${a.gripper_range_deg[0]}° to ${a.gripper_range_deg[1]}°. Release torque turns the motors off and the arm can drop, so support it first.</p>` : '');
  return `<section class="card"><h2>${esc(name === 'arm' ? 'Arm' : name.charAt(0).toUpperCase() + name.slice(1) + ' arm')} <span class="fine">${esc(a.channel)}</span></h2><p>${esc(status)}</p>${a.fault ? `<div class="notice">${esc(a.fault)}</div>` : ''}${a.temperature_warning ? `<div class="notice">${esc(a.temperature_warning)}</div>` : ''}<div class="joints">${joints}</div><div class="joints">${temps}</div>${buttons}</section>`;
}
function render() {
  const s = state, local = s.principal.local;
  document.querySelectorAll('.local').forEach(el => el.hidden = !local);
  $('fault').hidden = true;
  const names = Object.keys(s.arms);
  $('arms').innerHTML = names.map(name => armCard(name, s.arms[name], local)).join('');
  $('remote').checked = names.some(name => s.arms[name].remote_seconds > 0);
  const select = $('hebi-arm');
  if (select.options.length !== names.length) select.innerHTML = names.map(name => `<option value="${esc(name)}">${esc(name)}</option>`).join('');
  select.parentElement.hidden = names.length < 2;
  const h = s.arms[select.value || names[0]].controllers.hebi;
  $('hebi-status').textContent = !h.active ? 'Off' : h.calibrating ? 'Calibrating ' + h.calibrating + ': hold Move and move the phone' : h.source !== 'streaming' ? 'Looking for the HEBI app' : !h.ready ? 'Calibrate up, forward and left' : (h.note || (h.mode ? 'Moving' : 'Ready, hold Move'));
  $('gist').textContent = s.gist ? 'Quest bookmark: ' + s.gist : '';
  renderCameras(s);
  renderView(s);
}
async function poll() { try { state = await api('state'); $('connection').textContent = 'Connected'; render(); } catch (error) { $('connection').textContent = 'Connection lost'; } }
$('stop').onclick = act(() => api('stop', {}));
const armActions = {
  'power-home': arm => api('power', {arm, home: true}),
  'power-hold': arm => api('power', {arm, home: false}),
  'home': arm => api('home', {arm}),
  'reset': arm => api('reset', {arm}),
  'save-closed': arm => api('gripper-range', {arm, which: 'closed'}),
  'save-open': arm => api('gripper-range', {arm, which: 'open'}),
  'is-open': arm => api('gripper-state', {arm, which: 'open'}),
  'is-closed': arm => api('gripper-state', {arm, which: 'closed'}),
  'guide-on': arm => api('handguide', {arm, on: true}),
  'guide-off': arm => api('handguide', {arm, on: false}),
};
const watching = new Set();
let cameraKey = '';
function renderCameras(s) {
  const names = Object.keys(s.cameras || {});
  $('camera-card').hidden = !names.length;
  const key = names.join(',') + '|' + [...watching].join(',');
  if (key === cameraKey) return;
  cameraKey = key;
  $('camera-buttons').innerHTML = names.map(name => `<button data-camera="${esc(name)}">${watching.has(name) ? 'Hide' : 'Show'} ${esc(name)} camera</button>`).join('');
  $('cameras').innerHTML = [...watching].filter(name => names.includes(name)).map(name => `<figure><img src="/api/camera/${encodeURIComponent(name)}.mjpg" alt="${esc(name)} wrist camera"><figcaption>${esc(name)} wrist</figcaption></figure>`).join('');
}
document.addEventListener('click', event => {
  const button = event.target.closest('[data-camera]');
  if (!button) return;
  const name = button.dataset.camera;
  watching.has(name) ? watching.delete(name) : watching.add(name);
  renderCameras(state);
});
async function runCommand() {
  const text = $('command').value.trim();
  if (!text) return;
  $('command-result').textContent = 'Running: ' + text;
  try {
    const r = await api('command', {text});
    $('command-result').textContent = (r.interrupted ? 'Interrupted after: ' : 'Done: ') + r.done.join('; ');
  } catch (error) { $('command-result').textContent = error.message; toast(error.message, true); }
  await poll();
}
$('command-run').onclick = runCommand;
$('command').addEventListener('keydown', event => { if (event.key === 'Enter') runCommand(); });
document.addEventListener('click', event => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  if (!armActions[button.dataset.act] && button.dataset.act !== 'release') return;
  const arm = button.dataset.arm, kind = button.dataset.act;
  if (kind === 'release') {
    if (!armedRecently(arm)) { releaseArmed[arm] = Date.now(); render(); return; }
    delete releaseArmed[arm];
    return act(() => api('release', {arm}))();
  }
  act(() => armActions[kind](arm))();
});
$('remote').onchange = act(() => api('remote', {enabled: $('remote').checked}));
$('pair').onclick = act(async () => {
  const r = await api('pair', {});
  const routes = r.addresses.map(a => `<div class="route"><img src="${a.qr}" alt="QR for ${esc(a.label)}"><div><b>${esc(a.label)}</b><br><a class="applink" href="${esc(a.app_link)}">Open in app</a><br><span class="fine">${esc(a.url)}</span><br><span class="fine">Quest: ${esc(a.quest)}</span></div></div>`).join('');
  $('pairing').innerHTML = `<p class="code">${esc(r.code)}</p><p class="fine">Scan one code with the iPhone camera to open reBot Teleop and pair automatically. The code works once and expires in 5 minutes.</p>` + (routes || '<p class="fine">Start with --lan so phones and headsets can reach this computer.</p>') + (r.cloudflare && r.cloudflare !== 'running' ? `<p class="fine">Cloudflare tunnel: ${esc(r.cloudflare)}</p>` : '');
});
$('hebi-start').onclick = act(() => api('hebi/start', {arm: $('hebi-arm').value || undefined}));
$('hebi-stop').onclick = act(() => api('hebi/stop', {arm: $('hebi-arm').value || undefined}));
document.querySelectorAll('[data-cal]').forEach(el => el.onclick = act(() => api('hebi/calibrate', {arm: $('hebi-arm').value || undefined, step: el.dataset.cal})));
$('hebi-address-save').onclick = act(() => api('hebi/address', {arm: $('hebi-arm').value || undefined, address: $('hebi-address').value}));
(async () => { try { await api('session', {}); } catch {} await poll(); setInterval(poll, 500); })();

const view = {yaw: 120, pitch: -15, distance: 1.9, height: 0.95, on: true, src: '', drag: null};
const VIEWS = {front: [90, -10], left: [180, -12], right: [0, -12], back: [270, -12], top: [90, -80]};
function viewUrl() { return `/api/sim/view.mjpg?yaw=${view.yaw.toFixed(0)}&pitch=${view.pitch.toFixed(0)}&distance=${view.distance.toFixed(2)}&height=${view.height}`; }
function renderView(s) {
  $('view-card').hidden = !s.sim;
  if (!s.sim) return;
  const src = view.on ? viewUrl() : '';
  if (src !== view.src) { view.src = src; if (src) $('sim-view').src = src; else $('sim-view').removeAttribute('src'); }
  $('view-toggle').textContent = view.on ? 'Pause view' : 'Show view';
}
let viewTimer;
function updateView() { clearTimeout(viewTimer); viewTimer = setTimeout(() => state && renderView(state), 120); }
$('sim-view').addEventListener('pointerdown', event => { view.drag = {x: event.clientX, y: event.clientY, yaw: view.yaw, pitch: view.pitch}; event.target.setPointerCapture(event.pointerId); });
$('sim-view').addEventListener('pointermove', event => {
  if (!view.drag) return;
  view.yaw = view.drag.yaw - (event.clientX - view.drag.x) * 0.5;
  view.pitch = Math.max(-85, Math.min(30, view.drag.pitch - (event.clientY - view.drag.y) * 0.4));
  updateView();
});
['pointerup', 'pointercancel'].forEach(name => $('sim-view').addEventListener(name, () => { view.drag = null; }));
document.addEventListener('click', event => {
  const preset = event.target.closest('[data-view]');
  const zoom = event.target.closest('[data-zoom]');
  if (preset) [view.yaw, view.pitch] = VIEWS[preset.dataset.view];
  else if (zoom) view.distance = Math.max(0.5, Math.min(4, view.distance * Number(zoom.dataset.zoom)));
  else if (event.target.id === 'view-toggle') view.on = !view.on;
  else return;
  updateView();
});
