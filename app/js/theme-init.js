// Apply nonsecret appearance preference before first paint. Astryx receives the
// resolved mode from app.js; data-appearance keeps the native token layer in sync.
(() => {
  let preference = 'system';
  try { preference = localStorage.getItem('oncall-theme') || 'system'; } catch {}
  const dark = preference === 'dark' || (preference !== 'light' && matchMedia('(prefers-color-scheme: dark)').matches);
  document.documentElement.dataset.appearance = dark ? 'dark' : 'light';
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
})();
