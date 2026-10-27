// Same-origin when UI is served by command-center Cloud Run.
// Local split (UI :8088 + API :8090): leave default below, or:
//   localStorage.setItem('DASHBOARD_API', 'http://127.0.0.1:8090')
(function () {
  let stored = null;
  try { stored = localStorage.getItem('DASHBOARD_API'); } catch (_) { /* storage blocked: use the defaults below */ }
  if (stored !== null) {
    window.DASHBOARD_API = stored;
    return;
  }
  const host = window.location.hostname;
  if (host === 'localhost' || host === '127.0.0.1') {
    // python -m http.server on 8088 + uvicorn on 8090
    if (String(window.location.port) === '8088') {
      window.DASHBOARD_API = 'http://127.0.0.1:8090';
      return;
    }
  }
  // Prod / single Cloud Run: APIs on same host
  window.DASHBOARD_API = '';
})();
