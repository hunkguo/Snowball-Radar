// 测试用 GM 桥接（模拟油猴 API，供 Playwright 注入）
window.__gm_log = [];
window.__gm_store = window.__gm_store || {};
window.GM_getValue = (k, d) => (k in window.__gm_store) ? window.__gm_store[k] : d;
window.GM_setValue = (k, v) => { window.__gm_store[k] = v; };
window.GM_deleteValue = (k) => { delete window.__gm_store[k]; };
window.GM_listValues = () => Object.keys(window.__gm_store);
window.GM_registerMenuCommand = () => {};
window.GM_addStyle = (css) => {
  const s = document.createElement('style'); s.textContent = css;
  (document.head || document.documentElement).appendChild(s);
};
window.GM_notification = (o) => {
  window.__gm_log.push('NOTIFY: ' + o.title + ' | ' + o.text);
  console.log('NOTIFY', o.title, o.text);
};
window.GM_xmlhttpRequest = (opts) => {
  const xhr = new XMLHttpRequest();
  xhr.open(opts.method, opts.url, true);
  for (const k in (opts.headers || {})) {
    try { xhr.setRequestHeader(k, opts.headers[k]); } catch (e) {}
  }
  xhr.onload = () => opts.onload && opts.onload({ status: xhr.status, responseText: xhr.responseText });
  xhr.onerror = () => opts.onerror && opts.onerror();
  xhr.ontimeout = () => opts.ontimeout && opts.ontimeout();
  xhr.timeout = opts.timeout || 30000;
  xhr.send(opts.data);
};
