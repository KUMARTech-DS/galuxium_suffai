/** Shared helpers for the retail tabs (Store POS, Inventory, Warehouse). Needs session.js first. */
(function (global) {
  const S = global.SuffaiSession;
  const BRAND_KEY = 'suffai_retail_brand';
  const R = { session: null, me: null };

  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  R.esc = function (v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/[&<>"']/g, (c) => ESC[c]);
  };

  const INR = new Intl.NumberFormat('en-IN', { style: 'currency', currency: 'INR', minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const NUM = new Intl.NumberFormat('en-IN');
  R.money = (paise) => INR.format((Number(paise) || 0) / 100);
  R.rupees = (r) => INR.format(Number(r) || 0);
  R.num = (n) => NUM.format(Number(n) || 0);
  R.count = (n, one, many) => R.num(n) + ' ' + (Number(n) === 1 ? one : (many || one + 's'));
  R.rate = (bp) => ((Number(bp) || 0) / 100).toString().replace(/\.0+$/, '') + '%';
  /** Rupee text typed by a user -> paise (null when not a valid amount). */
  R.toPaise = function (v) {
    const s = String(v === null || v === undefined ? '' : v).trim().replace(/,/g, '');
    if (!/^\d+(\.\d{1,2})?$/.test(s)) return null;
    const [w, f = ''] = s.split('.');
    return Number(w) * 100 + Number((f + '00').slice(0, 2));
  };

  R.todayIST = () => new Date(Date.now() + 330 * 60000).toISOString().slice(0, 10);
  R.addDays = (day, n) => {
    const d = new Date(day + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() + n);
    return d.toISOString().slice(0, 10);
  };
  R.day = function (s) {
    if (!s) return '';
    const d = new Date(String(s).slice(0, 10) + 'T12:00:00+05:30');
    if (isNaN(d)) return String(s);
    return d.toLocaleDateString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', year: 'numeric' });
  };
  R.dateTime = function (s) {
    if (!s) return '';
    const d = new Date(s);
    if (isNaN(d)) return String(s);
    return d.toLocaleString('en-IN', { timeZone: 'Asia/Kolkata', day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
  };
  R.uuid = function () {
    if (global.crypto && global.crypto.randomUUID) return global.crypto.randomUUID();
    return 'r-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 12);
  };
  R.debounce = function (fn, ms) {
    let t = null;
    const run = function (...a) { clearTimeout(t); t = setTimeout(() => fn.apply(this, a), ms); };
    run.cancel = () => clearTimeout(t);
    return run;
  };
  R.can = (perm) => !!(R.me && R.me.can && R.me.can[perm]);
  R.isStaff = () => !!(R.me && (R.me.role === 'warehouse_staff' || R.me.role === 'store_staff'));

  /* ---------- API ---------- */
  R.brand = function () {
    try { return localStorage.getItem(BRAND_KEY) || ''; } catch (_) { return ''; }
  };
  R.setBrand = function (b) {
    try { localStorage.setItem(BRAND_KEY, b || ''); } catch (_) { /* storage blocked: picker resets on reload */ }
  };
  R.api = async function (path, opts) {
    const o = opts || {};
    const url = new URL(S.apiBase() + '/v1/retail' + path, global.location.href);
    if (R.session && R.session.role === 'admin' && R.brand() && !url.searchParams.has('brand')) {
      url.searchParams.set('brand', R.brand());
    }
    Object.entries(o.params || {}).forEach(([k, v]) => {
      if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, String(v));
    });
    const init = { method: o.method || (o.body !== undefined ? 'POST' : 'GET'),
                   headers: { Accept: 'application/json', ...S.authHeaders() } };
    if (o.body !== undefined) {
      if (typeof FormData !== 'undefined' && o.body instanceof FormData) {
        init.body = o.body; // browser sets multipart boundary
      } else {
        init.headers['Content-Type'] = 'application/json';
        init.body = JSON.stringify(o.body);
      }
    }
    let res;
    try {
      res = await fetch(url.toString(), init);
    } catch (_) {
      throw Object.assign(new Error('Can’t reach the server. Check your connection and try again.'), { status: 0 });
    }
    if (res.status === 401) {
      S.clearSession();
      try { global.top.location.replace('./login.html'); } catch (_) { global.location.replace('./login.html'); }
      throw Object.assign(new Error('Your session ended. Please sign in again.'), { status: 401 });
    }
    let data = null;
    try { data = await res.json(); } catch (_) { data = null; }
    if (!res.ok) {
      const d = data && data.detail;
      const err = new Error(S.formatApiDetail(d, 'Request failed (HTTP ' + res.status + ')'));
      err.status = res.status;
      err.errors = (d && Array.isArray(d.errors)) ? d.errors : [];
      throw err;
    }
    return data;
  };

  /* ---------- UI bits ---------- */
  R.$ = (sel, root) => (root || document).querySelector(sel);
  R.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function toastBox() {
    let box = document.querySelector('.rt-toasts');
    if (!box) {
      box = document.createElement('div');
      box.className = 'rt-toasts';
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    return box;
  }
  /** action: optional {label, run} button inside the toast (kept up longer). */
  R.toast = function (msg, kind, action) {
    const el = document.createElement('div');
    el.className = 'rt-toast' + (kind ? ' ' + kind : '');
    const text = document.createElement('span');
    text.textContent = msg;
    el.appendChild(text);
    if (action && action.run) {
      const b = document.createElement('button');
      b.type = 'button';
      b.textContent = action.label || 'Open';
      b.addEventListener('click', () => { el.remove(); action.run(); });
      el.appendChild(b);
    }
    toastBox().appendChild(el);
    setTimeout(() => el.remove(), kind === 'bad' ? 7000 : (action ? 8000 : 3500));
  };
  R.fail = (e) => R.toast((e && e.message) || 'Something went wrong.', 'bad');

  /* ---------- Numbers + small visuals ---------- */
  /** 1,234 → 1,234 · 45,600 → 45.6K · 12,30,000 → 12.3L · 4,50,00,000 → 4.5Cr */
  R.compact = function (n) {
    const v = Number(n) || 0, a = Math.abs(v), sign = v < 0 ? '−' : '';
    const one = (x) => (Math.round(x * 10) / 10).toFixed(1).replace(/\.0$/, '');
    if (a >= 1e7) return sign + one(a / 1e7) + 'Cr';
    if (a >= 1e5) return sign + one(a / 1e5) + 'L';
    if (a >= 1e4) return sign + one(a / 1e3) + 'K';
    return sign + NUM.format(Math.round(a));
  };
  R.compactMoney = (paise) => { const r = (Number(paise) || 0) / 100; return (r < 0 ? '−' : '') + '₹' + R.compact(Math.abs(r)); };
  let uid = 0;
  const nextId = (p) => p + (++uid) + Math.random().toString(36).slice(2, 6);

  R.spark = function (values) {
    const vals = (values || []).map((x) => Number(x) || 0);
    if (vals.length < 2) return '';
    const w = 160, h = 30, max = Math.max(...vals), min = Math.min(0, ...vals), span = (max - min) || 1;
    const pts = vals.map((v, i) => [(i / (vals.length - 1)) * w, h - 2 - ((v - min) / span) * (h - 4)]);
    const line = pts.map((p, i) => (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
    const id = nextId('sp');
    return '<svg class="spark" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" aria-hidden="true"><defs><linearGradient id="' + id +
      '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6B3A55" stop-opacity=".25"/><stop offset="1" stop-color="#6B3A55" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + line + ' L' + w + ' ' + h + ' L0 ' + h + ' Z" fill="url(#' + id + ')"/><path d="' + line +
      '" fill="none" stroke="#6B3A55" stroke-width="1.6" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/></svg>';
  };

  /** KPI tile. k: {icon, label, value, sub, tone ok|warn|bad, spark (svg), go (section id), title} */
  R.kpi = (k) => '<div class="tile' + (k.tone ? ' ' + k.tone : '') + (k.go ? ' click' : '') + '"' +
    (k.go ? ' data-go="' + R.esc(k.go) + '" role="button" tabindex="0"' : '') + (k.title ? ' title="' + R.esc(k.title) + '"' : '') + '>' +
    (k.icon ? '<i class="ic fa-solid ' + R.esc(k.icon) + '" aria-hidden="true"></i>' : '') +
    '<span>' + R.esc(k.label) + '</span><b>' + R.esc(k.value) + '</b>' + (k.sub ? '<small>' + R.esc(k.sub) + '</small>' : '') + (k.spark || '') + '</div>';

  R.skeleton = (kind, n) => Array.from({ length: n || 1 }, () => '<div class="skel ' + (kind || 'line') + '"></div>').join('');
  /** e: {icon, title, text, ok, html (extra markup, e.g. a button)} */
  R.emptyState = (e) => '<div class="estate' + (e.ok ? ' ok' : '') + '"><div class="ei"><i class="fa-solid ' + R.esc(e.icon || 'fa-box-open') + '"></i></div>' +
    '<b>' + R.esc(e.title || '') + '</b>' + (e.text ? '<p>' + R.esc(e.text) + '</p>' : '') + (e.html || '') + '</div>';

  /** Score ring 0-100 (null = no data). o: {size, stroke, label, sub, color} */
  R.ring = function (pct, o) {
    const opt = o || {}, size = opt.size || 76, sw = opt.stroke || Math.round(size / 9.5), r = (size - sw) / 2, c = 2 * Math.PI * r, mid = size / 2;
    const val = pct === null || pct === undefined || isNaN(Number(pct)) ? null : Math.max(0, Math.min(100, Number(pct)));
    const color = opt.color || (val === null ? '#C9B1BE' : val >= 75 ? '#3F7D3B' : val >= 55 ? '#C9A24A' : '#C0503C');
    const fs = Math.round(size * 0.24);
    return '<svg class="ring" width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '" role="img" aria-label="' +
      R.esc((opt.label || 'Score') + ': ' + (val === null ? 'no data' : Math.round(val) + ' out of 100')) + '">' +
      '<circle class="track" cx="' + mid + '" cy="' + mid + '" r="' + r + '" fill="none" stroke-width="' + sw + '"/>' +
      (val === null ? '' : '<circle class="val" cx="' + mid + '" cy="' + mid + '" r="' + r + '" fill="none" stroke="' + color + '" stroke-width="' + sw +
        '" stroke-linecap="round" stroke-dasharray="' + c.toFixed(2) + '" stroke-dashoffset="' + (c * (1 - val / 100)).toFixed(2) + '" transform="rotate(-90 ' + mid + ' ' + mid + ')"/>') +
      '<text x="' + mid + '" y="' + (mid + (opt.sub ? fs * 0.2 : fs * 0.36)) + '" text-anchor="middle" style="font-size:' + fs + 'px">' + (val === null ? '—' : Math.round(val)) + '</text>' +
      (opt.sub ? '<text class="s" x="' + mid + '" y="' + (mid + fs * 0.95) + '" text-anchor="middle">' + R.esc(opt.sub) + '</text>' : '') + '</svg>';
  };

  const COLORS = { black: '#151515', white: '#F7F5EF', 'off white': '#F2EDE2', offwhite: '#F2EDE2', ivory: '#F4EFDF', cream: '#F1E7CF', beige: '#D8C3A0',
    khaki: '#B8A57A', tan: '#C29A6B', camel: '#B88654', brown: '#6E4A2F', grey: '#8E8E8E', gray: '#8E8E8E', charcoal: '#3C3C3C', silver: '#C7C9CC',
    navy: '#1F2A44', blue: '#2F5DA8', denim: '#3B5878', indigo: '#33407A', 'light blue': '#9CC3E6', 'sky blue': '#86BCE8', teal: '#1F7A7A',
    turquoise: '#3BB6B0', green: '#2F7A45', olive: '#6B6B2E', mint: '#A8DCC0', yellow: '#E8C547', mustard: '#C99A2E', gold: '#C9A24A',
    orange: '#E07B32', rust: '#A8471F', red: '#C0322B', maroon: '#6B1E2A', wine: '#5A1A2E', burgundy: '#6A1B2D', pink: '#E58FAE',
    peach: '#F2B58F', coral: '#EE7B62', magenta: '#C2307A', purple: '#6A3D8F', lavender: '#B7A3D9', lilac: '#C8A2C8', violet: '#7A4BA8' };
  const MULTI = /^(multi|multicolou?r|printed|print|assorted)/;
  R.swatch = function (name) {
    const k = String(name || '').trim().toLowerCase();
    const hex = COLORS[k] || COLORS[k.replace(/[-_]/g, ' ')] || COLORS[k.split(/\s+/).pop()];
    const bg = hex || (MULTI.test(k) ? 'conic-gradient(#C0322B,#E8C547,#2F7A45,#2F5DA8,#6A3D8F,#C0322B)'
      : 'repeating-linear-gradient(45deg,#EAD9E0 0 3px,#fff 3px 6px)');
    return '<i class="swatch" style="background:' + bg + '" aria-hidden="true"></i>';
  };
  R.sizeChip = (s) => '<span class="size-chip">' + R.esc(s) + '</span>';

  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v))), m = v / p;
    return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p;
  }
  /** SVG bar chart with hover tooltips. points: [{label, value, tip}]; o: {height, axis(v), label, emptyTitle, emptyText} */
  R.barChart = function (el, points, o) {
    const opt = o || {}, pts = points || [];
    el.__chart = [points, o];
    if (!pts.length) { el.classList.remove('chart'); el.innerHTML = R.emptyState({ icon: 'fa-chart-column', title: opt.emptyTitle || 'No data yet', text: opt.emptyText || '' }); return; }
    const W = Math.max(280, el.clientWidth || 640), H = opt.height || 170, padL = 46, padR = 4, padB = 22, padT = 10;
    const vals = pts.map((p) => Number(p.value) || 0);
    const hi = Math.max(0, ...vals), lo = Math.min(0, ...vals);
    const step = niceMax(((hi - lo) || 1) / 4);
    const top = Math.max(step, Math.ceil(hi / step) * step), bottom = Math.floor(lo / step) * step;
    const span = top - bottom, iw = W - padL - padR, ih = H - padB - padT, bw = iw / pts.length;
    const y = (v) => padT + ih - ((v - bottom) / span) * ih;
    const fmt = opt.axis || R.compact;
    let grid = '';
    for (let v = bottom; v <= top + step / 2; v += step) {
      const yy = y(v).toFixed(1);
      grid += '<line x1="' + padL + '" x2="' + (W - padR) + '" y1="' + yy + '" y2="' + yy + '"/><text x="' + (padL - 8) + '" y="' + (Number(yy) + 3) + '" text-anchor="end">' + R.esc(fmt(v)) + '</text>';
    }
    const gid = nextId('bg');
    const bars = pts.map((p, i) => {
      const v = vals[i], w = Math.max(2, bw * 0.68), x = padL + i * bw + (bw - w) / 2;
      const y0 = y(Math.max(0, v)), y1 = y(Math.min(0, v)), hgt = v ? Math.max(2, y1 - y0) : 2;
      return '<rect class="b' + (v < 0 ? ' neg' : (v ? '' : ' zero')) + '" data-i="' + i + '" x="' + x.toFixed(1) + '" y="' + (v ? y0 : y(0) - 2).toFixed(1) +
        '" width="' + w.toFixed(1) + '" height="' + hgt.toFixed(1) + '" rx="' + Math.min(4, w / 3).toFixed(1) + '"' + (v >= 0 ? ' fill="url(#' + gid + ')"' : '') + '/>';
    }).join('');
    const hits = pts.map((p, i) => '<rect class="hit" data-i="' + i + '" x="' + (padL + i * bw).toFixed(1) + '" y="' + padT + '" width="' + bw.toFixed(1) + '" height="' + ih + '"/>').join('');
    const idx = [0, Math.floor((pts.length - 1) / 2), pts.length - 1].filter((v, i, a) => a.indexOf(v) === i);
    const xl = idx.map((i) => '<text x="' + (padL + i * bw + bw / 2).toFixed(1) + '" y="' + (H - 5) + '" text-anchor="' +
      (pts.length > 1 && i === 0 ? 'start' : pts.length > 1 && i === pts.length - 1 ? 'end' : 'middle') + '">' + R.esc(pts[i].label) + '</text>').join('');
    el.classList.add('chart');
    el.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" role="img" aria-label="' + R.esc(opt.label || 'Chart') + '"><defs><linearGradient id="' + gid +
      '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="#6B3A55"/><stop offset="1" stop-color="#A06A86"/></linearGradient></defs><g class="grid">' + grid + '</g>' +
      bars + '<g class="axis">' + xl + '</g>' + hits + '</svg><div class="chart-tip" hidden></div>';
    const tip = R.$('.chart-tip', el), svg = R.$('svg', el);
    const off = () => { tip.hidden = true; el.classList.remove('hovering'); R.$$('rect.b.on', el).forEach((r) => r.classList.remove('on')); };
    svg.addEventListener('mousemove', (e) => {
      const hit = e.target.closest('rect.hit');
      if (!hit) { off(); return; }
      const i = +hit.dataset.i, bar = R.$('rect.b[data-i="' + i + '"]', el), box = el.getBoundingClientRect(), bb = bar.getBoundingClientRect();
      R.$$('rect.b.on', el).forEach((r) => { if (r !== bar) r.classList.remove('on'); });
      bar.classList.add('on');
      el.classList.add('hovering');
      tip.innerHTML = pts[i].tip || (R.esc(pts[i].label) + ' · <b>' + R.esc(fmt(vals[i])) + '</b>');
      tip.hidden = false;
      tip.style.left = Math.min(box.width - 60, Math.max(60, bb.left - box.left + bb.width / 2)) + 'px';
      tip.style.top = (bb.top - box.top) + 'px';
    });
    svg.addEventListener('mouseleave', off);
    if (!el.__chartRO && global.ResizeObserver) {
      let lastW = W;
      el.__chartRO = new ResizeObserver(R.debounce(() => {
        const w = el.clientWidth;
        if (w && Math.abs(w - lastW) > 30 && el.__chart) { lastW = w; R.barChart(el, el.__chart[0], el.__chart[1]); }
      }, 120));
      el.__chartRO.observe(el);
    }
  };

  R.celebrate = function () {
    if (global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const box = document.createElement('div');
    box.className = 'celebrate';
    box.setAttribute('aria-hidden', 'true');
    const colors = ['#C9A24A', '#F3D58A', '#6B3A55', '#A06A86', '#3F7D3B'];
    for (let i = 0; i < 40; i++) {
      const p = document.createElement('i');
      p.style.left = (Math.random() * 100).toFixed(1) + '%';
      p.style.background = colors[i % colors.length];
      p.style.setProperty('--dx', (Math.random() * 160 - 80).toFixed(0) + 'px');
      p.style.setProperty('--r', (Math.random() * 720 - 360).toFixed(0) + 'deg');
      p.style.animationDelay = (Math.random() * 0.25).toFixed(2) + 's';
      box.appendChild(p);
    }
    document.body.appendChild(box);
    setTimeout(() => box.remove(), 1900);
  };

  /** Disable `btn` while `fn` runs; errors become a red toast. Resolves to fn's result or undefined. */
  R.busy = async function (btn, fn) {
    if (btn && btn.disabled) return undefined;
    const html = btn ? btn.innerHTML : '';
    if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span>' + html; }
    try {
      return await fn();
    } catch (e) {
      R.fail(e);
      return undefined;
    } finally {
      if (btn) { btn.disabled = false; btn.innerHTML = html; }
    }
  };

  /** Modal dialog. actions: [{label, kind, onClick(close) -> keep open when it returns false}] */
  R.modal = function ({ title, html, actions, wide, onClose }) {
    const wrap = document.createElement('div');
    wrap.className = 'rt-modal';
    wrap.innerHTML = '<div class="rt-dialog' + (wide ? ' wide' : '') + '" role="dialog" aria-modal="true">' +
      '<header><h3></h3><button type="button" aria-label="Close"><i class="fa-solid fa-xmark"></i></button></header>' +
      '<div class="body"></div><footer></footer></div>';
    wrap.querySelector('h3').textContent = title || '';
    wrap.querySelector('.body').innerHTML = html || '';
    const foot = wrap.querySelector('footer');
    const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey); if (onClose) onClose(); };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    (actions || [{ label: 'Close' }]).forEach((a) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'btn' + (a.kind ? ' ' + a.kind : '');
      b.innerHTML = a.label;
      b.addEventListener('click', async () => {
        if (!a.onClick) { close(); return; }
        if (b.disabled) return;
        const html = b.innerHTML;
        b.disabled = true;
        b.innerHTML = '<span class="spin"></span>' + html;
        try {
          if ((await a.onClick(close, wrap)) !== false) close();
        } catch (e) {
          R.fail(e); // keep the dialog open so the user can fix the input
        } finally {
          b.disabled = false;
          b.innerHTML = html;
        }
      });
      foot.appendChild(b);
    });
    if (!foot.children.length) foot.hidden = true;
    wrap.querySelector('header button').addEventListener('click', close);
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) close(); });
    document.addEventListener('keydown', onKey);
    document.body.appendChild(wrap);
    const first = wrap.querySelector('.body input, .body select, .body textarea');
    if (first) first.focus();
    return { el: wrap, close };
  };
  R.confirm = (title, text, label) => new Promise((resolve) => {
    let answered = false;
    R.modal({ title, html: '<p>' + R.esc(text) + '</p>', onClose: () => { if (!answered) resolve(false); },
              actions: [{ label: 'Cancel' }, { label: label || 'Confirm', kind: 'primary',
                          onClick: () => { answered = true; resolve(true); } }] });
  });

  /** Named form controls -> object (checkboxes -> booleans, empty strings kept). */
  R.formData = function (form) {
    const out = {};
    R.$$('[name]', form).forEach((el) => {
      if (el.type === 'checkbox') out[el.name] = el.checked;
      else if (el.type === 'radio') { if (el.checked) out[el.name] = el.value; }
      else out[el.name] = el.value.trim();
    });
    return out;
  };
  R.options = function (select, items, value, label, placeholder) {
    const keep = select.value;
    select.innerHTML = (placeholder !== undefined ? '<option value="">' + R.esc(placeholder) + '</option>' : '') +
      items.map((it) => '<option value="' + R.esc(value(it)) + '">' + R.esc(label(it)) + '</option>').join('');
    if (keep && items.some((it) => String(value(it)) === keep)) select.value = keep;
  };
  R.pager = function (el, data, onPage) {
    const pages = Math.max(1, Math.ceil((data.total || 0) / (data.size || 50)));
    el.innerHTML = data.total > data.size
      ? '<span>Page ' + data.page + ' of ' + pages + ' · ' + R.count(data.total, 'row') + '</span>' +
        '<button type="button" class="btn sm" data-p="prev"' + (data.page <= 1 ? ' disabled' : '') + '>Prev</button>' +
        '<button type="button" class="btn sm" data-p="next"' + (data.page >= pages ? ' disabled' : '') + '>Next</button>'
      : (data.total ? '<span>' + R.count(data.total, 'row') + '</span>' : '');
    R.$$('button', el).forEach((b) => b.addEventListener('click', () => onPage(data.page + (b.dataset.p === 'next' ? 1 : -1))));
  };
  R.csvDownload = function (name, header, rows) {
    const cell = (v) => {
      const s = v === null || v === undefined ? '' : String(v);
      return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    };
    const text = [header].concat(rows).map((r) => r.map(cell).join(',')).join('\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv' }));
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  R.readFile = (file) => new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result || ''));
    fr.onerror = () => reject(new Error('Could not read that file.'));
    fr.readAsText(file);
  });
  /** Per-row import errors from a 422 as a small table. */
  R.errorsTable = (errors) => !errors || !errors.length ? '' :
    '<div class="tbl-wrap" style="margin-top:10px;max-height:240px;overflow:auto"><table class="tbl"><thead><tr><th>Row</th><th>Problem</th></tr></thead><tbody>' +
    errors.map((e) => '<tr><td class="mono">' + R.esc(e.row) + '</td><td>' + R.esc(e.error) + '</td></tr>').join('') + '</tbody></table></div>';

  /** Sub-sections: nav buttons [data-sec] show <section data-sec>; remembered in the URL hash. */
  R.sections = function (nav, onShow) {
    const btns = R.$$('button[data-sec]', nav);
    const show = (id) => {
      const btn = btns.find((b) => b.dataset.sec === id && !b.hidden) || btns.find((b) => !b.hidden);
      if (!btn) return;
      btns.forEach((b) => { if (b === btn) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current'); });
      R.$$('section[data-sec]').forEach((s) => { s.hidden = s.dataset.sec !== btn.dataset.sec; });
      try { history.replaceState(null, '', '#' + btn.dataset.sec); } catch (_) { /* sandboxed */ }
      R.publishContext({ section: btn.dataset.sec });
      if (onShow) onShow(btn.dataset.sec);
    };
    btns.forEach((b) => b.addEventListener('click', () => show(b.dataset.sec)));
    const go = (e) => {
      const t = e.target.closest('[data-go]');
      if (!t || (e.type === 'keydown' && ((e.key !== 'Enter' && e.key !== ' ') || t.tagName === 'BUTTON'))) return;
      e.preventDefault();
      show(t.dataset.go);
      global.scrollTo({ top: 0, behavior: 'smooth' });
    };
    document.addEventListener('click', go);
    document.addEventListener('keydown', go);
    R.nav = { show, start: () => show((global.location.hash || '').slice(1)) };
    return R.nav;
  };
  /** Count bubble on a sub-nav button (0 removes it). */
  R.navCount = function (sec, n, tone) {
    const b = document.querySelector('.subnav button[data-sec="' + sec + '"]');
    if (!b) return;
    let c = b.querySelector('.n');
    if (!n) { if (c) c.remove(); return; }
    if (!c) { c = document.createElement('span'); b.appendChild(c); }
    c.className = 'n' + (tone ? ' ' + tone : '');
    c.textContent = n > 99 ? '99+' : String(n);
  };

  /** Barcode / SKU search box: scanning an exact barcode picks it at once; otherwise choose from the list. */
  R.skuPicker = function (input, { location, onPick }) {
    const box = document.createElement('div');
    box.className = 'picker-list';
    box.hidden = true;
    input.parentNode.classList.add('picker');
    input.parentNode.appendChild(box);
    input.setAttribute('autocomplete', 'off');
    let results = [], sel = -1, seq = 0, shownFor = '';
    const pick = (r) => { seq++; box.hidden = true; input.value = ''; results = []; onPick(r); input.focus(); };
    const render = () => {
      box.hidden = !results.length;
      box.innerHTML = results.map((r, i) =>
        '<button type="button" data-i="' + i + '" aria-selected="' + (i === sel) + '">' +
        '<div><b>' + R.esc(r.name) + '</b> <span class="meta mono">' + R.esc(r.sku) + '</span>' +
        '<div class="meta">' + R.esc(r.color) + ' · ' + R.esc(r.size) + ' · ' + R.esc(r.barcode) + (r.on_sale ? '' : ' · <b>inactive</b>') + '</div></div>' +
        '<div class="end">' + R.rupees(r.mrp) + (r.available !== undefined ? '<div class="meta">' + R.num(r.available) + ' in stock</div>' : '') + '</div></button>'
      ).join('');
      R.$$('button', box).forEach((b) => b.addEventListener('mousedown', (e) => { e.preventDefault(); pick(results[+b.dataset.i]); }));
    };
    // exactOnly is a scan: the box was already cleared so the next scan can start, and an exact
    // match is added even if newer scans are in flight.
    const search = async (q, exactOnly) => {
      const my = ++seq;
      const loc = typeof location === 'function' ? location() : location;
      try {
        const data = await R.api('/variants/search', { params: { q, location_id: loc || undefined, limit: 12 } });
        const found = data.results || [];
        if (exactOnly && found.length && found[0].exact) { onPick(found[0]); return; }
        if (exactOnly && !found.length) { R.toast('No SKU or barcode matches "' + q + '".', 'bad'); return; }
        if (my !== seq) return;
        if (exactOnly) { if (input.value.trim()) return; input.value = q; input.focus(); }
        results = found;
        sel = results.length ? 0 : -1;
        shownFor = q;
        render();
      } catch (e) { R.fail(e); }
    };
    const later = R.debounce((q) => {
      if (q !== input.value.trim()) return;
      if (q.length >= 2) search(q, false); else { results = []; render(); }
    }, 220);
    input.addEventListener('input', () => later(input.value.trim()));
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        if (!results.length) return;
        e.preventDefault();
        sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length;
        render();
      } else if (e.key === 'Enter') {
        e.preventDefault();
        const q = input.value.trim();
        if (!q) return;
        later.cancel();
        if (results.length && sel >= 0 && !box.hidden && shownFor === q) { pick(results[sel]); return; }
        input.value = ''; box.hidden = true; results = [];
        search(q, true);
      } else if (e.key === 'Escape') { box.hidden = true; }
    });
    input.addEventListener('blur', () => setTimeout(() => { box.hidden = true; }, 150));
  };

  /** SKU lines with per-line number inputs. cols: [{key, label, start, min, decimal}]; location() for stock hints. */
  R.lineEditor = function (root, cols, location) {
    root.innerHTML = '<div><input class="in" placeholder="Scan or search a SKU to add it" aria-label="Add SKU"></div>' +
      '<div class="tbl-wrap" style="margin-top:8px"><table class="tbl"><thead><tr><th>SKU</th>' +
      cols.map((c) => '<th class="r">' + c.label + '</th>').join('') + '<th></th></tr></thead><tbody></tbody></table>' +
      '<div class="empty">No SKUs yet.</div></div>';
    const items = [], body = R.$('tbody', root), empty = R.$('.empty', root);
    const render = () => {
      empty.hidden = items.length > 0;
      body.innerHTML = items.map((it, i) => '<tr data-i="' + i + '"><td><span class="mono">' + R.esc(it.sku) + '</span><div class="hint" style="margin:0">' +
        R.esc(it.name) + ' · ' + R.esc(it.color) + ' / ' + R.esc(it.size) +
        (it.available !== undefined ? ' · ' + R.num(it.available) + ' sellable here' : '') + '</div></td>' +
        cols.map((c) => '<td class="r"><input class="in sm" style="width:84px;text-align:right" data-k="' + c.key + '" value="' + R.esc(it[c.key]) + '"' +
          (c.decimal ? ' inputmode="decimal"' : ' type="number"' + (c.min !== undefined ? ' min="' + c.min + '"' : '')) + ' aria-label="' + R.esc(c.label) + '"></td>').join('') +
        '<td><button type="button" class="btn sm" data-rm aria-label="Remove"><i class="fa-solid fa-xmark"></i></button></td></tr>').join('');
    };
    body.addEventListener('input', (e) => {
      const tr = e.target.closest('tr');
      if (tr && e.target.dataset.k) items[+tr.dataset.i][e.target.dataset.k] = e.target.value;
    });
    body.addEventListener('click', (e) => {
      const b = e.target.closest('[data-rm]');
      if (b) { items.splice(+b.closest('tr').dataset.i, 1); render(); }
    });
    R.skuPicker(R.$('input', root), { location, onPick: (r) => {
      const ex = items.find((x) => x.variant_id === r.variant_id);
      const k = cols[0].key;
      if (ex) ex[k] = String((parseInt(ex[k], 10) || 0) + 1);
      else {
        const it = { ...r };
        cols.forEach((c) => { it[c.key] = typeof c.start === 'function' ? c.start(r) : (c.start !== undefined ? c.start : ''); });
        items.push(it);
      }
      render();
    } });
    render();
    return {
      lines: () => items.map((it) => { const o = { variant_id: it.variant_id }; cols.forEach((c) => { o[c.key] = it[c.key]; }); return o; }),
      clear: () => { items.length = 0; render(); },
      size: () => items.length,
    };
  };

  R.TRANSFER_STATUS = { draft: ['Draft', ''], in_transit: ['In transit', 'warn'], received: ['Received', 'ok'], cancelled: ['Cancelled', 'bad'] };
  R.statusBadge = (map, s) => { const x = map[s] || [s, '']; return '<span class="badge ' + x[1] + '">' + R.esc(x[0]) + '</span>'; };
  const SOURCES = { manual: 'Manual', replenishment: 'Replenishment', opening_plan: 'New store plan', aura: 'AURA Stock Pilot' };
  R.SOURCES = SOURCES;

  function transferDoc(tr, pick) {
    return '<div class="doc"><h2>' + (pick ? 'Pick list' : 'Stock transfer note') + '</h2><div class="dhead"><div><b>' + R.esc(R.me.brand_name) + '</b><br>From: ' +
      R.esc(tr.from_code) + ' · ' + R.esc(tr.from_name) + '<br>To: ' + R.esc(tr.to_code) + ' · ' + R.esc(tr.to_name) + '</div><div style="text-align:right">No: <b>' +
      R.esc(tr.transfer_number) + '</b><br>Created ' + R.dateTime(tr.created_at) + (tr.dispatched_at ? '<br>Dispatched ' + R.dateTime(tr.dispatched_at) + ' by ' + R.esc(tr.dispatched_by) : '') +
      '</div></div><table><thead><tr><th>#</th><th>SKU</th><th>Item</th><th>Colour / size</th>' + (pick ? '<th>Pick from bins</th><th class="r">Qty</th>' : '<th class="r">Sent</th><th class="r">Received</th>') +
      '</tr></thead><tbody>' + tr.lines.map((l) => '<tr><td>' + l.line_no + '</td><td>' + R.esc(l.sku) + '</td><td>' + R.esc(l.name) + '</td><td>' + R.esc(l.color) + ' / ' + R.esc(l.size) + '</td>' +
        (pick ? '<td>' + (l.pick || []).map((p) => R.esc(p.bin_code || 'Unassigned') + ' × ' + p.qty).join(', ') + '</td><td class="r">' + l.qty_requested + '</td>'
              : '<td class="r">' + l.qty_sent + '</td><td class="r">' + (tr.status === 'received' ? l.qty_received : '') + '</td>') + '</tr>').join('') +
      '</tbody></table>' + (tr.note ? '<div>Note: ' + R.esc(tr.note) + '</div>' : '') +
      '<div class="foot">Packed by: ________________ &nbsp; Received by: ________________ &nbsp; Date: ____________</div></div>';
  }

  /** Transfer detail with dispatch / receive / cancel for whoever may act on it. */
  R.openTransfer = async function (id, onChange) {
    let tr;
    try { tr = await R.api('/transfers/' + encodeURIComponent(id)); } catch (e) { R.fail(e); return; }
    const own = R.me.location ? R.me.location.location_id : null;
    const atFrom = !own || own === tr.from_location_id, atTo = !own || own === tr.to_location_id;
    const mode = tr.status === 'draft' && R.can('transfer.write') && atFrom ? 'dispatch'
      : tr.status === 'in_transit' && R.can('transfer.receive') && atTo ? 'receive' : '';
    const head = '<div class="tiles" style="margin-bottom:12px"><div class="tile"><span>From</span><b style="font-size:15px">' + R.esc(tr.from_code) + '</b><small>' + R.esc(tr.from_name) + '</small></div>' +
      '<div class="tile"><span>To</span><b style="font-size:15px">' + R.esc(tr.to_code) + '</b><small>' + R.esc(tr.to_name) + '</small></div>' +
      '<div class="tile"><span>Status</span><b style="font-size:15px">' + R.statusBadge(R.TRANSFER_STATUS, tr.status) + '</b><small>' + R.esc(SOURCES[tr.source] || tr.source) + ' · by ' + R.esc(tr.created_by) + '</small></div>' +
      '<div class="tile' + (tr.shortage ? ' bad' : '') + '"><span>Units</span><b style="font-size:15px">' + R.num(tr.units_requested) + ' → ' + R.num(tr.units_sent) + (tr.status === 'received' ? ' → ' + R.num(tr.units_received) : '') + '</b><small>' +
      (tr.shortage ? R.num(tr.shortage) + ' short on arrival' : 'requested → sent' + (tr.status === 'received' ? ' → received' : '')) + '</small></div></div>';
    const rows = tr.lines.map((l) => {
      const def = mode === 'dispatch' ? Math.min(l.qty_requested, l.available || 0) : l.qty_sent;
      return '<tr data-no="' + l.line_no + '"><td class="mono">' + R.esc(l.sku) + '</td><td>' + R.esc(l.name) + '<div class="hint" style="margin:0">' + R.esc(l.color) + ' / ' + R.esc(l.size) + '</div></td>' +
        '<td class="r">' + l.qty_requested + '</td><td class="r">' + (tr.status === 'draft' ? (l.available < l.qty_requested ? '<span class="badge warn">' + R.num(l.available) + ' available</span>' : R.num(l.available)) : l.qty_sent) + '</td>' +
        '<td class="r">' + (tr.status === 'received' ? l.qty_received : '') + '</td>' +
        (tr.status === 'draft' ? '<td class="mono">' + ((l.pick || []).map((p) => R.esc(p.bin_code || '—') + '×' + p.qty).join(' ') || '—') + '</td>' : '') +
        (mode ? '<td class="r"><input class="in sm qty" type="number" min="0" max="' + (mode === 'dispatch' ? l.qty_requested : l.qty_sent) + '" value="' + def + '" data-q aria-label="Quantity"></td>' : '') + '</tr>';
    }).join('');
    const html = head + (tr.note ? '<p class="hint" style="margin:0 0 10px">Note: ' + R.esc(tr.note) + '</p>' : '') +
      '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>SKU</th><th>Item</th><th class="r">Requested</th><th class="r">' + (tr.status === 'draft' ? 'Available' : 'Sent') + '</th><th class="r">Received</th>' +
      (tr.status === 'draft' ? '<th>Pick bins</th>' : '') + (mode ? '<th class="r">' + (mode === 'dispatch' ? 'Send' : 'Received now') + '</th>' : '') + '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
      (mode === 'dispatch' ? '<p class="hint">Units come out of the bins in pick order. Send less than requested if the warehouse is short.</p>' : '') +
      (mode === 'receive' ? '<p class="hint">Received units go to the unassigned bin at ' + R.esc(tr.to_code) + '. Anything not received is recorded as a shortage.</p>' : '');
    const qtys = (el) => R.$$('tr[data-no]', el).map((r) => ({ line_no: +r.dataset.no, qty: parseInt(R.$('[data-q]', r).value, 10) || 0 }));
    const after = (msg) => { R.toast(msg, 'ok'); if (onChange) onChange(); };
    const actions = [];
    if (tr.status === 'draft') actions.push({ label: '<i class="fa-solid fa-print"></i>Pick list', onClick: () => { R.print(transferDoc(tr, true)); return false; } });
    if (tr.status === 'in_transit' || tr.status === 'received') actions.push({ label: '<i class="fa-solid fa-print"></i>Transfer note', onClick: () => { R.print(transferDoc(tr, false)); return false; } });
    if (mode === 'dispatch') {
      actions.push({ label: 'Cancel transfer', kind: 'danger', onClick: async () => {
        await R.api('/transfers/' + encodeURIComponent(tr.transfer_id) + '/cancel', { body: {} });
        after(tr.transfer_number + ' cancelled.');
      } });
      actions.push({ label: '<i class="fa-solid fa-truck"></i>Dispatch', kind: 'primary', onClick: async (_, el) => {
        const r = await R.api('/transfers/' + encodeURIComponent(tr.transfer_id) + '/dispatch', { body: { lines: qtys(el) } });
        after(r.transfer_number + ': ' + R.count(r.units_sent, 'unit') + ' dispatched to ' + r.to_code + '.');
      } });
    }
    if (mode === 'receive') {
      actions.push({ label: '<i class="fa-solid fa-box-open"></i>Receive', kind: 'primary', onClick: async (_, el) => {
        const r = await R.api('/transfers/' + encodeURIComponent(tr.transfer_id) + '/receive', { body: { lines: qtys(el) } });
        after(r.transfer_number + ': ' + R.count(r.units_received, 'unit') + ' received' + (r.shortage ? ', ' + r.shortage + ' short.' : '.'));
      } });
    }
    actions.push({ label: 'Close' });
    R.modal({ title: 'Transfer ' + tr.transfer_number, wide: true, html, actions });
  };

  /* ---------- Command Center shell bridge (AURA) ---------- */
  const AURA_TABS = ['inventory', 'warehouse'];
  const ctx = { tab: '', section: '' };
  R.inShell = () => { try { return !!global.parent && global.parent !== global; } catch (_) { return false; } };
  const tell = (msg) => {
    if (!R.inShell()) return;
    try { global.parent.postMessage(msg, global.location.origin); } catch (_) { /* shell gone */ }
  };
  /** AURA answers brand and admin logins (staff can still use the Stock Pilot itself). */
  R.auraReady = () => R.inShell() && AURA_TABS.includes(ctx.tab) && !!R.session && (R.session.role === 'brand' || R.session.role === 'admin');
  R.publishContext = function (patch) {
    Object.assign(ctx, patch || {});
    if (!R.session) return;
    tell({ type: 'cc-context', source: 'retail', tab: ctx.tab, section: ctx.section,
           brand: R.session.role === 'admin' ? R.brand() : ((R.me && R.me.brand_name) || '') });
  };
  /** req: {message} for a question, or {event: 'stock_brief'|'stock_action', action_ref}; echo = what the chat shows as the user's turn. */
  R.askAura = function (req) {
    if (!R.auraReady()) { R.toast('AURA answers brand and admin logins inside the Command Center.'); return false; }
    R.publishContext();
    tell({ type: 'cc-ask-aura', message: String(req.message || '').slice(0, 2000), event: req.event || '',
           action_ref: req.action_ref || '', echo: String(req.echo || req.message || '').slice(0, 200) });
    return true;
  };
  /** Ask the shell to show another retail tab (and section). */
  const TAB_NAMES = { inventory: 'Inventory', warehouse: 'Warehouse', pos: 'Store POS' };
  R.canOpen = (tab) => tab === ctx.tab || !R.session || !Array.isArray(R.session.allowed_tabs) || R.session.allowed_tabs.includes(tab);
  R.openTab = (tab, section) => {
    if (tab === ctx.tab) { if (R.nav && section) R.nav.show(section); return; }
    if (!R.canOpen(tab)) { R.toast('Your login can’t open ' + (TAB_NAMES[tab] || tab) + '. Ask your brand admin to handle this one.'); return; }
    if (R.inShell()) tell({ type: 'cc-open-retail', tab, section: section || '' });
    else global.location.href = './' + tab + '.html' + (section ? '#' + section : '');
  };
  const cmdHandlers = {};
  const cmdQueue = [];
  let cmdLive = false;
  function runCommand(cmd) {
    const t = String(cmd.type || '');
    const fn = cmdHandlers[t];
    if (!fn) return;
    try { fn(cmd); } catch (e) { R.fail(e); }
    tell({ type: 'cc-agent-ack', source: ctx.tab, command: t, ok: true });
  }
  /** handlers: {type: fn(cmd)}. Commands that arrive before the page has booted wait for it. */
  R.onAgentCommand = (handlers) => Object.assign(cmdHandlers, handlers);
  global.addEventListener('message', (ev) => {
    if (!R.inShell() || ev.source !== global.parent) return; // only the Command Center shell may drive this page
    const data = ev.data;
    if (!data || typeof data !== 'object') return;
    if (data.type === 'cc-context-request') { R.publishContext(); return; }
    if (data.type !== 'cc-agent-command' || !data.command || typeof data.command !== 'object') return;
    if (cmdLive) runCommand(data.command); else cmdQueue.push(data.command);
  });
  R.onAgentCommand({
    show_section: (cmd) => { if (R.nav && cmd.section) R.nav.show(String(cmd.section)); },
  });

  /* ---------- ⌘K command palette ---------- */
  const extraCommands = [];
  /** items: [{group, icon ('fa-solid fa-…'), label, hint, perm, run}] */
  R.addCommands = (items) => { extraCommands.push(...items); };
  const SHELL_TABS = [['pos', 'Store POS', 'fa-cash-register'], ['inventory', 'Inventory', 'fa-boxes-stacked'], ['warehouse', 'Warehouse', 'fa-warehouse']];
  const ownText = (el) => Array.from(el.childNodes).filter((n) => n.nodeType === 3).map((n) => n.textContent).join('').trim();
  function paletteItems() {
    const items = [];
    R.$$('.subnav button[data-sec]').forEach((b) => {
      if (b.hidden) return;
      const ic = b.querySelector('i');
      items.push({ group: 'This page', icon: ic ? ic.className : 'fa-solid fa-arrow-right', label: ownText(b), run: () => b.click() });
    });
    extraCommands.forEach((c) => { if (!c.perm || R.can(c.perm)) items.push(c); });
    const allowed = (R.session && R.session.allowed_tabs) || [];
    if (R.inShell()) {
      SHELL_TABS.forEach(([t, name, ic]) => {
        if (t !== ctx.tab && allowed.includes(t)) items.push({ group: 'Go to', icon: 'fa-solid ' + ic, label: name, run: () => tell({ type: 'open-tab', tab: t }) });
      });
    }
    if (R.auraReady()) {
      items.push({ group: 'AURA', ai: true, icon: 'fa-solid fa-wand-magic-sparkles', label: 'Brief me on today’s stock moves',
                   run: () => R.askAura({ event: 'stock_brief', echo: 'Brief me on today’s stock moves' }) });
    }
    return items;
  }
  R.palette = function () {
    if (document.querySelector('.cmdk')) return;
    const aura = R.auraReady(), all = paletteItems();
    const wrap = document.createElement('div');
    wrap.className = 'cmdk';
    wrap.innerHTML = '<div class="box" role="dialog" aria-modal="true" aria-label="Jump to"><div class="q"><i class="fa-solid fa-magnifying-glass"></i>' +
      '<input placeholder="' + (aura ? 'Jump to a section, or ask AURA anything…' : 'Jump to a section…') + '" aria-label="Search" aria-controls="cmdkList"><kbd>Esc</kbd></div>' +
      '<div class="list" id="cmdkList" role="listbox"></div><div class="foot"><span><kbd>↑</kbd> <kbd>↓</kbd> move</span><span><kbd>↵</kbd> open</span>' +
      (aura ? '<span>Type a question and press ↵ to ask AURA</span>' : '') + '</div></div>';
    const input = R.$('input', wrap), list = R.$('.list', wrap);
    let shown = [], sel = 0;
    const close = () => { wrap.remove(); document.removeEventListener('keydown', onKey, true); };
    const run = (it) => { if (!it) return; close(); it.run(); };
    const render = () => {
      const text = input.value.trim(), q = text.toLowerCase();
      shown = all.filter((it) => !q || (it.label + ' ' + (it.hint || '') + ' ' + it.group).toLowerCase().includes(q));
      if (text && aura) shown.push({ group: 'AURA', ai: true, icon: 'fa-solid fa-comment-dots', label: 'Ask AURA: “' + text + '”', run: () => R.askAura({ message: text }) });
      sel = Math.max(0, Math.min(sel, shown.length - 1));
      let g = '';
      list.innerHTML = shown.length ? shown.map((it, i) => (it.group !== g ? '<div class="grp">' + R.esc(g = it.group) + '</div>' : '') +
        '<button type="button" class="it' + (it.ai ? ' ai' : '') + '" role="option" data-i="' + i + '" aria-selected="' + (i === sel) + '"><i class="ic ' + R.esc(it.icon) +
        '" aria-hidden="true"></i><span>' + R.esc(it.label) + '</span>' + (it.hint ? '<small>' + R.esc(it.hint) + '</small>' : '') + '</button>').join('')
        : '<div class="empty">Nothing matches.</div>';
      const on = R.$('[aria-selected="true"]', list);
      if (on) on.scrollIntoView({ block: 'nearest' });
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(); }
      else if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (shown.length) { sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % shown.length; render(); }
      } else if (e.key === 'Enter') { e.preventDefault(); run(shown[sel]); }
    };
    input.addEventListener('input', () => { sel = 0; render(); });
    list.addEventListener('click', (e) => { const b = e.target.closest('button[data-i]'); if (b) run(shown[+b.dataset.i]); });
    wrap.addEventListener('mousedown', (e) => { if (e.target === wrap) close(); });
    document.addEventListener('keydown', onKey, true);
    document.body.appendChild(wrap);
    render();
    input.focus();
  };
  const isMac = /Mac|iPhone|iPad/.test((global.navigator && global.navigator.platform) || '');
  function heroActions(slot) {
    if (!slot) return;
    const k = document.createElement('button');
    k.type = 'button';
    k.className = 'hbtn';
    k.innerHTML = '<i class="fa-solid fa-magnifying-glass"></i>Jump to <kbd>' + (isMac ? '⌘K' : 'Ctrl K') + '</kbd>';
    k.addEventListener('click', R.palette);
    slot.appendChild(k);
    if (!R.auraReady()) return;
    document.body.classList.add('has-aura'); // the shell's AURA button sits over the bottom-right corner
    const a = document.createElement('button');
    a.type = 'button';
    a.className = 'hbtn aura';
    a.innerHTML = '<img src="./assets/fashion-stylist.svg" alt="" width="18" height="18">Ask AURA';
    a.title = 'AURA reads this page’s stock, sales and open documents';
    a.addEventListener('click', () => R.askAura({ event: 'stock_brief', echo: 'Brief me on today’s stock moves' }));
    slot.appendChild(a);
  }
  document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && !e.altKey && String(e.key).toLowerCase() === 'k' && R.me) { e.preventDefault(); R.palette(); }
  });

  /* ---------- Boot ---------- */
  function hero(sel) { return document.querySelector(sel); }
  R.renderContext = function () {
    const me = R.me;
    const slot = hero('[data-rt-context]');
    if (!slot || !me) return;
    const loc = me.location ? '<span class="pill"><i class="fa-solid fa-location-dot"></i>' + R.esc(me.location.name) +
      ' · <span class="mono">' + R.esc(me.location.code) + '</span></span>' : '';
    slot.insertAdjacentHTML('beforeend', '<span class="pill"><i class="fa-solid fa-store"></i>' + R.esc(me.brand_name) + '</span>' + loc);
  };

  async function brandPicker(slot) {
    const pill = document.createElement('span');
    pill.className = 'pill';
    pill.innerHTML = '<i class="fa-solid fa-building"></i><select aria-label="Brand"><option value="">Pick a brand…</option></select>';
    slot.appendChild(pill);
    const sel = pill.querySelector('select');
    try {
      const data = await R.api('/brands');
      R.options(sel, data.brands || [], (b) => b, (b) => b, 'Pick a brand…');
      if (R.brand()) {
        if (!(data.brands || []).includes(R.brand())) sel.insertAdjacentHTML('beforeend', '<option>' + R.esc(R.brand()) + '</option>');
        sel.value = R.brand();
      }
    } catch (e) { R.fail(e); }
    sel.addEventListener('change', () => { R.setBrand(sel.value); global.location.reload(); });
  }

  /** Sign-in check, admin brand choice and /me. Calls ready(me) once the page may load data. */
  R.boot = async function (tab, ready) {
    const sess = S.requireAuth(tab);
    if (!sess) return;
    R.session = sess;
    ctx.tab = tab;
    const main = document.querySelector('[data-rt-main]');
    const slot = hero('[data-rt-context]');
    if (sess.role === 'admin' && slot) await brandPicker(slot);
    if (sess.role === 'admin' && !R.brand()) {
      if (main) main.innerHTML = '<div class="notice info"><i class="fa-solid fa-circle-info"></i><div>Pick the brand to work on from the menu above.</div></div>';
      return;
    }
    try {
      R.me = await R.api('/me');
    } catch (e) {
      if (main) {
        main.innerHTML = '<div class="notice bad"><i class="fa-solid fa-triangle-exclamation"></i><div><b>The retail suite isn’t available.</b><br>' +
          R.esc(e.message) + (e.status === 503 ? '<br>Ask your administrator to check the retail database (Cloud SQL).' : '') + '</div></div>';
      }
      return;
    }
    R.renderContext();
    heroActions(slot);
    R.$$('[data-perm]').forEach((el) => { if (!el.dataset.perm.split(' ').some(R.can)) el.hidden = true; });
    if (!R.me.settings || !R.me.settings.configured) {
      const n = document.querySelector('[data-rt-setup]');
      if (n) n.hidden = false;
    }
    R.publishContext();
    try {
      await ready(R.me);
    } finally {
      cmdLive = true;
      cmdQueue.splice(0).forEach(runCommand);
    }
  };

  /* ---------- Documents ---------- */
  /** Tax invoice / credit note markup (printable). */
  R.invoiceHtml = function (inv) {
    const s = inv.seller || {};
    const cn = inv.kind === 'credit_note';
    const igst = inv.inter_state;
    const rows = (inv.lines || []).map((l, i) =>
      '<tr><td>' + (i + 1) + '</td><td>' + R.esc(l.description) + '<br><span style="color:#555">' + R.esc(l.sku) + ' · ' +
      R.esc(l.color) + ' / ' + R.esc(l.size) + '</span></td><td>' + R.esc(l.hsn_code) + '</td><td class="r">' + l.qty +
      '</td><td class="r">' + R.money(l.mrp_paise) + '</td><td class="r">' + R.money(l.discount_paise) + '</td><td class="r">' +
      R.money(l.taxable_paise) + '</td><td class="r">' + R.rate(l.gst_rate_bp) + '</td>' +
      (igst ? '<td class="r">' + R.money(l.igst_paise) + '</td>'
            : '<td class="r">' + R.money(l.cgst_paise) + '</td><td class="r">' + R.money(l.sgst_paise) + '</td>') +
      '<td class="r">' + R.money(l.total_paise) + '</td></tr>').join('');
    const taxRows = (inv.tax_summary || []).map((t) => '<tr><td>' + R.rate(t.gst_rate_bp) + '</td><td class="r">' + R.money(t.taxable_paise) +
      '</td>' + (igst ? '<td class="r">' + R.money(t.igst_paise) + '</td>'
                      : '<td class="r">' + R.money(t.cgst_paise) + '</td><td class="r">' + R.money(t.sgst_paise) + '</td>') + '</tr>').join('');
    const pays = (inv.payments || []).map((p) => R.esc(p.mode.toUpperCase()) + ' ' + R.money(p.amount_paise) +
      (p.reference ? ' (' + R.esc(p.reference) + ')' : '')).join(' · ');
    return '<div class="doc"><h2>' + (cn ? 'Credit Note' : 'Tax Invoice') + '</h2>' +
      '<div class="dhead"><div><b>' + R.esc(s.legal_name) + '</b>' + (s.trade_name ? '<br>' + R.esc(s.trade_name) + ' · ' + R.esc(s.store_name) : '') +
      '<br>' + R.esc(s.address) + '<br>GSTIN: <b>' + R.esc(s.gstin) + '</b> · State: ' + R.esc(s.state) + ' (' + R.esc(s.state_code) + ')' +
      (s.phone ? '<br>Phone: ' + R.esc(s.phone) : '') + '</div>' +
      '<div style="text-align:right">' + (cn ? 'Credit note' : 'Invoice') + ' no: <b>' + R.esc(inv.invoice_number) + '</b><br>Date: ' + R.day(inv.business_date) +
      (cn ? '<br>Against invoice: <b>' + R.esc(inv.original_invoice_number) + '</b>' : '') +
      '<br>Place of supply: ' + R.esc(inv.place_of_supply_name) + ' (' + R.esc(inv.place_of_supply) + ')' +
      (inv.customer_name ? '<br>Customer: ' + R.esc(inv.customer_name) : '') +
      (inv.customer_gstin ? '<br>Customer GSTIN: <b>' + R.esc(inv.customer_gstin) + '</b>' : '') + '</div></div>' +
      '<table><thead><tr><th>#</th><th>Item</th><th>HSN</th><th class="r">Qty</th><th class="r">MRP</th><th class="r">Discount</th><th class="r">Taxable</th><th class="r">GST</th>' +
      (igst ? '<th class="r">IGST</th>' : '<th class="r">CGST</th><th class="r">SGST</th>') + '<th class="r">Amount</th></tr></thead><tbody>' + rows + '</tbody></table>' +
      '<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start"><table style="width:auto;flex:1 1 260px"><thead><tr><th>Rate</th><th class="r">Taxable</th>' +
      (igst ? '<th class="r">IGST</th>' : '<th class="r">CGST</th><th class="r">SGST</th>') + '</tr></thead><tbody>' + taxRows + '</tbody></table>' +
      '<table class="totals"><tbody><tr><td>Gross (MRP)</td><td class="r">' + R.money(inv.gross_paise) + '</td></tr>' +
      '<tr><td>Discount</td><td class="r">−' + R.money(inv.discount_paise) + '</td></tr>' +
      '<tr><td>Taxable value</td><td class="r">' + R.money(inv.taxable_paise) + '</td></tr>' +
      (igst ? '<tr><td>IGST</td><td class="r">' + R.money(inv.igst_paise) + '</td></tr>'
            : '<tr><td>CGST</td><td class="r">' + R.money(inv.cgst_paise) + '</td></tr><tr><td>SGST</td><td class="r">' + R.money(inv.sgst_paise) + '</td></tr>') +
      '<tr><td>Round-off</td><td class="r">' + R.money(inv.round_off_paise) + '</td></tr>' +
      '<tr class="grand"><td>' + (cn ? 'Refund' : 'Total') + '</td><td class="r">' + R.money(inv.total_paise) + '</td></tr></tbody></table></div>' +
      '<div class="words">' + R.esc(inv.amount_in_words) + '</div>' +
      '<div style="margin-top:6px">' + (cn ? 'Refunded: ' : 'Paid: ') + (pays || '—') +
      (inv.change_paise ? ' · Change returned: ' + R.money(inv.change_paise) : '') + '</div>' +
      (cn && inv.reason ? '<div>Reason: ' + R.esc(inv.reason) + '</div>' : '') +
      '<div class="foot">Prices include GST. ' + (s.footer ? R.esc(s.footer) + ' · ' : '') + 'Billed by ' + R.esc(inv.created_by) + ' · ' + R.dateTime(inv.created_at) + '</div></div>';
  };

  const EAN_L = ['0001101', '0011001', '0010011', '0111101', '0100011', '0110001', '0101111', '0111011', '0110111', '0001011'];
  const EAN_PARITY = ['LLLLLL', 'LLGLGG', 'LLGGLG', 'LLGGGL', 'LGLLGG', 'LGGLLG', 'LGGGLL', 'LGLGLG', 'LGLGGL', 'LGGLGL'];
  const invert = (s) => s.replace(/./g, (c) => (c === '1' ? '0' : '1'));
  /** EAN-13 as an SVG string, or '' when the code isn't a valid EAN-13 (then print the digits only). */
  R.ean13Svg = function (code) {
    const s = String(code || '');
    if (!/^\d{13}$/.test(s)) return '';
    const sum = s.slice(0, 12).split('').reduce((a, c, i) => a + Number(c) * (i % 2 ? 3 : 1), 0);
    if ((10 - sum % 10) % 10 !== Number(s[12])) return '';
    const parity = EAN_PARITY[Number(s[0])];
    let bits = '101';
    for (let i = 1; i <= 6; i++) {
      const l = EAN_L[Number(s[i])];
      bits += parity[i - 1] === 'L' ? l : invert(l).split('').reverse().join('');
    }
    bits += '01010';
    for (let i = 7; i <= 12; i++) bits += invert(EAN_L[Number(s[i])]);
    bits += '101';
    let rects = '';
    for (let i = 0; i < bits.length; i++) if (bits[i] === '1') rects += '<rect x="' + (i + 9) + '" y="0" width="1" height="50"/>';
    return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 113 60" width="113" height="60" role="img" aria-label="Barcode ' + s + '">' +
      '<g fill="#000">' + rects + '</g><text x="56.5" y="59" font-size="9" text-anchor="middle" font-family="monospace">' + s + '</text></svg>';
  };

  /** Print some markup through the page's #printArea. */
  R.print = function (html) {
    let area = document.getElementById('printArea');
    if (!area) {
      area = document.createElement('div');
      area.id = 'printArea';
      document.body.appendChild(area);
    }
    area.innerHTML = html;
    global.print();
  };

  global.Retail = R;
})(window);
