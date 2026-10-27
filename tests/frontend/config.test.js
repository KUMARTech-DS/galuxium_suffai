const test = require('node:test');
const assert = require('node:assert/strict');
const { fakeStorage, runScript } = require('./harness');

function apiFor({ hostname = 'suffai.run.app', port = '', stored, blocked = false } = {}) {
  const localStorage = fakeStorage(blocked);
  if (stored !== undefined) localStorage._map.set('DASHBOARD_API', stored);
  return runScript('config.js', { localStorage, location: { hostname, port } }).DASHBOARD_API;
}

test('production / Cloud Run uses same-origin APIs', () => {
  assert.equal(apiFor(), '');
  assert.equal(apiFor({ hostname: 'localhost', port: '8083' }), '');
});

test('local split UI on :8088 talks to the API on :8090', () => {
  assert.equal(apiFor({ hostname: '127.0.0.1', port: '8088' }), 'http://127.0.0.1:8090');
  assert.equal(apiFor({ hostname: 'localhost', port: 8088 }), 'http://127.0.0.1:8090');
});

test('a stored override wins, even an empty one', () => {
  assert.equal(apiFor({ hostname: 'localhost', port: '8088', stored: 'http://10.0.0.5:9000' }), 'http://10.0.0.5:9000');
  assert.equal(apiFor({ hostname: 'localhost', port: '8088', stored: '' }), '');
});

test('blocked storage falls back to the defaults instead of crashing', () => {
  assert.equal(apiFor({ hostname: 'localhost', port: '8088', blocked: true }), 'http://127.0.0.1:8090');
});
