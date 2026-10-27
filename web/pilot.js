/** AURA Stock Pilot UI: Warehouse › Stock Pilot (full) and the Inventory overview panel (compact). Needs retail.js. */
(function (global) {
  const R = global.Retail;
  const esc = R.esc;
  const AV = './assets/fashion-stylist.svg';

  const KINDS = {
    restock: { label: 'Restock', icon: 'fa-truck-ramp-box' },
    rebalance: { label: 'Rebalance', icon: 'fa-right-left' },
    reorder: { label: 'Reorder', icon: 'fa-cart-flatbed' },
    receive: { label: 'Receive', icon: 'fa-box-open' },
    dispatch: { label: 'Dispatch', icon: 'fa-truck-fast' },
    po_late: { label: 'Late PO', icon: 'fa-clock' },
    slow: { label: 'Slow stock', icon: 'fa-snowflake' },
    damaged: { label: 'Damaged', icon: 'fa-heart-crack' },
  };
  const FILTERS = [
    ['all', 'All', null],
    ['restock', 'Restock', ['restock']],
    ['rebalance', 'Rebalance', ['rebalance']],
    ['reorder', 'Reorder', ['reorder']],
    ['stuck', 'Stuck documents', ['receive', 'dispatch', 'po_late']],
    ['clear', 'Slow & damaged', ['slow', 'damaged']],
  ];
  const SEV = { high: 'Urgent', medium: 'Soon', low: 'When you can' };
  const BASIS = { sales: ['Selling', ''], par: ['Below par', 'gold'], demand: ['Wardrobe gap', 'ai'], slow: ['Slow elsewhere', ''], network: ['Brand-wide', 'warn'] };
  const OPEN = { 'inventory/stock': 'Open stock', 'inventory/incoming': 'Open receiving', 'inventory/tools': 'Open adjust & count',
                 'warehouse/transfers': 'Open transfers', 'warehouse/po': 'Open purchase orders' };
  const METHOD_ICONS = ['fa-chart-line', 'fa-warehouse', 'fa-right-left', 'fa-cart-flatbed', 'fa-shirt', 'fa-user-check'];

  const days = (d) => (d === null || d === undefined ? '' : (d >= 100 ? '99+' : (Math.round(d * 10) / 10).toString()));
  function cover(l) {
    if (l.basis === 'demand') return '<span class="cover new" title="Not stocked here yet">new</span>';
    if (l.match_pct !== undefined) return '<span class="cover new" title="How closely it matches what shoppers there wear">' + esc(l.match_pct) + '% match</span>';
    if ((l.basis === 'sales' || l.basis === 'par') && !l.on_hand && !l.incoming) return '<span class="cover out" title="Out of stock and still selling">out</span>';
    if (l.cover_days === null || l.cover_days === undefined) return '';
    const brandWide = l.basis === 'network';
    return '<span class="cover' + (l.cover_days < (brandWide ? 21 : 10) ? ' low' : '') + '" title="' +
      (brandWide ? 'Days of stock left across the whole brand' : 'Days of stock left at the current pace') + '">' + days(l.cover_days) + 'd</span>';
  }
  function locChip(l, icon) {
    if (!l) return '';
    return '<span class="loc"><i class="fa-solid ' + (icon || (l.kind === 'warehouse' ? 'fa-warehouse' : 'fa-store')) + '"></i><b>' + esc(l.code) + '</b>' + esc(l.name) + '</span>';
  }
  function route(a) {
    const doc = a.document ? '<span class="badge mono">' + esc(a.document) + '</span>' : '';
    if (a.kind === 'reorder') {
      return '<div class="route"><span class="loc"><i class="fa-solid fa-industry"></i>' + (a.supplier ? esc(a.supplier.name) : '<i>Pick a supplier</i>') +
        '</span><span class="arrow" aria-label="to"></span>' + locChip(a.to) + '</div>';
    }
    if (a.from && a.to && a.from.code !== a.to.code) return '<div class="route">' + locChip(a.from) + '<span class="arrow" aria-label="to"></span>' + locChip(a.to) + doc + '</div>';
    return a.at ? '<div class="route">' + locChip(a.at) + doc + '</div>' : '';
  }
  function value(a) {
    if (a.kind === 'reorder') return [R.compactMoney(a.value_cost_paise), 'est. at cost'];
    if (a.impact_paise) return [R.compactMoney(a.impact_paise), a.kind === 'restock' || a.kind === 'rebalance' ? 'at MRP' : 'held at MRP'];
    if (a.value_cost_paise) return [R.compactMoney(a.value_cost_paise), 'at cost'];
    return null;
  }
  const swatchOf = (l) => (l.color ? R.swatch(l.color) : '');
  function lineRow(l) {
    const meta = l.basis === 'slow' && l.age_days !== undefined
      ? '<span class="cover">' + (l.age_days === null ? 'no sale 28d' : esc(l.age_days) + 'd old') + '</span>' : cover(l);
    return '<div class="ln"><div class="nm">' + swatchOf(l) + esc(l.label) + '<small>' + esc(l.sku) + '</small></div>' + meta +
      '<span class="qty">×' + R.num(l.qty) + '</span></div>';
  }
  function fixLabel(a) {
    if (a.fix.type === 'transfer') return 'draft transfer';
    if (a.fix.type === 'purchase_order') return 'draft PO';
    return '';
  }
  function lockNote(a) {
    if (a.fix.type === 'purchase_order') return 'Only a brand admin raises purchase orders';
    if (a.from && a.from.kind === 'warehouse') return 'A brand admin or the sending warehouse creates this';
    return 'Only a brand admin moves stock between stores';
  }

  function actionCard(a, i) {
    const k = KINDS[a.kind] || { label: a.kind, icon: 'fa-circle' };
    const v = value(a);
    const lines = (a.lines || []).slice(0, 3);
    const more = (a.lines || []).length - lines.length + (a.more_lines || 0);
    const writes = a.fix.type === 'transfer' || a.fix.type === 'purchase_order';
    let foot = '';
    if (writes && a.can_apply) foot += '<button type="button" class="btn primary sm" data-review="' + esc(a.id) + '"><i class="fa-solid fa-wand-magic-sparkles"></i>Review &amp; create ' + fixLabel(a) + '</button>';
    else if (writes) foot += '<button type="button" class="btn sm" data-review="' + esc(a.id) + '"><i class="fa-solid fa-list"></i>See lines</button><span class="meta"><i class="fa-solid fa-lock"></i> ' + lockNote(a) + '</span>';
    else foot += '<button type="button" class="btn sm" data-open="' + esc(a.id) + '"><i class="fa-solid fa-arrow-up-right-from-square"></i>' + esc(OPEN[a.fix.tab + '/' + a.fix.section] || 'Open') + '</button>';
    if (R.auraReady()) foot += '<button type="button" class="btn ghost sm" data-ask="' + esc(a.id) + '"><img class="av" src="' + AV + '" alt="">Ask AURA why</button>';
    foot += '<span class="sp"></span>' + (a.units ? '<span class="meta">' + R.count(a.units, 'unit') + ' · ' + R.count(a.skus, 'SKU') + '</span>' : '');
    return '<article class="act ' + esc(a.severity) + '" style="animation-delay:' + Math.min(i, 12) * 40 + 'ms" data-id="' + esc(a.id) + '">' +
      '<div class="a-top"><div class="a-ic"><i class="fa-solid ' + k.icon + '"></i></div><div class="a-h"><div class="a-k">' + esc(k.label) +
      ' <span class="sev ' + esc(a.severity) + '">' + esc(SEV[a.severity] || a.severity) + '</span></div><h3>' + esc(a.title) + '</h3><p class="a-sum">' + esc(a.summary) + '</p></div>' +
      (v ? '<div class="a-val"><b>' + esc(v[0]) + '</b><small>' + esc(v[1]) + '</small></div>' : '') + '</div>' + route(a) +
      '<ul class="why">' + (a.why || []).slice(0, 4).map((w) => '<li>' + esc(w) + '</li>').join('') + '</ul>' +
      (lines.length ? '<div class="lines">' + lines.map(lineRow).join('') + (more > 0 ? '<div class="ln"><span class="more">+ ' + R.count(more, 'more SKU') + '</span><span></span><span></span></div>' : '') + '</div>' : '') +
      '<div class="a-foot">' + foot + '</div></article>';
  }

  const DIMS = [['category', 'Category'], ['color', 'Colour'], ['size', 'Size']];
  function chip(g, kind) {
    const lead = g.dim === 'color' ? R.swatch(g.label) : (g.dim === 'size' ? '' : '<i class="fa-solid fa-shirt" style="font-size:10px;opacity:.7"></i>');
    const label = g.dim === 'size' ? 'Size ' + g.label : g.label;
    return '<span class="gap ' + kind + '" title="' + esc(g.demand_pct + '% of local demand, ' + g.stock_pct + '% of this store’s stock') + '">' + lead + esc(label) +
      ' <small>' + esc(g.demand_pct) + '% wanted · ' + esc(g.stock_pct) + '% stocked</small></span>';
  }
  function fitCard(f, i) {
    const loc = f.location || {};
    const meta = ['PIN ' + (f.pin_code || '—'), R.count(f.units, 'unit')];
    if (f.garments) meta.push(R.num(f.garments) + ' garments seen');
    let body = '';
    if (f.fit === null || f.fit === undefined) body = '<p class="note">' + esc(f.note || 'Not enough data yet.') + '</p>';
    else {
      body = '<div class="dims">' + DIMS.filter(([d]) => f.dims[d] !== undefined).map(([d, n]) =>
        '<div class="dim"><span>' + n + '</span><div class="share"><i style="width:' + Math.max(2, f.dims[d]) + '%"></i></div><b>' + esc(f.dims[d]) + '</b></div>').join('') + '</div>';
      if ((f.gaps || []).length) body += '<div><div class="hint" style="margin:0 0 6px">Under-stocked for local wardrobes</div><div class="gaps">' + f.gaps.map((g) => chip(g, 'need')).join('') + '</div></div>';
      if ((f.excess || []).length) body += '<div><div class="hint" style="margin:0 0 6px">More than locals ask for</div><div class="gaps">' + f.excess.map((g) => chip(g, 'over')).join('') + '</div></div>';
    }
    const top = (f.demand_top && f.demand_top.color) || [];
    if (top.length) body += '<div class="hint" style="margin:0">Locals wear: ' + top.map((c) => R.swatch(c.label) + esc(c.label) + ' ' + esc(c.pct) + '%').join(' · ') + '</div>';
    return '<div class="fit" style="animation-delay:' + Math.min(i, 12) * 50 + 'ms"><div class="f-top">' + R.ring(f.fit, { size: 64, label: 'Wardrobe fit', sub: 'fit' }) +
      '<div><h4>' + esc(loc.name) + ' <span class="mono" style="color:var(--muted);font-weight:500">' + esc(loc.code) + '</span></h4><div class="meta">' + esc(meta.join(' · ')) + '</div></div></div>' + body + '</div>';
  }

  function kpis(k) {
    return [
      R.kpi({ icon: 'fa-bolt', label: 'Urgent', value: R.num(k.urgent), sub: 'of ' + R.count(k.actions, 'suggestion'), tone: k.urgent ? 'bad' : 'ok' }),
      R.kpi({ icon: 'fa-circle-exclamation', label: 'Stock-outs', value: R.num(k.stockouts), sub: 'selling SKUs at zero', tone: k.stockouts ? 'bad' : '' }),
      R.kpi({ icon: 'fa-hourglass-half', label: 'Running low', value: R.num(k.at_risk), sub: 'under 10 days or below par', tone: k.at_risk ? 'warn' : '' }),
      R.kpi({ icon: 'fa-truck-fast', label: 'Units to move', value: R.compact(k.units_to_move), sub: 'warehouse and store moves' }),
      R.kpi({ icon: 'fa-cart-flatbed', label: 'Units to order', value: R.compact(k.units_to_order), sub: 'from suppliers' }),
      R.kpi({ icon: 'fa-indian-rupee-sign', label: 'Stock in moves', value: R.compactMoney(k.sales_value_paise), sub: 'at MRP' }),
      R.kpi({ icon: 'fa-snowflake', label: 'Slow stock', value: R.compactMoney(k.slow_value_paise), sub: 'at cost, not selling' }),
    ].join('');
  }

  function showMethod(p) {
    R.modal({ title: 'How AURA Stock Pilot decides', html: '<p class="hint" style="margin:0 0 12px">Every number comes from your own POS sales, stock, transfers and purchase orders (' +
      esc(p.label) + ', last ' + esc(p.lookback_days) + ' days). Wardrobe fit uses SuffAI demand around each store’s PIN. Nothing is created until a person confirms it.</p>' +
      '<ul class="method">' + (p.method || []).map((m, i) => '<li><i class="fa-solid ' + (METHOD_ICONS[i] || 'fa-check') + '"></i><span>' + esc(m) + '</span></li>').join('') + '</ul>' });
  }

  /* ---------- Review & create ---------- */
  async function review(a, done) {
    const isPO = a.fix.type === 'purchase_order', edit = !!a.can_apply;
    let suppliers = [];
    if (isPO && edit) {
      try { suppliers = (await R.api('/suppliers')).suppliers || []; } catch (e) { R.fail(e); return; }
    }
    const head = '<tr><th>Item</th><th>Why</th><th class="r">Sells / day</th><th class="r">' + (isPO ? 'Brand stock' : 'Has') + '</th><th class="r">Cover</th>' +
      '<th class="r">' + (isPO ? 'Unit cost' : 'Can send') + '</th><th class="r">Qty</th></tr>';
    const rows = a.lines.map((l, i) => {
      const b = BASIS[l.basis] || [l.basis, ''];
      const max = isPO ? 100000 : (l.source_units || l.qty);
      return '<tr data-i="' + i + '"><td>' + swatchOf(l) + esc(l.label) + '<div class="hint mono" style="margin:0">' + esc(l.sku) + '</div></td>' +
        '<td><span class="badge ' + b[1] + '">' + esc(b[0]) + '</span>' + (l.match_pct !== undefined ? ' <span class="hint">' + esc(l.match_pct) + '% match</span>' : '') + '</td>' +
        '<td class="r num-cell">' + (l.daily_sales ? esc(l.daily_sales) : '—') + '</td>' +
        '<td class="r num-cell">' + R.num(isPO ? l.network_units : l.on_hand) + (!isPO && l.incoming ? ' <span class="hint">+' + R.num(l.incoming) + ' coming</span>' : '') + '</td>' +
        '<td class="r">' + cover(l) + '</td><td class="r num-cell">' + (isPO ? (l.unit_cost ? R.rupees(l.unit_cost) : '—') : R.num(l.source_units)) + '</td>' +
        '<td class="r">' + (edit ? '<input class="in sm qty" type="number" min="0" max="' + max + '" value="' + l.qty + '" data-q aria-label="Quantity for ' + esc(l.sku) + '">' : R.num(l.qty)) + '</td></tr>';
    }).join('');
    const supSel = isPO && edit ? '<label class="f" style="max-width:360px;margin-bottom:12px">Supplier <span class="req">*</span><select class="in" data-sup>' +
      '<option value="">Pick a supplier…</option>' + suppliers.map((s) => '<option value="' + esc(s.supplier_id) + '"' +
      (a.supplier && a.supplier.supplier_id === s.supplier_id ? ' selected' : '') + '>' + esc(s.name) + '</option>').join('') + '</select></label>' +
      (suppliers.length ? '' : '<div class="notice"><i class="fa-solid fa-circle-exclamation"></i><div>Add a supplier under <b>Suppliers</b> first.</div></div>') : '';
    const html = '<div class="review">' + route(a) + '<ul class="why">' + (a.why || []).map((w) => '<li>' + esc(w) + '</li>').join('') + '</ul>' + supSel +
      '<div class="tbl-wrap" style="margin-top:12px;max-height:46vh;overflow:auto"><table class="tbl"><thead>' + head + '</thead><tbody>' + rows + '</tbody>' +
      '<tfoot><tr><td colspan="6" class="r">Total</td><td class="r num-cell" data-total></td></tr></tfoot></table></div>' +
      (a.more_lines ? '<p class="hint">' + R.count(a.more_lines, 'more SKU') + ' will show after these are handled.</p>' : '') +
      '<p class="hint"><i class="fa-solid fa-shield-halved"></i> ' + (isPO
        ? 'AURA drafts the PO only. Nothing is ordered until you place it under Purchase orders.'
        : 'AURA drafts the transfer only. Nothing leaves ' + esc(a.from.code) + ' until someone dispatches it under Transfers.') + '</p></div>';
    const lineQty = (el) => R.$$('tr[data-i]', el).map((tr) => ({ l: a.lines[+tr.dataset.i], q: Math.max(0, parseInt((R.$('[data-q]', tr) || {}).value, 10) || 0) }));
    const total = (el) => {
      const xs = lineQty(el);
      const units = xs.reduce((s, x) => s + x.q, 0);
      const val = xs.reduce((s, x) => s + x.q * (isPO ? (x.l.unit_cost || 0) : (x.l.mrp || 0)), 0);
      R.$('[data-total]', el).innerHTML = R.count(units, 'unit') + '<div class="hint" style="margin:0">' + R.rupees(val) + (isPO ? ' at cost' : ' at MRP') + '</div>';
    };
    const actions = [{ label: edit ? 'Cancel' : 'Close' }];
    if (edit) {
      actions.push({ label: '<i class="fa-solid fa-wand-magic-sparkles"></i>Create ' + fixLabel(a), kind: 'primary', onClick: async (close, el) => {
        const lines = lineQty(el).map((x) => ({ variant_id: x.l.variant_id, qty: x.q }));
        const body = { lines };
        if (isPO) {
          body.supplier_id = R.$('[data-sup]', el).value;
          if (!body.supplier_id) throw new Error('Pick a supplier.');
        }
        let made;
        try {
          made = await R.api('/pilot/actions/' + encodeURIComponent(a.id) + '/apply', { body });
        } catch (e) {
          if (e.status === 409 && /changed/.test(e.message)) { R.fail(e); close(); done(null); return false; }
          throw e;
        }
        R.celebrate();
        const open = made.type === 'transfer'
          ? { label: 'Open ' + made.number, run: () => R.openTransfer(made.transfer_id, () => done(null)) }
          : { label: 'Open POs', run: () => R.openTab('warehouse', 'po') };
        R.toast(made.number + ' drafted: ' + R.count(made.units, 'unit') + (made.type === 'transfer' ? ' from ' + made.from.code + ' to ' + made.to.code : ' into ' + made.to.code) + '.', 'ok', open);
        done(made);
        return true;
      } });
    }
    const m = R.modal({ title: (KINDS[a.kind] || {}).label + ' · ' + a.title, wide: true, html, actions });
    total(m.el);
    m.el.addEventListener('input', (e) => { if (e.target.matches('[data-q]')) total(m.el); });
  }

  /* ---------- Mount ---------- */
  /** opts: {compact, onLoad(plan), onCreated(made)} → {refresh(), plan()} */
  function mount(root, opts) {
    const o = opts || {};
    const st = { plan: null, filter: 'all', seq: 0, all: false };
    const find = (id) => ((st.plan && st.plan.actions) || []).find((a) => a.id === id);
    const created = (made) => { load(); if (made && o.onCreated) o.onCreated(made); };

    async function load() {
      const my = ++st.seq;
      if (!st.plan) root.innerHTML = o.compact ? '<div class="pilot-mini"><div class="pm-head"><div class="t"><small>AURA Stock Pilot</small><b>Reading your stock and sales…</b></div></div>' +
        '<div style="padding:12px 18px">' + R.skeleton('line', 3) + '</div></div>'
        : '<div class="skel block" style="height:180px;border-radius:20px"></div><div class="kpi-strip">' + R.skeleton('tile', 6) + '</div>' + R.skeleton('block', 2);
      let plan;
      try { plan = await R.api('/pilot'); } catch (e) {
        if (my !== st.seq) return;
        root.innerHTML = '<div class="notice bad"><i class="fa-solid fa-triangle-exclamation"></i><div><b>Stock Pilot couldn’t load.</b><br>' + esc(e.message) + '</div></div>';
        return;
      }
      if (my !== st.seq) return;
      st.plan = plan;
      if (o.compact) renderMini(); else renderFull();
      if (o.onLoad) o.onLoad(plan);
    }

    function renderFull() {
      const p = st.plan, b = p.brief, k = p.kpis;
      const counts = p.counts || {};
      const filters = FILTERS.map(([id, label, kinds]) => [id, label, kinds ? kinds.reduce((s, x) => s + (counts[x] || 0), 0) : p.actions.length])
        .filter(([id, , n]) => id === 'all' || n > 0);
      if (!filters.some(([id]) => id === st.filter)) st.filter = 'all';
      const kinds = (FILTERS.find(([id]) => id === st.filter) || [])[2];
      const acts = p.actions.filter((a) => !kinds || kinds.includes(a.kind));
      const aura = R.auraReady();
      root.innerHTML =
        '<div class="pilot-hero"><div class="ph-row"><div class="ph-av"><img src="' + AV + '" alt=""></div><div class="ph-main">' +
        '<div class="ph-eye">AURA Stock Pilot <span class="label-actual">' + esc(p.label) + ' · last ' + esc(p.lookback_days) + ' days</span></div>' +
        '<h2>' + esc(b.headline) + '</h2>' + (b.points.length ? '<ul>' + b.points.map((x) => '<li>' + esc(x) + '</li>').join('') + '</ul>' : '') +
        '<div class="ph-act">' + (aura ? '<button type="button" class="btn gold" data-brief><img class="av" src="' + AV + '" alt="">Talk it through with AURA</button>' : '') +
        '<button type="button" class="btn" data-method><i class="fa-solid fa-circle-question"></i>How AURA decides</button>' +
        '<button type="button" class="btn" data-refresh><i class="fa-solid fa-rotate"></i>Refresh</button></div>' +
        '<div class="ph-time">Updated ' + esc(R.dateTime(p.generated_at)) + ' · ' + R.count(k.stores, 'store') + ' · suggestions only: you confirm every draft</div></div>' +
        (b.avg_fit !== null && b.avg_fit !== undefined ? '<div class="ph-side">' + R.ring(b.avg_fit, { size: 96, label: 'Average wardrobe fit', sub: 'wardrobe fit' }) + '</div>' : '') +
        '</div></div>' +
        '<div class="kpi-strip">' + kpis(k) + '</div>' +
        '<div class="chipbar" role="toolbar" aria-label="Filter suggestions">' + filters.map(([id, label, n]) =>
          '<button type="button" data-filter="' + id + '" aria-pressed="' + (id === st.filter) + '">' + esc(label) + '<span class="c">' + n + '</span></button>').join('') +
        '<span class="sp"></span><span class="hint" style="margin:0">Sorted by urgency, then sales at stake</span></div>' +
        (acts.length ? '<div class="acts">' + acts.map(actionCard).join('') + '</div>'
          : '<div class="card">' + R.emptyState({ ok: true, icon: 'fa-circle-check', title: 'All clear', text: b.headline }) + '</div>') +
        (p.stores.length ? '<div class="card" style="margin-top:16px"><div class="card-h"><h2><i class="fa-solid fa-shirt"></i>Wardrobe fit by store</h2>' +
          '<span class="sub">How each store’s stock mix (category, colour, size) matches what shoppers around its PIN wear. 100 = perfect match.</span></div>' +
          '<div class="fits">' + p.stores.map(fitCard).join('') + '</div></div>' : '');
    }

    function renderMini() {
      const p = st.plan, b = p.brief;
      const aura = R.auraReady();
      const pilotTab = R.canOpen('warehouse');
      const top = st.all && !pilotTab ? p.actions : p.actions.slice(0, 3);
      const rest = p.actions.length - top.length;
      root.innerHTML = '<div class="pilot-mini"><div class="pm-head"><img src="' + AV + '" alt=""><div class="t"><small>AURA Stock Pilot · ' + esc(p.label) + '</small><b>' + esc(b.headline) + '</b></div>' +
        (aura ? '<button type="button" class="btn gold sm" data-brief><i class="fa-solid fa-comment-dots"></i>Ask AURA</button>' : '') +
        (pilotTab ? '<button type="button" class="btn sm" data-pilot>Open Stock Pilot<i class="fa-solid fa-arrow-right"></i></button>' : '') + '</div>' +
        top.map((a) => {
          const k = KINDS[a.kind] || { icon: 'fa-circle' }, v = value(a);
          return '<div class="pm-row ' + esc(a.severity) + '" data-row="' + esc(a.id) + '" role="button" tabindex="0"><div class="a-ic"><i class="fa-solid ' + k.icon + '"></i></div><div style="min-width:0"><b>' +
            esc(a.title) + '</b><span>' + esc(a.summary) + '</span></div><span class="sev ' + esc(a.severity) + '">' + esc(SEV[a.severity]) + '</span><span class="v">' + (v ? esc(v[0]) : '') + '</span></div>';
        }).join('') +
        (rest > 0 ? '<div class="pm-row" ' + (pilotTab ? 'data-pilot' : 'data-more') + ' role="button" tabindex="0"><div></div><div><span>+ ' + R.count(rest, 'more suggestion') + '</span></div><span></span><span></span></div>' : '') +
        '</div>';
    }

    function openFix(a) {
      if (a.fix.type === 'open') R.openTab(a.fix.tab, a.fix.section);
      else review(a, created);
    }
    function onActivate(e) {
      if (e.type === 'keydown' && e.key !== 'Enter' && e.key !== ' ') return;
      const t = e.target.closest('[data-review],[data-open],[data-ask],[data-brief],[data-method],[data-refresh],[data-filter],[data-pilot],[data-more],[data-row]');
      if (!t || !root.contains(t)) return;
      if (e.type === 'keydown') { if (t.tagName === 'BUTTON') return; e.preventDefault(); }
      const a = find(t.dataset.review || t.dataset.open || t.dataset.ask || t.dataset.row);
      if (t.dataset.review && a) review(a, created);
      else if (t.dataset.open && a) openFix(a);
      else if (t.dataset.ask && a) R.askAura({ event: 'stock_action', action_ref: a.id, echo: 'Why “' + a.title + '”?' });
      else if (t.dataset.row !== undefined && a) openFix(a);
      else if (t.hasAttribute('data-brief')) R.askAura({ event: 'stock_brief', echo: 'Brief me on today’s stock moves' });
      else if (t.hasAttribute('data-method')) showMethod(st.plan);
      else if (t.hasAttribute('data-refresh')) R.busy(t, load);
      else if (t.dataset.filter) { st.filter = t.dataset.filter; renderFull(); }
      else if (t.hasAttribute('data-pilot')) R.openTab('warehouse', 'pilot');
      else if (t.hasAttribute('data-more')) { st.all = true; renderMini(); }
    }
    root.addEventListener('click', onActivate);
    root.addEventListener('keydown', onActivate);
    load();
    return { refresh: load, plan: () => st.plan };
  }

  global.StockPilot = { mount, KINDS };
})(window);
