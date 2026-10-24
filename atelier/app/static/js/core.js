/*!
 * Atelier Sayyar - core.js
 *
 * window.Atelier: shared client runtime.  No dependencies, no build step.
 * Needs money.js and jalali.js loaded first (base.html does that).
 *
 * Public API
 *   Atelier.fetchJSON(url, opts)      JSON in/out, adds X-CSRFToken, throws AtelierError (Persian message)
 *   Atelier.toast(msg, tone, opts)    tone: success | error | warning | info
 *   Atelier.openModal(id) / closeModal(id)
 *   Atelier.confirm(message, opts)    Promise<boolean> confirm dialog
 *   Atelier.toFaDigits(s) / toLatinDigits(s)
 *   Atelier.formatToman(n, {fa, suffix}) / parseMoney(s)
 *   Atelier.jalali                    toJalali, toGregorian, monthLength, isLeap (+ helpers)
 *   Atelier.init(root)                (re)initialise widgets inside root (call after inserting HTML)
 *   Atelier.setValue(el, v) / getValue(el)   programmatic access to money / stepper / jdate inputs
 *   Atelier.toggleCollapse(id, open)  Atelier.theme.get/set
 *
 * Declarative widgets (see docs/UI_GUIDE.md)
 *   [data-money] [data-jdate] [data-stepper] [data-modal-open] [data-modal-close]
 *   [data-accordion-toggle] [data-confirm] [data-tabs] [data-print] [data-history-back] [data-theme-toggle]
 *
 * Custom events (bubble from the visible input / the element):
 *   atelier:input   {name, value}           money / stepper / jdate value changed
 *   atelier:modal-open / atelier:modal-close
 *   atelier:toggle  {open}                  accordion / collapse
 *   atelier:tab     {key}
 */
(function () {
  'use strict';

  var doc = document;
  var root = doc.documentElement;
  var M = window.AtelierMoney;
  var J = window.AtelierJalali;

  if (!M || !J) {
    // money.js / jalali.js missing: fail loudly in the console, keep the page usable.
    if (window.console) console.error('Atelier: money.js and jalali.js must be loaded before core.js');
    return;
  }

  /* ======================================================================
   * Helpers
   * ==================================================================== */
  function $(sel, ctx) { return (ctx || doc).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || doc).querySelectorAll(sel)); }
  function byId(id) { return typeof id === 'string' ? doc.getElementById(id) : id; }
  function esc(s) { return window.CSS && CSS.escape ? CSS.escape(s) : String(s).replace(/["\\]/g, '\\$&'); }
  function reducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }
  function isPhone() { return !!(window.matchMedia && window.matchMedia('(max-width: 639.98px)').matches); }
  function isCoarse() { return !!(window.matchMedia && window.matchMedia('(pointer: coarse)').matches); }
  function store(key, value) {
    try {
      if (value === undefined) return window.localStorage.getItem(key);
      if (value === null) window.localStorage.removeItem(key);
      else window.localStorage.setItem(key, value);
    } catch (e) { /* storage blocked */ }
    return null;
  }
  function fire(el, name, detail) {
    var ev;
    try {
      ev = new CustomEvent(name, { bubbles: true, detail: detail || {} });
    } catch (e) {
      ev = doc.createEvent('CustomEvent');
      ev.initCustomEvent(name, true, false, detail || {});
    }
    el.dispatchEvent(ev);
  }
  function el(tag, attrs, children) {
    var node = doc.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (k) {
        if (k === 'class') node.className = attrs[k];
        else if (k === 'text') node.textContent = attrs[k];
        else node.setAttribute(k, attrs[k]);
      });
    }
    (children || []).forEach(function (c) { if (c) node.appendChild(c); });
    return node;
  }
  var SVGNS = 'http://www.w3.org/2000/svg';
  function svgIcon(name, cls) {
    var svg = doc.createElementNS(SVGNS, 'svg');
    svg.setAttribute('class', 'icon' + (cls ? ' ' + cls : ''));
    svg.setAttribute('aria-hidden', 'true');
    svg.setAttribute('focusable', 'false');
    var use = doc.createElementNS(SVGNS, 'use');
    use.setAttribute('href', '#i-' + name);
    svg.appendChild(use);
    return svg;
  }
  var FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  function focusables(container) {
    return $$(FOCUSABLE, container).filter(function (n) {
      return !n.hidden && n.getClientRects().length > 0 && getComputedStyle(n).visibility !== 'hidden';
    });
  }
  function trapTab(container, e) {
    var list = focusables(container);
    if (!list.length) { e.preventDefault(); container.focus(); return; }
    var first = list[0];
    var last = list[list.length - 1];
    var active = doc.activeElement;
    if (e.shiftKey && (active === first || !container.contains(active))) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (active === last || !container.contains(active))) { e.preventDefault(); first.focus(); }
  }

  /* ======================================================================
   * Digits, money, dates (thin wrappers over money.js / jalali.js)
   * ==================================================================== */
  function parseMoney(s, opts) { return M.parseMoney(s, opts); }
  function formatToman(n, opts) { return M.formatToman(n, opts); }

  /* ======================================================================
   * fetchJSON
   * ==================================================================== */
  function AtelierError(message, status, data) {
    var err = new Error(message);
    err.name = 'AtelierError';
    err.status = status || 0;
    err.data = data === undefined ? null : data;
    return err;
  }

  function csrfToken() {
    var meta = $('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
  }

  function sameOrigin(url) {
    try { return new URL(url, window.location.href).origin === window.location.origin; }
    catch (e) { return true; }
  }

  var STATUS_MESSAGES = {
    400: 'درخواست نامعتبر است.',
    401: 'نشست شما منقضی شده است؛ دوباره وارد شوید.',
    403: 'دسترسی شما به این بخش مجاز نیست.',
    404: 'مورد درخواستی پیدا نشد.',
    405: 'این عملیات مجاز نیست.',
    409: 'این مورد هم‌زمان تغییر کرده است؛ صفحه را تازه کنید و دوباره تلاش کنید.',
    413: 'حجم فایل یا اطلاعات ارسالی زیاد است.',
    422: 'اطلاعات واردشده معتبر نیست.',
    429: 'تعداد درخواست‌ها زیاد است؛ کمی صبر کنید و دوباره تلاش کنید.'
  };
  function statusMessage(status) {
    if (STATUS_MESSAGES[status]) return STATUS_MESSAGES[status];
    if (status >= 500) return 'خطایی در سرور رخ داد؛ کمی بعد دوباره تلاش کنید.';
    return 'عملیات انجام نشد؛ دوباره تلاش کنید.';
  }

  function isPlainBody(b) {
    if (b === null || typeof b !== 'object') return false;
    if (typeof FormData !== 'undefined' && b instanceof FormData) return false;
    if (typeof Blob !== 'undefined' && b instanceof Blob) return false;
    if (typeof URLSearchParams !== 'undefined' && b instanceof URLSearchParams) return false;
    if (typeof ArrayBuffer !== 'undefined' && (b instanceof ArrayBuffer || ArrayBuffer.isView(b))) return false;
    return true;
  }

  /**
   * fetchJSON(url, {method, json, body, headers, timeout, signal})
   *  - `json` (or a plain-object `body`) is sent as application/json; FormData etc. pass through.
   *  - Same-origin requests carry X-CSRFToken from <meta name="csrf-token">.
   *  - Resolves with the parsed JSON (null for empty bodies); rejects with an AtelierError
   *    whose .message is Persian, .status the HTTP status (0 = network) and .data the parsed body.
   */
  function fetchJSON(url, opts) {
    opts = opts || {};
    var headers = {};
    if (opts.headers) {
      Object.keys(opts.headers).forEach(function (k) { headers[k] = opts.headers[k]; });
    }
    if (!headers.Accept && !headers.accept) headers.Accept = 'application/json';
    var payload = opts.body;
    var hasJson = opts.json !== undefined;
    if (hasJson) payload = JSON.stringify(opts.json);
    else if (isPlainBody(payload)) { payload = JSON.stringify(payload); hasJson = true; }
    if (hasJson && !headers['Content-Type'] && !headers['content-type']) headers['Content-Type'] = 'application/json';

    var method = (opts.method || (payload !== undefined && payload !== null ? 'POST' : 'GET')).toUpperCase();
    if (sameOrigin(url)) {
      var token = csrfToken();
      if (token && !headers['X-CSRFToken']) headers['X-CSRFToken'] = token;
      headers['X-Requested-With'] = 'XMLHttpRequest';
    }

    var controller = typeof AbortController !== 'undefined' ? new AbortController() : null;
    var timeout = opts.timeout === undefined ? 30000 : opts.timeout;
    var timer = null;
    if (controller && timeout) timer = setTimeout(function () { controller.abort(); }, timeout);
    if (controller && opts.signal) {
      if (opts.signal.aborted) controller.abort();
      else opts.signal.addEventListener('abort', function () { controller.abort(); });
    }

    var init = {
      method: method,
      headers: headers,
      credentials: opts.credentials || 'same-origin',
      body: method === 'GET' || method === 'HEAD' ? undefined : payload,
      signal: controller ? controller.signal : undefined
    };

    return fetch(url, init).then(function (res) {
      if (timer) clearTimeout(timer);
      var type = res.headers.get('content-type') || '';
      var parse = res.status === 204 ? Promise.resolve(null)
        : type.indexOf('json') !== -1 ? res.json().catch(function () { return null; })
        : res.text().then(function (t) { return t === '' ? null : t; });
      return parse.then(function (data) {
        if (!res.ok) {
          var msg = data && typeof data === 'object' ? (data.message || data.error || data.detail) : null;
          if (msg && typeof msg === 'object') msg = null;
          throw AtelierError(msg || statusMessage(res.status), res.status, data);
        }
        return data;
      });
    }, function (err) {
      if (timer) clearTimeout(timer);
      var aborted = err && err.name === 'AbortError';
      throw AtelierError(
        aborted ? 'پاسخی از سرور دریافت نشد؛ دوباره تلاش کنید.' : 'اتصال به سرور برقرار نشد؛ اینترنت خود را بررسی کنید.',
        0, null
      );
    });
  }

  /* ======================================================================
   * Toasts
   * ==================================================================== */
  var TONE_ALIASES = { error: 'error', danger: 'error', success: 'success', warning: 'warning', info: 'info', message: 'info' };
  var TONE_ICONS = { success: 'check-circle', error: 'x-circle', warning: 'alert', info: 'info' };
  var TONE_TIMEOUT = { success: 4500, info: 5500, warning: 8000, error: 10000 };
  var MAX_TOASTS = 4;

  function toast(message, tone, opts) {
    opts = opts || {};
    var kind = TONE_ALIASES[tone] || 'info';
    var host = byId('toasts');
    if (!host) {
      host = el('div', { id: 'toasts', class: 'toasts', 'aria-live': 'polite' });
      doc.body.appendChild(host);
    }
    while (host.children.length >= MAX_TOASTS) host.removeChild(host.firstChild);

    var close = el('button', { type: 'button', class: 'icon-btn', 'aria-label': 'بستن پیام' });
    close.appendChild(svgIcon('close'));
    var node = el('div', { class: 'toast toast--' + kind, role: kind === 'error' ? 'alert' : 'status' }, [
      svgIcon(TONE_ICONS[kind]),
      el('div', { class: 'toast__text', text: String(message) }),
      close
    ]);
    host.appendChild(node);

    var timer = null;
    var ms = opts.timeout === undefined ? TONE_TIMEOUT[kind] : opts.timeout;
    function dismiss() {
      if (timer) clearTimeout(timer);
      if (!node.parentNode) return;
      if (reducedMotion()) { node.parentNode.removeChild(node); return; }
      node.classList.add('is-leaving');
      setTimeout(function () { if (node.parentNode) node.parentNode.removeChild(node); }, 220);
    }
    function arm() { if (ms) timer = setTimeout(dismiss, ms); }
    function pause() { if (timer) { clearTimeout(timer); timer = null; } }
    close.addEventListener('click', dismiss);
    node.addEventListener('mouseenter', pause);
    node.addEventListener('mouseleave', arm);
    node.addEventListener('focusin', pause);
    node.addEventListener('focusout', arm);
    arm();
    node.dismiss = dismiss;
    return node;
  }

  function showFlashes() {
    var list = byId('flash-source');
    if (!list) return;
    $$('li', list).forEach(function (li) { toast(li.textContent, li.getAttribute('data-tone')); });
    if (list.parentNode) list.parentNode.removeChild(list);
  }

  /* ======================================================================
   * Modals: stack, focus trap, scroll lock, Esc, backdrop
   * ==================================================================== */
  var modalStack = [];
  var scrollLocks = 0;

  function lockScroll() {
    if (scrollLocks++ > 0) return;
    var gap = window.innerWidth - root.clientWidth;
    doc.body.classList.add('has-modal');
    if (gap > 0) doc.body.style.setProperty('padding-inline-end', gap + 'px');
  }
  function unlockScroll() {
    if (--scrollLocks > 0) return;
    scrollLocks = 0;
    doc.body.classList.remove('has-modal');
    doc.body.style.removeProperty('padding-inline-end');
  }

  function openModal(id) {
    var modal = byId(id);
    if (!modal) return null;
    for (var i = 0; i < modalStack.length; i++) if (modalStack[i].el === modal) return modal;
    var entry = { el: modal, opener: doc.activeElement };
    modalStack.push(entry);
    modal.style.setProperty('--modal-z', String(100 + modalStack.length * 10));
    modal.hidden = false;
    void modal.offsetWidth; // let the browser paint the start state so the transition runs
    modal.classList.add('is-open');
    lockScroll();
    var dialog = $('.modal__dialog', modal) || modal;
    var target = $('[data-autofocus], [autofocus]', modal) || dialog;
    try { target.focus({ preventScroll: true }); } catch (e) { target.focus(); }
    fire(modal, 'atelier:modal-open', { id: modal.id });
    return modal;
  }

  function closeModal(id) {
    var modal = byId(id);
    if (!modal) return;
    var idx = -1;
    for (var i = 0; i < modalStack.length; i++) if (modalStack[i].el === modal) idx = i;
    if (idx === -1) return;
    var entry = modalStack.splice(idx, 1)[0];
    modal.classList.remove('is-open');
    var done = function () { if (!modal.classList.contains('is-open')) modal.hidden = true; };
    if (reducedMotion()) done(); else setTimeout(done, 240);
    unlockScroll();
    if (idx === modalStack.length && entry.opener && doc.body.contains(entry.opener)) {
      try { entry.opener.focus({ preventScroll: true }); } catch (e) { /* element not focusable any more */ }
    }
    fire(modal, 'atelier:modal-close', { id: modal.id });
  }

  function topModal() { return modalStack.length ? modalStack[modalStack.length - 1].el : null; }

  /* Confirm dialog (single, lazily built, reused) ----------------------- */
  var confirmState = null;
  function ensureConfirm() {
    if (confirmState) return confirmState;
    var title = el('h2', { class: 'modal__title', id: 'atelier-confirm-title' });
    var closeBtn = el('button', { type: 'button', class: 'icon-btn', 'data-modal-close': '', 'aria-label': 'بستن' });
    closeBtn.appendChild(svgIcon('close'));
    var message = el('p', { class: 'confirm__message', id: 'atelier-confirm-message' });
    var cancel = el('button', { type: 'button', class: 'btn', 'data-confirm-cancel': '' });
    var ok = el('button', { type: 'button', class: 'btn btn--danger', 'data-confirm-ok': '' });
    var footer = el('div', { class: 'modal__footer' }, [ok, cancel]);
    var dialog = el('div', { class: 'modal__dialog', tabindex: '-1' }, [
      el('header', { class: 'modal__header' }, [title, closeBtn]),
      el('div', { class: 'modal__body' }, [message, footer])
    ]);
    var modal = el('div', {
      class: 'modal modal--sm modal--confirm', id: 'atelier-confirm', role: 'alertdialog', 'aria-modal': 'true',
      'aria-labelledby': 'atelier-confirm-title', 'aria-describedby': 'atelier-confirm-message', hidden: ''
    }, [el('div', { class: 'modal__backdrop', 'data-modal-close': '' }), dialog]);
    doc.body.appendChild(modal);
    confirmState = { modal: modal, title: title, message: message, ok: ok, cancel: cancel, resolve: null };

    function settle(value) {
      var resolve = confirmState.resolve;
      confirmState.resolve = null;
      closeModal(modal);
      if (resolve) resolve(value);
    }
    ok.addEventListener('click', function () { settle(true); });
    cancel.addEventListener('click', function () { settle(false); });
    modal.addEventListener('atelier:modal-close', function () {
      // closed through Esc / backdrop / X: treat as "cancel"
      if (confirmState.resolve) { var r = confirmState.resolve; confirmState.resolve = null; r(false); }
    });
    return confirmState;
  }

  /**
   * Atelier.confirm(message, {title, confirmLabel, cancelLabel, tone}) -> Promise<boolean>
   * tone: 'danger' (default, red confirm button) | 'primary'
   */
  function confirmDialog(message, opts) {
    opts = opts || {};
    var st = ensureConfirm();
    if (st.resolve) { var prev = st.resolve; st.resolve = null; prev(false); }
    st.title.textContent = opts.title || 'تأیید عملیات';
    st.message.textContent = message || 'آیا مطمئن هستید؟';
    st.ok.textContent = opts.confirmLabel || 'تأیید';
    st.cancel.textContent = opts.cancelLabel || 'انصراف';
    st.ok.className = 'btn ' + (opts.tone === 'primary' ? 'btn--primary' : 'btn--danger');
    return new Promise(function (resolve) {
      st.resolve = resolve;
      openModal(st.modal);
      // Focus the safe choice first.
      try { st.cancel.focus({ preventScroll: true }); } catch (e) { st.cancel.focus(); }
    });
  }

  /* ======================================================================
   * Accordion / collapse
   * ==================================================================== */
  function toggleCollapse(id, force) {
    var target = byId(id);
    if (!target) return;
    var isAccordion = target.classList.contains('accordion');
    var panel = isAccordion ? $(':scope > .collapse', target) : target;
    if (!panel) return;
    var open = force === undefined ? !target.classList.contains('is-open') : !!force;
    if (open) {
      var group = target.getAttribute('data-accordion-group');
      if (group) {
        $$('.accordion[data-accordion-group="' + esc(group) + '"].is-open').forEach(function (other) {
          if (other !== target) toggleCollapse(other.id, false);
        });
      }
    }
    target.classList.toggle('is-open', open);
    panel.classList.toggle('is-open', open);
    $$('[data-accordion-toggle="' + esc(target.id) + '"]').forEach(function (btn) {
      btn.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    fire(target, 'atelier:toggle', { open: open, id: target.id });
  }

  /* ======================================================================
   * Tabs (client-side)
   * ==================================================================== */
  function selectTab(tab, focus) {
    var host = tab.closest('[data-tabs]');
    if (!host) return;
    var key = tab.getAttribute('data-tab');
    $$('[data-tab]', host).forEach(function (t) {
      if (t.closest('[data-tabs]') !== host) return;
      var on = t === tab;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.setAttribute('tabindex', on ? '0' : '-1');
    });
    $$('[data-tab-panel]', host).forEach(function (p) {
      if (p.closest('[data-tabs]') !== host) return;
      p.hidden = p.getAttribute('data-tab-panel') !== key;
    });
    if (focus) tab.focus();
    fire(host, 'atelier:tab', { key: key });
  }

  /* ======================================================================
   * Numeric / date widgets: shared plumbing
   * ==================================================================== */
  // The visible input loses its name; a hidden input with the original name carries the raw value.
  function splitName(input) {
    // A widget cloned after initialisation (cloneNode) still carries its previous hidden twin: drop it.
    var twin = input.previousElementSibling;
    if (twin && twin.hasAttribute('data-atelier-hidden')) twin.parentNode.removeChild(twin);
    var hidden = doc.createElement('input');
    hidden.type = 'hidden';
    hidden.setAttribute('data-atelier-hidden', '');
    var name = input.getAttribute('name') || input.getAttribute('data-name');
    if (name) {
      hidden.name = name;
      input.setAttribute('data-name', name);
      input.removeAttribute('name');
    }
    var formAttr = input.getAttribute('form');
    if (formAttr) hidden.setAttribute('form', formAttr);
    input.parentNode.insertBefore(hidden, input);
    return hidden;
  }

  function onFormReset(input, restore) {
    var form = input.form;
    if (!form) return;
    form.addEventListener('reset', function () { setTimeout(restore, 0); });
  }

  function countDigits(text) {
    var n = 0;
    for (var i = 0; i < text.length; i++) if (/[0-9\u06f0-\u06f9\u0660-\u0669\uff10-\uff19]/.test(text.charAt(i))) n++;
    return n;
  }
  function caretAfterDigits(text, digits) {
    if (digits <= 0) return 0;
    var seen = 0;
    for (var i = 0; i < text.length; i++) {
      if (/[0-9\u06f0-\u06f9\u0660-\u0669\uff10-\uff19]/.test(text.charAt(i))) {
        seen++;
        if (seen === digits) return i + 1;
      }
    }
    return text.length;
  }
  function setCaret(input, pos) {
    if (doc.activeElement !== input) return;
    try { input.setSelectionRange(pos, pos); } catch (e) { /* type not selectable */ }
  }

  /* -- [data-money] ------------------------------------------------------ */
  var MAX_MONEY_DIGITS = String(M.MAX_TOMAN).length;

  function moneyRaw(text, allowNegative) {
    var latin = M.normalizeDigits(text);
    var negative = allowNegative && /^\s*[-\u2212\u2010\u2011\u2012\u2013]/.test(latin);
    var digits = latin.replace(/\D/g, '').replace(/^0+(?=\d)/, '');
    if (digits.length > MAX_MONEY_DIGITS) digits = digits.slice(0, MAX_MONEY_DIGITS);
    if (digits === '') return negative ? '-' : '';
    return (negative && digits !== '0' ? '-' : '') + digits;
  }
  function moneyDisplay(raw) {
    if (raw === '' ) return '';
    if (raw === '-') return '-';
    var neg = raw.charAt(0) === '-';
    var body = M.toPersianDigits(M.groupDigits(neg ? raw.slice(1) : raw)).replace(/,/g, '\u066c');
    return (neg ? '-' : '') + body;
  }

  function initMoney(input) {
    if (input.__aMoney) return;
    input.__aMoney = true;
    var negative = input.hasAttribute('data-negative');
    var hidden = splitName(input);
    input.setAttribute('inputmode', negative ? 'text' : 'numeric');
    input.setAttribute('autocomplete', 'off');
    if (!input.getAttribute('dir')) input.setAttribute('dir', 'ltr');
    input.__aHidden = hidden;
    var initial = input.defaultValue;

    function bound(attr, fallback) {
      var v = input.getAttribute(attr);
      return v === null || v === '' || isNaN(Number(v)) ? fallback : Number(v);
    }
    function validate(raw) {
      var n = raw === '' || raw === '-' ? null : Number(raw);
      var msg = '';
      if (n !== null) {
        var min = bound('data-min', negative ? -M.MAX_TOMAN : 0);
        var max = bound('data-max', M.MAX_TOMAN);
        if (n < min) msg = 'مبلغ نباید کمتر از ' + M.formatToman(min) + ' تومان باشد.';
        else if (n > max) msg = 'مبلغ نباید بیشتر از ' + M.formatToman(max) + ' تومان باشد.';
      }
      input.setCustomValidity(msg);
      return n;
    }
    // Write the canonical display text, keeping the caret after the same number of digits.
    function apply(raw, keepCaret) {
      var caret = input.selectionStart;
      var digitsBefore = keepCaret && caret !== null ? countDigits(input.value.slice(0, caret)) : null;
      var display = moneyDisplay(raw);
      if (input.value !== display) input.value = display;
      if (digitsBefore !== null) {
        var pos = caretAfterDigits(display, digitsBefore);
        if (digitsBefore === 0 && display.charAt(0) === '-') pos = 1;
        setCaret(input, pos);
      }
      hidden.value = raw === '-' ? '' : raw;
      return validate(raw);
    }
    function set(value, silent) {
      var raw = '';
      if (typeof value === 'number') raw = String(value);
      else if (value !== null && value !== undefined && String(value).trim() !== '') {
        var n = M.parseMoney(value, { allowNegative: negative });
        raw = n === null ? moneyRaw(String(value), negative) : String(n);
      }
      var parsed = apply(raw, false);
      if (!silent) fire(input, 'atelier:input', { name: input.getAttribute('data-name'), value: parsed });
    }
    input.__aSet = set;
    input.__aGet = function () { return hidden.value === '' ? null : Number(hidden.value); };

    input.addEventListener('input', function () {
      var n = apply(moneyRaw(input.value, negative), true);
      fire(input, 'atelier:input', { name: input.getAttribute('data-name'), value: n });
    });
    input.addEventListener('blur', function () {
      if (input.value === '-') apply('', false);
    });
    set(initial, true);
    onFormReset(input, function () { set(initial, true); });
  }

  /* -- [data-stepper] ---------------------------------------------------- */
  // Accepts a ready-made .stepper wrapper (ui.html stepper_field) or a bare <input data-stepper>.
  function buildStepper(inputEl) {
    var wrapper = el('div', { class: 'stepper' });
    inputEl.parentNode.insertBefore(wrapper, inputEl);
    var dec = el('button', { type: 'button', class: 'stepper__btn', 'data-stepper-dec': '', 'aria-label': 'کاهش' }, [svgIcon('minus')]);
    var inc = el('button', { type: 'button', class: 'stepper__btn', 'data-stepper-inc': '', 'aria-label': 'افزایش' }, [svgIcon('plus')]);
    wrapper.appendChild(dec);
    wrapper.appendChild(inputEl);
    wrapper.appendChild(inc);
    inputEl.classList.add('stepper__input');
    ['min', 'max', 'step'].forEach(function (a) {
      var v = inputEl.getAttribute('data-' + a) !== null ? inputEl.getAttribute('data-' + a) : inputEl.getAttribute(a);
      if (v !== null) wrapper.setAttribute('data-' + a, v);
      inputEl.removeAttribute(a);
    });
    if (inputEl.hasAttribute('data-default')) wrapper.setAttribute('data-default', inputEl.getAttribute('data-default'));
    wrapper.setAttribute('data-stepper', '');
    inputEl.removeAttribute('data-stepper');
    if (inputEl.type !== 'text') inputEl.type = 'text';
    return wrapper;
  }

  function initStepper(host) {
    var input = host.tagName === 'INPUT' ? host : $('input', host);
    if (!input || input.__aStepper) return;
    var wrapper = host.tagName === 'INPUT' ? buildStepper(host) : host;
    input.__aStepper = true;
    input.setAttribute('inputmode', 'numeric');
    input.setAttribute('autocomplete', 'off');
    var hidden = splitName(input);
    var dec = $('[data-stepper-dec]', wrapper);
    var inc = $('[data-stepper-inc]', wrapper);
    var initial = input.defaultValue;

    function num(attr, fallback) {
      var v = wrapper.getAttribute(attr);
      return v === null || v === '' || isNaN(Number(v)) ? fallback : Number(v);
    }
    function min() { return num('data-min', 0); }
    function max() { return num('data-max', 999999); }
    function step() { return Math.max(1, num('data-step', 1)); }
    function dflt() { return Math.min(max(), Math.max(min(), num('data-default', 1))); }
    function clamp(n) { return Math.min(max(), Math.max(min(), n)); }
    function parse(text) {
      var digits = M.normalizeDigits(text).replace(/[^\d-]/g, '');
      if (digits === '' || digits === '-') return null;
      var n = parseInt(digits, 10);
      return isNaN(n) ? null : n;
    }
    function show(n, silent) {
      input.value = M.toPersianDigits(String(n));
      hidden.value = String(n);
      if (dec) dec.disabled = n <= min();
      if (inc) inc.disabled = n >= max();
      if (!silent) fire(input, 'atelier:input', { name: input.getAttribute('data-name'), value: n });
    }
    function current() { var n = parse(hidden.value); return n === null ? dflt() : n; }
    function set(v, silent) {
      var n = typeof v === 'number' ? v : parse(String(v === null || v === undefined ? '' : v));
      show(clamp(n === null || isNaN(n) ? dflt() : n), silent);
    }
    input.__aSet = set;
    input.__aGet = current;

    function bump(dir) { set(current() + dir * step()); }
    var delay = null;
    var timer = null;
    function stop() { clearTimeout(delay); clearInterval(timer); delay = timer = null; }
    function hold(btn, dir) {
      btn.addEventListener('click', function (e) {
        if (!btn.__aHeld || e.detail === 0) bump(dir); // a tap or keyboard press; a long press already repeated
        btn.__aHeld = false;
      });
      btn.addEventListener('pointerdown', function () {
        btn.__aHeld = false;
        stop();
        delay = setTimeout(function () {
          timer = setInterval(function () { btn.__aHeld = true; bump(dir); }, 90);
        }, 450);
      });
      ['pointerup', 'pointerleave', 'pointercancel', 'blur'].forEach(function (ev) { btn.addEventListener(ev, stop); });
    }
    if (dec) hold(dec, -1);
    if (inc) hold(inc, +1);

    input.addEventListener('input', function () {
      var n = parse(input.value);
      if (n === null) { hidden.value = ''; return; }
      show(clamp(n));
    });
    input.addEventListener('blur', function () { set(parse(input.value)); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowUp') { e.preventDefault(); bump(+1); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); bump(-1); }
    });
    set(initial === '' ? dflt() : initial, true);
    onFormReset(input, function () { set(initial === '' ? dflt() : initial, true); });
  }

  /* -- [data-jdate] ------------------------------------------------------ */
  var JD_PLACEHOLDER = '\u06f1\u06f4\u06f0\u06f4/\u06f0\u06f1/\u06f0\u06f1';
  var openPicker = null;

  function isoOf(g) { return g ? J.isoString(g[0], g[1], g[2]) : ''; }
  function jalaliText(g) {
    if (!g) return '';
    var j = J.toJalali(g[0], g[1], g[2]);
    return J.format(j[0], j[1], j[2], 'numeric', { fa: true });
  }
  function cmpG(a, b) { return a[0] - b[0] || a[1] - b[1] || a[2] - b[2]; }
  function addDays(g, n) {
    var d = new Date(Date.UTC(g[0], g[1] - 1, g[2] + n));
    return [d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate()];
  }

  function initJdate(input) {
    if (input.__aJdate) return;
    input.__aJdate = true;
    var hidden = splitName(input);
    input.setAttribute('autocomplete', 'off');
    input.setAttribute('inputmode', isCoarse() ? 'none' : 'numeric');
    if (!input.getAttribute('dir')) input.setAttribute('dir', 'ltr');
    if (!input.getAttribute('placeholder')) input.setAttribute('placeholder', JD_PLACEHOLDER);
    input.__aHidden = hidden;
    var initial = input.defaultValue;
    var current = null; // [gy, gm, gd] | null

    function limits() {
      return { min: J.parseISO(input.getAttribute('data-min') || ''), max: J.parseISO(input.getAttribute('data-max') || '') };
    }
    // Native validity message: invalid text, or outside data-min / data-max.
    function validity(g, text) {
      var msg = '';
      if (text && !g) msg = 'تاریخ نامعتبر است؛ نمونه: ' + JD_PLACEHOLDER;
      else if (g) {
        var l = limits();
        if (l.min && cmpG(g, l.min) < 0) msg = 'تاریخ نباید قبل از ' + jalaliText(l.min) + ' باشد.';
        else if (l.max && cmpG(g, l.max) > 0) msg = 'تاریخ نباید بعد از ' + jalaliText(l.max) + ' باشد.';
      }
      input.setCustomValidity(msg);
    }
    // Make `g` ([gy, gm, gd] or null) the value: hidden ISO field, visible Jalali text, validity.
    function commit(g, silent) {
      current = g;
      hidden.value = isoOf(g);
      input.value = jalaliText(g);
      validity(g, '');
      input.removeAttribute('aria-invalid');
      if (!silent) {
        fire(input, 'atelier:input', { name: input.getAttribute('data-name'), value: hidden.value });
        input.dispatchEvent(new Event('change', { bubbles: true }));
      }
    }
    function setValue(v, silent) {
      var g = null;
      if (Array.isArray(v)) g = v;
      else if (v) g = J.parseISO(String(v)) || J.parse(String(v));
      commit(g, silent);
    }
    input.__aSet = setValue;
    input.__aGet = function () { return hidden.value || null; };
    input.__aOpen = function (focusGrid) { openJdatePicker(input, current, focusGrid, commit); };

    setValue(initial, true);
    onFormReset(input, function () { setValue(initial, true); });

    // Typing: digits become Persian, the hidden ISO value follows as soon as the text is a valid date.
    input.addEventListener('input', function () {
      var pos = input.selectionStart;
      var fa = J.toFa(J.toLatin(input.value));
      if (fa !== input.value) { input.value = fa; setCaret(input, pos); }
      var text = input.value.trim();
      var g = text === '' ? null : J.parse(text);
      current = g;
      hidden.value = isoOf(g);
      validity(g, text);
      if (g || text === '') input.removeAttribute('aria-invalid');
      fire(input, 'atelier:input', { name: input.getAttribute('data-name'), value: hidden.value });
    });
    // Leaving the field: normalise valid text (1404/1/5 -> ۱۴۰۴/۰۱/۰۵), flag invalid text.
    input.addEventListener('blur', function () {
      var text = input.value.trim();
      if (text === '') { commit(null, true); return; }
      var g = J.parse(text);
      if (g) commit(g, true); else input.setAttribute('aria-invalid', 'true');
    });
    input.addEventListener('click', function () {
      if (input.readOnly || input.disabled) return;
      input.__aOpen(isCoarse());
    });
    input.addEventListener('keydown', function (e) {
      if (input.readOnly || input.disabled) return;
      if (e.key === 'ArrowDown' || (e.altKey && e.key === 'ArrowUp')) { e.preventDefault(); input.__aOpen(true); }
      else if (e.key === 'Tab' && openPicker) closePicker(false);
    });
  }

  function closePicker(refocus) {
    var p = openPicker;
    if (!p) return;
    openPicker = null;
    doc.removeEventListener('pointerdown', p.onDown, true);
    window.removeEventListener('resize', p.onResize);
    window.removeEventListener('scroll', p.onResize, true);
    if (p.pop.parentNode) p.pop.parentNode.removeChild(p.pop);
    if (p.scrim && p.scrim.parentNode) p.scrim.parentNode.removeChild(p.scrim);
    p.input.setAttribute('aria-expanded', 'false');
    if (refocus) { try { p.input.focus({ preventScroll: true }); } catch (e) { p.input.focus(); } }
  }

  function openJdatePicker(input, selected, focusGrid, commit) {
    if (openPicker && openPicker.input === input) return;
    closePicker(false);

    var todayJ = J.todayJalali();
    var todayG = J.todayGregorian();
    var lim = {
      min: J.parseISO(input.getAttribute('data-min') || ''),
      max: J.parseISO(input.getAttribute('data-max') || '')
    };
    // Open on the selected day, else today - clamped into [min, max] so the grid is never all disabled
    // (e.g. a birth-date field whose max is years in the past).
    var base = selected || todayG;
    if (lim.max && cmpG(base, lim.max) > 0) base = lim.max;
    if (lim.min && cmpG(base, lim.min) < 0) base = lim.min;
    var focusG = base;
    var view = J.toJalali(base[0], base[1], base[2]); // [jy, jm, jd]
    var viewY = view[0];
    var viewM = view[1];

    var yearsBack = parseInt(input.getAttribute('data-years-back') || '5', 10);
    var yearsAhead = parseInt(input.getAttribute('data-years-ahead') || '10', 10);
    var yMin = lim.min ? J.toJalali(lim.min[0], lim.min[1], lim.min[2])[0] : todayJ[0] - yearsBack;
    var yMax = lim.max ? J.toJalali(lim.max[0], lim.max[1], lim.max[2])[0] : todayJ[0] + yearsAhead;

    var pop = el('div', { class: 'jd-pop', role: 'dialog', 'aria-label': 'انتخاب تاریخ' });
    var scrim = null;

    var prev = el('button', { type: 'button', class: 'jd-pop__nav', 'aria-label': 'ماه قبل' }, [svgIcon('chevron-right')]);
    var next = el('button', { type: 'button', class: 'jd-pop__nav', 'aria-label': 'ماه بعد' }, [svgIcon('chevron-left')]);
    var monthSel = el('select', { class: 'select', 'aria-label': 'ماه' });
    J.MONTHS.forEach(function (name, i) {
      monthSel.appendChild(el('option', { value: String(i + 1), text: name }));
    });
    var yearSel = el('select', { class: 'select', 'aria-label': 'سال' });
    var dow = el('div', { class: 'jd-pop__dow', 'aria-hidden': 'true' });
    J.WEEKDAYS_SHORT.forEach(function (d) { dow.appendChild(el('span', { text: d })); });
    var grid = el('div', { class: 'jd-pop__grid', role: 'grid', 'aria-label': 'روزهای ماه' });
    var todayBtn = el('button', { type: 'button', class: 'btn btn--sm btn--secondary', text: 'امروز' });
    var clearBtn = el('button', { type: 'button', class: 'btn btn--sm', text: 'پاک کردن' });
    var closeBtn = el('button', { type: 'button', class: 'btn btn--sm btn--ghost', text: 'بستن' });
    var foot = el('div', { class: 'jd-pop__foot' }, [todayBtn, clearBtn, closeBtn]);
    pop.appendChild(el('div', { class: 'jd-pop__head' }, [prev, el('div', { class: 'jd-pop__selects' }, [monthSel, yearSel]), next]));
    pop.appendChild(dow);
    pop.appendChild(grid);
    pop.appendChild(foot);

    function fillYears() {
      var lo = Math.min(yMin, viewY);
      var hi = Math.max(yMax, viewY);
      while (yearSel.firstChild) yearSel.removeChild(yearSel.firstChild);
      for (var y = lo; y <= hi; y++) yearSel.appendChild(el('option', { value: String(y), text: M.toPersianDigits(String(y)) }));
    }

    function render(focusDay) {
      var len = J.monthLength(viewY, viewM);
      var first = J.toGregorian(viewY, viewM, 1);
      var lead = J.weekdayIndex(first[0], first[1], first[2]);
      monthSel.value = String(viewM);
      if (!yearSel.querySelector('option[value="' + viewY + '"]')) fillYears();
      yearSel.value = String(viewY);
      while (grid.firstChild) grid.removeChild(grid.firstChild);
      for (var i = 0; i < lead; i++) grid.appendChild(el('span', { 'aria-hidden': 'true' }));
      var focusJ = J.toJalali(focusG[0], focusG[1], focusG[2]);
      var focusInView = focusJ[0] === viewY && focusJ[1] === viewM;
      var focusBtn = null;
      for (var d = 1; d <= len; d++) {
        var g = J.toGregorian(viewY, viewM, d);
        var col = (lead + d - 1) % 7;
        var cls = 'jd-day';
        var isSel = selected && cmpG(g, selected) === 0;
        var isToday = g[0] === todayG[0] && g[1] === todayG[1] && g[2] === todayG[2];
        if (col === 6) cls += ' is-weekend';
        if (isToday) cls += ' is-today';
        if (isSel) cls += ' is-selected';
        var label = J.WEEKDAYS[col] + ' ' + M.toPersianDigits(d + ' ' + J.MONTHS[viewM - 1] + ' ' + viewY);
        var btn = el('button', {
          type: 'button', class: cls, text: M.toPersianDigits(String(d)), 'data-d': String(d), 'aria-label': label,
          tabindex: focusInView && focusJ[2] === d ? '0' : '-1'
        });
        if (isSel) btn.setAttribute('aria-pressed', 'true');
        if (isToday) btn.setAttribute('aria-current', 'date');
        if ((lim.min && cmpG(g, lim.min) < 0) || (lim.max && cmpG(g, lim.max) > 0)) btn.disabled = true;
        if (focusInView && focusJ[2] === d) focusBtn = btn;
        grid.appendChild(btn);
      }
      if (!focusBtn) {
        focusBtn = grid.querySelector('.jd-day:not(:disabled)');
        if (focusBtn) focusBtn.setAttribute('tabindex', '0');
      }
      if (focusDay && focusBtn) focusBtn.focus({ preventScroll: true });
    }

    function gotoMonth(delta) {
      var m = viewM + delta;
      var y = viewY;
      while (m < 1) { m += 12; y--; }
      while (m > 12) { m -= 12; y++; }
      if (y < J.MIN_YEAR || y > J.MAX_YEAR) return;
      viewY = y; viewM = m;
      var day = Math.min(J.toJalali(focusG[0], focusG[1], focusG[2])[2], J.monthLength(y, m));
      focusG = J.toGregorian(y, m, day);
      render(false);
    }
    function pick(g) {
      commit(g);
      closePicker(true);
    }
    function moveFocus(g) {
      if (lim.min && cmpG(g, lim.min) < 0) return;
      if (lim.max && cmpG(g, lim.max) > 0) return;
      focusG = g;
      var j = J.toJalali(g[0], g[1], g[2]);
      viewY = j[0]; viewM = j[1];
      render(true);
    }

    prev.addEventListener('click', function () { gotoMonth(-1); });
    next.addEventListener('click', function () { gotoMonth(1); });
    monthSel.addEventListener('change', function () { viewM = parseInt(monthSel.value, 10); gotoMonth(0); });
    yearSel.addEventListener('change', function () { viewY = parseInt(yearSel.value, 10); gotoMonth(0); });
    todayBtn.disabled = (lim.min && cmpG(todayG, lim.min) < 0) || (lim.max && cmpG(todayG, lim.max) > 0);
    todayBtn.addEventListener('click', function () { pick(todayG); });
    clearBtn.addEventListener('click', function () { commit(null); closePicker(true); });
    closeBtn.addEventListener('click', function () { closePicker(true); });
    grid.addEventListener('click', function (e) {
      var b = e.target.closest('.jd-day');
      if (!b || b.disabled) return;
      pick(J.toGregorian(viewY, viewM, parseInt(b.getAttribute('data-d'), 10)));
    });
    pop.addEventListener('keydown', function (e) {
      if (e.key === 'Tab') { trapTab(pop, e); return; }
      var inGrid = e.target && e.target.classList && e.target.classList.contains('jd-day');
      if (!inGrid) return;
      var d = parseInt(e.target.getAttribute('data-d'), 10);
      var cur = J.toGregorian(viewY, viewM, d);
      var rtl = true; // the whole UI is RTL: days run right-to-left
      var step = null;
      if (e.key === 'ArrowLeft') step = rtl ? 1 : -1;
      else if (e.key === 'ArrowRight') step = rtl ? -1 : 1;
      else if (e.key === 'ArrowDown') step = 7;
      else if (e.key === 'ArrowUp') step = -7;
      else if (e.key === 'Home') step = -J.weekdayIndex(cur[0], cur[1], cur[2]);
      else if (e.key === 'End') step = 6 - J.weekdayIndex(cur[0], cur[1], cur[2]);
      else if (e.key === 'PageDown' || e.key === 'PageUp') {
        e.preventDefault();
        var delta = (e.key === 'PageDown' ? 1 : -1) * (e.shiftKey ? 12 : 1);
        var m = viewM + delta, y = viewY;
        while (m < 1) { m += 12; y--; }
        while (m > 12) { m -= 12; y++; }
        var nd = Math.min(d, J.monthLength(y, m));
        moveFocus(J.toGregorian(y, m, nd));
        return;
      }
      if (step !== null) { e.preventDefault(); moveFocus(addDays(cur, step)); }
    });

    function place() {
      if (isPhone()) {
        pop.style.left = '';
        pop.style.top = '';
        return;
      }
      var r = input.getBoundingClientRect();
      var pw = pop.offsetWidth;
      var ph = pop.offsetHeight;
      var left = Math.min(Math.max(8, r.right - pw), window.innerWidth - pw - 8);
      var top = r.bottom + 6;
      if (top + ph > window.innerHeight - 8 && r.top - ph - 6 > 8) top = r.top - ph - 6;
      pop.style.left = left + 'px';
      pop.style.top = Math.max(8, top) + 'px';
    }

    fillYears();
    render(false);
    if (isPhone()) {
      scrim = el('div', { class: 'jd-scrim' });
      scrim.addEventListener('click', function () { closePicker(true); });
      doc.body.appendChild(scrim);
    }
    doc.body.appendChild(pop);
    place();
    input.setAttribute('aria-expanded', 'true');

    var state = {
      input: input, pop: pop, scrim: scrim,
      onDown: function (e) {
        if (pop.contains(e.target) || e.target === input || (e.target.closest && e.target.closest('[data-jdate-open]'))) return;
        closePicker(false);
      },
      onResize: place
    };
    openPicker = state;
    doc.addEventListener('pointerdown', state.onDown, true);
    window.addEventListener('resize', state.onResize);
    window.addEventListener('scroll', state.onResize, true);
    if (focusGrid) render(true);
  }

  /* ======================================================================
   * init + value access
   * ==================================================================== */
  function init(scope) {
    scope = scope || doc;
    $$('[data-money]', scope).forEach(initMoney);
    $$('[data-stepper]', scope).forEach(initStepper);
    $$('[data-jdate]', scope).forEach(initJdate);
    if (scope.nodeType === 1) {
      if (scope.matches && scope.matches('[data-money]')) initMoney(scope);
      if (scope.matches && scope.matches('[data-stepper]')) initStepper(scope);
      if (scope.matches && scope.matches('[data-jdate]')) initJdate(scope);
    }
  }

  function widgetInput(target) {
    var node = typeof target === 'string'
      ? (byId(target) || $(target) || $('[data-name="' + esc(target) + '"]'))
      : target;
    if (!node) return null;
    if (node.__aSet) return node;
    var inner = node.querySelector ? $('[data-money], [data-jdate], .stepper__input', node) : null;
    return inner && inner.__aSet ? inner : null;
  }
  /** Set a money / stepper / jdate input from code: Atelier.setValue('#amount', 1870000). */
  function setValue(target, value) {
    var input = widgetInput(target);
    if (!input) return false;
    input.__aSet(value);
    return true;
  }
  /** Raw value of a widget: integer for money / stepper, ISO date string for jdate, null when empty. */
  function getValue(target) {
    var input = widgetInput(target);
    return input ? input.__aGet() : null;
  }

  /* ======================================================================
   * Theme, drawer, sidebar
   * ==================================================================== */
  var theme = {
    get: function () { return root.getAttribute('data-theme') || 'auto'; },
    effective: function () {
      var t = root.getAttribute('data-theme');
      if (t) return t;
      return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    },
    set: function (value) {
      if (value === 'light' || value === 'dark') {
        root.setAttribute('data-theme', value);
        store('atelier:theme', value);
      } else {
        root.removeAttribute('data-theme');
        store('atelier:theme', null);
      }
      fire(root, 'atelier:theme', { theme: value });
    }
  };

  var drawerOpener = null;
  function drawerOpen(opener) {
    drawerOpener = opener || null;
    root.classList.add('is-drawer-open');
    $$('[data-drawer-open]').forEach(function (b) { b.setAttribute('aria-expanded', 'true'); });
    var sidebar = byId('sidebar');
    if (sidebar) {
      var link = $('.nav__link.is-active', sidebar) || $('.nav__link', sidebar);
      if (link) setTimeout(function () { link.focus({ preventScroll: true }); }, 60);
    }
  }
  function drawerClose(refocus) {
    if (!root.classList.contains('is-drawer-open')) return;
    root.classList.remove('is-drawer-open');
    $$('[data-drawer-open]').forEach(function (b) { b.setAttribute('aria-expanded', 'false'); });
    if (refocus && drawerOpener) { try { drawerOpener.focus({ preventScroll: true }); } catch (e) { /* gone */ } }
    drawerOpener = null;
  }

  function syncSidebarToggle() {
    var collapsed = root.classList.contains('sb-collapsed');
    $$('[data-sidebar-toggle]').forEach(function (b) { b.setAttribute('aria-expanded', collapsed ? 'false' : 'true'); });
  }

  /* ======================================================================
   * Double-submit protection
   * ==================================================================== */
  function lockForm(form, submitter) {
    if (form.__aLocked) return;
    form.__aLocked = true;
    form.setAttribute('aria-busy', 'true');
    var main = submitter && submitter.tagName ? submitter : form.querySelector('button[type="submit"], input[type="submit"], button:not([type])');
    if (main && main.classList) main.classList.add('is-loading');
    setTimeout(function () {
      $$('button[type="submit"], input[type="submit"], button:not([type])', form).forEach(function (b) { b.disabled = true; });
    }, 0);
    function unlock() {
      form.__aLocked = false;
      form.removeAttribute('aria-busy');
      $$('button[type="submit"], input[type="submit"], button:not([type])', form).forEach(function (b) {
        b.disabled = false;
        b.classList.remove('is-loading');
      });
    }
    form.__aUnlock = unlock;
    setTimeout(unlock, 15000); // e.g. a file download never navigates away
  }

  /* ======================================================================
   * Global event wiring
   * ==================================================================== */
  function wire() {
    // Confirm: capture phase so it runs before any other click handler on the control.
    doc.addEventListener('click', function (e) {
      var ctl = e.target.closest && e.target.closest('[data-confirm]');
      if (!ctl || ctl.tagName === 'FORM') return;
      if (ctl.__aConfirmed) { ctl.__aConfirmed = false; return; }
      e.preventDefault();
      e.stopImmediatePropagation();
      confirmDialog(ctl.getAttribute('data-confirm'), {
        title: ctl.getAttribute('data-confirm-title') || undefined,
        confirmLabel: ctl.getAttribute('data-confirm-label') || undefined,
        cancelLabel: ctl.getAttribute('data-confirm-cancel') || undefined,
        tone: ctl.getAttribute('data-confirm-tone') || 'danger'
      }).then(function (ok) {
        if (!ok) return;
        var form = ctl.form || ctl.closest('form');
        if (ctl.tagName === 'A') { window.location.href = ctl.href; return; }
        if (form && (ctl.type === 'submit' || !ctl.type)) {
          if (form.requestSubmit) form.requestSubmit(ctl.type === 'submit' ? ctl : undefined);
          else form.submit();
        } else {
          ctl.__aConfirmed = true;
          ctl.click();
        }
      });
    }, true);

    // <form data-confirm="..."> asks on submit.
    doc.addEventListener('submit', function (e) {
      var form = e.target;
      if (!form.hasAttribute || !form.hasAttribute('data-confirm') || form.__aConfirmed) { if (form.__aConfirmed) form.__aConfirmed = false; return; }
      e.preventDefault();
      e.stopImmediatePropagation();
      confirmDialog(form.getAttribute('data-confirm'), {
        title: form.getAttribute('data-confirm-title') || undefined,
        confirmLabel: form.getAttribute('data-confirm-label') || undefined,
        tone: form.getAttribute('data-confirm-tone') || 'danger'
      }).then(function (ok) {
        if (!ok) return;
        form.__aConfirmed = true;
        if (form.requestSubmit) form.requestSubmit(); else form.submit();
      });
    }, true);

    // Double-submit lock (bubble phase: modules that handle the submit themselves call preventDefault first).
    doc.addEventListener('submit', function (e) {
      var form = e.target;
      if (e.defaultPrevented || !form.tagName || form.tagName !== 'FORM') return;
      if ((form.method || '').toLowerCase() !== 'post' || form.hasAttribute('data-no-lock') || form.target === '_blank') return;
      if (form.__aLocked) { e.preventDefault(); return; }
      lockForm(form, e.submitter);
    });
    window.addEventListener('pageshow', function (e) {
      if (e.persisted) $$('form[aria-busy]').forEach(function (f) { if (f.__aUnlock) f.__aUnlock(); });
    });

    doc.addEventListener('click', function (e) {
      var t = e.target;
      if (!t.closest) return;
      var openBtn = t.closest('[data-modal-open]');
      if (openBtn) { e.preventDefault(); openModal(openBtn.getAttribute('data-modal-open')); return; }
      var closeBtn = t.closest('[data-modal-close]');
      if (closeBtn) {
        var id = closeBtn.getAttribute('data-modal-close');
        var modal = id ? byId(id) : closeBtn.closest('.modal');
        if (modal) { e.preventDefault(); closeModal(modal); }
        return;
      }
      var acc = t.closest('[data-accordion-toggle]');
      if (acc) { e.preventDefault(); toggleCollapse(acc.getAttribute('data-accordion-toggle')); return; }
      var tab = t.closest('[data-tab]');
      if (tab) { e.preventDefault(); selectTab(tab, false); return; }
      var jdOpen = t.closest('[data-jdate-open]');
      if (jdOpen) {
        var field = jdOpen.parentNode.querySelector('[data-jdate]');
        if (field && field.__aOpen && !field.disabled && !field.readOnly) {
          if (openPicker && openPicker.input === field) closePicker(false); else field.__aOpen(true);
        }
        return;
      }
      if (t.closest('[data-print]')) { e.preventDefault(); window.print(); return; }
      if (t.closest('[data-history-back]')) {
        e.preventDefault();
        if (window.history.length > 1) window.history.back(); else window.location.href = '/';
        return;
      }
      var dOpen = t.closest('[data-drawer-open]');
      if (dOpen) { drawerOpen(dOpen); return; }
      if (t.closest('[data-drawer-close]')) { drawerClose(true); return; }
      if (t.closest('.sidebar .nav__link')) { drawerClose(false); return; }
      var sbt = t.closest('[data-sidebar-toggle]');
      if (sbt) {
        root.classList.toggle('sb-collapsed');
        store('atelier:sidebar', root.classList.contains('sb-collapsed') ? 'collapsed' : null);
        syncSidebarToggle();
        return;
      }
      if (t.closest('[data-theme-toggle]')) { theme.set(theme.effective() === 'dark' ? 'light' : 'dark'); return; }
    });

    doc.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        if (openPicker) { e.preventDefault(); closePicker(true); return; }
        var top = topModal();
        if (top && !top.hasAttribute('data-modal-static')) { e.preventDefault(); closeModal(top); return; }
        if (!top && root.classList.contains('is-drawer-open')) { e.preventDefault(); drawerClose(true); return; }
      }
      if (e.key === 'Tab') {
        var topEl = topModal();
        if (topEl) { trapTab($('.modal__dialog', topEl) || topEl, e); return; }
        if (root.classList.contains('is-drawer-open')) { var sb = byId('sidebar'); if (sb) trapTab(sb, e); }
        return;
      }
      // Roving tabindex for client-side tabs. Arrow keys follow the visual direction (RTL).
      var tabEl = e.target.closest ? e.target.closest('[data-tab]') : null;
      if (tabEl && /^(Arrow(Left|Right)|Home|End)$/.test(e.key)) {
        var host = tabEl.closest('[data-tabs]');
        var tabs = $$('[data-tab]', host).filter(function (t) { return t.closest('[data-tabs]') === host; });
        var i = tabs.indexOf(tabEl);
        var rtl = getComputedStyle(host).direction === 'rtl';
        var forward = e.key === 'ArrowRight' ? !rtl : e.key === 'ArrowLeft' ? rtl : null;
        var nextI = e.key === 'Home' ? 0 : e.key === 'End' ? tabs.length - 1 : (i + (forward ? 1 : -1) + tabs.length) % tabs.length;
        e.preventDefault();
        selectTab(tabs[nextI], true);
      }
    });

    // Keep focus inside the top modal if something else grabs it.
    doc.addEventListener('focusin', function (e) {
      var topEl = topModal();
      if (!topEl) return;
      var dialog = $('.modal__dialog', topEl) || topEl;
      if (!dialog.contains(e.target) && !e.target.closest('.jd-pop, .toasts')) dialog.focus({ preventScroll: true });
    });

    // Leaving the phone layout closes the drawer.
    if (window.matchMedia) {
      var mq = window.matchMedia('(min-width: 900px)');
      var onChange = function (ev) { if (ev.matches) drawerClose(false); };
      if (mq.addEventListener) mq.addEventListener('change', onChange); else if (mq.addListener) mq.addListener(onChange);
    }
  }

  function registerServiceWorker() {
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
    var url = $('meta[name="sw-url"]');
    var scope = $('meta[name="sw-scope"]');
    if (!url || !url.content) return;
    var go = function () {
      navigator.serviceWorker.register(url.content, { scope: scope && scope.content ? scope.content : '/' })
        .catch(function () { /* optional enhancement: ignore (e.g. scope not allowed) */ });
    };
    if (doc.readyState === 'complete') go(); else window.addEventListener('load', go);
  }

  /* ======================================================================
   * Public API + boot
   * ==================================================================== */
  var Atelier = {
    version: '1.0.0',
    fetchJSON: fetchJSON,
    AtelierError: AtelierError,
    toast: toast,
    openModal: openModal,
    closeModal: closeModal,
    confirm: confirmDialog,
    toFaDigits: M.toFaDigits,
    toLatinDigits: M.toLatinDigits,
    formatToman: formatToman,
    parseMoney: parseMoney,
    jalali: J,
    init: init,
    setValue: setValue,
    getValue: getValue,
    toggleCollapse: toggleCollapse,
    theme: theme,
    csrfToken: csrfToken
  };
  window.Atelier = Atelier;

  function boot() {
    root.classList.remove('no-js');
    root.classList.add('js');
    wire();
    init(doc);
    syncSidebarToggle();
    showFlashes();
    registerServiceWorker();
    fire(doc, 'atelier:ready', {});
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
