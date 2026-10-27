/* Styled dropdowns: every single-choice <select> gets a themed button + listbox instead of the
   OS menu (which renders black in macOS dark mode). The native <select> stays the source of truth —
   page code keeps reading/writing .value, repopulating options and listening for 'change'. */
(function () {
  const css = `
  :root { color-scheme: light; }
  .csel-native { display: none !important; }
  .csel-btn { display: flex !important; align-items: center; justify-content: space-between; gap: .5rem; width: 100%;
    text-align: left; cursor: pointer; background: #fff; color: #3B2733; text-transform: none; letter-spacing: normal; font-weight: 400; }
  .csel-btn span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .csel-btn svg { flex: none; color: #8A7480; transition: transform .18s; }
  .csel-btn[aria-expanded="true"] { border-color: #6B3A55 !important; box-shadow: 0 0 0 3px rgba(197,159,176,.35); }
  .csel-btn[aria-expanded="true"] svg { transform: rotate(180deg); color: #6B3A55; }
  .csel-btn:disabled { opacity: .55; cursor: not-allowed; }
  .csel-pop { position: fixed; z-index: 1000; margin: 0; padding: .3rem; list-style: none; overflow-y: auto; max-height: 280px;
    background: #fff; border: 1px solid #EAD9E0; border-radius: 12px; box-shadow: 0 16px 36px rgba(59,39,51,.18);
    font: 13px Outfit, Inter, system-ui, sans-serif; color: #3B2733; animation: cselIn .12s ease-out; }
  @keyframes cselIn { from { opacity: 0; transform: translateY(-4px); } }
  .csel-pop li { display: flex; align-items: center; justify-content: space-between; gap: .5rem; padding: .5rem .65rem;
    border-radius: 8px; cursor: pointer; outline: none; }
  .csel-pop li:hover, .csel-pop li:focus { background: #F6EEF0; }
  .csel-pop li[aria-selected="true"] { color: #6B3A55; font-weight: 600; }
  .csel-pop li[aria-disabled="true"] { opacity: .45; cursor: not-allowed; }
  .csel-pop li svg { visibility: hidden; color: #6B3A55; flex: none; }
  .csel-pop li[aria-selected="true"] svg { visibility: visible; }
  /* multi-select listboxes (e.g. PIN codes) render inline: theme their rows */
  select[multiple] { color-scheme: light; }
  select[multiple] option { padding: .3rem .45rem; border-radius: 6px; color: #3B2733; }
  select[multiple] option:checked { background: #6B3A55 linear-gradient(#6B3A55, #6B3A55); color: #fff; }
  @media (prefers-reduced-motion: reduce) { .csel-pop { animation: none; } }`;
  const style = document.createElement('style');
  style.textContent = css;
  document.head.appendChild(style);

  const CHEV = '<svg viewBox="0 0 20 20" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 8l4 4 4-4"/></svg>';
  const TICK = '<svg viewBox="0 0 20 20" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4.5 10.5l3.5 3.5 7.5-8"/></svg>';
  const valueDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value');
  const indexDesc = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'selectedIndex');
  let open = null; // { sel, btn, pop }
  let uid = 0;

  function close(focusBtn) {
    if (!open) return;
    const { btn, pop } = open;
    pop.remove();
    btn.setAttribute('aria-expanded', 'false');
    open = null;
    if (focusBtn) btn.focus();
  }

  function enhance(sel) {
    if (sel.multiple || sel.hidden || sel.dataset.csel) return; // hidden selects stay hidden
    sel.dataset.csel = '1';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = sel.className + ' csel-btn';
    btn.id = sel.id ? sel.id + '__btn' : 'csel' + (++uid);
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.setAttribute('aria-expanded', 'false');
    btn.innerHTML = '<span></span>' + CHEV;
    sel.classList.add('csel-native');
    sel.after(btn);
    // a <label> wrapping the select names the button too
    const label = sel.closest('label') || (sel.id && document.querySelector(`label[for="${CSS.escape(sel.id)}"]`));
    if (label) { if (!label.id) label.id = btn.id + '__lbl'; btn.setAttribute('aria-labelledby', label.id); }
    if (label && label.htmlFor === sel.id) label.htmlFor = btn.id;

    const render = () => {
      const o = sel.options[sel.selectedIndex];
      btn.firstChild.textContent = o ? o.textContent : '';
      btn.disabled = sel.disabled;
    };
    // Programmatic `sel.value = …` / `selectedIndex = …` fire no event; mirror them.
    Object.defineProperty(sel, 'value', { configurable: true, get() { return valueDesc.get.call(this); }, set(v) { valueDesc.set.call(this, v); render(); } });
    Object.defineProperty(sel, 'selectedIndex', { configurable: true, get() { return indexDesc.get.call(this); }, set(v) { indexDesc.set.call(this, v); render(); } });
    new MutationObserver(render).observe(sel, { childList: true, subtree: true, attributes: true, attributeFilter: ['disabled'] });
    sel.addEventListener('change', render);
    render();

    function pick(i) {
      const changed = sel.selectedIndex !== i;
      indexDesc.set.call(sel, i);
      render();
      if (changed) {
        sel.dispatchEvent(new Event('input', { bubbles: true }));
        sel.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    function show() {
      close(false);
      const pop = document.createElement('ul');
      pop.className = 'csel-pop';
      pop.setAttribute('role', 'listbox');
      if (btn.hasAttribute('aria-labelledby')) pop.setAttribute('aria-labelledby', btn.getAttribute('aria-labelledby'));
      Array.from(sel.options).forEach((o, i) => {
        const li = document.createElement('li');
        li.setAttribute('role', 'option');
        li.tabIndex = -1;
        li.dataset.i = i;
        li.setAttribute('aria-selected', String(i === sel.selectedIndex));
        if (o.disabled) li.setAttribute('aria-disabled', 'true');
        li.innerHTML = '<span></span>' + TICK;
        li.firstChild.textContent = o.textContent;
        pop.appendChild(li);
      });
      document.body.appendChild(pop);
      const r = btn.getBoundingClientRect();
      pop.style.minWidth = r.width + 'px';
      pop.style.left = Math.max(8, Math.min(r.left, window.innerWidth - pop.offsetWidth - 8)) + 'px';
      const below = window.innerHeight - r.bottom - 8;
      if (below < Math.min(pop.offsetHeight, 200) && r.top > below) { pop.style.bottom = (window.innerHeight - r.top + 6) + 'px'; pop.style.maxHeight = Math.min(280, r.top - 14) + 'px'; }
      else { pop.style.top = (r.bottom + 6) + 'px'; pop.style.maxHeight = Math.max(140, Math.min(280, below)) + 'px'; }
      btn.setAttribute('aria-expanded', 'true');
      open = { sel, btn, pop };
      const cur = pop.children[sel.selectedIndex] || pop.firstElementChild;
      if (cur) { cur.focus(); cur.scrollIntoView({ block: 'nearest' }); }

      pop.addEventListener('click', (e) => {
        const li = e.target.closest('li');
        if (!li || li.getAttribute('aria-disabled') === 'true') return;
        pick(Number(li.dataset.i));
        close(true);
      });
      pop.addEventListener('keydown', (e) => {
        const items = Array.from(pop.children).filter(li => li.getAttribute('aria-disabled') !== 'true');
        const i = items.indexOf(document.activeElement);
        const go = (n) => { e.preventDefault(); const t = items[(n + items.length) % items.length]; if (t) t.focus(); };
        if (e.key === 'ArrowDown') go(i + 1);
        else if (e.key === 'ArrowUp') go(i - 1);
        else if (e.key === 'Home') go(0);
        else if (e.key === 'End') go(items.length - 1);
        else if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); if (items[i]) { pick(Number(items[i].dataset.i)); close(true); } }
        else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); close(true); }
        else if (e.key === 'Tab') close(false);
        else if (e.key.length === 1) { // type-ahead
          const k = e.key.toLowerCase();
          const hit = items.find((li, j) => j > i && li.textContent.trim().toLowerCase().startsWith(k)) || items.find(li => li.textContent.trim().toLowerCase().startsWith(k));
          if (hit) hit.focus();
        }
      });
    }
    btn.addEventListener('click', () => (open && open.sel === sel ? close(true) : show()));
    btn.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); show(); }
    });
  }

  document.addEventListener('pointerdown', (e) => { if (open && !open.pop.contains(e.target) && !open.btn.contains(e.target)) close(false); }, true);
  window.addEventListener('resize', () => close(false));
  document.addEventListener('scroll', (e) => { if (open && !open.pop.contains(e.target)) close(false); }, true);

  const scan = (root) => (root.querySelectorAll ? root.querySelectorAll('select:not([multiple])') : []).forEach(enhance);
  const start = () => {
    scan(document);
    new MutationObserver((muts) => muts.forEach(m => m.addedNodes.forEach(n => {
      if (n.nodeType !== 1) return;
      if (n.matches('select:not([multiple])')) enhance(n); else scan(n);
    }))).observe(document.body, { childList: true, subtree: true });
  };
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start); else start();
})();
