// Simulated Alexa+ (Echo Show) surface for OnCall. All evidence comes from the server;
// approvals follow app.js exactly: "yes" never approves, codes are checked server-side.
import { requestHeaders, looksLikeCode, commandIntent, resolveTheme } from './state.mjs';
import { VoiceSession } from './voice-session.mjs';
import * as view from './alexa-view.mjs';

const $ = id => document.getElementById(id);
const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const hasSynthesis = 'speechSynthesis' in window;

// Same sessionStorage keys as the workspace (app.js); the token never leaves sessionStorage.
function stored(key) { try { return sessionStorage.getItem(key); } catch { return null; } }
function store(key, value) { try { value ? sessionStorage.setItem(key, value) : sessionStorage.removeItem(key); } catch {} }
let session = stored('oncall-session');
if (!session) {
  session = crypto.randomUUID ? crypto.randomUUID() : Array.from(crypto.getRandomValues(new Uint8Array(24)), n => n.toString(16).padStart(2, '0')).join('');
  store('oncall-session', session);
}
let token = stored('oncall-token') || '';

const ui = {
  state: null, connected: false, authBlocked: false, busy: false, polling: false,
  brief: null, correlation: null, recovery: null, report: null, focus: null,
  announcedId: stored('oncall-alexa-announced'), waking: false, wakeTimer: null,
  speaking: false, windowUntil: 0, followUp: false, audioUnlocked: false, queuedSpeech: '',
  recoveryTimer: null,
};

// ---------- theme (page only; the device is always dark) ----------
const systemTheme = matchMedia('(prefers-color-scheme: dark)');
let themePreference = 'system';
try { themePreference = localStorage.getItem('oncall-theme') || 'system'; } catch {}
if (!['system', 'light', 'dark'].includes(themePreference)) themePreference = 'system';
$('theme-select').value = themePreference;
function applyTheme() {
  const theme = resolveTheme(themePreference, systemTheme.matches);
  document.documentElement.dataset.appearance = theme;
  document.documentElement.dataset.theme = theme;
}
$('theme-select').addEventListener('change', event => {
  themePreference = event.target.value;
  try { localStorage.setItem('oncall-theme', themePreference); } catch {}
  applyTheme();
});
systemTheme.addEventListener('change', applyTheme);
applyTheme();

// ---------- network ----------
async function request(path, body, { timeout } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout ?? (body === undefined ? 15000 : 90000));
  try {
    const response = await fetch(path, { method: body === undefined ? 'GET' : 'POST', headers: requestHeaders(session, token), ...(body === undefined ? {} : { body: JSON.stringify(body) }), signal: controller.signal, cache: 'no-store', credentials: 'same-origin' });
    const raw = await response.text();
    let data = null;
    try { data = JSON.parse(raw); } catch {}
    if (!response.ok) {
      const detail = data?.detail ?? data?.error ?? data?.message;
      const error = new Error(typeof detail === 'string' ? detail : `The server rejected this request (HTTP ${response.status}).`);
      error.status = response.status;
      if (response.status === 401) {
        ui.authBlocked = true; ui.connected = false;
        setConnection('error', 'Connect');
        openSettings('The server needs an API bearer token. Enter it to reconnect. Nothing was retried.');
      }
      throw error;
    }
    if (data === null) throw new Error(`The server returned an unreadable response (${response.status}).`);
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(body === undefined ? 'The server took too long to answer.' : 'The request timed out. Its outcome is unknown; I will not retry it.');
    if (error instanceof TypeError) throw new Error('I cannot reach the OnCall server right now.');
    throw error;
  } finally { clearTimeout(timer); }
}

function setConnection(status, label) {
  $('connection-button').dataset.status = status;
  $('connection-label').textContent = label;
}
function openSettings(message) {
  if (message) $('settings-message').textContent = message;
  $('api-token').value = token;
  if (!$('settings-dialog').open) $('settings-dialog').showModal();
  $('api-token').focus();
}
function announce(text) { $('live').textContent = text; }

async function refresh({ force = false } = {}) {
  if (ui.polling || ui.busy || (ui.authBlocked && !force)) return;
  ui.polling = true;
  try {
    applyState(await request('/api/state'));
  } catch (error) {
    ui.connected = false;
    if (!ui.authBlocked) setConnection('error', ui.state ? 'Offline · last known' : 'Server unavailable');
    $('offline-detail').textContent = error.message;
    render();
  } finally { ui.polling = false; }
}

function applyState(data) {
  if (!data || !['sandbox', 'live'].includes(data.mode)) throw new Error('The server did not return a valid OnCall state.');
  if (ui.state?.incident?.id !== data.incident?.id) { ui.correlation = null; ui.report = null; if (ui.recovery?.status !== 'watching') ui.recovery = null; ui.focus = null; }
  ui.state = data;
  ui.connected = true;
  ui.authBlocked = false;
  setConnection('connected', data.mode === 'sandbox' ? 'Sandbox connected' : 'Live connected');
  render();
  const wake = view.wakeIncident(data, ui.announcedId);
  if (wake) proactiveWake(wake);
}

// ---------- audio ----------
let audio;
function unlockAudio() {
  if (ui.audioUnlocked) return;
  ui.audioUnlocked = true;
  $('sound-hint').hidden = true;
  try { audio ||= new (window.AudioContext || window.webkitAudioContext)(); audio.resume(); } catch {}
  if (ui.queuedSpeech) { const text = ui.queuedSpeech; ui.queuedSpeech = ''; chime(); speak(text); }
}
// Generated two-note tone; no recorded audio.
function chime() {
  if (!audio) return;
  const start = audio.currentTime + 0.02;
  [[784, 0], [1047, 0.16], [1319, 0.32]].forEach(([freq, offset]) => {
    const osc = audio.createOscillator(), gain = audio.createGain();
    osc.type = 'sine'; osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.0001, start + offset);
    gain.gain.exponentialRampToValueAtTime(0.18, start + offset + 0.02);
    gain.gain.exponentialRampToValueAtTime(0.0001, start + offset + 0.5);
    osc.connect(gain).connect(audio.destination);
    osc.start(start + offset); osc.stop(start + offset + 0.55);
  });
}
['pointerdown', 'keydown'].forEach(type => window.addEventListener(type, unlockAudio, { capture: true }));

function makeUtterance(text) {
  const u = new SpeechSynthesisUtterance(text);
  u.lang = 'en-US'; u.rate = 1.02;
  return u;
}
// Every Alexa reply: show it, announce it, speak it, then open the follow-up window.
function speak(text) {
  $('t-alexa').textContent = text;
  announce(text);
  const aloud = $('spoken').checked && hasSynthesis;
  ui.followUp = true;
  if (voice.active) { voice.reply(text, aloud); render(); return; }
  if (!aloud) { openWindow(); return; }
  if (!ui.audioUnlocked) { ui.queuedSpeech = text; return; }
  speechSynthesis.cancel();
  const u = makeUtterance(text);
  ui.speaking = true;
  const done = () => { ui.speaking = false; openWindow(); render(); };
  u.onend = done; u.onerror = done;
  speechSynthesis.speak(u);
  render();
}
function openWindow() { ui.followUp = false; ui.windowUntil = Date.now() + view.WAKE_WINDOW_MS; render(); }
const windowOpen = () => Date.now() < ui.windowUntil;

// ---------- proactive wake ----------
async function proactiveWake(id) {
  ui.announcedId = id;
  store('oncall-alexa-announced', id);
  ui.waking = true;
  ui.focus = null;
  clearTimeout(ui.wakeTimer);
  ui.wakeTimer = setTimeout(() => { ui.waking = false; render(); }, 20000);
  render();
  let speech = '';
  try {
    const data = await request('/command', { text: 'status' });
    const evidence = view.evidenceFromSteps(data.steps);
    if (evidence.brief) ui.brief = evidence.brief;
    speech = evidence.briefSpeech || '';
  } catch {}
  const text = view.announcement(ui.state, speech);
  if (!ui.audioUnlocked) { ui.queuedSpeech = text; $('t-alexa').textContent = text; announce(text); render(); return; }
  chime();
  setTimeout(() => speak(text), 650);
}
function settle() { if (ui.waking) { ui.waking = false; clearTimeout(ui.wakeTimer); } }

// ---------- conversation ----------
const ACK = /^(i'?m on it|acknowledge( it)?|ack|i('ve)? got (it|this))[.!]?$/i;

async function submit(raw, { source = 'typed' } = {}) {
  const parsed = view.parseWake(raw, { windowOpen: source === 'typed' || windowOpen() });
  if (!parsed.accept) {
    if (parsed.woke && !parsed.text) { openWindow(); $('t-alexa').textContent = 'Listening…'; if (voice.active) voice.reply('', false); return; }
    if (voice.active) voice.reply('', false); // not addressed to Alexa: keep listening silently
    return;
  }
  const text = parsed.text;
  settle();
  ui.windowUntil = 0;
  if (ui.busy) { speak('One moment, I am still on the last request.'); return; }
  if (!ui.connected) { openSettings('Connect to the OnCall server first.'); if (voice.active) voice.reply('', false); return; }
  const pending = ui.state?.pending;
  const challenge = ui.state?.voice_challenge;
  // Spoken or typed one-time code: verified server-side against the exact pending action.
  if (pending && challenge?.status === 'pending' && looksLikeCode(text)) {
    $('t-user').textContent = view.maskDigits(text);
    await operation(() => request('/api/voice-approval', { action_id: pending.id, code: text }), 'Checking the code…');
    return;
  }
  $('t-user').textContent = text;
  const intent = commandIntent(text);
  if (intent === 'approve' && !/^approve/i.test(text)) {
    speak(pending ? 'On this device a rollback needs a one-time code. Say "approve" and I will send one to your pager.' : 'Nothing is waiting for approval.');
    return;
  }
  if (intent === 'cancel' && pending) { await operation(() => request('/confirm', { approved: false, action_id: pending.id }), 'Cancelling…'); return; }
  if (intent === 'ambiguous' && !view.APPROVE.test(text)) {
    speak(pending ? 'Saying yes does not approve a change. Say "approve" for a one-time code, or "cancel".' : 'There is nothing to approve. Ask me to roll back first.');
    return;
  }
  if (ACK.test(text)) { await acknowledge(); return; }
  const card = view.cardForUtterance(text);
  if (card === 'recovery') { await checkRecovery(); return; }
  if (card === 'handoff') { await handoff(); return; }
  await operation(() => request('/command', { text }), 'Thinking…', { card });
}

async function operation(call, label, { card = null } = {}) {
  if (ui.busy) return;
  ui.busy = true;
  announce(label);
  render();
  let speech = '';
  let watch = false;
  try {
    const before = ui.state?.pending?.id;
    const data = await call();
    const evidence = view.evidenceFromSteps(data.steps);
    if (evidence.brief) ui.brief = evidence.brief;
    if (evidence.correlation) ui.correlation = evidence.correlation;
    if (ui.state && Object.hasOwn(data, 'pending')) ui.state = { ...ui.state, pending: data.pending };
    if (ui.state && data.voice_challenge !== undefined) ui.state = { ...ui.state, voice_challenge: data.voice_challenge };
    speech = data.speech || '';
    if (data.pending && data.pending.id !== before && /^Review rollback/.test(speech)) speech = view.proposalSpeech(data.pending);
    if (card && ['impact', 'runbook', 'history'].includes(card) && (!speech || view.GENERIC_REPLY.test(speech))) speech = await localCardSpeech(card);
    if (card) ui.focus = card;
    if (evidence.rolledBack) { ui.focus = null; speech = `${speech} I'll watch fresh traffic now.`; watch = true; }
  } catch (error) {
    speech = error.message;
  } finally {
    ui.busy = false;
    await refresh();
    render();
    if (speech) speak(speech); else if (voice.active) voice.reply('', false);
    if (watch) checkRecovery({ quiet: true });
  }
}

// Card-only answers from server briefing data when the router has no matching intent.
async function localCardSpeech(card) {
  try {
    const data = await request('/command', { text: 'status' });
    const evidence = view.evidenceFromSteps(data.steps);
    if (evidence.brief) ui.brief = evidence.brief;
  } catch {}
  if (card === 'impact') return view.impactSpeech(view.impactCard(ui.state?.blast_radius || ui.brief?.blast_radius));
  if (card === 'runbook') return view.runbookSpeech(view.runbookCard(ui.brief?.runbook));
  return view.historySpeech(view.historyCard(ui.brief?.similar));
}

async function acknowledge() {
  const id = ui.state?.incident?.id;
  if (!id || ui.state.incident.acknowledged_at) { speak(id ? 'This incident is already acknowledged.' : 'There is no open incident to acknowledge.'); return; }
  settle();
  await operation(async () => { await request('/api/ack', { incident_id: id }); return { speech: 'Acknowledged. I let the team know you are on it.' }; }, 'Acknowledging…');
}

// Non-blocking recovery: POST /api/check-recovery, poll every 3 s while watching.
// Older servers without it fall back to the blocking /command verify path.
async function checkRecovery({ quiet = false } = {}) {
  clearTimeout(ui.recoveryTimer);
  ui.focus = 'recovery';
  let data;
  try {
    data = await request('/api/check-recovery', {});
  } catch (error) {
    if ([404, 405].includes(error.status)) { await verifyFallback(); return; }
    ui.recovery = { status: 'unknown', health: 'unknown' };
    render();
    speak(error.message);
    return;
  }
  const previous = ui.recovery?.status;
  ui.recovery = { ...data, fetchedAt: Date.now() };
  render();
  if (data.status === 'watching') {
    if (!quiet && previous !== 'watching') speak(data.speech || 'Watching fresh traffic.');
    ui.recoveryTimer = setTimeout(() => { if (!document.hidden) checkRecovery({ quiet: true }); else ui.recoveryTimer = setTimeout(() => checkRecovery({ quiet: true }), 3000); }, 3000);
    return;
  }
  await refresh();
  const done = data.speech || view.recoveryCard(data).headline;
  speak(data.status === 'complete' && data.health === 'healthy' ? `${done} Want the handoff?` : done);
}
async function verifyFallback() {
  ui.recovery = { status: 'watching', health: 'unknown', fetchedAt: Date.now() };
  render();
  speak('Watching fresh traffic. This takes about half a minute.');
  try {
    const data = await request('/command', { text: 'verify recovery' });
    const step = (data.steps || []).find(s => s.tool === 'verify_recovery' && !s.is_error);
    ui.recovery = { status: step ? 'complete' : 'unknown', health: step?.result?.health || 'unknown', sample_count: step?.result?.sample_count, fetchedAt: Date.now() };
    await refresh();
    speak(ui.recovery.health === 'healthy' ? `${data.speech} Want the handoff?` : (data.speech || 'Recovery is not verified.'));
  } catch (error) {
    ui.recovery = { status: 'unknown', health: 'unknown' };
    render();
    speak(error.message);
  }
}

async function handoff() {
  ui.focus = 'handoff';
  try {
    const data = await request('/api/report');
    ui.report = view.handoffCard(data.markdown);
    render();
    speak(view.handoffSpeech(ui.report));
  } catch (error) { ui.report = { available: false }; render(); speak(error.message); }
}

// ---------- rendering ----------
function setText(id, value) { const el = $(id); if (el.textContent !== value) el.textContent = value; }
function setHealth(pillId, labelId, health, label) { $(pillId).dataset.health = health; setText(labelId, label); }

function render() {
  const s = ui.state;
  const now = Date.now();
  const screen = view.selectScreen({ state: ui.connected || s ? s : null, focus: ui.focus, recovery: ui.recovery });
  const device = $('device');
  device.dataset.screen = screen;
  device.dataset.bar = view.lightBar({ waking: ui.waking, speaking: ui.speaking, busy: ui.busy, voice: voice?.state, windowOpen: windowOpen() });
  setText('status-clock', view.clockLabel(now));
  setText('status-mode', !s ? 'Not connected' : `${s.mode === 'sandbox' ? 'Sandbox' : 'Live'}${ui.connected ? '' : ' · offline'}`);

  if (screen === 'idle' || screen === 'offline') {
    const idle = view.idleCard(s || {}, now);
    setText('idle-time', idle.time); setText('idle-date', idle.date); setText('idle-note', idle.note);
    setHealth('idle-pill', 'idle-health', idle.health, idle.line);
  }
  if (s) {
    const inc = view.incidentCard(s, ui.brief, now);
    setText('inc-title', inc.title);
    setText('inc-kicker', `${inc.serviceLabel} · ${inc.acknowledged ? 'acknowledged' : 'not acknowledged'}${typeof s.escalation?.status === 'string' ? ` · ${s.escalation.status.slice(0, 30)}` : ''}`);
    setText('inc-rate', inc.errorRate); setText('inc-samples', inc.samples);
    setHealth('inc-pill', 'inc-health', inc.health, inc.stale && s ? 'Stale · unknown' : inc.healthLabel);
    setText('inc-since', inc.since ? `Since ${inc.sinceClock}` : '');
    setText('inc-commit', inc.commit); setText('inc-deployed', inc.deployedAt ? `Shipped ${view.agoLabel(inc.deployedAt, now)}` : '');
    if (inc.spark) {
      $('inc-spark-line').setAttribute('points', inc.spark.points);
      $('inc-spark-dot').setAttribute('cx', inc.spark.last[0]); $('inc-spark-dot').setAttribute('cy', inc.spark.last[1]);
      setText('inc-spark-label', `Observed error rate, ${inc.spark.count} measurements, latest ${view.percent(inc.spark.current)}, peak ${view.percent(inc.spark.peak)}`);
      setText('inc-spark-caption', inc.spark.count === 1
        ? `1 observed measurement so far · ${view.percent(inc.spark.peak)}`
        : `${inc.spark.count} observed measurements · peak ${view.percent(inc.spark.peak)}`);
    } else {
      $('inc-spark-line').setAttribute('points', '');
      $('inc-spark-dot').setAttribute('cx', -10);
      setText('inc-spark-caption', 'No observed measurements yet');
    }
    $('ack-button').hidden = !(view.isActive(s.incident) && !inc.acknowledged);
    $('ack-button').disabled = ui.busy || !ui.connected;

    const imp = view.impactCard(s.blast_radius || ui.brief?.blast_radius);
    setText('imp-failed', imp.available ? imp.failed : 'unknown');
    setText('imp-total', imp.available ? `of ${imp.total} requests · ${imp.errorRate}` : 'Impact data not reported by the server');
    setText('imp-minutes', imp.available ? imp.minutes : 'unknown'); setText('imp-trend', imp.available ? `Trend: ${imp.trend}` : '');
    setText('imp-revenue', imp.available ? imp.revenue : 'unknown'); setText('imp-estimate', imp.estimateNote || 'Estimate, not a measurement');
    setText('imp-summary', imp.summary || '');

    const sus = view.suspectCard(ui.correlation, s, now);
    setText('sus-title', sus.commit); setText('sus-deployed', sus.deployedClock); setText('sus-ago', sus.deployedAt ? sus.deployedAgo : '');
    setText('sus-lead', sus.lead); setText('sus-summary', sus.summary); setText('sus-caveat', sus.caveat);

    const run = view.runbookCard(ui.brief?.runbook);
    setText('run-title', run.available ? run.title : 'No runbook reported');
    $('run-steps').replaceChildren(...(run.available ? run.steps : ['Ask “What does the runbook say?” to load it from the server.']).map(step => Object.assign(document.createElement('li'), { textContent: step })));
    setText('run-source', run.source ? `Source: ${run.source}` : '');

    const his = view.historyCard(ui.brief?.similar);
    $('his-list').replaceChildren(...(his.items.length ? his.items.map(item => {
      const li = document.createElement('li');
      li.append(Object.assign(document.createElement('strong'), { textContent: item.title }), Object.assign(document.createElement('span'), { textContent: `${item.resolved} · ${item.recovered}` }));
      if (item.fix) li.append(Object.assign(document.createElement('p'), { textContent: `Fix: ${item.fix}` }));
      return li;
    }) : [Object.assign(document.createElement('li'), { textContent: his.available ? 'No similar incidents on record.' : 'History not reported by the server.' })]));

    renderApproval(now);
    renderRecovery(now);
    renderHandoff();
  }
  renderControls(screen);
}

function renderApproval(now) {
  const pending = ui.state?.pending;
  if (!pending) return;
  const a = view.approvalCard(pending, ui.state.voice_challenge, now);
  setText('apr-target', a.target); setText('apr-target-exact', a.target); setText('apr-id', a.shortId);
  $('apr-id').title = a.id;
  setText('apr-prompt', a.prompt);
  setText('apr-countdown', a.expired ? 'Expired' : `${a.countdown}s`);
  const ring = $('apr-ring');
  ring.style.strokeDasharray = a.ring.circumference; ring.style.strokeDashoffset = a.ring.offset;
  const code = ui.state.voice_challenge?.display_code;
  $('apr-code').hidden = !(a.codeLive && code);
  if (a.codeLive && code) {
    // Small label plus large digits, so only the code itself gets the wide display treatment.
    const label = document.createElement('span');
    label.className = 'approval-code-label';
    label.textContent = 'Sandbox stand-in for your pager';
    const digits = document.createElement('span');
    digits.className = 'approval-code-digits';
    digits.textContent = code;
    $('apr-code').replaceChildren(label, digits);
  }
  $('apr-code-form').hidden = !a.codeLive;
  $('apr-request').hidden = a.codeLive;
  $('apr-request').disabled = !a.canApprove || ui.busy;
  $('apr-request').textContent = a.codeLive ? 'Code sent' : 'Send me a code';
  $('apr-cancel').disabled = !a.canCancel || ui.busy;
}

function renderRecovery(now) {
  if (!ui.recovery) return;
  const r = view.recoveryCard(ui.recovery, now);
  setText('rec-title', r.headline); setText('rec-detail', r.detail); setText('rec-samples', r.samples || '');
  $('rec-ring-wrap').dataset.verdict = r.verdict;
  setText('rec-label', r.verdict === 'watching' ? (r.remaining === null ? '…' : `${r.remaining}s`) : r.verdict === 'verified' ? '✓' : r.verdict === 'idle' ? '–' : '!');
  const ring = $('rec-ring');
  ring.style.strokeDasharray = r.ring.circumference; ring.style.strokeDashoffset = r.ring.offset;
}

function renderHandoff() {
  const h = ui.report;
  if (!h) return;
  setText('hand-title', h.available ? h.title : 'No handoff yet');
  setText('hand-status', h.status || 'unknown'); setText('hand-ack', h.acknowledge || 'not recorded'); setText('hand-resolve', h.resolve || 'not recorded');
  $('hand-chronology').replaceChildren(...(h.chronology || []).map(item => Object.assign(document.createElement('li'), { textContent: item })));
}

let chipSignature = '';
function renderControls(screen) {
  const chips = view.chipsFor(screen, ui.state?.pending);
  const signature = chips.join('|');
  if (signature !== chipSignature) {
    chipSignature = signature;
    $('chips').replaceChildren(...chips.map(label => {
      const b = Object.assign(document.createElement('button'), { type: 'button', className: 'chip', textContent: `“${label}”` });
      b.setAttribute('aria-label', `Say: ${label}`);
      b.addEventListener('click', () => submit(label));
      return b;
    }));
  }
  const disabled = ui.busy || !ui.connected;
  $('chips').querySelectorAll('button').forEach(b => { b.disabled = disabled; });
  $('say-send').disabled = disabled;
  // The server reports whether it accepts demo start/reset (sandbox, or live with
  // ONCALL_DEMO_CONTROLS=true); older servers without the field fall back to sandbox-only.
  const demoAllowed = ui.state ? (ui.state.demo_controls ?? ui.state.mode === 'sandbox') : false;
  $('demo-start').disabled = disabled || !demoAllowed;
  $('demo-reset').disabled = disabled || !demoAllowed;
  if (!demoAllowed && ui.state) setText('judge-status', 'Demo controls are disabled on this server.');
}

// ---------- voice ----------
const voice = new VoiceSession({
  recognition: SpeechRecognition ? new SpeechRecognition() : null,
  synthesis: hasSynthesis ? window.speechSynthesis : null,
  makeUtterance,
  // Pass-through: wake-word-prefixed "approve" or a code must reach submit(), not be held as assent.
  isCode: text => {
    const spoken = view.parseWake(text, { windowOpen: true }).text;
    return view.APPROVE.test(spoken) || (!!ui.state?.pending && ui.state?.voice_challenge?.status === 'pending' && looksLikeCode(spoken));
  },
  onCommand: text => submit(text, { source: 'voice' }),
  onDraft: (text, interim) => {
    const parsed = view.parseWake(text, { windowOpen: windowOpen() });
    if (!parsed.accept && !parsed.woke) return;
    if (interim) { $('t-user').textContent = parsed.text || '…'; return; }
    // Final bare assent was held by VoiceSession; answer it, never send it.
    $('t-user').textContent = parsed.text;
    speak(ui.state?.pending ? 'Saying yes does not approve. Say "approve" for a one-time code, or "cancel".' : 'Nothing is waiting for approval.');
  },
  onState: (status, detail) => {
    if (status === 'listening' && ui.followUp) openWindow();
    const labels = { idle: '', starting: 'Opening microphone…', listening: windowOpen() ? 'Listening' : 'Waiting for “Alexa”', working: '', speaking: '', paused: 'Mic paused after silence. Tap to talk to resume.', error: `Microphone ${detail || 'unavailable'}. Typing still works.`, unsupported: 'Voice needs a browser with speech recognition. Typing works everywhere.' };
    setText('mic-status', labels[status] ?? '');
    $('talk-button').setAttribute('aria-pressed', String(voice?.active && ['listening', 'starting'].includes(status)));
    if (status === 'error' || status === 'unsupported') $('hands-free').checked = false;
    render();
  },
});
if (!SpeechRecognition) {
  $('talk-button').disabled = true; $('hands-free').disabled = true;
  setText('mic-status', 'Voice needs a browser with speech recognition. Typing works everywhere.');
}
if (!hasSynthesis) { $('spoken').checked = false; $('spoken').disabled = true; }

$('talk-button').addEventListener('click', () => {
  if (!ui.connected) { openSettings(); return; }
  settle();
  if (voice.state === 'speaking') { voice.interrupt(); openWindow(); return; }
  if (hasSynthesis && ui.speaking) { speechSynthesis.cancel(); ui.speaking = false; }
  openWindow();
  if (!voice.active) voice.start(); else if (voice.state === 'paused') voice.listen();
  $('t-alexa').textContent = 'Listening…';
});
$('hands-free').addEventListener('change', event => {
  if (event.target.checked) { if (!voice.active) voice.start(); setText('mic-status', 'Hands-free: start with “Alexa”.'); }
  else if (!windowOpen()) voice.end();
});
$('spoken').addEventListener('change', () => { if (!$('spoken').checked && hasSynthesis) speechSynthesis.cancel(); });

// ---------- controls ----------
$('say-form').addEventListener('submit', event => {
  event.preventDefault();
  const text = $('say-input').value.trim();
  if (!text) return;
  $('say-input').value = '';
  submit(text);
});
$('ack-button').addEventListener('click', acknowledge);
$('apr-request').addEventListener('click', () => submit('approve'));
$('apr-cancel').addEventListener('click', () => { const p = ui.state?.pending; if (p) { $('t-user').textContent = 'Cancel'; operation(() => request('/confirm', { approved: false, action_id: p.id }), 'Cancelling…'); } });
$('apr-code-form').addEventListener('submit', event => {
  event.preventDefault();
  const code = $('apr-code-input').value.trim();
  $('apr-code-input').value = '';
  if (code) submit(code);
});
$('demo-start').addEventListener('click', async () => {
  if (ui.busy) return;
  ui.busy = true; render();
  setText('judge-status', 'Starting an isolated checkout failure…');
  try {
    const data = await request('/api/demo/start', {});
    setText('judge-status', 'Demo incident started. Watch the device.');
    ui.busy = false;
    applyState(data.state || data);
  } catch (error) { setText('judge-status', error.message); }
  finally { ui.busy = false; render(); }
});
$('demo-reset').addEventListener('click', async () => {
  if (ui.busy) return;
  ui.busy = true; render();
  try {
    const data = await request('/api/demo/reset', {});
    clearTimeout(ui.recoveryTimer);
    Object.assign(ui, { brief: null, correlation: null, recovery: null, report: null, focus: null, waking: false, announcedId: null });
    store('oncall-alexa-announced', '');
    $('t-user').textContent = ''; $('t-alexa').textContent = '';
    setText('judge-status', 'Demo reset.');
    ui.busy = false;
    if (data && ['sandbox', 'live'].includes(data.mode)) applyState(data); else await refresh();
  } catch (error) {
    setText('judge-status', [404, 405].includes(error.status) ? 'Reset is not available on this server version.' : error.message);
  } finally { ui.busy = false; render(); }
});
$('connection-button').addEventListener('click', () => openSettings());
$('close-settings').addEventListener('click', () => $('settings-dialog').close());
$('settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  token = $('api-token').value.trim();
  store('oncall-token', token);
  ui.authBlocked = false;
  $('settings-dialog').close();
  setConnection('loading', 'Reconnecting…');
  await refresh({ force: true });
});
$('clear-token').addEventListener('click', () => { token = ''; store('oncall-token', ''); $('api-token').value = ''; $('api-token').focus(); });

render();
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 5000);
setInterval(() => {
  if (document.hidden) return;
  // Close the push-to-talk mic once the follow-up window ends, unless hands-free is on.
  if (voice.active && !$('hands-free').checked && !windowOpen() && voice.state === 'listening' && !ui.followUp) voice.end();
  render();
}, 1000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
