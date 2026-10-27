const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { read, fakeStorage, runScript, PAGES } = require('./harness');

const XSS = `<img src=x onerror="alert('1')">&`;
const XSS_SAFE = '&lt;img src=x onerror=&quot;alert(&#39;1&#39;)&quot;&gt;&amp;';
const RETAIL_PAGES = ['pos.html', 'inventory.html', 'warehouse.html'];

/** session.js + retail.js in one sandbox with stub storage, location and fetch. */
function load({ session, brand, fetch } = {}) {
  const localStorage = fakeStorage();
  if (session) localStorage._map.set('suffai_session', JSON.stringify(session));
  if (brand) localStorage._map.set('suffai_retail_brand', brand);
  const location = { href: 'https://cc.example/pos.html', replaced: null, replace(u) { this.replaced = u; } };
  const calls = [];
  const ctx = runScript('session.js', {
    localStorage, location, DASHBOARD_API: 'https://api.example', Intl, Date, Math, Blob: function () {},
    fetch: async (url, opts) => { calls.push({ url, opts }); return fetch(url, opts); },
    addEventListener() {}, document: { addEventListener() {} },
  });
  ctx.top = ctx;
  vm.runInContext(read('retail.js'), ctx, { filename: 'retail.js' });
  return { R: ctx.Retail, localStorage, location, calls };
}
const reply = (status, body) => async () => ({ ok: status >= 200 && status < 300, status, json: async () => body });

test('retail pages load config, session and retail.js before their inline code', () => {
  for (const page of RETAIL_PAGES) {
    assert.ok(PAGES.includes(page), page);
    const src = read(page);
    const ses = src.indexOf('src="./session.js"'), ret = src.indexOf('src="./retail.js"');
    assert.ok(ses > -1 && ret > ses, `${page}: session.js then retail.js`);
    assert.ok(src.search(/<script(?![^>]*\bsrc=)[^>]*>/) > ret, `${page}: inline code after retail.js`);
    assert.ok(src.includes('href="./retail.css"'), `${page}: retail.css`);
    assert.ok(src.includes('id="printArea"'), `${page}: print area`);
  }
});

test('retail esc matches every other page', () => {
  const { R } = load();
  assert.equal(R.esc(XSS), XSS_SAFE);
  assert.equal(R.esc(null), '');
  assert.equal(R.esc(undefined), '');
  assert.equal(R.esc(0), '0');
});

test('money, rates and rupee parsing', () => {
  const { R } = load();
  assert.match(R.money(199900), /^₹1,999\.00$/);
  assert.match(R.money(1234567890), /^₹1,23,45,678\.90$/);
  assert.match(R.money(-50), /-₹0\.50|₹-0\.50|−₹0\.50/);
  assert.equal(R.rate(500), '5%');
  assert.equal(R.rate(1800), '18%');
  assert.equal(R.rate(250), '2.5%');
  assert.equal(R.toPaise('1999'), 199900);
  assert.equal(R.toPaise('1,999.5'), 199950);
  assert.equal(R.toPaise(' 0.05 '), 5);
  for (const bad of ['', 'abc', '12.345', '-5', '1e3', null]) assert.equal(R.toPaise(bad), null, String(bad));
  assert.equal(R.addDays('2026-10-31', 1), '2026-11-01');
  assert.equal(R.addDays('2027-03-01', -1), '2027-02-28');
  assert.equal(R.count(1, 'unit'), '1 unit');
  assert.equal(R.count(0, 'bill'), '0 bills');
  assert.equal(R.count(1500, 'SKU'), '1,500 SKUs');
  assert.equal(R.count(2, 'box', 'boxes'), '2 boxes');
});

test('compact numbers and tab access for staff logins', () => {
  const { R } = load();
  assert.equal(R.compact(120000), '1.2L');
  assert.equal(R.compact(45000000), '4.5Cr');
  assert.equal(R.compact(45600), '45.6K');
  assert.equal(R.canOpen('warehouse'), true, 'no session yet: let the shell decide');
  R.session = { role: 'store_staff', allowed_tabs: ['pos', 'inventory'] };
  assert.equal(R.canOpen('inventory'), true);
  assert.equal(R.canOpen('warehouse'), false);
  R.session = { role: 'warehouse_staff', allowed_tabs: ['warehouse', 'inventory'] };
  assert.equal(R.canOpen('warehouse'), true);
  assert.equal(R.canOpen('pos'), false);
});

test('ean13Svg encodes a valid EAN-13 and refuses anything else', () => {
  const { R } = load();
  const L = ['0001101', '0011001', '0010011', '0111101', '0100011', '0110001', '0101111', '0111011', '0110111', '0001011'];
  const PARITY = ['LLLLLL', 'LLGLGG', 'LLGGLG', 'LLGGGL', 'LGLLGG', 'LGGLLG', 'LGGGLL', 'LGLGLG', 'LGLGGL', 'LGGLGL'];
  for (const code of ['4006381333931', '2000000000015', '8901030865275']) {
    const svg = R.ean13Svg(code);
    assert.ok(svg.startsWith('<svg'), code);
    const bits = Array(95).fill('0');
    for (const m of svg.matchAll(/<rect x="(\d+)"/g)) bits[Number(m[1]) - 9] = '1';
    const s = bits.join('');
    assert.equal(s.slice(0, 3), '101');
    assert.equal(s.slice(45, 50), '01010');
    assert.equal(s.slice(92), '101');
    let parity = '', digits = '';
    for (let i = 0; i < 6; i++) {
      const c = s.slice(3 + i * 7, 10 + i * 7);
      const ones = c.split('').filter((b) => b === '1').length;
      parity += ones % 2 ? 'L' : 'G';
      const asL = ones % 2 ? c : c.split('').reverse().map((b) => (b === '1' ? '0' : '1')).join('');
      digits += L.indexOf(asL);
    }
    for (let i = 0; i < 6; i++) {
      const c = s.slice(50 + i * 7, 57 + i * 7);
      digits += L.indexOf(c.split('').map((b) => (b === '1' ? '0' : '1')).join(''));
    }
    assert.equal(parity, PARITY[Number(code[0])], code);
    assert.equal(digits, code.slice(1), code);
    assert.ok(svg.includes('>' + code + '</text>'));
  }
  for (const bad of ['4006381333932', '400638133393', 'ABCDEFGHIJKLM', '', null]) assert.equal(R.ean13Svg(bad), '', String(bad));
});

test('api: admin calls carry the chosen brand; staff calls do not', async () => {
  const ok = reply(200, { ok: true });
  const admin = load({ session: { token: 't', role: 'admin' }, brand: 'Zara', fetch: ok });
  admin.R.session = { role: 'admin' };
  await admin.R.api('/stock', { params: { q: 'TS 100', location_id: '', page: 2 } });
  const u = new URL(admin.calls[0].url);
  assert.equal(u.origin + u.pathname, 'https://api.example/v1/retail/stock');
  assert.equal(u.searchParams.get('brand'), 'Zara');
  assert.equal(u.searchParams.get('q'), 'TS 100');
  assert.equal(u.searchParams.get('page'), '2');
  assert.equal(u.searchParams.has('location_id'), false);
  assert.equal(admin.calls[0].opts.headers.Authorization, 'Bearer t');

  const staff = load({ session: { token: 's', role: 'store_staff' }, brand: 'Zara', fetch: ok });
  staff.R.session = { role: 'store_staff' };
  await staff.R.api('/pos/sales', { body: { lines: [] } });
  const call = staff.calls[0];
  assert.equal(new URL(call.url).searchParams.has('brand'), false);
  assert.equal(call.opts.method, 'POST');
  assert.equal(call.opts.body, '{"lines":[]}');
  assert.equal(call.opts.headers['Content-Type'], 'application/json');
});

test('api: errors keep the message and row errors; 401 signs out', async () => {
  const bad = load({ session: { token: 't', role: 'brand' },
    fetch: reply(422, { detail: { message: '2 rows need fixing.', errors: [{ row: 3, error: 'MRP' }] } }) });
  await assert.rejects(bad.R.api('/catalog/import', { body: { csv: 'x' } }), (e) => {
    assert.equal(e.message, '2 rows need fixing.');
    assert.equal(e.status, 422);
    assert.equal(e.errors[0].row, 3);
    return true;
  });
  const masked = load({ session: { token: 't', role: 'brand' }, fetch: reply(503, { detail: 'Retail is unavailable (ref abc).' }) });
  await assert.rejects(masked.R.api('/me'), (e) => e.status === 503 && /ref abc/.test(e.message) && e.errors.length === 0);

  const gone = load({ session: { token: 't', role: 'brand' }, fetch: reply(401, {}) });
  await assert.rejects(gone.R.api('/me'), (e) => e.status === 401);
  assert.equal(gone.localStorage._map.has('suffai_session'), false);
  assert.equal(gone.location.replaced, './login.html');

  const offline = load({ session: { token: 't', role: 'brand' }, fetch: async () => { throw new Error('net'); } });
  await assert.rejects(offline.R.api('/me'), (e) => e.status === 0 && /reach the server/.test(e.message));
});

test('invoice markup escapes customer text and splits tax by supply type', () => {
  const { R } = load();
  R.me = { brand_name: 'Zara' };
  const base = {
    kind: 'invoice', invoice_number: 'BLR012627-000001', business_date: '2026-10-20', place_of_supply: '29',
    place_of_supply_name: 'Karnataka', customer_name: XSS, seller: { legal_name: 'Zara India Pvt Ltd', gstin: '29AAACZ1234Q1Z5', state: 'Karnataka', state_code: '29' },
    lines: [{ description: 'Tee', sku: 'TS100-WHITE-M', color: 'White', size: 'M', hsn_code: '6109', qty: 1, mrp_paise: 199900, discount_paise: 0,
      taxable_paise: 190381, gst_rate_bp: 500, cgst_paise: 4760, sgst_paise: 4760, igst_paise: 0, total_paise: 199901 }],
    tax_summary: [{ gst_rate_bp: 500, taxable_paise: 190381, cgst_paise: 4760, sgst_paise: 4760, igst_paise: 0 }],
    payments: [{ mode: 'upi', amount_paise: 199900, reference: '<b>' }], gross_paise: 199900, discount_paise: 0, taxable_paise: 190381,
    cgst_paise: 4760, sgst_paise: 4760, igst_paise: 0, round_off_paise: -1, total_paise: 199900, amount_in_words: 'Rupees One Thousand Nine Hundred Ninety Nine Only',
    created_by: 'cashier', created_at: '2026-10-20T05:00:00+00:00',
  };
  const intra = R.invoiceHtml({ ...base, inter_state: false });
  assert.ok(intra.includes('Tax Invoice'));
  assert.ok(intra.includes(XSS_SAFE) && !intra.includes(XSS));
  assert.ok(intra.includes('(&lt;b&gt;)'));
  assert.ok(intra.includes('>CGST<') && intra.includes('>SGST<') && !intra.includes('>IGST<'));
  const inter = R.invoiceHtml({ ...base, inter_state: true, customer_gstin: '27AAACA1234Q1Z5' });
  assert.ok(inter.includes('>IGST<') && !inter.includes('>CGST<'));
  const cn = R.invoiceHtml({ ...base, inter_state: false, kind: 'credit_note', original_invoice_number: 'BLR012627-000001', reason: 'Size' });
  assert.ok(cn.includes('Credit Note') && cn.includes('Against invoice') && cn.includes('Refund'));
});
