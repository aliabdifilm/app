/*
 * Atelier Sayyar - accounting module behaviour (ES5 style, no build step, no inline script).
 *
 *  1. The invoice popup: loads the server-rendered form into the modal, saves it with fetch (a validation error
 *     comes back as the re-rendered form), and adds / removes "+ other size" and extra-expense rows.
 *  2. The live summary: every change posts the whole form to the preview endpoint; the server computes (with
 *     app.services.ledger + payroll) and this file only DISPLAYS the numbers.  It never applies the pay formula.
 *  3. The payroll sheet: selecting lines, the "mark paid" dialog.
 *
 * Hooks: [data-invoice-open] [data-invoice-form] [data-summary] [data-custom-rows] [data-extra-rows]
 *        [data-add-custom] [data-add-extra] [data-remove-row] [data-restore-default] [data-pay-form]
 * Depends on core.js (window.Atelier).
 */
(function () {
  'use strict';

  var A = window.Atelier;
  if (!A) return;
  var doc = document;
  var MODAL_ID = 'invoice-modal';

  function $(sel, ctx) { return (ctx || doc).querySelector(sel); }
  function $$(sel, ctx) { return Array.prototype.slice.call((ctx || doc).querySelectorAll(sel)); }
  function closest(node, sel) { return node && node.closest ? node.closest(sel) : null; }
  function fa(value) { return A.toFaDigits(String(value)); }
  function money(value) { return A.formatToman(value); }
  function signed(value) { return (value < 0 ? '−' : '') + money(Math.abs(value)); }
  function setText(root, name, text) {
    $$('[data-sum="' + name + '"]', root).forEach(function (node) { node.textContent = text; });
  }
  function make(tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  /* ======================================================================
   * 1. Invoice popup
   * ==================================================================== */
  function modalHost() { return $('#' + MODAL_ID + ' [data-invoice-host]'); }

  function loadInvoice(link) {
    var host = modalHost();
    if (!host) { window.location.href = link.getAttribute('href'); return; }
    var title = $('#' + MODAL_ID + '-title');
    if (title) title.textContent = link.getAttribute('data-invoice-title') || 'ثبت فاکتور';
    host.textContent = '';
    host.appendChild(make('p', 'text-muted text-center', 'در حال بارگذاری فاکتور…'));
    host.setAttribute('aria-busy', 'true');
    A.openModal(MODAL_ID);
    var url = link.getAttribute('href');
    url += (url.indexOf('?') === -1 ? '?' : '&') + 'fragment=1';
    A.fetchJSON(url, { headers: { Accept: 'text/html' } }).then(function (html) {
      if (typeof html !== 'string') throw new Error('bad response');
      host.innerHTML = html;
      host.removeAttribute('aria-busy');
      A.init(host);
      setupForm($('[data-invoice-form]', host));
      var first = $('.stepper__input, input', host);
      if (first && first.focus) { try { first.focus({ preventScroll: true }); } catch (e) { /* old WebView */ } }
    }, function (err) {
      A.closeModal(MODAL_ID);
      A.toast(err.message, 'error');
    });
  }

  doc.addEventListener('click', function (e) {
    var link = closest(e.target, '[data-invoice-open]');
    if (!link || e.defaultPrevented || e.button > 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    e.preventDefault();
    loadInvoice(link);
  });

  doc.addEventListener('atelier:modal-close', function (e) {
    if (!e.detail || e.detail.id !== MODAL_ID) return;
    var host = modalHost();
    setTimeout(function () { if (host && !closest(host, '.modal.is-open')) host.textContent = ''; }, 350);
  });

  function goTo(target) {
    var url;
    try { url = new URL(target, window.location.href); } catch (e) { window.location.reload(); return; }
    if (url.pathname + url.search === window.location.pathname + window.location.search) {
      window.location.hash = url.hash; // only the hash differs: force a reload so the new data shows
      window.location.reload();
    } else {
      window.location.href = url.href;
    }
  }

  function setBusy(form, busy) {
    $$('button[type="submit"]', form).forEach(function (b) {
      b.classList.toggle('is-loading', busy);
      b.disabled = busy;
    });
    if (busy) form.setAttribute('aria-busy', 'true'); else form.removeAttribute('aria-busy');
  }

  // Inside the popup the form is sent with fetch so a validation error keeps the user in place.
  doc.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.matches || !form.matches('[data-invoice-form]') || !closest(form, '#' + MODAL_ID)) return;
    e.preventDefault();
    if (form.__saving) return;
    form.__saving = true;
    setBusy(form, true);
    A.fetchJSON(form.getAttribute('action'), { method: 'POST', body: new FormData(form) }).then(function (res) {
      if (form.__clearDraft) form.__clearDraft();
      goTo(res && res.data && res.data.redirect ? res.data.redirect : window.location.href);
    }, function (err) {
      form.__saving = false;
      setBusy(form, false);
      var html = err.data && typeof err.data === 'object' ? err.data.html : null;
      if (err.status === 422 && typeof html === 'string') {
        var host = modalHost();
        host.innerHTML = html;
        A.init(host);
        setupForm($('[data-invoice-form]', host));
        var body = closest(host, '.modal__body');
        if (body) body.scrollTop = 0;
        var bad = $('[aria-invalid="true"], .inv-row--invalid input', host);
        if (bad && bad.focus) { try { bad.focus(); } catch (x) { /* ignore */ } }
      }
      A.toast(err.message, 'error');
    });
  }, true);

  /* ---- the form itself ------------------------------------------------ */
  function setupForm(form) {
    if (!form || form.__acc) return;
    form.__acc = true;
    var panel = $('[data-summary]', form);
    var busy = panel ? $('[data-sum-busy]', panel) : null;
    var timer = null;
    var seq = 0;

    function renderPeople(people) {
      var list = $('[data-sum-people]', form);
      if (list) {
        list.textContent = '';
        people.forEach(function (p) {
          var li = make('li', 'inv-person');
          li.setAttribute('data-person', p.assignment_id);
          var head = make('div', 'inv-person__head');
          head.appendChild(make('strong', '', p.name));
          head.appendChild(make('span', 'text-muted', p.role_label + (p.paid ? ' · پرداخت‌شده' : '')));
          var parts = make('div', 'inv-person__parts text-sm');
          [['پایه', p.base], ['پورسانت', p.commission], ['پاداش', p.bonus]].forEach(function (pair) {
            var span = make('span', '', pair[0] + ' ');
            span.appendChild(make('b', 'num', money(pair[1])));
            parts.appendChild(span);
          });
          if (p.applied_tier) {
            parts.lastChild.appendChild(make('small', 'text-muted', ' (پلهٔ ' + fa(p.applied_tier.threshold) + '+)'));
          }
          var total = make('div', 'inv-person__total', 'جمع ');
          total.appendChild(make('b', 'num', money(p.total)));
          total.appendChild(make('small', 'text-muted', ' تومان'));
          li.appendChild(head); li.appendChild(parts); li.appendChild(total);
          list.appendChild(li);
        });
      }
      people.forEach(function (p) {
        $$('[data-person-total="' + p.assignment_id + '"]', form).forEach(function (n) { n.textContent = money(p.total); });
      });
      $$('[data-sum-nobody], [data-sum-nobody-inline]', form).forEach(function (n) { n.hidden = people.length > 0; });
    }

    function renderWarnings(items, tone) {
      var box = $('[data-sum-warnings]', form);
      if (!box) return;
      box.textContent = '';
      items.forEach(function (text) {
        var alertBox = make('div', 'alert alert--' + tone);
        alertBox.appendChild(make('div', 'alert__body', text));
        box.appendChild(alertBox);
      });
    }

    function render(data) {
      setText(form, 'units', fa(data.units));
      setText(form, 'income', money(data.income.total));
      setText(form, 'staff_pay', money(data.expenses.staff_pay));
      setText(form, 'other_expenses', money(data.expenses.non_staff));
      setText(form, 'expense_total', money(data.expenses.total));
      setText(form, 'net', signed(data.net_profit));
      var box = $('[data-sum-net-box]', form);
      if (box) box.classList.toggle('is-negative', data.net_profit < 0);
      renderPeople(data.people);
      renderWarnings(data.warnings || [], 'warning');
    }

    function run() {
      var mine = ++seq;
      if (panel) panel.classList.add('is-busy');
      if (busy) busy.hidden = false;
      A.fetchJSON(form.getAttribute('data-preview-url'), { method: 'POST', body: new FormData(form) }).then(function (res) {
        if (mine !== seq) return;
        if (panel) panel.classList.remove('is-busy');
        if (busy) busy.hidden = true;
        render(res.data);
      }, function (err) {
        if (mine !== seq) return;
        if (panel) panel.classList.remove('is-busy');
        if (busy) busy.hidden = true;
        renderWarnings([err.status === 422 ? err.message : 'محاسبهٔ زنده انجام نشد؛ اتصال را بررسی کنید. ثبت فاکتور همچنان محاسبه را درست انجام می‌دهد.'], err.status === 422 ? 'danger' : 'warning');
      });
    }

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(run, 320);
    }

    ['input', 'change', 'atelier:input'].forEach(function (name) { form.addEventListener(name, schedule); });

    /* rows: "+ other size" and extra expenses */
    function addRow(container, template, max, label, forcedIndex) {
      if (!container || !template) return null;
      if (container.children.length >= max) {
        A.toast('حداکثر ' + fa(max) + ' ردیف برای «' + label + '» مجاز است.', 'warning');
        return null;
      }
      var index = forcedIndex !== undefined ? forcedIndex : (parseInt(container.getAttribute('data-next-index'), 10) || 0);
      container.setAttribute('data-next-index', String(Math.max(index + 1, parseInt(container.getAttribute('data-next-index'), 10) || 0)));
      var holder = doc.createElement('div');
      holder.innerHTML = template.innerHTML.replace(/__IDX__/g, String(index));
      var row = holder.firstElementChild;
      container.appendChild(row);
      A.init(row);
      if (forcedIndex === undefined) {
        var focus = $('input[type="text"]', row);
        if (focus && focus.focus) { try { focus.focus(); } catch (e) { /* ignore */ } }
      }
      schedule();
      return row;
    }
    form.__addRow = addRow;

    form.addEventListener('click', function (e) {
      var addCustom = closest(e.target, '[data-add-custom]');
      if (addCustom) {
        var cat = addCustom.getAttribute('data-add-custom');
        var container = $('[data-custom-rows="' + cat + '"]', form);
        addRow(container, $('template[data-custom-template="' + cat + '"]', form), parseInt(container.getAttribute('data-max'), 10) || 15, addCustom.textContent.trim());
        return;
      }
      if (closest(e.target, '[data-add-extra]')) {
        var extras = $('[data-extra-rows]', form);
        addRow(extras, $('template[data-extra-template]', form), parseInt(extras.getAttribute('data-max'), 10) || 30, 'هزینهٔ دیگر');
        return;
      }
      var remove = closest(e.target, '[data-remove-row]');
      if (remove) {
        var row = closest(remove, '[data-custom-row], [data-extra-row]');
        if (row && row.parentNode) {
          var parent = row.parentNode;
          parent.removeChild(row);
          var next = $('[data-add-custom], [data-add-extra]', closest(parent, '.inv-group') || form);
          if (next && next.focus) next.focus();
          schedule();
        }
        return;
      }
      var restore = closest(e.target, '[data-restore-default]');
      if (restore) {
        A.setValue(restore.getAttribute('data-restore-default'), parseInt(restore.getAttribute('data-default'), 10));
        restore.hidden = true;
        schedule();
      }
    });

    // the "back to the default" link of the venue rent only shows while the value differs
    var restoreBtn = $('[data-restore-default]', form);
    if (restoreBtn) {
      form.addEventListener('atelier:input', function (e) {
        if (e.detail && e.detail.name === 'venue_rent') {
          restoreBtn.hidden = e.detail.value === parseInt(restoreBtn.getAttribute('data-default'), 10);
        }
      });
    }

    setupDraft(form, schedule);
    schedule();
  }

  /* ---- unsent drafts: survive a closed popup, a reload or a dropped connection ------------- */
  var DRAFT_SKIP = { csrf_token: 1, save_token: 1, base_revision: 1 };
  var DRAFT_TTL = 3 * 24 * 3600 * 1000;

  function draftKey(form) {
    var rev = form.querySelector('[name="base_revision"]');
    return 'atelier:invoice-draft:' + form.getAttribute('action') + ':' + (rev ? rev.value : '0');
  }
  function readDraft(key) {
    try {
      var data = JSON.parse(window.localStorage.getItem(key) || 'null');
      if (!data || !data.fields || Date.now() - data.at > DRAFT_TTL) return null;
      return data;
    } catch (e) { return null; }
  }
  function writeDraft(key, value) {
    try {
      if (value === null) window.localStorage.removeItem(key); else window.localStorage.setItem(key, JSON.stringify(value));
    } catch (e) { /* storage blocked or full: drafts are a convenience only */ }
  }
  function serialize(form) {
    var fields = [];
    new FormData(form).forEach(function (value, name) {
      if (!DRAFT_SKIP[name] && typeof value === 'string') fields.push([name, value]);
    });
    return fields;
  }
  function applyDraft(form, fields) {
    var rows = form.__addRow;
    fields.forEach(function (pair) {
      var m = /^custom__(print|frame|chassis)__(\d+)__/.exec(pair[0]);
      if (m && !$('[name^="custom__' + m[1] + '__' + m[2] + '__"], [data-name^="custom__' + m[1] + '__' + m[2] + '__"]', form)) {
        rows($('[data-custom-rows="' + m[1] + '"]', form), $('template[data-custom-template="' + m[1] + '"]', form), 99, '', parseInt(m[2], 10));
      }
      var e = /^extra__(\d+)__/.exec(pair[0]);
      if (e && !$('[name^="extra__' + e[1] + '__"], [data-name^="extra__' + e[1] + '__"]', form)) {
        rows($('[data-extra-rows]', form), $('template[data-extra-template]', form), 99, '', parseInt(e[1], 10));
      }
    });
    var seen = {};
    fields.forEach(function (pair) { seen[pair[0]] = true; });
    $$('input[type="checkbox"]', form).forEach(function (box) { if (box.name && !DRAFT_SKIP[box.name]) box.checked = !!seen[box.name]; });
    fields.forEach(function (pair) {
      var node = $('[name="' + pair[0] + '"]', form);
      if (node && node.type === 'checkbox') { node.checked = true; return; }
      if (node && node.type !== 'hidden') { node.value = pair[1]; return; }
      A.setValue(pair[0], pair[1]);  // money / stepper widgets keep their value in a hidden twin
    });
  }

  function setupDraft(form, schedule) {
    var key = draftKey(form);
    var dirty = false;
    var timer = null;

    var baseline = JSON.stringify(serialize(form));  // what the server rendered: only real changes are worth a draft

    function save() {
      if (!dirty || form.__closed) return;
      var fields = serialize(form);
      writeDraft(key, JSON.stringify(fields) === baseline ? null : { at: Date.now(), fields: fields });
    }
    function flush() { clearTimeout(timer); save(); }
    ['input', 'change', 'atelier:input'].forEach(function (name) {
      form.addEventListener(name, function () {
        if (form.__closed) return;
        dirty = true;
        clearTimeout(timer);
        timer = setTimeout(save, 700);
      });
    });
    window.addEventListener('pagehide', flush);
    doc.addEventListener('atelier:modal-close', function onClose(e) {  // closing the popup keeps the draft and ends this form's life
      if (!e.detail || e.detail.id !== MODAL_ID || !closest(form, '#' + MODAL_ID)) return;
      flush();
      form.__closed = true;
      doc.removeEventListener('atelier:modal-close', onClose);
    });
    form.__clearDraft = function () { form.__closed = true; writeDraft(key, null); };

    form.addEventListener('submit', function () { if (!closest(form, '#' + MODAL_ID)) form.__clearDraft(); });  // full-page flow: the server re-renders what was typed
    var draft = form.querySelector('.alert--danger') ? null : readDraft(key);  // a re-rendered form already shows the user's input
    if (!draft || !draft.fields.length) return;
    var banner = make('div', 'alert alert--info inv-draft');
    var text = make('div', 'alert__body', 'پیش‌نویس ذخیره‌نشده‌ای از همین فاکتور روی این دستگاه پیدا شد. ');
    var restore = make('button', 'btn btn--sm btn--primary', 'بازیابی پیش‌نویس');
    var drop = make('button', 'btn btn--sm btn--ghost', 'نادیده گرفتن');
    restore.type = 'button'; drop.type = 'button';
    var actions = make('div', 'cluster cluster--sm inv-draft__actions');
    actions.appendChild(restore); actions.appendChild(drop);
    text.appendChild(actions);
    banner.appendChild(text);
    var head = $('.inv-head', form);
    form.insertBefore(banner, head ? head.nextSibling : form.firstChild);
    restore.addEventListener('click', function () {
      applyDraft(form, draft.fields);
      banner.parentNode.removeChild(banner);
      dirty = true;
      schedule();
      A.toast('پیش‌نویس بازیابی شد؛ مقدارها را بررسی و فاکتور را ثبت کنید.', 'info');
    });
    drop.addEventListener('click', function () {
      writeDraft(key, null);
      banner.parentNode.removeChild(banner);
    });
  }

  /* ======================================================================
   * 3. Payroll sheet
   * ==================================================================== */
  function setupPay(form) {
    if (!form || form.__acc) return;
    form.__acc = true;
    var count = $('[data-pay-count]', form);
    var sum = $('[data-pay-sum]', form);
    var open = $('[data-pay-open]', form);
    var bar = $('[data-pay-bar]', form);
    var note = $('[data-pay-summary]', form);
    var base = note ? note.textContent : '';

    function checked() { return $$('[data-pay-line]:checked', form); }
    function update() {
      var boxes = checked();
      var total = 0;
      boxes.forEach(function (b) { total += parseInt(b.getAttribute('data-amount'), 10) || 0; });
      if (count) count.textContent = fa(boxes.length);
      if (sum) sum.textContent = money(total);
      if (open) open.disabled = boxes.length === 0;
      if (bar) bar.hidden = boxes.length === 0;
      if (note) note.textContent = boxes.length ? fa(boxes.length) + ' ردیف به مبلغ ' + money(total) + ' تومان پرداخت‌شده ثبت می‌شود. ' + base : base;
      $$('.pay-person', form).forEach(function (card) {
        var master = $('[data-select-person]', card);
        if (!master) return;
        var boxesOfCard = $$('[data-pay-line]', card);
        var on = boxesOfCard.filter(function (b) { return b.checked; }).length;
        master.checked = on > 0 && on === boxesOfCard.length;
        master.indeterminate = on > 0 && on < boxesOfCard.length;
      });
    }

    form.addEventListener('change', function (e) {
      if (e.target.matches && e.target.matches('[data-select-person]')) {
        var card = closest(e.target, '.pay-person');
        $$('[data-pay-line]', card).forEach(function (b) { b.checked = e.target.checked; });
      }
      update();
    });
    form.addEventListener('click', function (e) {
      var one = closest(e.target, '[data-pay-one]');
      if (one) {
        $$('[data-pay-line]', form).forEach(function (b) { b.checked = b.value === one.getAttribute('data-pay-one'); });
        update();
        A.openModal('pay-modal');
        return;
      }
      if (closest(e.target, '[data-pay-open]')) A.openModal('pay-modal');
    });
    update();
    if (form.hasAttribute('data-reopen')) A.openModal('pay-modal');
  }

  function boot() {
    $$('[data-invoice-form]').forEach(function (form) { if (!closest(form, '#' + MODAL_ID)) setupForm(form); });
    $$('[data-pay-form]').forEach(setupPay);
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', boot); else boot();
})();
