const test = require('node:test');
const assert = require('node:assert/strict');
const { fakeStorage, runScript } = require('./harness');

const SESSION = { token: 'tok-1', role: 'brand', username: 'acme', allowed_tabs: ['geo', 'ads', 'campaigns'] };

/** Load session.js with stub storage/location/fetch; `fetch` may be replaced per test. */
function load({ session, blocked = false, fetch, api = '' } = {}) {
  const localStorage = fakeStorage(blocked);
  if (session) localStorage._map.set('suffai_session', JSON.stringify(session));
  const location = { replaced: null, href: '', replace(u) { this.replaced = u; } };
  const calls = [];
  const ctx = runScript('session.js', {
    localStorage,
    location,
    DASHBOARD_API: api,
    fetch: async (url, opts) => { calls.push({ url, opts }); return fetch ? fetch(url, opts) : { ok: true, status: 200, json: async () => ({}) }; },
  });
  return { S: ctx.SuffaiSession, localStorage, location, calls };
}
const reply = (status, body = {}) => () => ({ ok: status >= 200 && status < 300, status, json: async () => body });

test('getSession: missing or corrupt storage yields null', () => {
  assert.equal(load().S.getSession(), null);
  const { S, localStorage } = load();
  localStorage._map.set('suffai_session', '{not json');
  assert.equal(S.getSession(), null);
});

test('setSession stores the session and the legacy flags', () => {
  const { S, localStorage } = load();
  S.setSession(SESSION);
  assert.equal(JSON.stringify(S.getSession()), JSON.stringify(SESSION)); // cross-realm: compare as JSON
  assert.equal(localStorage._map.get('suffai_demo_user'), 'acme');
  S.clearSession();
  assert.equal(S.getSession(), null);
  assert.equal(localStorage._map.size, 0);
});

test('setSession explains blocked storage; clearSession tolerates it', () => {
  const { S } = load({ blocked: true });
  assert.throws(() => S.setSession(SESSION), /blocking site storage/);
  assert.doesNotThrow(() => S.clearSession());
  assert.equal(S.getSession(), null);
});

test('authHeaders: empty when signed out, bearer + header token when signed in', () => {
  assert.deepEqual({ ...load().S.authHeaders() }, {});
  assert.deepEqual({ ...load({ session: SESSION }).S.authHeaders() }, { Authorization: 'Bearer tok-1', 'X-Session-Token': 'tok-1' });
});

test('requireAuth: signed out → login, wrong tab → shell, allowed tab → session', () => {
  let t = load();
  assert.equal(t.S.requireAuth('geo'), null);
  assert.equal(t.location.replaced, './login.html');

  t = load({ session: SESSION });
  assert.equal(t.S.requireAuth('admin'), null);
  assert.equal(t.location.replaced, './index.html');

  t = load({ session: SESSION });
  assert.equal(t.S.requireAuth('geo').username, 'acme');
  assert.equal(t.location.replaced, null);
  assert.equal(t.S.canAccessTab('style'), false);
});

test('fetchMe refreshes the session from /v1/auth/me (API base without trailing slash)', async () => {
  const me = { role: 'brand', username: 'acme', brand_name: 'Acme', allowed_tabs: ['geo'] };
  const { S, calls } = load({ session: SESSION, api: 'https://api.example/', fetch: reply(200, me) });
  const next = await S.fetchMe();
  assert.equal(calls[0].url, 'https://api.example/v1/auth/me');
  assert.equal(next.token, 'tok-1', 'keeps the existing token when the server omits it');
  assert.equal(next.display_name, 'acme');
  assert.deepEqual([...S.getSession().allowed_tabs], ['geo']);
});

test('fetchMe signs out only on 401/403', async () => {
  for (const status of [401, 403]) {
    const { S } = load({ session: SESSION, fetch: reply(status) });
    assert.equal(await S.fetchMe(), null);
    assert.equal(S.getSession(), null, `HTTP ${status} clears the session`);
  }
});

test('fetchMe keeps the session through a server outage', async () => {
  const { S } = load({ session: SESSION, fetch: reply(503) });
  await assert.rejects(S.fetchMe(), /HTTP 503/);
  assert.equal(S.getSession().token, 'tok-1');
});

test('fetchMe without a session makes no request', async () => {
  const { S, calls } = load();
  assert.equal(await S.fetchMe(), null);
  assert.equal(calls.length, 0);
});

test('logout clears the session and leaves even if the server is down', async () => {
  const { S, location } = load({ session: SESSION, fetch: () => { throw new Error('offline'); } });
  await S.logout();
  assert.equal(S.getSession(), null);
  assert.equal(location.href, './login.html');
});

test('formatApiDetail never shows [object Object]', () => {
  const { S } = load();
  assert.equal(S.formatApiDetail(null, 'Failed'), 'Failed');
  assert.equal(S.formatApiDetail('', 'Failed'), 'Failed');
  assert.equal(S.formatApiDetail('Bad PIN'), 'Bad PIN');
  assert.equal(S.formatApiDetail({ message: 'Quota hit', code: 7 }), 'Quota hit');
  assert.equal(
    S.formatApiDetail([{ loc: ['body', 'budget'], msg: 'must be > 0' }, { loc: ['query', 'pin'], msg: 'required' }]),
    'budget: must be > 0; pin: required',
  );
  assert.equal(S.formatApiDetail({ a: 1 }), '{"a":1}');
});
