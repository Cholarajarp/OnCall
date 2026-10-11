// Pure view logic for the simulated Alexa+ (Echo Show) surface. No DOM, no fetch.
// Every number shown comes from server data; missing data renders as "unknown".
import { chartPoints, viewState, approvalState } from './state.mjs';

export const WAKE_WINDOW_MS = 12000;
export const FOCUS_CARDS = ['incident', 'impact', 'suspect', 'runbook', 'history', 'recovery', 'handoff'];
// Same anchored "approve" pass-through that voice-session.mjs and app.js use.
export const APPROVE = /^(ok(ay)?\s+)?approve( it| the rollback| rollback)?\.?$/i;
// brain.route's catch-all reply when it does not recognise an utterance.
export const GENERIC_REPLY = /^I can diagnose the incident/;
const WAKE = /^\s*(?:(?:hey|ok(?:ay)?)[\s,]+)?alexa\b[\s,.!?:;-]*/i;

const finite = value => typeof value === 'number' && Number.isFinite(value);
const text = (value, limit = 240) => typeof value === 'string' ? value.replace(/\s+/g, ' ').trim().slice(0, limit) : '';
const capital = value => value ? value.charAt(0).toUpperCase() + value.slice(1) : value;

export function isActive(incident) {
  return Boolean(incident && !['resolved', 'closed'].includes(incident.status));
}

// "Alexa, roll back" -> accepted, stripped. Without the wake word only accepted
// while the follow-up window is open (right after Alexa spoke or the user tapped).
export function parseWake(utterance, { windowOpen = false } = {}) {
  if (typeof utterance !== 'string') return { accept: false, woke: false, text: '' };
  const match = utterance.match(WAKE);
  if (match) {
    const rest = utterance.slice(match[0].length).trim();
    return { accept: rest.length > 0, woke: true, text: rest };
  }
  const clean = utterance.trim();
  return { accept: windowOpen && clean.length > 0, woke: false, text: clean };
}

// Returns the incident id to announce, or null. Only open, unacknowledged, not yet announced.
export function wakeIncident(state, announcedId) {
  const incident = state?.incident;
  if (!isActive(incident) || !incident.id || incident.acknowledged_at) return null;
  return incident.id === announcedId ? null : incident.id;
}

export function selectScreen({ state, focus = null, recovery = null } = {}) {
  if (!state) return 'offline';
  if (state.pending?.id) return 'approval';
  if (recovery?.status === 'watching') return 'recovery';
  if (focus === 'recovery' && recovery) return 'recovery';
  if (focus === 'handoff') return 'handoff';
  if (focus && FOCUS_CARDS.includes(focus) && focus !== 'recovery' && state.incident) return focus;
  return isActive(state.incident) ? 'incident' : 'idle';
}

export function lightBar({ waking = false, speaking = false, busy = false, voice = 'idle', windowOpen = false } = {}) {
  if (waking) return 'alert';
  if (speaking || voice === 'speaking') return 'speaking';
  if (busy || voice === 'working') return 'thinking';
  if (['listening', 'starting'].includes(voice) && windowOpen) return 'listening';
  return 'off';
}

export function cardForUtterance(utterance) {
  const t = String(utterance || '').toLowerCase();
  if (/\broll ?back\b|\brevert\b|\bapprove\b|\bcancel\b/.test(t)) return null;
  if (/\b(hand ?off|report|wrap up|summary)\b/.test(t)) return 'handoff';
  if (/\b(recover(y|ed)?|verify|fixed|healthy again|back to normal)\b/.test(t)) return 'recovery';
  if (/\b(runbook|playbook|what should (i|we) do|next steps?)\b/.test(t)) return 'runbook';
  if (/\b(before|history|similar|last time|previous(ly)?|seen this)\b/.test(t)) return 'history';
  if (/how bad|\b(impact|blast|customers?|revenue|affected|users?)\b/.test(t)) return 'impact';
  if (/what changed|\b(suspect|culprit|which (deploy|release)|cause|diagnose|why)\b/.test(t)) return 'suspect';
  if (/on fire|\b(status|incident|health|brief|wrong|happening)\b/.test(t)) return 'incident';
  return null;
}

export function percent(rate) {
  return finite(rate) && rate >= 0 && rate <= 1 ? `${(rate * 100).toLocaleString('en-US', { maximumFractionDigits: 1 })}%` : 'unknown';
}
export function count(value) {
  return finite(value) && value >= 0 ? Math.round(value).toLocaleString('en-US') : 'unknown';
}
export function money(value, currency) {
  if (!finite(value) || value < 0) return 'unknown';
  try {
    if (typeof currency === 'string' && /^[A-Z]{3}$/.test(currency)) return new Intl.NumberFormat('en-US', { style: 'currency', currency, maximumFractionDigits: 0 }).format(value);
  } catch {}
  return Math.round(value).toLocaleString('en-US');
}
export function minutesAgo(at, now = Date.now()) {
  const stamp = Date.parse(at);
  if (!Number.isFinite(stamp)) return null;
  return Math.max(0, Math.floor((now - stamp) / 60000));
}
export function agoLabel(at, now = Date.now()) {
  const minutes = minutesAgo(at, now);
  if (minutes === null) return 'time unknown';
  return minutes === 0 ? 'just now' : minutes === 1 ? '1 minute ago' : `${minutes} minutes ago`;
}
export function clockLabel(at) {
  const stamp = typeof at === 'number' ? at : Date.parse(at);
  return Number.isFinite(stamp) ? new Date(stamp).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }) : 'unknown';
}

// Sparkline geometry for observed points only. null when there is nothing to draw.
export function sparkline(series, width = 240, height = 64, pad = 4) {
  const points = chartPoints(series);
  if (!points.length) return null;
  const peak = Math.max(...points.map(p => p.rate));
  const top = Math.max(0.1, peak) * 1.1;
  const start = points[0].at, end = points.at(-1).at;
  const x = at => end === start ? width / 2 : pad + (at - start) / (end - start) * (width - pad * 2);
  const y = rate => height - pad - rate / top * (height - pad * 2);
  const coords = points.map(p => [Math.round(x(p.at) * 10) / 10, Math.round(y(p.rate) * 10) / 10]);
  return { points: coords.map(c => c.join(',')).join(' '), last: coords.at(-1), count: points.length, peak, current: points.at(-1).rate };
}

export function ring(fraction, radius = 54) {
  const circumference = 2 * Math.PI * radius;
  const f = finite(fraction) ? Math.min(1, Math.max(0, fraction)) : 0;
  return { radius, circumference, offset: circumference * (1 - f), fraction: f };
}

function latestDeploy(state) {
  const deploys = Array.isArray(state?.deploys) ? state.deploys : [];
  return deploys.filter(d => d?.active).at(-1) || deploys.at(-1) || null;
}

export function healthLabel(health) {
  return { healthy: 'Healthy', degraded: 'Degraded', unknown: 'Unknown' }[health] || 'Unknown';
}

export function incidentCard(state = {}, brief = null, now = Date.now()) {
  const view = viewState(state, now);
  const incident = state.incident || null;
  const deploy = brief?.latest_deploy || latestDeploy(state);
  const service = text(state.service, 40) || 'service';
  return {
    service, serviceLabel: capital(service),
    title: text(incident?.title || brief?.title, 120) || `${capital(service)} incident`,
    health: view.health, healthLabel: healthLabel(view.health), stale: view.stale,
    errorRate: percent(view.errorRate), samples: view.samples ? `${count(view.samples)} requests sampled` : 'No request samples',
    since: incident?.started_at || null, sinceClock: clockLabel(incident?.started_at),
    commit: text(deploy?.commit || deploy?.sha, 40) || 'unknown', deployedAt: deploy?.deployed_at || null,
    acknowledged: Boolean(incident?.acknowledged_at), status: text(incident?.status, 20) || 'unknown',
    spark: sparkline(state.series),
  };
}

export function impactCard(blast) {
  if (!blast || typeof blast !== 'object') return { available: false };
  const status = text(blast.status, 20) || 'unknown';
  return {
    available: true, status, unknown: status === 'unknown',
    minutes: finite(blast.minutes) ? `${Math.round(blast.minutes)} min` : 'unknown',
    failed: count(blast.failed_requests), total: count(blast.total_requests),
    errorRate: percent(blast.error_rate), trend: text(blast.trend, 20) || 'unknown',
    revenue: money(blast.est_revenue_at_risk, blast.currency),
    estimateNote: typeof blast.estimate === 'string' ? text(blast.estimate, 160) : '',
    summary: text(blast.summary, 280),
  };
}

export function suspectCard(correlation, state = {}, now = Date.now()) {
  const candidate = correlation?.candidate;
  const deploy = candidate || latestDeploy(state);
  const lead = correlation?.lead_minutes;
  return {
    fromDiagnosis: Boolean(candidate),
    commit: text(deploy?.commit || deploy?.sha, 40) || 'unknown',
    deployedAt: deploy?.deployed_at || null, deployedClock: clockLabel(deploy?.deployed_at), deployedAgo: agoLabel(deploy?.deployed_at, now),
    lead: finite(lead) ? (lead >= 0 ? `${lead} min before errors rose` : 'Same measurement bucket as the spike') : 'Lead time unknown',
    summary: text(correlation?.summary, 280) || (candidate ? '' : 'Ask me to diagnose to correlate this release with the errors.'),
    caveat: 'Timing fits are evidence, not proof of cause.',
  };
}

export function runbookCard(runbook) {
  if (!runbook || typeof runbook !== 'object') return { available: false };
  const steps = (Array.isArray(runbook.steps) ? runbook.steps : [])
    .map(step => text(typeof step === 'string' ? step : step?.text || step?.title, 200)).filter(Boolean).slice(0, 6);
  return { available: true, title: text(runbook.title, 120) || 'Runbook', steps, source: text(runbook.source, 160) };
}

export function historyCard(similar) {
  const items = (Array.isArray(similar) ? similar : []).filter(item => item && typeof item === 'object').slice(0, 3).map(item => ({
    title: text(item.title, 120) || 'Untitled incident',
    resolved: Number.isFinite(Date.parse(item.resolved_at)) ? new Date(item.resolved_at).toLocaleDateString([], { month: 'short', day: 'numeric' }) : 'date unknown',
    recovered: finite(item.minutes_to_recover) ? `${Math.round(item.minutes_to_recover)} min to recover` : 'recovery time unknown',
    fix: text(item.fix, 160),
  }));
  return { available: Array.isArray(similar), items };
}

export function approvalCard(pending, challenge, now = Date.now(), totalSeconds = 120, codeSeconds = 90) {
  const a = approvalState(pending, now);
  const id = typeof pending?.id === 'string' ? pending.id : '';
  const live = Boolean(challenge && challenge.action_id === id && challenge.status === 'pending' && Date.parse(challenge.expires_at) > now);
  const codeLeft = live ? Math.max(0, Math.round((Date.parse(challenge.expires_at) - now) / 1000)) : 0;
  const status = challenge && challenge.action_id === id ? challenge.status : null;
  return {
    ...a, id, shortId: id ? `${id.slice(0, 8)}…` : 'missing',
    target: pending?.service && pending?.commit ? `${pending.service}@${pending.commit}` : 'Incomplete target',
    service: text(pending?.service, 40), commit: text(pending?.commit, 40),
    codeLive: live, codeLeft, attemptsLeft: live && finite(challenge.attempts_left) ? challenge.attempts_left : null,
    delivery: live ? challenge.delivery : null,
    prompt: a.expired ? 'This approval expired. Cancel and ask for a fresh review.'
      : live ? 'Read the 6-digit code from your pager'
      : status === 'locked' ? 'Voice approval is locked for this action. Cancel and review again.'
      : 'Say “approve” and I’ll send a one-time code to your pager',
    // While a code is live, the countdown tracks the code (what Alexa just said), not the outer approval.
    countdown: live ? codeLeft : a.remaining,
    ring: ring(live ? codeLeft / codeSeconds : a.remaining / totalSeconds),
  };
}

// fetchedAt lets the ring advance smoothly between 3-second polls.
export function recoveryCard(recovery, now = Date.now()) {
  const status = ['watching', 'complete', 'idle', 'unknown'].includes(recovery?.status) ? recovery.status : 'unknown';
  const health = ['healthy', 'degraded', 'unknown'].includes(recovery?.health) ? recovery.health : 'unknown';
  const drift = status === 'watching' && finite(recovery?.fetchedAt) ? Math.max(0, (now - recovery.fetchedAt) / 1000) : 0;
  const total = finite(recovery?.elapsed_seconds) && finite(recovery?.remaining_seconds) ? recovery.elapsed_seconds + recovery.remaining_seconds : null;
  const elapsed = finite(recovery?.elapsed_seconds) ? Math.min(total ?? Infinity, recovery.elapsed_seconds + drift) : null;
  const remaining = total !== null && elapsed !== null ? Math.max(0, Math.ceil(total - elapsed)) : null;
  const verdict = status === 'complete' ? (health === 'healthy' ? 'verified' : 'not_verified') : status;
  const fraction = status === 'complete' ? 1 : total ? elapsed / total : 0;
  return {
    status, health, verdict, remaining, indeterminate: status === 'watching' && total === null,
    // Unknown health means no fresh evidence; a count there would contradict the detail line.
    samples: finite(recovery?.sample_count) && recovery.sample_count > 0 && health !== 'unknown' ? `${count(recovery.sample_count)} fresh requests` : null,
    headline: { watching: 'Watching fresh traffic', verified: 'Recovery verified', not_verified: 'Recovery not verified', idle: 'Nothing to verify', unknown: 'Recovery unknown' }[verdict],
    detail: verdict === 'watching' ? (remaining === null ? 'Waiting for the watch window to finish' : `${remaining}s left in the watch window`)
      : verdict === 'verified' ? 'Every fresh batch passed. Keep monitoring.'
      : verdict === 'not_verified' ? (health === 'unknown' ? 'Fresh evidence is unavailable, so health is unknown.' : 'Fresh requests still show failures.')
      : verdict === 'idle' ? 'There is no incident waiting on recovery.' : 'The check did not complete. Health is unknown.',
    ring: ring(fraction),
  };
}

// Markdown -> plain blocks. Rendered with textContent only, never as HTML.
export function markdownBlocks(markdown, limit = 200) {
  if (typeof markdown !== 'string') return [];
  const inline = line => line.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1').replace(/(\*\*|__|`)/g, '').trim();
  const blocks = [];
  for (const raw of markdown.split(/\r?\n/)) {
    const line = raw.trimEnd();
    if (!line.trim()) continue;
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    const item = line.match(/^\s*[-*+]\s+(.*)$/);
    if (heading) blocks.push({ type: 'heading', level: heading[1].length, text: inline(heading[2]) });
    else if (item) blocks.push({ type: 'item', text: inline(item[1]) });
    else blocks.push({ type: 'text', text: inline(line) });
    if (blocks.length >= limit) break;
  }
  return blocks;
}

export function handoffCard(markdown) {
  const blocks = markdownBlocks(markdown);
  if (!blocks.length) return { available: false };
  const meta = {};
  let section = '';
  const sections = {};
  for (const block of blocks) {
    if (block.type === 'heading') { if (block.level > 1) section = block.text.toLowerCase(); continue; }
    if (block.type === 'text' && !section) {
      const pair = block.text.match(/^([A-Za-z ]{2,30}):\s*(.*)$/);
      if (pair) meta[pair[1].toLowerCase()] = pair[2].slice(0, 160);
    }
    if (block.type === 'item' && section) (sections[section] ||= []).push(block.text.replace(/^\S+\s+—\s+/, '').slice(0, 160));
  }
  return {
    available: true, title: meta.incident || 'No incident recorded', status: meta.status || 'unknown',
    acknowledge: meta['time to acknowledge'] || 'not recorded', resolve: meta['time to resolve'] || 'not recorded',
    chronology: (sections.chronology || []).slice(-5), blocks,
  };
}

export function handoffSpeech(card) {
  if (!card?.available) return 'There is no handoff report yet.';
  const timing = [card.acknowledge !== 'not recorded' ? `acknowledged in ${card.acknowledge}` : '', card.resolve !== 'not recorded' ? `resolved in ${card.resolve}` : ''].filter(Boolean).join(', ');
  return `Here's the handoff. ${card.title}. Status: ${card.status}.${timing ? ` It was ${timing}.` : ''} The timeline is on screen and in the workspace.`;
}

export function impactSpeech(card) {
  if (!card.available) return "I don't have impact numbers from the server yet. The incident card shows what is measured.";
  if (card.summary) return card.summary;
  if (card.unknown) return 'Impact is unknown right now; there is not enough fresh telemetry.';
  return `${card.failed} of ${card.total} requests failed over ${card.minutes}. Revenue at risk is an estimate: ${card.revenue}.`;
}
export function runbookSpeech(card) {
  if (!card.available) return "I couldn't find a runbook for this service on the server.";
  if (!card.steps.length) return `${card.title} has no steps listed.`;
  return `${card.title}. Step one: ${card.steps[0]}${card.steps[1] ? ` Then: ${card.steps[1]}` : ''}`;
}
export function historySpeech(card) {
  if (!card.available) return "I don't have incident history from the server yet.";
  if (!card.items.length) return "I haven't seen anything similar before.";
  const top = card.items[0];
  return `This looks like ${top.title}, ${top.recovered}.${top.fix ? ` The fix was: ${top.fix}` : ''}`;
}
export function suspectSpeech(card) {
  if (card.commit === 'unknown') return 'I have no deployment record to point at.';
  return card.summary || `The latest release is ${card.commit}, deployed ${card.deployedAgo}. ${card.caveat}`;
}
export function proposalSpeech(pending) {
  return `I can roll back ${pending.service} from release ${pending.commit}. Say "approve" and I'll send a one-time code to your pager, or say "cancel".`;
}

export function announcement(state = {}, briefSpeech = '', now = Date.now()) {
  const card = incidentCard(state, null, now);
  if (text(briefSpeech, 400)) return `Heads up. ${text(briefSpeech, 400)}`;
  const rate = card.errorRate === 'unknown' ? `${card.serviceLabel} health is unknown` : `${card.serviceLabel} is failing ${card.errorRate} of requests`;
  const deploy = card.commit !== 'unknown' ? ` The latest release, ${card.commit}, shipped ${agoLabel(card.deployedAt, now)}.` : '';
  return `Heads up. ${rate}.${deploy} Want me to walk you through it?`;
}

export function idleCard(state = {}, now = Date.now()) {
  const view = viewState(state, now);
  const service = capital(text(state.service, 40) || 'service');
  const incident = state.incident;
  return {
    time: clockLabel(now), date: new Date(now).toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' }),
    health: view.health, line: view.health === 'unknown' ? `${service} health unknown` : `${service} ${view.health}`,
    note: incident?.status === 'resolved' ? `Last incident resolved ${clockLabel(incident.resolved_at)}` : 'No open incidents',
  };
}

export function chipsFor(screen, pending) {
  if (pending) return ['Approve', 'Cancel', 'How bad is it?'];
  if (screen === 'idle') return ["What's on fire?", 'Check recovery', 'Give me the handoff'];
  return ["What's on fire?", 'How bad is it?', 'What does the runbook say?', 'Roll back', 'Check recovery', 'Give me the handoff'];
}

// Pull structured evidence out of /command tool steps.
export function evidenceFromSteps(steps) {
  const out = { brief: null, briefSpeech: '', correlation: null, rolledBack: false, codeSent: false };
  for (const step of Array.isArray(steps) ? steps : []) {
    if (!step || step.is_error || !step.result || typeof step.result !== 'object') continue;
    const r = step.result;
    if (step.tool === 'incident_briefing' && r.card) { out.brief = r.card; out.briefSpeech = text(r.speech, 400); }
    if (r.correlation && typeof r.correlation === 'object') out.correlation = r.correlation;
    if (['execute_action', 'approve_with_voice_code'].includes(step.tool) && r.status === 'rolled_back') out.rolledBack = true;
    if (step.tool === 'request_voice_approval' && r.status === 'code_sent') out.codeSent = true;
  }
  return out;
}

// Masks numerals and spoken digit words ("zero seven…") so a one-time code never shows on screen.
const SPOKEN_DIGIT = /\b(zero|oh|o|one|two|to|too|three|four|for|five|six|seven|eight|ate|nine)\b/gi;
export function maskDigits(value) {
  return String(value).replace(/\d/g, '•').replace(SPOKEN_DIGIT, '•');
}
