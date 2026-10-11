import { viewState, approvalState, commandIntent, apiBase, requestHeaders, resolveTheme, workflowProgress, looksLikeCode } from './state.mjs';
import { updateControls } from '../vendor/astryx.js';
import { VoiceSession } from './voice-session.mjs';

const $ = id => document.getElementById(id);
const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
const json = value => JSON.stringify(value ?? null, null, 2);
const time = value => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : 'Time unavailable';
const fullTime = value => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : 'Not supplied';
const percent = value => `${(value * 100).toLocaleString(undefined, { maximumFractionDigits: 1 })}%`;
const base = apiBase(window.location);
let storageAvailable = true;
function stored(key) { try { return sessionStorage.getItem(key); } catch { storageAvailable = false; return null; } }
function store(key, value) { try { value ? sessionStorage.setItem(key, value) : sessionStorage.removeItem(key); } catch { storageAvailable = false; } }
let session = stored('oncall-session');
if (!session) {
  session = crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(24)), n => n.toString(16).padStart(2, '0')).join('');
  store('oncall-session', session);
}
let token = stored('oncall-token') || '';
let state = null;
let connected = false;
let busy = false;
let polling = false;
let authBlocked = false;
let generation = 0;
let lastStateSignature = '';
let lastApprovalId = null;
let tools = [];
let investigated = false;
let verified = false;
let nextAction = 'connect';
let voice;
let detailsOpen = false;
let responseSpeech = '';
let rolledBack = false;
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
let themePreference = 'system';
try { themePreference = localStorage.getItem('oncall-theme') || 'system'; } catch {}
if (!['system', 'light', 'dark'].includes(themePreference)) themePreference = 'system';
$('theme-select').value = themePreference;
// Narrow screens: "System" instead of "System theme" so the select never truncates mid-word.
const narrow = matchMedia('(max-width: 760px)');
const labelThemes = () => { for (const o of $('theme-select').options) { o.textContent = `${o.value[0].toUpperCase()}${o.value.slice(1)}${narrow.matches ? '' : ' theme'}`; } };
labelThemes();
narrow.addEventListener('change', labelThemes);
function renderControls() {
  const a = approvalState(state?.pending);
  const theme = resolveTheme(themePreference, systemTheme.matches);
  document.documentElement.dataset.appearance = theme;
  updateControls({ theme, canApprove: a.canApprove && !busy && connected, canCancel: a.canCancel && !busy && connected, commandsDisabled: busy || !connected, onConfirm: confirmAction, onCommand: submitCommand });
}
$('theme-select').addEventListener('change', event => {
  themePreference = event.target.value;
  try { localStorage.setItem('oncall-theme', themePreference); } catch {}
  renderControls();
  announce(`${themePreference === 'system' ? 'System' : themePreference} theme selected.`);
});
systemTheme.addEventListener('change', renderControls);

function announce(text) { $('live').textContent = text; }
function setConnection(status, label) {
  $('connection-button').dataset.status = status;
  $('connection-label').textContent = label;
}
function openSettings(message) {
  if (message) $('settings-message').textContent = message;
  $('api-origin').value = base || window.location.origin;
  $('api-token').value = token;
  if (!$('settings-dialog').open) $('settings-dialog').showModal();
  $('api-token').focus();
}
function showError(error) {
  const message = error.message || 'The request could not be completed.';
  $('error-title').textContent = error.status === 401 || error.status === 403 ? 'API access needs configuration' : 'Request not completed';
  $('error-detail').textContent = message;
  $('error-banner').hidden = false;
  announce(message);
}
async function request(path, body) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), body === undefined ? 15000 : 90000);
  try {
    const response = await fetch(base + path, { method: body === undefined ? 'GET' : 'POST', headers: requestHeaders(session, token), ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal, cache: 'no-store', credentials: 'same-origin' });
    const text = await response.text();
    let data;
    try { data = JSON.parse(text); } catch { throw new Error(`The API returned an unreadable response (${response.status}). Check the server address and version.`); }
    if (!response.ok) {
      const detail = data.detail ?? data.error ?? data.message;
      const error = new Error(typeof detail === 'string' ? detail : `The API rejected this request (HTTP ${response.status}).${detail ? ' ' + json(detail) : ''}`);
      error.status = response.status;
      if (response.status === 401 || response.status === 403) {
        authBlocked = true;
        connected = false;
        setConnection('error', 'Configure API access');
        openSettings('The server denied API access. Enter a valid bearer token, then save to reconnect. No action has been retried.');
      }
      throw error;
    }
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(body === undefined ? 'The server took too long to return state. Check the connection and retry.' : 'The request timed out. Its outcome is unknown; refresh state before trying the action again.');
    if (error instanceof TypeError) throw new Error('Cannot reach the OnCall API. Check that your server is running and this address is allowed. Last received evidence may be out of date.');
    throw error;
  } finally { clearTimeout(timer); }
}

function applyState(data) {
  if (!data || !['sandbox', 'live'].includes(data.mode)) throw new Error('The server did not return a valid workspace state. Upgrade the API to the documented /api/state contract.');
  if (state?.incident?.id !== data.incident?.id) { investigated = false; verified = false; rolledBack = false; }
  state = data;
  connected = true;
  authBlocked = false;
  setConnection('connected', 'API connected');
  render();
}
async function refresh({ force = false } = {}) {
  if (polling || busy || (authBlocked && !force)) return;
  polling = true;
  const started = generation;
  try {
    const data = await request('/api/state');
    if (started === generation) applyState(data);
  } catch (error) {
    if (started !== generation) return;
    connected = false;
    if (!authBlocked) setConnection('error', state ? 'Offline · last known state' : 'API unavailable');
    showError(error);
    render();
  } finally { polling = false; }
}

function render() {
  const s = state || {};
  const v = viewState(s);
  const mode = s.mode;
  $('rail-mode').textContent = mode === 'sandbox' ? 'Local sandbox' : mode === 'live' ? 'Live environment' : 'Not connected';
  $('rail-description').textContent = mode === 'sandbox' ? 'Isolated fault-injection scenario. No production traffic.' : mode === 'live' ? 'Server-configured integrations. Changes require approval.' : 'Mode is reported by the server.';
  $('mode-label').textContent = mode === 'sandbox' ? 'Sandbox' : mode === 'live' ? 'Live' : 'Connection';
  $('mode-description').textContent = mode === 'sandbox' ? 'Isolated local requests. Alexa+ interaction simulation; no Alexa device or production connection.' : mode === 'live' ? 'Live mode. Missing integrations block operations; configured does not mean externally verified.' : 'Connect to inspect the server’s mode and service evidence.';
  $('footer-scope').textContent = mode === 'sandbox' ? 'Sandbox evidence only. Not a production readiness claim.' : 'Server-reported evidence. Operator-approved changes.';
  // Previously read a field the server never sent; use the reported reasoning mode.
  const reasoning = s.integrations?.reasoning?.mode;
  $('reasoner-label').hidden = !reasoning;
  $('reasoner-label').textContent = reasoning?.startsWith('bedrock') ? 'Bedrock investigation · read-only tools, human-gated actions' : 'Deterministic investigation';
  const incident = s.incident;
  $('session-summary').textContent = incident ? `${s.service || 'Service'} · ${incident.status === 'resolved' ? 'Last issue resolved' : 'Issue in progress'}` : 'No action runs until you ask.';
  $('sandbox-start').disabled = !connected || busy || !v.canStart;
  document.querySelector('.sandbox-menu').hidden = mode !== 'sandbox';
  if (!voice?.active) $('session-status').textContent = busy ? 'Checking the evidence…' : connected ? 'Ready when you are' : 'Waiting for connection';
  const incidentActive = incident && !['resolved', 'closed'].includes(incident.status);
  $('incident-title').textContent = incident?.title || (mode === 'live' ? 'Your live workspace.' : 'A clear place to start.');
  $('incident-status').textContent = !state ? 'Waiting for state' : incident ? String(incident.status || 'Status unknown').replaceAll('_', ' ') : 'No active incident';
  $('incident-status').className = `badge${incidentActive ? ' warning' : ''}`;
  $('incident-description').textContent = !state ? 'Connect to your OnCall server to view service evidence and available operations.' : incident ? (incidentActive ? 'Follow the evidence before changing the deployment. Every rollback requires an exact, time-limited approval.' : 'The server has recorded this incident as resolved. Inspect the latest measurements and download the handoff.') : mode === 'sandbox' ? 'Start an isolated checkout failure. Investigate real local requests, review a rollback, and verify recovery—without touching production.' : 'Investigate your configured service. Sandbox fault injection is unavailable in live mode.';
  $('incident-meta').hidden = !incident;
  $('incident-meta').innerHTML = incident ? `<code>${escape(incident.id)}</code><span>Started ${escape(fullTime(incident.started_at))}</span>${incident.source === 'alert' ? '<span>Opened by monitoring alert</span>' : ''}${incident.acknowledged_at ? `<span>Acknowledged ${escape(fullTime(incident.acknowledged_at))}</span>` : ''}${incident.resolved_at ? `<span>Resolved ${escape(fullTime(incident.resolved_at))}</span>` : ''}` : '';
  const canAck = connected && !busy && incidentActive && !incident.acknowledged_at;
  $('ack-button').hidden = !(incidentActive && !incident?.acknowledged_at);
  $('ack-button').disabled = !canAck;
  $('service-label').textContent = s.service ? `${s.service} / ${mode === 'sandbox' ? 'local request evidence' : 'server telemetry'}` : 'Waiting for a service';
  $('health-badge').textContent = !connected && state ? 'Last known · offline' : v.stale && state ? 'Stale telemetry' : ({ healthy: 'Healthy', degraded: 'Degraded', unknown: 'Unknown' })[v.health];
  $('health-badge').className = `badge ${connected ? v.health : ''}`;
  $('error-rate').textContent = v.errorRate === null ? '—' : percent(v.errorRate);
  $('sample-count').textContent = v.samples ? `${v.samples.toLocaleString()} request samples` : 'No request samples';
  $('last-updated').textContent = s.last_updated ? `Measured ${time(s.last_updated)}${v.stale ? ' · stale' : ''}` : 'No telemetry received';
  const signature = JSON.stringify([s.series, s.deploys, s.timeline, s.integrations, s.readiness, s.mode]);
  if (signature !== lastStateSignature) {
    renderChart(v.points);
    renderDeployments(s.deploys || []);
    renderTimeline(s.timeline || []);
    renderIntegrations(s);
    lastStateSignature = signature;
  }
  renderApproval();
  nextAction = !connected ? 'connect' : s.pending ? 'review' : !incident && mode === 'sandbox' ? 'start' : !investigated ? 'investigate' : incidentActive && !verified ? 'rollback' : 'verify';
  if (connected && !s.pending && (rolledBack || incident?.status === 'verifying')) nextAction = 'verify';
  if (connected && (incident?.status === 'resolved' || incident?.status === 'closed')) nextAction = mode === 'sandbox' && v.canStart ? 'start' : verified ? 'report' : 'verify';
  const actions = { connect: ['Configure connection', 'API access is required before any operation.'], start: ['Start sandbox incident', 'Creates a new isolated scenario; existing records are retained.'], investigate: ['Investigate incident', 'Retrieve deployment and request evidence.'], rollback: ['Review rollback', 'Review only. No deployment changes until approval.'], review: ['Review pending approval', 'The exact rollback target is shown in the assistant.'], verify: ['Verify recovery', mode === 'sandbox' ? 'Fresh local requests every 10 seconds across the watch window; every batch must pass.' : 'Read current recovery evidence from configured integrations.'], report: ['Download incident report', 'Export the server’s Markdown handoff.'] };
  $('next-action').textContent = busy ? 'Working…' : actions[nextAction][0];
  $('next-action-note').textContent = actions[nextAction][1];
  $('next-action').disabled = busy || (nextAction === 'start' && !v.canStart);
  $('next-action').classList.toggle('primary', !s.pending);
  $('export-button').disabled = !connected || busy || !incident;
  $('send-button').disabled = busy || !connected;
  document.querySelectorAll('[data-command]').forEach(button => { button.disabled = busy || !connected; });
  $('refresh-button').disabled = busy || polling;
  const activeStep = !incident ? 0 : s.pending ? 3 : verified ? 5 : incident.status === 'resolved' ? 4 : investigated ? 2 : 1;
  [...$('workflow-list').children].forEach((li, index) => { li.className = index === activeStep ? 'current' : index < activeStep ? 'done' : ''; if (index === activeStep) li.setAttribute('aria-current', 'step'); else li.removeAttribute('aria-current'); });
}

function renderChart(points) {
  $('measurement-details').hidden = !points.length;
  if (!points.length) {
    $('chart-container').innerHTML = '<div class="chart-empty"><span class="empty-cross" aria-hidden="true">+</span><strong>Evidence, not estimates.</strong><p>Actual request measurements will appear here once the server returns telemetry.</p></div>';
    return;
  }
  const start = points[0].at;
  const end = points.at(-1).at;
  const x = at => end === start ? 340 : 52 + (at - start) / (end - start) * 584;
  const y = rate => 172 - rate * 144;
  const grid = [1, .5, 0].map(rate => `<line class="grid-line" x1="52" x2="636" y1="${y(rate)}" y2="${y(rate)}"/><text x="40" y="${y(rate) + 5}" text-anchor="end">${rate * 100}%</text>`).join('');
  const dots = points.map(p => `<circle cx="${x(p.at)}" cy="${y(p.rate)}" r="3"><title>${escape(fullTime(p.at))}: ${percent(p.rate)}</title></circle>`).join('');
  // Date.parse(number) is intentionally avoided for observed epoch points.
  const stamp = at => new Date(at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  $('chart-container').innerHTML = `<svg class="chart" viewBox="0 0 660 220" role="img" aria-label="Observed error rate, zero to one hundred percent. ${points.length} measurements. A data table follows."><title>Observed request errors over time</title>${grid}${points.length > 1 ? `<polyline class="data-line" points="${points.map(p => `${x(p.at)},${y(p.rate)}`).join(' ')}"/>` : ''}${dots}<text x="52" y="208">${escape(stamp(start))}</text>${end !== start ? `<text x="636" y="208" text-anchor="end">${escape(stamp(end))}</text>` : ''}</svg>`;
  $('measurement-table').innerHTML = `<table><caption class="sr-only">Observed error-rate measurements</caption><thead><tr><th scope="col">Observed at</th><th scope="col">Error rate</th></tr></thead><tbody>${points.map(p => `<tr><td>${escape(new Date(p.at).toLocaleString())}</td><td>${percent(p.rate)}</td></tr>`).join('')}</tbody></table>`;
}
function renderDeployments(deploys) {
  const list = Array.isArray(deploys) ? deploys : [];
  $('deploy-count').textContent = list.length ? `${list.length} returned` : 'No records';
  $('deployments').innerHTML = !list.length ? '<div class="empty-block"><strong>No deployments returned yet</strong><p>Investigate to retrieve deployment history and compare it with measured failures.</p></div>' : [...list].reverse().map(d => `<article class="deploy-item"><div><p class="deploy-title">${escape(d.service || state.service || 'Service not supplied')} <code>${escape(d.commit || d.sha || 'Commit not supplied')}</code></p><p>${escape(d.message || d.description || 'Deployment record')}</p><p><time>${escape(fullTime(d.deployed_at || d.at))}</time></p></div><div><span class="badge${d.rolled_back ? '' : d.faulty ? ' warning' : ''}">${d.rolled_back ? 'Rolled back' : d.faulty ? 'Flagged by server' : escape(d.status || 'Recorded')}</span></div></article>`).join('');
}
function renderTimeline(entries) {
  const list = Array.isArray(entries) ? [...entries].sort((a, b) => Date.parse(a.at) - Date.parse(b.at)) : [];
  const markup = items => items.length ? items.map(item => `<li class="timeline-item"><time title="${escape(fullTime(item.at))}">${escape(time(item.at))}</time><div><strong>${escape(item.title || item.kind || 'Recorded event')}</strong><p>${escape(typeof item.detail === 'object' ? json(item.detail) : item.detail)}</p></div></li>`).join('') : '<li class="empty-block"><strong>The record starts with your first action</strong><p>Investigation, approvals and verification are recorded by the server.</p></li>';
  $('timeline').innerHTML = markup(list.slice(-5));
  $('activity-timeline').innerHTML = markup(list);
}
function renderIntegrations(s) {
  const disclosed = s.integrations || {};
  const entries = (Array.isArray(disclosed) ? disclosed.map(item => [item.name || item.provider, item]) : Object.entries(disclosed)).filter(([key]) => !['mode', 'service'].includes(key));
  const names = { github: 'GitHub', prometheus: 'Prometheus', rollback: 'Rollback webhook', rollback_webhook: 'Rollback webhook', bedrock: 'Bedrock', mcp: 'MCP tools', metrics: 'Metrics source', notifications: 'Paging', reasoning: 'Investigation', loki: 'Loki logs' };
  const rows = entries.length ? entries : ['github', 'prometheus', 'rollback', 'bedrock'].map(name => [name, null]);
  $('integration-summary').textContent = s.mode === 'sandbox' ? 'Sandbox mode uses isolated local operations. External integrations are not required for this workflow.' : 'Live health comes from fail-closed provider evidence. Configuration shown below is not a connectivity check or external validation.';
  $('integration-list').innerHTML = rows.map(([key, value]) => {
    const configured = typeof value === 'boolean' ? value : value?.configured;
    const status = configured === true ? 'Configured · not verified' : configured === false ? 'Not configured' : typeof value === 'string' ? value : value?.status || 'Not reported';
    const detail = value?.message || value?.detail || (configured === true ? 'Configuration was reported by the API; this is not an external connectivity check.' : 'Configure this integration on the server. No secret values are entered here.');
    return `<div class="integration-row"><dt>${escape(names[key] || key)}<span class="badge">${escape(status)}</span></dt><dd>${escape(typeof detail === 'object' ? json(detail) : detail)}</dd></div>`;
  }).join('');
}
function renderApproval() {
  const pending = state?.pending;
  const a = approvalState(pending);
  $('approval-panel').hidden = !pending;
  if (!pending) { lastApprovalId = null; renderControls(); return; }
  $('approval-service').textContent = pending.service || 'Missing target';
  $('approval-commit').textContent = pending.commit || 'Missing commit';
  $('approval-id').textContent = pending.id || 'Missing action ID';
  $('approval-expiry').textContent = fullTime(pending.expires_at);
  $('approval-countdown').textContent = a.expired ? 'Approval expired. Cancel or request a fresh review.' : a.canApprove ? `${a.remaining}s remaining. Approval is single-use and session-bound.` : 'Incomplete approval target. Approval is disabled; request a fresh review.';
  renderControls();
  renderVoiceChallenge(pending, a);
  if (lastApprovalId !== pending.id) {
    lastApprovalId = pending.id;
    announce(`Approval required for ${pending.service}, commit ${pending.commit}. Review the exact target in the assistant.`);
  }
}

// Voice-code approval: the server issues a single-use code bound to this exact action.
// Sandbox shows it on screen; live mode pages it and never returns it to the browser.
function renderVoiceChallenge(pending, a) {
  const c = state?.voice_challenge;
  const live = c && c.action_id === pending.id && c.status === 'pending' && Date.parse(c.expires_at) > Date.now();
  const usable = a.canApprove && connected && !busy;
  $('request-code').hidden = !!live;
  $('request-code').disabled = !usable;
  $('request-code').textContent = c && c.action_id === pending.id && c.status !== 'pending' ? 'Request a new voice code' : 'Approve by voice code';
  $('code-form').hidden = !live;
  $('code-input').disabled = $('code-submit').disabled = !live || !usable;
  if (live) {
    const left = Math.max(0, Math.round((Date.parse(c.expires_at) - Date.now()) / 1000));
    $('code-hint').textContent = c.display_code
      ? `Sandbox code: ${c.display_code.split('').join(' ')}. Say it or type it. ${left}s, ${c.attempts_left} tries left.`
      : `A code was sent to your pager. Say it or type it. ${left}s, ${c.attempts_left} tries left.`;
  }
}
async function voiceApproval(code) {
  const pending = state?.pending;
  if (!pending || busy || !connected) return;
  await operation('/api/voice-approval', code ? { action_id: pending.id, code } : { action_id: pending.id },
    code ? 'Checking the approval code…' : 'Requesting a one-time approval code…',
    data => { if (data.voice_challenge !== undefined && state) state = { ...state, voice_challenge: data.voice_challenge }; });
  $('code-input').value = '';
}

function message(role, text) {
  const log = $('conversation');
  log.querySelector('.assistant-welcome')?.remove();
  const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
  const article = document.createElement('article');
  article.className = `message ${role}`;
  article.innerHTML = `<div class="message-label"><span>${role === 'user' ? 'You' : role === 'assistant' ? 'OnCall' : 'Workspace'}</span><time>${escape(time(new Date().toISOString()))}</time></div><p></p>`;
  article.querySelector('p').textContent = text;
  const jump = jumpButton();
  log.insertBefore(article, jump);
  if (nearBottom || role === 'user') log.scrollTop = log.scrollHeight;
  updateJump();
}
// "Latest" pill: shown only when the operator has scrolled up and newer messages exist.
function jumpButton() {
  const log = $('conversation');
  let jump = log.querySelector('.conversation-jump');
  if (!jump) {
    jump = document.createElement('button');
    jump.type = 'button';
    jump.className = 'conversation-jump';
    jump.hidden = true;
    jump.textContent = 'Latest ↓';
    jump.setAttribute('aria-label', 'Jump to the latest message');
    jump.addEventListener('click', () => { log.scrollTop = log.scrollHeight; updateJump(); });
    log.append(jump);
    log.addEventListener('scroll', updateJump, { passive: true });
  }
  return jump;
}
function updateJump() {
  const log = $('conversation');
  const jump = log.querySelector('.conversation-jump');
  if (jump) jump.hidden = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
}
function appendTools(steps) {
  if (!Array.isArray(steps) || !steps.length) return;
  if (!tools.length) $('tool-results').replaceChildren();
  for (const step of steps) {
    tools.push(step);
    const details = document.createElement('details');
    details.className = `tool-call${step.is_error ? ' error' : ''}`;
    details.innerHTML = `<summary>${escape(step.tool || 'Unnamed tool')}${step.is_error ? ' · Error' : ''}</summary><p class="tool-meta">${escape(time(step.at))} · ${Number.isFinite(step.duration_ms) ? `${escape(step.duration_ms)} ms` : 'Duration not supplied'} · ${step.is_error ? 'Tool reported an error' : 'Returned by server'}</p><h3>Arguments</h3><pre>${escape(json(step.args))}</pre><h3>Result</h3><pre>${escape(json(step.result))}</pre>`;
    $('tool-results').append(details);
  }
  $('tool-count').textContent = `${tools.length} ${tools.length === 1 ? 'call' : 'calls'} this session`;
}
function response(data) {
  const progress = workflowProgress(data.steps);
  investigated ||= progress.investigated;
  rolledBack ||= progress.rolledBack;
  verified ||= progress.verified;
  appendTools(data.steps);
  if (data.state) applyState(data.state);
  if (state && Object.hasOwn(data, 'pending')) state = { ...state, pending: data.pending };
  if (data.speech) {
    message('assistant', data.speech);
    announce(data.speech);
    responseSpeech = data.speech;
    if (!voice?.active && $('voice-output').checked && 'speechSynthesis' in window) {
      speechSynthesis.cancel();
      const utterance = new SpeechSynthesisUtterance(data.speech);
      utterance.lang = 'en-US';
      speechSynthesis.speak(utterance);
    }
  }
  render();
}
async function operation(path, payload, label, onSuccess) {
  if (busy || !connected) return;
  busy = true;
  generation += 1;
  $('error-banner').hidden = true;
  announce(label);
  render();
  try {
    const data = await request(path, payload);
    if (path === '/api/demo/start') { applyState(data.state || data); message('system', 'Sandbox scenario started. The workspace now shows its server-recorded evidence.'); }
    else response(data);
    onSuccess?.(data);
  } catch (error) { showError(error); message('system', error.message); }
  finally { busy = false; render(); await refresh(); if (voice?.active) voice.reply(responseSpeech || 'The request has finished. Check the result on screen.', $('voice-output').checked); responseSpeech = ''; }
}
// Every exit path answers the voice loop, so hands-free never stalls on "Checking…".
function localReply(text, role = 'system') {
  message(role, text);
  announce(text);
  if (voice?.active) voice.reply(text, $('voice-output').checked);
}
async function submitCommand(text) {
  const clean = text.trim();
  if (!clean) return;
  if (busy) { if (voice?.active) voice.reply('One moment, I am still on the last request.', $('voice-output').checked); return; }
  if (!connected) { openSettings('Connect to the API before sending a command. Your draft is preserved.'); voice?.end(); return; }
  const pending = state?.pending;
  $('command-input').value = '';
  // Spoken or typed one-time code: the server verifies it against the exact pending action.
  if (pending && state?.voice_challenge?.status === 'pending' && looksLikeCode(clean)) {
    message('user', clean.replace(/\d/g, '•'));
    await voiceApproval(clean);
    return;
  }
  const intent = commandIntent(clean);
  if (intent === 'approve' && !/^approve/i.test(clean)) {
    // "confirm" is screen-only: the visible button is the only non-code approval.
    message('user', clean);
    if (!pending) { localReply('Nothing is pending. Ask me to review a rollback first.'); return; }
    localReply('Check the target, then press Confirm rollback. Or say "approve" for a one-time code.');
    $('approval-panel').focus({ preventScroll: true });
    return;
  }
  if (intent === 'cancel' && pending) { message('user', clean); await confirmAction(false); return; }
  if (intent === 'ambiguous' && !/^(ok(ay)?\s+)?approve( it| the rollback| rollback)?\.?$/i.test(clean)) {
    message('user', clean);
    localReply(pending ? 'Saying yes does not approve a change. Say "approve" for a one-time code, or use the buttons.' : 'There is nothing to approve. Ask me to review a rollback first.');
    return;
  }
  message('user', clean);
  await operation('/command', { text: clean }, 'Retrieving evidence…');
}
async function confirmAction(approved) {
  const pending = state?.pending;
  const a = approvalState(pending);
  if (!pending || (approved ? !a.canApprove : !a.canCancel)) { announce('Approval is unavailable or expired. Request a fresh review.'); return; }
  await operation('/confirm', { approved, action_id: pending.id }, approved ? 'Submitting the exact rollback approval…' : 'Cancelling the pending action…');
}
async function downloadReport() {
  if (!connected || busy) return;
  $('export-button').disabled = true;
  try {
    const data = await request('/api/report');
    if (typeof data.markdown !== 'string' || !data.markdown.trim()) throw new Error('The server has not produced an incident report yet.');
    const url = URL.createObjectURL(new Blob([data.markdown], { type: 'text/markdown;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = `oncall-${String(state?.incident?.id || 'report').replace(/[^a-zA-Z0-9_-]/g, '-')}.md`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    announce('Server incident report downloaded.');
  } catch (error) { showError(error); }
  finally { render(); }
}

function navigate() {
  const page = ['workspace', 'activity', 'integrations'].includes(location.hash.slice(1)) ? location.hash.slice(1) : 'workspace';
  const names = { workspace: ['Workspace', 'Incident workspace', 'Understand the signal. Review the change. Verify the recovery.'], activity: ['Activity', 'The incident record', 'A chronological account of investigation, decisions and recovery.'], integrations: ['Integrations', 'Know what’s connected.', 'Inspect configuration. Keep sandbox and live operations separate.'] };
  for (const name of Object.keys(names)) $(`${name}-view`).hidden = name !== page;
  $('recent-activity').hidden = page !== 'workspace';
  document.querySelectorAll('[data-nav]').forEach(link => { if (link.dataset.nav === page) link.setAttribute('aria-current', 'page'); else link.removeAttribute('aria-current'); });
  $('context-panel').hidden = page === 'workspace' && !detailsOpen;
  $('context-toggle').setAttribute('aria-expanded', String(!$('context-panel').hidden));
  $('context-toggle').textContent = $('context-panel').hidden ? 'View details' : 'Close details';
  $('section-label').textContent = page === 'workspace' ? 'OnCall' : names[page][0];
  $('page-title').textContent = names[page][1];
  $('page-subtitle').textContent = names[page][2];
  $('page-context').textContent = page === 'workspace' ? 'Your incident command center' : 'Incident operations';
  document.title = `OnCall — ${names[page][0]}`;
}
window.addEventListener('hashchange', () => { navigate(); $('main').focus({ preventScroll: true }); });
document.querySelectorAll('[data-settings]').forEach(button => button.addEventListener('click', () => openSettings()));
$('close-settings').addEventListener('click', () => $('settings-dialog').close());
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  token = $('api-token').value.trim();
  store('oncall-token', token);
  generation += 1;
  authBlocked = false;
  $('settings-dialog').close();
  $('error-banner').hidden = true;
  setConnection('loading', 'Reconnecting…');
  await refresh({ force: true });
  if (!storageAvailable) message('system', 'Session storage is unavailable. Credentials and session identity are held in memory until this page reloads.');
});
$('clear-token').addEventListener('click', () => { token = ''; store('oncall-token', ''); $('api-token').value = ''; $('api-token').focus(); announce('Bearer token cleared. Save to reconnect without it.'); });
$('dismiss-error').addEventListener('click', () => { $('error-banner').hidden = true; });
$('refresh-button').addEventListener('click', () => refresh({ force: true }));
$('command-form').addEventListener('submit', event => { event.preventDefault(); submitCommand($('command-input').value); });
$('command-input').addEventListener('keydown', event => { if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); submitCommand($('command-input').value); } });
document.querySelectorAll('[data-command]').forEach(button => button.addEventListener('click', () => submitCommand(button.dataset.command)));

$('export-button').addEventListener('click', downloadReport);
$('next-action').addEventListener('click', async () => {
  if (nextAction === 'connect') openSettings();
  else if (nextAction === 'start') await operation('/api/demo/start', {}, 'Starting an isolated sandbox incident…');
  else if (nextAction === 'investigate') await submitCommand("What's on fire?");
  else if (nextAction === 'rollback') await submitCommand('Roll back the latest deploy');
  else if (nextAction === 'review') { $('approval-panel').scrollIntoView({ block: 'center', behavior: 'instant' }); $('approval-panel').focus({ preventScroll: true }); }
  else if (nextAction === 'verify') await operation('/api/verify', {}, 'Verifying with fresh evidence…', data => { if (data.steps?.length && !data.steps.some(step => step.is_error)) verified = true; });
  else if (nextAction === 'report') await downloadReport();
});
$('ack-button').addEventListener('click', async () => {
  const id = state?.incident?.id;
  if (id) await operation('/api/ack', { incident_id: id }, 'Acknowledging the incident…');
});
$('voice-output').addEventListener('change', () => { if (!$('voice-output').checked && 'speechSynthesis' in window) speechSynthesis.cancel(); });
if (!('speechSynthesis' in window)) { $('voice-output').disabled = true; $('voice-output').closest('label').title = 'Spoken output is not supported by this browser.'; }

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
voice = new VoiceSession({
  recognition: SpeechRecognition ? new SpeechRecognition() : null,
  synthesis: window.speechSynthesis,
  makeUtterance: text => { const u = new SpeechSynthesisUtterance(text); u.lang = 'en-US'; u.rate = 1.04; return u; },
  isCode: text => !!state?.pending && state?.voice_challenge?.status === 'pending' && looksLikeCode(text),
  onCommand: text => submitCommand(text),
  onDraft: (text, interim) => {
    $('command-input').value = interim ? text : '';
    if (!interim) { message('user', text); localReply(state?.pending ? 'Saying yes does not approve. Say "approve" for a one-time code, or use the buttons.' : 'Nothing is pending to approve.'); }
  },
  onState: (status, detail) => {
    document.body.dataset.voice = status;
    const labels = { idle: 'Ready when you are', starting: 'Opening microphone…', listening: 'Listening to you', working: 'Checking the evidence…', speaking: 'OnCall is speaking', paused: 'Voice paused · tap Resume', error: 'Microphone unavailable', unsupported: 'Voice unavailable in this browser' };
    $('session-status').textContent = labels[status];
    $('mic-button').setAttribute('aria-pressed', String(status === 'listening'));
    const micLabel = ['listening', 'starting', 'working'].includes(status) && voice?.active ? 'Stop voice' : status === 'speaking' ? 'Interrupt' : status === 'paused' ? 'Resume voice' : 'Start voice';
    $('mic-button').querySelector('span').textContent = micLabel;
    $('mic-button').setAttribute('aria-label', micLabel);
    $('end-voice').hidden = !voice?.active;
    $('interrupt-voice').hidden = status !== 'speaking';
    $('mic-status').textContent = status === 'error' ? `Microphone ${detail || 'unavailable'}. You can keep typing.` : status === 'listening' ? 'Listening. Just keep talking; I reply and listen again. Changes still need approval.' : status === 'paused' ? 'Paused after silence. Tap Resume voice to continue.' : status === 'unsupported' ? 'Type below. Voice requires a browser with speech recognition.' : 'Browser speech may use your browser provider’s service.';
    // Idle hints are hidden on narrow screens; active states (listening, paused, error) stay visible.
    $('mic-status').dataset.state = ['error', 'listening', 'paused'].includes(status) ? 'active' : 'idle';
  }
});
$('mic-button').disabled = !SpeechRecognition;
$('mic-status').textContent = SpeechRecognition ? 'Start voice to speak and hear replies. Browser speech may use an external service.' : 'Type below. Voice requires a browser with speech recognition.';
$('mic-status').dataset.state = 'idle';
$('mic-button').addEventListener('click', () => {
  if (!connected || busy) return;
  if (voice.active && ['listening', 'starting'].includes(voice.state)) { voice.end(); return; }
  if (voice.active && voice.state === 'speaking') { voice.interrupt(); return; }
  if (voice.active && voice.state === 'paused') { voice.listen(); return; }
  $('voice-output').checked = 'speechSynthesis' in window;
  voice.start();
});
$('end-voice').addEventListener('click', () => voice.end());
$('request-code').addEventListener('click', () => voiceApproval());
$('code-form').addEventListener('submit', event => { event.preventDefault(); const code = $('code-input').value.trim(); if (code) voiceApproval(code); });
$('interrupt-voice').addEventListener('click', () => voice.interrupt());
$('context-toggle').addEventListener('click', () => { detailsOpen = $('context-panel').hidden; if (location.hash && location.hash !== '#workspace') location.hash = '#workspace'; else navigate(); });
$('sandbox-start').addEventListener('click', async () => { document.querySelector('.sandbox-menu').open = false; await operation('/api/demo/start', {}, 'Creating a local test incident…'); await submitCommand("What's on fire?"); });
document.querySelectorAll('[data-nav]').forEach(link => link.setAttribute('aria-label', link.textContent.trim()));

navigate();
render();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
setInterval(() => { if (!document.hidden && state) { renderApproval(); const v = viewState(state); if (v.stale) { $('health-badge').textContent = connected ? 'Stale telemetry' : 'Last known · offline'; $('health-badge').className = 'badge'; } } }, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
