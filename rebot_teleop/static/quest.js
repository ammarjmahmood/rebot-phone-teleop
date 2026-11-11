const $ = id => document.getElementById(id);
const CAMERAS = ['head', 'left', 'right'];
let ws = null, session = null, refSpace = null, gl = null, panel = null;
let cameraIndex = 0, panelOn = true, lastButtons = {}, gripWas = {}, lastStatus = {}, seq = 0, rtt = null;

function status(text) { $('status').textContent = text; }

function connect() {
  ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/ws/teleop');
  ws.onopen = () => { status(session ? 'In VR. Squeeze grip to move.' : 'Connected. Press Enter VR.'); ws.send(JSON.stringify({ type: 'power' })); };
  ws.onmessage = event => {
    const m = JSON.parse(event.data);
    if (m.type !== 'status' && m.type !== 'started') return;
    lastStatus = m.arms || lastStatus;
    if (m.type === 'status' && m.t) { rtt = Date.now() - m.t; const tl = m.teleop || {}; $('link').textContent = `Round trip ${rtt} ms, ${tl.input_hz || 0} frames/s, ${tl.dropped || 0} dropped`; }
    const chips = Object.entries(m.arms || {}).map(([n, a]) => `<span class="${a.ready ? 'ok' : 'warn'}">${n}: ${a.ready ? 'ready' : a.fault ? 'FAULT' : a.connected ? 'torque off' : 'offline'}</span>`);
    $('arms').innerHTML = chips.join('');
    const faults = Object.entries(m.arms || {}).filter(([, a]) => a.fault).map(([n, a]) => `${n} arm: ${a.fault}`);
    if (faults.length) status(faults.join(' '));
    if (m.type === 'started' && m.offline && m.offline.length) status(`Ready with ${m.powered.join(' and ') || 'no arm'}. ${m.offline.join(' and ')} offline.`);
  };
  ws.onclose = () => { status('Connection lost, reconnecting. The arms hold.'); setTimeout(connect, 500); };
}
connect();

function setFeed() { $('feed').src = `/api/camera/${CAMERAS[cameraIndex]}.mjpg?vr=${Date.now()}`; }

function makePanel(gl) {
  const vs = 'attribute vec3 p;attribute vec2 t;uniform mat4 P,V,M;varying vec2 u;void main(){u=t;gl_Position=P*V*M*vec4(p,1.0);}';
  const fs = 'precision mediump float;varying vec2 u;uniform sampler2D s;void main(){gl_FragColor=texture2D(s,u);}';
  const shader = (type, src) => { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); return s; };
  const program = gl.createProgram();
  gl.attachShader(program, shader(gl.VERTEX_SHADER, vs));
  gl.attachShader(program, shader(gl.FRAGMENT_SHADER, fs));
  gl.linkProgram(program);
  const w = 0.32, h = 0.24;
  const data = new Float32Array([-w, -h, 0, 0, 1, w, -h, 0, 1, 1, -w, h, 0, 0, 0, w, h, 0, 1, 0]);
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
  const texture = gl.createTexture();
  gl.bindTexture(gl.TEXTURE_2D, texture);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
  gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 1, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([20, 20, 20, 255]));
  return { program, buffer, texture, model: null, loc: { p: gl.getAttribLocation(program, 'p'), t: gl.getAttribLocation(program, 't'), P: gl.getUniformLocation(program, 'P'), V: gl.getUniformLocation(program, 'V'), M: gl.getUniformLocation(program, 'M') } };
}

function placePanel(pose) {
  const o = pose.transform.orientation, p = pose.transform.position;
  const fx = -2 * (o.x * o.z + o.w * o.y), fz = -(1 - 2 * (o.x * o.x + o.y * o.y));
  const len = Math.hypot(fx, fz) || 1;
  const dx = fx / len, dz = fz / len;
  const cx = p.x + dx * 0.75, cy = p.y - 0.22, cz = p.z + dz * 0.75;
  const yaw = Math.atan2(-dx, -dz);
  const c = Math.cos(yaw), s = Math.sin(yaw);
  const tilt = -0.35, ct = Math.cos(tilt), st = Math.sin(tilt);
  panel.model = new Float32Array([c, 0, -s, 0, s * st, ct, c * st, 0, s * ct, -st, c * ct, 0, cx, cy, cz, 1]);
}

function drawPanel(view) {
  const img = $('feed');
  gl.useProgram(panel.program);
  gl.bindTexture(gl.TEXTURE_2D, panel.texture);
  if (img.complete && img.naturalWidth) { try { gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img); } catch {} }
  gl.bindBuffer(gl.ARRAY_BUFFER, panel.buffer);
  gl.enableVertexAttribArray(panel.loc.p);
  gl.vertexAttribPointer(panel.loc.p, 3, gl.FLOAT, false, 20, 0);
  gl.enableVertexAttribArray(panel.loc.t);
  gl.vertexAttribPointer(panel.loc.t, 2, gl.FLOAT, false, 20, 12);
  gl.uniformMatrix4fv(panel.loc.P, false, view.projectionMatrix);
  gl.uniformMatrix4fv(panel.loc.V, false, view.transform.inverse.matrix);
  gl.uniformMatrix4fv(panel.loc.M, false, panel.model);
  gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
}

function headsetYaw(pose) {
  const o = pose.transform.orientation;
  const fx = -2 * (o.x * o.z + o.w * o.y), fz = -(1 - 2 * (o.x * o.x + o.y * o.y));
  return Math.atan2(-fx, -fz) * 180 / Math.PI;
}

function pulse(pad, strength, ms) { const h = pad && pad.hapticActuators && pad.hapticActuators[0]; if (h) h.pulse(strength, ms).catch(() => {}); }

function onFrame(time, frame) {
  const s = frame.session;
  s.requestAnimationFrame(onFrame);
  const viewer = frame.getViewerPose(refSpace);
  const layer = s.renderState.baseLayer;
  gl.bindFramebuffer(gl.FRAMEBUFFER, layer.framebuffer);
  gl.clearColor(0, 0, 0, 0);
  gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  if (viewer && !panel.model) placePanel(viewer);
  if (viewer && panelOn && panel.model) {
    for (const view of viewer.views) {
      const vp = layer.getViewport(view);
      gl.viewport(vp.x, vp.y, vp.width, vp.height);
      drawPanel(view);
    }
  }
  const hands = {};
  for (const source of s.inputSources) {
    const hand = source.handedness;
    if (!source.gripSpace || (hand !== 'left' && hand !== 'right')) continue;
    const pad = source.gamepad;
    const b = i => (pad && pad.buttons[i]) ? pad.buttons[i] : { pressed: false, value: 0 };
    const axes = pad ? pad.axes : [];
    const pose = frame.getPose(source.gripSpace, refSpace);
    const grip = b(1).value;
    const buttons = { stick: b(3).pressed, a: hand === 'right' && b(4).pressed, b: hand === 'right' && b(5).pressed, x: hand === 'left' && b(4).pressed, y: hand === 'left' && b(5).pressed };
    const prev = lastButtons[hand] || {};
    if (hand === 'right' && buttons.stick && !prev.stick) panelOn = !panelOn;
    if (hand === 'right' && buttons.b && !prev.b) { cameraIndex = (cameraIndex + 1) % CAMERAS.length; setFeed(); }
    if (hand === 'left' && buttons.stick && !prev.stick && viewer) placePanel(viewer);
    lastButtons[hand] = buttons;
    const engaged = grip > 0.5;
    if (engaged && !gripWas[hand]) {
      const arm = lastStatus[hand];
      if (arm && !arm.ready) { pulse(pad, 1.0, 60); setTimeout(() => pulse(pad, 1.0, 60), 120); }
      else pulse(pad, 0.4, 30);
    }
    gripWas[hand] = engaged;
    const entry = { grip, trigger: b(0).value, stick: [axes[2] || 0, axes[3] || 0], buttons };
    if (pose) {
      const p = pose.transform.position, q = pose.transform.orientation;
      entry.p = [p.x, p.y, p.z];
      entry.q = [q.x, q.y, q.z, q.w];
    }
    hands[hand] = entry;
  }
  if (ws && ws.readyState === 1) {
    ws.send(JSON.stringify({ type: 'hands', source: 'quest', seq: seq++, t: Date.now(), rtt, yaw: viewer ? headsetYaw(viewer) : 0, hands }));
  }
}

async function enter() {
  if (!navigator.xr) { status('No WebXR here. Open this page in the Quest browser over https.'); return; }
  try {
    const ar = await navigator.xr.isSessionSupported('immersive-ar').catch(() => false);
    session = await navigator.xr.requestSession(ar ? 'immersive-ar' : 'immersive-vr', { requiredFeatures: ['local-floor'] });
    const canvas = document.createElement('canvas');
    gl = canvas.getContext('webgl', { xrCompatible: true, alpha: true });
    await session.updateRenderState({ baseLayer: new XRWebGLLayer(session, gl), depthNear: 0.05, depthFar: 20 });
    refSpace = await session.requestReferenceSpace('local-floor');
    panel = makePanel(gl);
    setFeed();
    session.addEventListener('end', ended);
    if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'start', source: 'quest' }));
    $('enter').hidden = true;
    $('exit').hidden = false;
    status('In VR. Squeeze grip to move.');
    session.requestAnimationFrame(onFrame);
  } catch (error) {
    status('Could not start VR: ' + error.message);
  }
}

function ended() {
  session = null;
  $('feed').src = '';
  $('enter').hidden = false;
  $('exit').hidden = true;
  if (ws && ws.readyState === 1) ws.send(JSON.stringify({ type: 'stop' }));
  status('VR ended. The arms are holding.');
}

$('enter').onclick = enter;
$('exit').onclick = () => session && session.end();
