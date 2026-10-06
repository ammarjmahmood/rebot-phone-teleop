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
  const status = !a.connected ? 'Arm not answering. Check its 48 V supply and CAN adapter, then press Power on.' : a.estopped ? 'Stop is latched. Check the arm, then Reset stop.' : !a.torque ? 'Motors off.' : a.homing ? 'Moving to zero.' : a.owner ? 'Under teleop control.' : 'Motors on and holding.';
  const joints = a.joints_deg.map((v, i) => `<div>J${i + 1} ${v.toFixed(1)}°</div>`).join('') + `<div>Grip ${a.gripper_deg}°</div>`;
  const buttons = local ? `<div class="row">${a.torque ? '' : `<button class="primary" data-act="power-home" data-arm="${esc(name)}">Power on and go to zero</button><button data-act="power-hold" data-arm="${esc(name)}">Power on and hold here</button>`}${a.torque ? `<button data-act="home" data-arm="${esc(name)}">Home</button>` : ''}${a.estopped ? `<button data-act="reset" data-arm="${esc(name)}">Reset stop</button>` : ''}${a.torque ? `<button class="danger" data-act="release" data-arm="${esc(name)}">${armedRecently(name) ? 'Click again: arm may drop' : 'Release torque'}</button>` : ''}</div><div class="row"><button data-act="save-closed" data-arm="${esc(name)}">Save gripper as fully closed</button><button data-act="save-open" data-arm="${esc(name)}">Save gripper as fully open</button></div><p class="fine">Gripper range ${a.gripper_range_deg[0]}° to ${a.gripper_range_deg[1]}°. Release torque turns the motors off and the arm can drop, so support it first.</p>` : '';
  return `<section class="card"><h2>${esc(name === 'arm' ? 'Arm' : name.charAt(0).toUpperCase() + name.slice(1) + ' arm')} <span class="fine">${esc(a.channel)}</span></h2><p>${esc(status)}</p>${a.fault ? `<div class="notice">${esc(a.fault)}</div>` : ''}<div class="joints">${joints}</div>${buttons}</section>`;
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
};
document.addEventListener('click', event => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
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
