(() => {
  if (window.__farsiPanel) return;
  const P = { outbox: [], current: null, collapsed: false };
  window.__farsiPanel = P;

  const host = document.createElement('div');
  host.id = 'farsi-panel-host';
  host.setAttribute('style',
    'all:initial;display:block;position:fixed;bottom:12px;left:12px;' +
    'width:330px;z-index:2147483000;');
  const root = host.attachShadow({ mode: 'open' });

  root.innerHTML = `
<style>
:host{all:initial}
*{box-sizing:border-box;font-family:Tahoma,system-ui,sans-serif}
.box{direction:rtl;background:#1e2430;color:#e8eaed;border:1px solid #39414f;
border-radius:10px;box-shadow:0 6px 20px rgba(0,0,0,.35);font-size:12px;overflow:hidden}
.head{display:flex;align-items:center;gap:6px;padding:7px 9px;background:#252c3a}
.head b{font-size:12px;font-weight:600;flex:1}
.head button{background:none;border:none;color:#9aa4b2;cursor:pointer;font-size:14px;padding:0 3px}
.body{padding:9px;display:block}
.title{color:#8fd0ff;margin-bottom:5px;font-weight:600}
.text{line-height:1.7;white-space:pre-wrap;max-height:120px;overflow:auto}
.cmd{margin-top:7px;padding:6px;background:#161b24;border-radius:6px;
font-family:Consolas,monospace;font-size:11px;direction:ltr;text-align:left;
white-space:pre-wrap;word-break:break-all;max-height:70px;overflow:auto}
.row{display:flex;gap:6px;margin-top:8px}
.row button{flex:1;padding:5px;border:1px solid #39414f;border-radius:6px;
background:#2b3342;color:#e8eaed;cursor:pointer;font-size:11px}
.row button:hover{background:#354055}
.meta{margin-top:6px;color:#8b95a3;font-size:10px}
.toggles{display:flex;gap:10px;padding:6px 9px;border-top:1px solid #39414f;
background:#202734;color:#9aa4b2;font-size:10px}
.toggles label{display:flex;align-items:center;gap:4px;cursor:pointer}
.empty{color:#8b95a3}
</style>
<div class="box">
  <div class="head"><b>دستیار</b><button id="fold" title="جمع کردن">–</button></div>
  <div class="body" id="body"><div class="empty">هنوز پیشنهادی نیست.</div></div>
  <div class="toggles">
    <label><input type="checkbox" id="learn"><span>یادگیری</span></label>
    <label><input type="checkbox" id="suggest"><span>پیشنهاد</span></label>
    <span id="stat" style="flex:1;text-align:left"></span>
  </div>
</div>`;

  const body = root.getElementById('body');
  const fold = root.getElementById('fold');
  const learn = root.getElementById('learn');
  const suggest = root.getElementById('suggest');
  const stat = root.getElementById('stat');

  fold.onclick = () => {
    P.collapsed = !P.collapsed;
    body.style.display = P.collapsed ? 'none' : 'block';
    fold.textContent = P.collapsed ? '+' : '–';
  };
  learn.onchange = () => P.outbox.push({ type: 'toggle', name: 'learn', value: learn.checked });
  suggest.onchange = () => P.outbox.push({ type: 'toggle', name: 'suggest', value: suggest.checked });

  const esc = (s) => String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

  P.show = (item) => {
    P.current = item.id;
    body.innerHTML =
      `<div class="title">${esc(item.title)}</div>` +
      `<div class="text">${esc(item.text)}</div>` +
      `<div class="cmd">${esc(item.command)}</div>` +
      `<div class="row"><button id="run">اجرا</button>` +
      `<button id="copy">کپی</button><button id="close">بستن</button></div>` +
      `<div class="meta">اعتماد: ${esc(item.confidence)}</div>`;
    root.getElementById('run').onclick = () => {
      P.outbox.push({ type: 'run', id: item.id });
      root.querySelectorAll('.row button').forEach(b => { b.disabled = true; });
      stat.textContent = 'در انتظار تأیید…';
    };
    root.getElementById('copy').onclick = () => {
      navigator.clipboard.writeText(item.command).catch(() => {});
      stat.textContent = 'کپی شد';
    };
    root.getElementById('close').onclick = () => {
      P.outbox.push({ type: 'dismiss', id: item.id });
      P.current = null;
      body.innerHTML = '<div class="empty">بسته شد.</div>';
    };
    if (P.collapsed) fold.onclick();
  };

  P.result = (r) => {
    const box = root.querySelector('.meta');
    if (box) {
      box.textContent = (r.ok ? 'انجام شد: ' : 'انجام نشد: ')
        + (r.detail || r.status || '');
      box.style.color = r.ok ? '#8fd08f' : '#e0a0a0';
    }
    root.querySelectorAll('.row button').forEach(b => { b.disabled = b.id === 'run'; });
    stat.textContent = r.ok ? 'موفق' : 'ناموفق';
  };


  P.status = (s) => {
    learn.checked = !!s.learning;
    suggest.checked = !!s.suggesting;
    stat.textContent = s.note || '';
  };

  P.note = (t) => { stat.textContent = String(t || ''); };

  const attach = () => {
    if (!document.getElementById('farsi-panel-host')) {
      (document.body || document.documentElement).appendChild(host);
    }
  };
  attach();
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', attach, { once: true });
  }
})();