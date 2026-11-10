const $ = id => document.getElementById(id);
let next = 0, running = false, timer = null, toastTimer = null;

async function api(path, body) {
  const response = await fetch('/api/' + path, {method: body === undefined ? 'GET' : 'POST', headers: body === undefined ? {} : {'Content-Type': 'application/json'}, body: body === undefined ? undefined : JSON.stringify(body)});
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Request failed');
  return data;
}

function toast(text, error) {
  const box = $('toast');
  box.textContent = text;
  box.className = error ? 'error' : '';
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { box.hidden = true; }, 5000);
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function render(event) {
  const log = $('log');
  const placeholder = log.querySelector('p.fine');
  if (placeholder) placeholder.remove();
  log.querySelectorAll('.event.thinking').forEach(node => node.remove());
  let node;
  if (event.kind === 'code') {
    node = element('div', 'event code');
    node.append(element('div', 'tag', 'Running'), element('pre', '', event.text));
  } else if (event.kind === 'output') {
    node = element('div', 'event output' + (event.ok === false ? ' bad' : ''));
    const label = (event.ok === false ? 'Failed' : 'Output') + (event.seconds !== undefined ? ` after ${event.seconds} s` : '');
    node.append(element('div', 'tag', label), element('pre', '', event.text));
  } else if (event.kind === 'image') {
    node = element('div', 'event info', event.text);
    const image = element('img');
    image.src = 'data:image/jpeg;base64,' + event.image;
    image.alt = event.text;
    node.append(image);
  } else if (event.kind === 'text') {
    node = element('div', 'event text');
    node.append(element('div', 'tag', event.model || 'Model'), document.createTextNode(event.text));
  } else if (event.kind === 'thinking') {
    node = element('div', 'event thinking');
    node.append(document.createTextNode(event.text), element('span', 'dots'));
  } else {
    node = element('div', 'event ' + (event.kind === 'error' ? 'error' : 'info'), event.text);
  }
  log.append(node);
  if (running) node.scrollIntoView({block: 'nearest', behavior: 'smooth'});
}

function show(view) {
  running = view.running;
  $('run').disabled = running || !!view.missing;
  $('instruction').disabled = running;
  const state = $('state');
  state.className = 'state ' + (running ? 'running' : view.result || '');
  state.textContent = running ? 'Running' : view.result === 'done' ? 'Done' + (view.saved ? `, saved ${view.saved}` : '') : view.result === 'failed' ? 'Stopped or failed' + (view.saved ? `, saved ${view.saved}` : '') : '';
  const notice = $('notice');
  notice.hidden = !(view.missing || !view.key);
  notice.textContent = view.missing || 'No Anthropic API key on the arm computer yet. Add a line ANTHROPIC_API_KEY=... to the .env file in its data folder (~/.rebot-teleop/.env), then press Run.';
  for (const event of view.events || []) render(event);
  if (view.next !== undefined) next = view.next;
}

async function poll() {
  try {
    show(await api('cap?after=' + next));
  } catch (error) {
    $('state').textContent = error.message;
  }
  clearTimeout(timer);
  timer = setTimeout(poll, running ? 700 : 3000);
}

async function start() {
  await api('session', {});
  const view = await api('cap');
  const effort = $('effort');
  for (const level of view.efforts) effort.append(new Option(level, level, false, level === 'high'));
  let saved = '';
  try { saved = localStorage.getItem('cap-model') || ''; } catch {}
  $('model').value = saved || view.default_model;
  if (view.events.length && view.instruction) $('instruction').value = view.instruction;
  show(view);
  const state = await api('state').catch(() => ({}));
  $('mode').textContent = state.sim ? 'Simulator' : 'Real arms';
  if (state.sim) {
    $('sim').src = '/api/sim/view.mjpg';
  } else {
    $('simcard').hidden = true;
  }
  poll();
}

$('form').addEventListener('submit', async event => {
  event.preventDefault();
  const model = $('model').value.trim();
  try { localStorage.setItem('cap-model', model); } catch {}
  $('log').replaceChildren();
  next = 0;
  try {
    show(await api('cap', {instruction: $('instruction').value.trim(), model, effort: $('effort').value}));
    poll();
  } catch (error) {
    toast(error.message, true);
  }
});

$('stop').addEventListener('click', async () => {
  try {
    show(await api('cap/stop', {}));
    toast('Stopped the arms');
  } catch (error) {
    toast(error.message, true);
  }
});

$('examples').addEventListener('click', event => {
  if (event.target.tagName === 'BUTTON') $('instruction').value = event.target.textContent;
});

$('instruction').addEventListener('keydown', event => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) $('form').requestSubmit();
});

start().catch(error => { $('state').textContent = error.message; });
