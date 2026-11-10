const $ = id => document.getElementById(id);
let xrSession = null, refSpace = null, gl = null, sending = false, lastSent = 0, lastNote = null, stopping = false;
let socket = null, socketReady = false;
const status = text => { $('status').textContent = text; };
async function api(path, body) {
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
  return data;
}

function connect() {
  if (socket && (socket.readyState === 0 || socket.readyState === 1)) return;
  socket = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/quest');
  socket.onopen = () => { socketReady = true; };
  socket.onclose = () => { socketReady = false; setTimeout(() => { if (xrSession) connect(); }, 1000); };
  socket.onerror = () => { socketReady = false; };
  socket.onmessage = event => {
    let data = {};
    try { data = JSON.parse(event.data); } catch { return; }
    if (data.type !== 'status') return;
    const message = data.fault || data.note || data.warning;
    if (message && message !== lastNote) status(message);
    lastNote = message || null;
  };
}
function send(payload) {
  if (socket && socketReady && socket.bufferedAmount < 65536) { socket.send(JSON.stringify(payload)); return true; }
  return false;
}
async function showReady() {
  $('pair').hidden = true;
  $('ready').hidden = false;
  try { const v = localStorage.getItem('view'); if (v) $('view').value = v; } catch {}
  const state = await api('state');
  if (!navigator.xr) { status('No WebXR here. Open this page in the Quest browser over https.'); return; }
  status((state.torque ? 'Arm motors on. ' : 'Power the arm on at the computer first. ') + (state.remote_seconds > 0 || (state.principal && state.principal.local) ? '' : 'Allow phone and headset motion at the computer first.'));
}
$('pair').onsubmit = async event => {
  event.preventDefault();
  try { await api('session', {token: $('code').value.trim()}); await showReady(); } catch (error) { status(error.message); }
};
$('view').onchange = () => { try { localStorage.setItem('view', $('view').value); } catch {} };
$('enter').onclick = async () => {
  try {
    connect();
    if (!send({type: 'start', view: $('view').value, rotation: $('rotation').checked})) await api('quest/start', {view: $('view').value, rotation: $('rotation').checked});
    const mode = await navigator.xr.isSessionSupported('immersive-ar').catch(() => false) ? 'immersive-ar' : 'immersive-vr';
    xrSession = await navigator.xr.requestSession(mode, {requiredFeatures: ['local-floor']});
    gl = document.createElement('canvas').getContext('webgl', {xrCompatible: true, alpha: true});
    await xrSession.updateRenderState({baseLayer: new XRWebGLLayer(xrSession, gl)});
    refSpace = await xrSession.requestReferenceSpace('local-floor');
    xrSession.addEventListener('end', endVR);
    $('enter').hidden = true;
    $('exit').hidden = false;
    status('In VR. Hold grip to move.');
    xrSession.requestAnimationFrame(onFrame);
  } catch (error) {
    status(error.message);
    api('quest/stop', {}).catch(() => {});
  }
};
$('reset').onclick = () => { if (!send({type: 'reset'})) api('reset', {}).catch(error => status(error.message)); else status('Reset sent.'); };
$('exit').onclick = () => { if (xrSession) xrSession.end(); else endVR(); };
async function endVR() {
  if (stopping) return;
  stopping = true;
  xrSession = null;
  $('enter').hidden = false;
  $('exit').hidden = true;
  if (!send({type: 'stop'})) { try { await api('quest/stop', {}); } catch (error) { status(error.message); } }
  status('VR control stopped. The arm is holding.');
  stopping = false;
}
let lastNotes = {};
function onFrame(time, frame) {
  const session = frame.session;
  session.requestAnimationFrame(onFrame);
  gl.bindFramebuffer(gl.FRAMEBUFFER, session.renderState.baseLayer.framebuffer);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT);
  if (time - lastSent < 11) return;
  const hands = {};
  for (const source of session.inputSources) {
    if (!source.gripSpace || (source.handedness !== 'left' && source.handedness !== 'right')) continue;
    const pose = frame.getPose(source.gripSpace, refSpace);
    if (!pose) continue;
    const pad = source.gamepad;
    const button = i => pad && pad.buttons[i] ? pad.buttons[i] : {pressed: false, value: 0};
    const p = pose.transform.position, q = pose.transform.orientation;
    hands[source.handedness] = {p: [p.x, p.y, p.z], q: [q.w, q.x, q.y, q.z], inputs: {b1: button(1).value > 0.5 ? 1 : 0, grip: 1 - button(0).value, b8: button(5).pressed ? 1 : 0}};
  }
  if (!Object.keys(hands).length) return;
  lastSent = time;
  if (!send({type: 'pose', hands})) {
    if (sending) return;
    sending = true;
    api('quest/pose', {hands: Object.fromEntries(Object.entries(hands).map(([k, v]) => [k, {position: v.p, quaternion_wxyz: v.q, inputs: v.inputs}]))})
      .catch(error => { status(error.message); })
      .finally(() => { sending = false; });
  }
}
(async () => {
  const pairing = location.hash.match(/^#pair=(\d{8})$/);
  if (pairing) history.replaceState(null, '', location.pathname);
  try { await api('session', pairing ? {token: pairing[1]} : {}); await showReady(); }
  catch (error) { $('pair').hidden = false; status('Enter the pairing code shown on the arm computer.'); }
})();
