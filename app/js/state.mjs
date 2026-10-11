// Pure projections of backend evidence. No generated measurements or optimistic health.
export function chartPoints(series = []) {
  return (Array.isArray(series) ? series : []).flatMap(point => {
    const at = Date.parse(point.ts ?? point.at ?? point.timestamp);
    const rate = point.error_rate;
    return Number.isFinite(at) && typeof rate === 'number' && Number.isFinite(rate) && rate >= 0 && rate <= 1 ? [{ at, rate }] : [];
  }).sort((a, b) => a.at - b.at);
}

export function viewState(state = {}, now = Date.now()) {
  const samples = Number.isFinite(state.sample_count) ? Math.max(0, state.sample_count) : 0;
  const rate = state.current_error_rate;
  const errorRate = samples > 0 && typeof rate === 'number' && Number.isFinite(rate) && rate >= 0 && rate <= 1 ? rate : null;
  const updated = Date.parse(state.last_updated);
  const stale = !Number.isFinite(updated) || now - updated > 60000;
  const health = errorRate !== null && !stale && ['healthy', 'degraded'].includes(state.health) ? state.health : 'unknown';
  const active = state.incident && !['resolved', 'closed'].includes(state.incident.status);
  return { samples, errorRate, stale, health, points: chartPoints(state.series), canStart: state.mode === 'sandbox' && !active && !state.pending };
}

export function approvalState(pending, now = Date.now()) {
  if (!pending) return { canApprove: false, canCancel: false, expired: false, remaining: 0 };
  const expiry = Date.parse(pending.expires_at);
  const valid = Boolean(pending.id && pending.type === 'rollback' && pending.service && pending.commit && Number.isFinite(expiry));
  const remaining = Number.isFinite(expiry) ? Math.max(0, Math.ceil((expiry - now) / 1000)) : 0;
  return { canApprove: valid && remaining > 0, canCancel: Boolean(pending.id), expired: Number.isFinite(expiry) && remaining === 0, remaining };
}

export function commandIntent(text) {
  const clean = text.trim().toLowerCase();
  if (/^(confirm|confirm rollback|approve rollback)$/.test(clean)) return 'approve';
  if (/^(cancel|cancel rollback|stop|no)$/.test(clean)) return 'cancel';
  // Ambiguous confirmation language is held locally, not sent to a loose intent router.
  if (/\b(confirm|approve|yes|do it|go ahead)\b/.test(clean)) return 'ambiguous';
  return 'command';
}

const DIGIT_WORDS = new Set(['zero', 'oh', 'o', 'one', 'two', 'to', 'too', 'three', 'four', 'for', 'five', 'six', 'seven', 'eight', 'ate', 'nine']);
const CODE_FILLER = new Set(['code', 'is', 'the', 'my', 'approve', 'approval', 'with', 'confirm', 'and', 'dash']);
// Mirrors oncall_core.spoken_code; the server remains the authority.
export function looksLikeCode(text) {
  if (typeof text !== 'string' || text.length > 200) return false;
  let digits = 0;
  for (const t of text.toLowerCase().match(/[a-z]+|\d/g) || []) {
    if (/\d/.test(t) || DIGIT_WORDS.has(t)) digits++;
    else if (!CODE_FILLER.has(t)) return false;
  }
  return digits === 6;
}

export function resolveTheme(preference, systemDark) {
  return preference === 'dark' || (preference !== 'light' && systemDark) ? 'dark' : 'light';
}

export function workflowProgress(steps = []) {
  const valid = steps.filter(step => !step.is_error);
  return {
    investigated: valid.some(step => /incident_briefing|diagnose_incident|list_deploys|get_error_rates|search_logs/.test(step.tool)),
    rolledBack: valid.some(step => step.tool === 'execute_action' && step.result?.status === 'rolled_back'),
    verified: valid.some(step => step.tool === 'verify_recovery' && step.result?.health === 'healthy'),
  };
}

// FastAPI serves the UI, so the API is always same-origin.
export function apiBase() {
  return '';
}
export function requestHeaders(session, token) {
  return { 'Content-Type': 'application/json', 'X-OnCall-Session': session, ...(token ? { Authorization: `Bearer ${token}` } : {}) };
}
