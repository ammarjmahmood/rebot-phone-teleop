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
function render() {
  const s = state, local = s.principal.local;
  document.querySelectorAll('.local').forEach(el => el.hidden = !local);
  $('fault').textContent = s.fault || ''; $('fault').hidden = !s.fault;
  $('arm-status').textContent = !s.connected ? 'Arm not answering. Check the 48 V supply and the CAN adapter, then press Power on.' : s.estopped ? 'Stop is latched. Check the arm, then Reset stop.' : !s.torque ? 'Motors off.' : s.homing ? 'Moving to zero.' : s.owner ? 'Under teleop control.' : 'Motors on and holding.';
  $('joints').innerHTML = s.joints_deg.map((v, i) => `<div>J${i + 1} ${v.toFixed(1)}°</div>`).join('') + `<div>Grip ${s.gripper_deg}°</div>`;
  $('power-home').hidden = s.torque; $('power-hold').hidden = s.torque; $('release').hidden = !s.torque; $('home').hidden = !s.torque; $('reset').hidden = !s.estopped;
  $('remote').checked = s.remote_seconds > 0;
  const h = s.controllers.hebi;
  $('hebi-status').textContent = !h.active ? 'Off' : h.calibrating ? 'Calibrating ' + h.calibrating + ': hold Move and move the phone' : h.source !== 'streaming' ? 'Looking for the HEBI app' : !h.ready ? 'Calibrate up, forward and left' : (h.note || (h.mode ? 'Moving' : 'Ready, hold Move'));
  $('gripper').textContent = `Gripper ${s.gripper_deg}°, saved range ${s.gripper_range_deg[0]}° to ${s.gripper_range_deg[1]}°`;
}
async function poll() { try { state = await api('state'); $('connection').textContent = 'Connected'; render(); } catch (error) { $('connection').textContent = 'Connection lost'; } }
$('stop').onclick = act(() => api('stop', {}));
$('power-home').onclick = act(() => api('power', {home: true}));
$('power-hold').onclick = act(() => api('power', {home: false}));
$('home').onclick = act(() => api('home', {}));
$('reset').onclick = act(() => api('reset', {}));
$('release').onclick = act(async () => { if ($('release').dataset.armed) { delete $('release').dataset.armed; $('release').textContent = 'Release torque'; await api('release', {}); } else { $('release').dataset.armed = 1; $('release').textContent = 'Click again: arm may drop'; } });
$('remote').onchange = act(() => api('remote', {enabled: $('remote').checked}));
$('pair').onclick = act(async () => {
  const r = await api('pair', {});
  $('pairing').innerHTML = `<p class="code">${esc(r.code)}</p><p class="fine">Expires in 5 minutes. Enter it in the reBot Teleop app or the Quest page.</p>` + (r.cloudflare && r.cloudflare !== 'running' ? `<p class="fine">Cloudflare tunnel: ${esc(r.cloudflare)}</p>` : '') + (r.lan || r.addresses.length ? r.addresses.map(a => `<p><img src="${a.qr}" alt="Address QR"><br><b>${esc(a.url)}</b> (${esc(a.interface)})<br>Quest: ${esc(a.quest)}</p>`).join('') : '<p class="fine">Start with --lan so phones and headsets can reach this computer.</p>');
});
$('hebi-start').onclick = act(() => api('hebi/start', {}));
$('hebi-stop').onclick = act(() => api('hebi/stop', {}));
document.querySelectorAll('[data-cal]').forEach(el => el.onclick = act(() => api('hebi/calibrate', {step: el.dataset.cal})));
$('hebi-address-save').onclick = act(() => api('hebi/address', {address: $('hebi-address').value}));
$('save-closed').onclick = act(() => api('gripper-range', {which: 'closed'}));
$('save-open').onclick = act(() => api('gripper-range', {which: 'open'}));
(async () => { try { await api('session', {}); } catch {} await poll(); setInterval(poll, 500); })();
