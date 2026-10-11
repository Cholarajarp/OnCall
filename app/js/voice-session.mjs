// Hands-free voice loop: listen → send → speak → listen again, until the user ends it.
// Safety: speech never authorizes a change. Bare assent ("yes", "go ahead") is held
// locally. "approve" only asks the server for an out-of-band one-time code, and a spoken
// code is checked server-side against the hashed, single-use code for the exact action.
// Any approval-flavoured language ("yes", "do not confirm", "yes cancel", "approve and roll
// back now") is ambiguous over speech, so it is held locally and never sent.
const ASSENT = /\b(yes|yeah|yep|confirm(ed)?|approve[sd]?|go ahead|do it)\b/i;
const APPROVE = /^(ok(ay)?\s+)?approve( it| the rollback| rollback)?\.?$/i;
const RETRY_LIMIT = 3; // consecutive silent restarts before pausing to save battery/privacy

export function holdsAssent(text) { return ASSENT.test(text.trim()); }

export class VoiceSession {
  constructor({ recognition, synthesis, makeUtterance, onState = () => {}, onCommand = () => {}, onDraft = () => {}, isCode = () => false, schedule = (fn, ms) => setTimeout(fn, ms) }) {
    Object.assign(this, { recognition, synthesis, makeUtterance, onState, onCommand, onDraft, isCode, schedule });
    this.active = false;
    this.state = recognition ? 'idle' : 'unsupported';
    this.epoch = 0;
    this.silent = 0;
    this.heard = false;
    if (!recognition) return;
    recognition.continuous = false; // one utterance per turn; the loop restarts listening
    recognition.interimResults = true;
    recognition.lang = 'en-US';
    recognition.onstart = () => { if (this.active && this.state === 'starting') this.set('listening'); };
    recognition.onresult = event => {
      if (!this.active || this.state !== 'listening') return;
      const results = Array.from(event.results).slice(event.resultIndex || 0);
      const final = results.filter(r => r.isFinal).map(r => r[0].transcript).join(' ').trim();
      if (!final) { this.heard = true; this.onDraft(results.map(r => r[0].transcript).join(' '), true); return; }
      this.silent = 0;
      this.heard = true;
      this.set('working');
      try { recognition.stop(); } catch {}
      const passthrough = APPROVE.test(final) || this.isCode(final);
      if (!passthrough && holdsAssent(final)) {
        this.onDraft(final, false);
        // Keep the conversation going; the hint is shown, nothing is sent.
        this.relisten();
        return;
      }
      this.onCommand(final);
    };
    recognition.onerror = event => {
      if (!this.active) return;
      // Transient: silence, network blip or our own abort. Keep the loop alive.
      if (['no-speech', 'aborted', 'network'].includes(event.error)) return;
      this.active = false;
      this.epoch++;
      this.set('error', event.error === 'not-allowed' ? 'permission denied' : event.error);
    };
    recognition.onend = () => {
      if (!this.active || !['listening', 'starting'].includes(this.state)) return;
      // Ended without a final result (silence timeout). Restart a few times, then pause.
      if (!this.heard) this.silent++;
      this.heard = false;
      if (this.silent >= RETRY_LIMIT) { this.silent = 0; this.set('paused'); return; }
      this.relisten();
    };
  }
  set(state, detail = '') { this.state = state; this.onState(state, detail); }
  start() {
    if (!this.recognition) { this.set('unsupported'); return; }
    this.active = true;
    this.silent = 0;
    this.listen();
  }
  listen() {
    if (!this.active) return;
    this.heard = false;
    this.set('starting');
    try { this.recognition.start(); } catch { this.relisten(250); }
  }
  relisten(delay = 120) {
    const epoch = this.epoch;
    this.schedule(() => { if (this.active && this.epoch === epoch && this.state !== 'speaking') this.listen(); }, delay);
  }
  reply(text, aloud = true) {
    if (!this.active) return;
    const epoch = ++this.epoch;
    try { this.recognition.abort(); } catch {}
    if (!aloud || !this.synthesis || !this.makeUtterance || !text) { this.relisten(); return; }
    this.set('speaking');
    this.synthesis.cancel();
    const utterance = this.makeUtterance(text);
    const next = () => { if (this.active && this.epoch === epoch) { this.set('working'); this.relisten(); } };
    utterance.onend = next;
    utterance.onerror = next;
    this.synthesis.speak(utterance);
  }
  interrupt() {
    if (!this.active) return;
    this.epoch++;
    this.synthesis?.cancel();
    try { this.recognition.abort(); } catch {}
    this.set('working');
    this.relisten();
  }
  end() {
    this.active = false;
    this.epoch++;
    this.synthesis?.cancel();
    try { this.recognition?.abort(); } catch {}
    this.set(this.recognition ? 'idle' : 'unsupported');
  }
}
