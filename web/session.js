/** Shared Command Center session helpers (role-based ACL). */
(function (global) {
  const KEY = 'suffai_session';

  function apiBase() {
    return (global.DASHBOARD_API || '').replace(/\/$/, '');
  }

  function getSession() {
    try {
      const raw = localStorage.getItem(KEY);
      if (!raw) return null;
      return JSON.parse(raw);
    } catch (_) {
      return null;
    }
  }

  function setSession(sess) {
    try {
      localStorage.setItem(KEY, JSON.stringify(sess || {}));
      // Back-compat flags used by older checks
      localStorage.setItem('suffai_demo_session', '1');
      localStorage.setItem('suffai_demo_user', (sess && sess.username) || '');
    } catch (_) {
      throw new Error('This browser is blocking site storage, so you can’t stay signed in. Allow storage (or leave private mode) and try again.');
    }
  }

  function clearSession() {
    try {
      localStorage.removeItem(KEY);
      localStorage.removeItem('suffai_demo_session');
      localStorage.removeItem('suffai_demo_user');
    } catch (_) { /* storage unavailable: nothing to clear */ }
  }

  function authHeaders() {
    const s = getSession();
    if (!s || !s.token) return {};
    return {
      Authorization: 'Bearer ' + s.token,
      'X-Session-Token': s.token,
    };
  }

  function allowedTabs() {
    const s = getSession();
    return (s && Array.isArray(s.allowed_tabs)) ? s.allowed_tabs : [];
  }

  function canAccessTab(tab) {
    return allowedTabs().indexOf(tab) >= 0;
  }

  /** Redirect to login if no session. Optionally require a tab id. */
  function requireAuth(tab) {
    const s = getSession();
    if (!s || !s.token) {
      location.replace('./login.html');
      return null;
    }
    if (tab && !canAccessTab(tab)) {
      // Send to shell; shell will open first allowed tab
      location.replace('./index.html');
      return null;
    }
    return s;
  }

  async function fetchMe() {
    const s = getSession();
    if (!s || !s.token) return null;
    const res = await fetch(apiBase() + '/v1/auth/me', {
      headers: { Accept: 'application/json', ...authHeaders() },
    });
    // Only a rejected session signs the user out; a server outage (5xx) must not.
    if (res.status === 401 || res.status === 403) {
      clearSession();
      return null;
    }
    if (!res.ok) throw new Error('Session check failed (HTTP ' + res.status + ')');
    const data = await res.json();
    const next = {
      token: data.token || s.token,
      role: data.role,
      username: data.username,
      brand_name: data.brand_name || null,
      customer_id: data.customer_id || null,
      location_code: data.location_code || null,
      display_name: data.display_name || data.username,
      allowed_tabs: data.allowed_tabs || [],
    };
    setSession(next);
    return next;
  }

  async function logout() {
    try {
      await fetch(apiBase() + '/v1/auth/logout', {
        method: 'POST',
        headers: { ...authHeaders() },
        // a hung server must not trap the user on a page they asked to leave
        signal: AbortSignal.timeout ? AbortSignal.timeout(5000) : undefined,
      });
    } catch (_) { /* best effort: the local session is cleared regardless */ }
    clearSession();
    location.href = './login.html';
  }

  /** FastAPI often returns detail as string | object | list — never show [object Object]. */
  function formatApiDetail(detail, fallback) {
    if (detail == null || detail === '') return fallback || '';
    if (typeof detail === 'string') return detail;
    // FastAPI 422: [{loc: [..., 'field'], msg: '...'}] → "field: msg"
    if (Array.isArray(detail) && detail.length && detail.every(d => d && typeof d.msg === 'string')) {
      return detail.map(d => {
        const field = Array.isArray(d.loc) ? d.loc.filter(p => p !== 'body' && p !== 'query').join('.') : '';
        return (field ? field + ': ' : '') + d.msg;
      }).join('; ');
    }
    if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message;
    try {
      return JSON.stringify(detail);
    } catch (_) {
      return String(detail);
    }
  }

  global.SuffaiSession = {
    KEY,
    getSession,
    setSession,
    clearSession,
    authHeaders,
    allowedTabs,
    canAccessTab,
    requireAuth,
    fetchMe,
    logout,
    apiBase,
    formatApiDetail,
  };
})(window);
