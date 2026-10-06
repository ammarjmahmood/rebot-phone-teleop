const $ = id => document.getElementById(id);
let xrSession = null, refSpace = null, gl = null, sending = false, lastSent = 0, lastNote = null, stopping = false;
const status = text => { $('status').textContent = text; };
async function api(path, body) {
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
  return data;
}
async function showReady() {
  $('pair').hidden = true;
  $('ready').hidden = false;
  try { const v = localStorage.getItem('view'); if (v) $('view').value = v; } catch {}
  const state = await api('state');
  if (!navigator.xr) { status('No WebXR here. Open this page in the Quest browser over https.'); return; }
  status((state.torque ? 'Arm motors on. ' : 'Power the arm on at the computer first. ') + (state.remote_seconds > 0 || state.principal.local ? '' : 'Allow phone and headset motion at the computer first.'));
}
$('pair').onsubmit = async event => {
  event.preventDefault();
  try { await api('session', {token: $('code').value.trim()}); await showReady(); } catch (error) { status(error.message); }
};
$('view').onchange = () => { try { localStorage.setItem('view', $('view').value); } catch {} };
$('enter').onclick = async () => {
  try {
    await api('quest/start', {view: $('view').value, rotation: $('rotation').checked});
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
$('exit').onclick = () => { if (xrSession) xrSession.end(); else endVR(); };
async function endVR() {
  if (stopping) return;
  stopping = true;
  xrSession = null;
  $('enter').hidden = false;
  $('exit').hidden = true;
  try { await api('quest/stop', {}); status('VR control stopped. The arm is holding.'); } catch (error) { status(error.message); }
  stopping = false;
}
let lastNotes = {};
function onFrame(time, frame) {
  const session = frame.session;
  session.requestAnimationFrame(onFrame);
  gl.bindFramebuffer(gl.FRAMEBUFFER, session.renderState.baseLayer.framebuffer);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT);
  if (sending || time - lastSent < 22) return;
  const hands = {}, pads = {};
  for (const source of session.inputSources) {
    if (!source.gripSpace || (source.handedness !== 'left' && source.handedness !== 'right')) continue;
    const pose = frame.getPose(source.gripSpace, refSpace);
    if (!pose) continue;
    const pad = source.gamepad;
    const button = i => pad && pad.buttons[i] ? pad.buttons[i] : {pressed: false, value: 0};
    const p = pose.transform.position, q = pose.transform.orientation;
    hands[source.handedness] = {position: [p.x, p.y, p.z], quaternion_wxyz: [q.w, q.x, q.y, q.z], inputs: {b1: button(1).value > 0.5 ? 1 : 0, grip: 1 - button(0).value, b8: button(5).pressed ? 1 : 0}};
    pads[source.handedness] = pad;
  }
  if (!Object.keys(hands).length) return;
  sending = true;
  lastSent = time;
  api('quest/pose', {hands}).then(result => {
    for (const [hand, note] of Object.entries(result.notes || {})) {
      const pad = pads[hand];
      if (note && note !== lastNotes[hand] && pad && pad.hapticActuators && pad.hapticActuators[0]) pad.hapticActuators[0].pulse(0.6, 80).catch(() => {});
      lastNotes[hand] = note;
    }
  }).catch(error => {
    status(error.message);
    if (/Start VR control/.test(error.message) && xrSession) xrSession.end();
  }).finally(() => { sending = false; });
}
(async () => {
  const pairing = location.hash.match(/^#pair=(\d{8})$/);
  if (pairing) history.replaceState(null, '', location.pathname);
  try { await api('session', pairing ? {token: pairing[1]} : {}); await showReady(); }
  catch (error) { $('pair').hidden = false; status('Enter the pairing code shown on the arm computer.'); }
})();
