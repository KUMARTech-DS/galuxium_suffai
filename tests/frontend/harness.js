/* Test harness for the static web/ pages: no DOM library, just node:vm sandboxes. */
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WEB = path.join(__dirname, '..', '..', 'web');
const read = (file) => fs.readFileSync(path.join(WEB, file), 'utf8');

/** In-memory localStorage; `blocked` makes every call throw like Safari private mode. */
function fakeStorage(blocked = false) {
  const m = new Map();
  const guard = (fn) => (...a) => { if (blocked) throw new Error('SecurityError'); return fn(...a); };
  return {
    getItem: guard((k) => (m.has(k) ? m.get(k) : null)),
    setItem: guard((k, v) => { m.set(k, String(v)); }),
    removeItem: guard((k) => { m.delete(k); }),
    _map: m,
  };
}

/** Run a web/*.js file in a fresh sandbox whose globals are `globals`; returns the sandbox. */
function runScript(file, globals = {}) {
  const ctx = vm.createContext({ console, URL, AbortSignal, ...globals });
  ctx.window = ctx;
  vm.runInContext(read(file), ctx, { filename: file });
  return ctx;
}

/** Inline (non-src) <script> bodies of a page, with their starting line numbers. */
function inlineScripts(file) {
  const src = read(file);
  return [...src.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
    .map((m) => ({ code: m[1], line: src.slice(0, m.index).split('\n').length }));
}

/** Shortest source slice from `start` that ends at `stop` and parses on its own. */
function sliceDecl(code, start, stop) {
  for (let end = code.indexOf(stop, start); end !== -1; end = code.indexOf(stop, end + 1)) {
    const snippet = code.slice(start, end + 1);
    try { new vm.Script(snippet); return snippet; } catch (_) { /* not complete yet */ }
  }
  return null;
}

/**
 * Pull named top-level helpers (`function name(` or `const name =`) out of a page's inline
 * scripts and evaluate them together in one sandbox, so pure page logic is testable in place.
 */
function pageHelpers(file, names, globals = {}) {
  const code = inlineScripts(file).map((s) => s.code).join('\n');
  const parts = names.map((name) => {
    const fn = new RegExp(`(?:async\\s+)?function\\s+${name}\\s*\\(`).exec(code);
    const decl = fn ? sliceDecl(code, fn.index, '}') : null;
    const cm = decl ? null : new RegExp(`const\\s+${name}\\s*=`).exec(code);
    const out = decl || (cm && sliceDecl(code, cm.index, ';'));
    if (!out) throw new Error(`${file}: helper "${name}" not found`);
    return out.replace(/^const\s/, 'var '); // vm only exposes var/function on the context
  });
  const ctx = vm.createContext({ console, URL, ...globals });
  ctx.window = ctx;
  vm.runInContext(parts.join('\n'), ctx, { filename: file });
  return ctx;
}

const PAGES = fs.readdirSync(WEB).filter((f) => f.endsWith('.html'));

module.exports = { WEB, read, fakeStorage, runScript, inlineScripts, pageHelpers, PAGES };
