(() => {
  if (window.__agentRecorder) return;
  const R = { events: [] };
  window.__agentRecorder = R;
  const stable = (el) => {
    for (const a of ['data-testid', 'data-test', 'data-qa', 'name', 'aria-label', 'id']) {
      const v = el.getAttribute(a);
      if (v && v.length < 80 && !/^[0-9]+$/.test(v) && !/[0-9a-f]{8,}/i.test(v)) {
        const s = el.tagName.toLowerCase() + '[' + a + '=' + JSON.stringify(v) + ']';
        try { if (document.querySelectorAll(s).length === 1) return s; } catch (e) {}
      }
    }
    let node = el, parts = [];
    while (node && node.nodeType === 1 && parts.length < 5) {
      let part = node.tagName.toLowerCase();
      const p = node.parentElement;
      if (p) {
        const same = Array.from(p.children).filter(c => c.tagName === node.tagName);
        if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
      }
      parts.unshift(part);
      node = p;
      if (node && node.id) { parts.unshift('#' + node.id); break; }
    }
    return parts.join(' > ');
  };
  const target = (el) => {
    const ph = el.getAttribute('placeholder');
    if (ph) return { placeholder: ph };
    const tag = el.tagName.toLowerCase();
    const txt = (el.innerText || '').trim().slice(0, 80);
    if ((tag === 'button' || tag === 'a') && txt) return { text: txt, exact: true };
    return { css: stable(el) };
  };
  const push = (ev) => { ev.url = location.href; ev.ts = Date.now(); R.events.push(ev); };
  document.addEventListener('click', (e) => {
    if (e.target && e.target.nodeType === 1) push({ t: 'click', target: target(e.target) });
  }, true);
  document.addEventListener('change', (e) => {
    const el = e.target;
    if (!el || el.nodeType !== 1) return;
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') return;
    const secret = (el.getAttribute('type') || '').toLowerCase() === 'password';
    push({
      t: 'fill', target: target(el), value: secret ? null : String(el.value), secret: secret,
      label: el.getAttribute('placeholder') || el.getAttribute('name') || el.getAttribute('aria-label') || ''
    });
  }, true);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && e.target && e.target.nodeType === 1) {
      push({ t: 'press', target: target(e.target), key: 'Enter' });
    }
  }, true);
})();