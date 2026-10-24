/* Atelier Sayyar - hall manager panel behaviour.
 * ES2015-level syntax (no ?., ??, async) so cheap Android WebViews keep working.
 * Everything is declarative (data-* hooks), no inline script:
 *   [data-multidate]                        Jalali multi-date picker of the "add many dates" page
 *   [data-rule] [data-rule-apply]           turn "every Thursday and Friday ..." into dates (server expands them)
 *   [data-bulk-pick] [data-bulk-all]        checkboxes with a select-all and a counter ([data-bulk-count])
 *   [data-bulkbar]                          action bar that appears while something is selected
 *   [data-carry-form] / [data-carry-scope]  keep the ateliers ticked on the offer page while searching / paging
 *   [data-hall-share] / [data-hall-copy]    send the event facts to a messenger / copy them
 */
(function () {
  'use strict';
  var doc = document;
  var A = function () { return window.Atelier; };
  var J = function () { return window.AtelierJalali; };

  function closest(el, selector) {
    return el && el.closest ? el.closest(selector) : null;
  }
  function toast(message, tone) {
    if (A() && A().toast) A().toast(message, tone || 'info');
  }
  function fa(value) {
    return A() ? A().toFaDigits(String(value)) : String(value);
  }
  function el(tag, className, text) {
    var node = doc.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function icon(name) {
    var wrap = el('span');
    wrap.innerHTML = '<svg class="icon" aria-hidden="true" focusable="false"><use href="#i-' + name + '"/></svg>';
    return wrap.firstChild;
  }

  /* ---------------------------------------------------------------- multi-date picker */
  function MultiDate(root) {
    var jal = J();
    if (!jal) return;
    var min = root.getAttribute('data-min') || '';
    var maxCount = parseInt(root.getAttribute('data-max'), 10) || 366;
    var selected = {};
    var initial = (root.getAttribute('data-initial') || '').split(',');
    for (var i = 0; i < initial.length; i++) {
      if (jal.parseISO(initial[i])) selected[initial[i]] = true;
    }
    var today = jal.todayJalali();
    var minParts = jal.parseISO(min);
    var minJ = minParts ? jal.toJalali(minParts[0], minParts[1], minParts[2]) : today;
    var firstSelected = Object.keys(selected).sort()[0];
    var startJ = today;
    if (firstSelected) {
      var p = jal.parseISO(firstSelected);
      startJ = jal.toJalali(p[0], p[1], p[2]);
    } else if (minParts) {
      startJ = minJ;
    }
    var view = { jy: startJ[0], jm: startJ[1] };

    root.textContent = '';
    var bar = el('div', 'hl-md__bar');
    var prev = el('button', 'icon-btn');
    prev.type = 'button';
    prev.setAttribute('aria-label', 'ماه قبل');
    prev.appendChild(icon('chevron-right'));
    var title = el('h3', 'hl-md__title');
    title.setAttribute('aria-live', 'polite');
    var next = el('button', 'icon-btn');
    next.type = 'button';
    next.setAttribute('aria-label', 'ماه بعد');
    next.appendChild(icon('chevron-left'));
    bar.appendChild(prev);
    bar.appendChild(title);
    bar.appendChild(next);

    var grid = el('div', 'hl-md__grid');
    grid.setAttribute('role', 'group');
    var tools = el('div', 'hl-md__tools');
    var allBtn = el('button', 'btn btn--sm btn--secondary', 'انتخاب همهٔ روزهای این ماه');
    var monthBtn = el('button', 'btn btn--sm btn--ghost', 'برداشتن این ماه');
    var clearBtn = el('button', 'btn btn--sm btn--ghost', 'پاک کردن همه');
    allBtn.type = monthBtn.type = clearBtn.type = 'button';
    tools.appendChild(allBtn);
    tools.appendChild(monthBtn);
    tools.appendChild(clearBtn);
    var count = el('p', 'hl-md__count');
    count.setAttribute('aria-live', 'polite');
    var chips = el('ul', 'hl-chips hl-md__chips');
    chips.setAttribute('aria-label', 'تاریخ‌های انتخاب‌شده');
    var inputs = el('div');
    inputs.hidden = true;
    root.appendChild(bar);
    root.appendChild(grid);
    root.appendChild(tools);
    root.appendChild(count);
    root.appendChild(chips);
    root.appendChild(inputs);

    function isPast(iso) { return min && iso < min; }
    function total() { return Object.keys(selected).length; }

    function monthDays(jy, jm) {
      var days = [];
      var length = jal.monthLength(jy, jm);
      for (var d = 1; d <= length; d++) {
        var g = jal.toGregorian(jy, jm, d);
        days.push(jal.isoString(g[0], g[1], g[2]));
      }
      return days;
    }

    function syncInputs() {
      inputs.textContent = '';
      var keys = Object.keys(selected).sort();
      for (var k = 0; k < keys.length; k++) {
        var input = doc.createElement('input');
        input.type = 'hidden';
        input.name = 'dates';
        input.value = keys[k];
        inputs.appendChild(input);
      }
      count.textContent = keys.length ? fa(keys.length) + ' تاریخ انتخاب شده است.' : 'هنوز تاریخی انتخاب نشده است.';
      chips.textContent = '';
      for (var c = 0; c < keys.length; c++) {
        (function (iso) {
          var text = jal.isoToJalaliText(iso, 'numeric', { fa: true });
          var long = jal.isoToJalaliText(iso, 'weekday_long', { fa: true });
          var li = el('li');
          var chip = el('span', 'hl-chip');
          chip.appendChild(el('span', '', text));
          var remove = el('button', '', '×');
          remove.type = 'button';
          remove.setAttribute('aria-label', 'حذف ' + long);
          remove.addEventListener('click', function () {
            delete selected[iso];
            render();
          });
          chip.appendChild(remove);
          li.appendChild(chip);
          chips.appendChild(li);
        })(keys[c]);
      }
    }

    function toggle(iso) {
      if (selected[iso]) {
        delete selected[iso];
      } else {
        if (total() >= maxCount) {
          toast('در هر بار حداکثر ' + fa(maxCount) + ' تاریخ انتخاب می‌شود.', 'warning');
          return;
        }
        selected[iso] = true;
      }
      render();
    }

    function render() {
      title.textContent = jal.MONTHS[view.jm - 1] + ' ' + fa(view.jy);
      grid.textContent = '';
      for (var w = 0; w < 7; w++) {
        grid.appendChild(el('div', 'hl-md__dow' + (w === 6 ? ' hl-md__dow--end' : ''), jal.WEEKDAYS_SHORT[w]));
      }
      var weeks = jal.monthGrid(view.jy, view.jm);
      for (var r = 0; r < weeks.length; r++) {
        for (var c = 0; c < 7; c++) {
          var d = weeks[r][c];
          if (!d) {
            grid.appendChild(el('span', 'hl-md__day hl-md__day--blank'));
            continue;
          }
          var g = jal.toGregorian(view.jy, view.jm, d);
          var iso = jal.isoString(g[0], g[1], g[2]);
          var btn = el('button', 'hl-md__day' + (c === 6 ? ' hl-md__day--friday' : '') + (isPast(iso) ? ' hl-md__day--past' : ''), fa(d));
          btn.type = 'button';
          btn.setAttribute('data-iso', iso);
          btn.setAttribute('aria-pressed', selected[iso] ? 'true' : 'false');
          btn.setAttribute('aria-label', jal.format(view.jy, view.jm, d, 'weekday_long', { fa: true }));
          if (isPast(iso)) btn.disabled = true;
          grid.appendChild(btn);
        }
      }
      prev.disabled = view.jy * 12 + view.jm <= minJ[0] * 12 + minJ[1];
      syncInputs();
    }

    grid.addEventListener('click', function (e) {
      var btn = closest(e.target, '[data-iso]');
      if (btn && !btn.disabled) toggle(btn.getAttribute('data-iso'));
    });
    function shift(delta) {
      var index = view.jy * 12 + (view.jm - 1) + delta;
      view = { jy: Math.floor(index / 12), jm: (index % 12) + 1 };
      render();
    }
    prev.addEventListener('click', function () { shift(-1); });
    next.addEventListener('click', function () { shift(1); });
    allBtn.addEventListener('click', function () {
      var days = monthDays(view.jy, view.jm);
      var added = 0;
      for (var n = 0; n < days.length; n++) {
        if (!isPast(days[n]) && !selected[days[n]] && total() < maxCount) { selected[days[n]] = true; added++; }
      }
      if (!added) toast('روزی برای افزودن نمانده است.', 'info');
      render();
    });
    monthBtn.addEventListener('click', function () {
      var days = monthDays(view.jy, view.jm);
      for (var n = 0; n < days.length; n++) delete selected[days[n]];
      render();
    });
    clearBtn.addEventListener('click', function () {
      selected = {};
      render();
    });

    root.__multidate = {
      add: function (list) {
        var added = 0;
        var skipped = 0;
        for (var n = 0; n < list.length; n++) {
          if (!jal.parseISO(list[n])) continue;
          if (isPast(list[n])) { skipped++; continue; }
          if (!selected[list[n]]) {
            if (total() >= maxCount) { skipped++; continue; }
            selected[list[n]] = true;
            added++;
          }
        }
        var first = Object.keys(selected).sort()[0];
        if (first && added) {
          var parts = jal.parseISO(first);
          var jd = jal.toJalali(parts[0], parts[1], parts[2]);
          view = { jy: jd[0], jm: jd[1] };
        }
        render();
        return { added: added, skipped: skipped };
      }
    };
    root.setAttribute('data-ready', '1');
    render();
  }

  /* ---------------------------------------------------------------- repeat rule -> dates */
  function ruleValue(box, name) {
    var field = box.querySelector('[name="' + name + '"]');
    return field ? field.value : '';
  }

  function showRuleParts(box) {
    var mode = ruleValue(box, 'rule_mode');
    var weekly = box.querySelector('[data-rule-weekly]');
    var monthly = box.querySelector('[data-rule-monthly]');
    if (weekly) weekly.hidden = mode !== 'weekly';
    if (monthly) monthly.hidden = mode !== 'monthly';
  }

  function applyRule(box) {
    var form = closest(box, 'form');
    var target = form ? form.querySelector('[data-multidate]') : null;
    if (!target || !target.__multidate || !A()) return;
    var mode = ruleValue(box, 'rule_mode');
    if (!mode) { toast('نوع الگو را انتخاب کنید.', 'warning'); return; }
    var from = A().getValue('rule_from');
    var to = A().getValue('rule_to');
    if (!from || !to) { toast('تاریخ شروع و پایان بازه را وارد کنید.', 'warning'); return; }
    var payload = { mode: mode, from: from, to: to };
    if (mode === 'weekly') {
      var boxes = box.querySelectorAll('input[name="rule_weekdays"]:checked');
      payload.weekdays = [];
      for (var i = 0; i < boxes.length; i++) payload.weekdays.push(parseInt(boxes[i].value, 10));
    } else {
      var text = A().toLatinDigits(ruleValue(box, 'rule_days')).split(/[,،\s]+/);
      payload.days = [];
      for (var j = 0; j < text.length; j++) if (/^\d+$/.test(text[j])) payload.days.push(parseInt(text[j], 10));
      payload.short = ruleValue(box, 'rule_short') || 'clamp';
    }
    var button = box.querySelector('[data-rule-apply]');
    if (button) button.classList.add('is-loading');
    A().fetchJSON(box.getAttribute('data-expand-url'), { method: 'POST', json: payload }).then(function (res) {
      var result = target.__multidate.add(res.data.dates || []);
      if (result.added) toast(fa(result.added) + ' تاریخ به انتخاب اضافه شد.', 'success');
      else toast('تاریخ تازه‌ای پیدا نشد.', 'info');
      if (result.skipped) toast(fa(result.skipped) + ' تاریخ گذشته یا اضافه‌ی سقف نادیده گرفته شد.', 'warning');
    }, function (err) {
      toast(err && err.message ? err.message : 'ساخت تاریخ‌ها انجام نشد.', 'error');
    }).then(function () {
      if (button) button.classList.remove('is-loading');
    });
  }

  /* ---------------------------------------------------------------- bulk selection */
  function picksIn(scope) {
    return (scope || doc).querySelectorAll('[data-bulk-pick]');
  }

  function updateBulk() {
    var picks = picksIn(doc);
    var checked = 0;
    for (var i = 0; i < picks.length; i++) if (picks[i].checked) checked++;
    var counters = doc.querySelectorAll('[data-bulk-count]');
    for (var c = 0; c < counters.length; c++) counters[c].textContent = fa(checked);
    var bar = doc.querySelector('[data-bulkbar]');
    if (bar) bar.hidden = checked === 0;
    var all = doc.querySelector('[data-bulk-all]');
    if (all) all.checked = picks.length > 0 && checked === picks.length;
  }

  /* ---------------------------------------------------------------- keep ateliers ticked while searching */
  function checkedAteliers(scope) {
    var boxes = (scope || doc).querySelectorAll('input[name="atelier_id"]:checked');
    var ids = [];
    for (var i = 0; i < boxes.length; i++) ids.push(boxes[i].value);
    return ids;
  }

  function carryForm(form) {
    var scope = doc.querySelector('[data-carry-scope]');
    var old = form.querySelectorAll('input[name="atelier"]');
    for (var o = 0; o < old.length; o++) old[o].parentNode.removeChild(old[o]);
    var ids = checkedAteliers(scope);
    for (var i = 0; i < ids.length; i++) {
      var hidden = doc.createElement('input');
      hidden.type = 'hidden';
      hidden.name = 'atelier';
      hidden.value = ids[i];
      form.appendChild(hidden);
    }
  }

  function carryLink(link) {
    var scope = closest(link, '[data-carry-scope]');
    if (!scope || !window.URL || !window.URLSearchParams) return false;
    var url;
    try { url = new URL(link.href, window.location.href); } catch (e) { return false; }
    url.searchParams.delete('atelier');
    var ids = checkedAteliers(scope);
    for (var i = 0; i < ids.length; i++) url.searchParams.append('atelier', ids[i]);
    link.href = url.pathname + url.search + url.hash;
    return true;
  }

  /* ---------------------------------------------------------------- share / copy */
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var area = doc.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.className = 'visually-hidden';
      doc.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = doc.execCommand('copy'); } catch (e) { ok = false; }
      doc.body.removeChild(area);
      if (ok) resolve(); else reject(new Error('copy failed'));
    });
  }

  /* ---------------------------------------------------------------- wiring */
  doc.addEventListener('click', function (e) {
    var apply = closest(e.target, '[data-rule-apply]');
    if (apply) { e.preventDefault(); applyRule(closest(apply, '[data-rule]')); return; }
    var share = closest(e.target, '[data-hall-share]');
    if (share) {
      e.preventDefault();
      if (navigator.share) {
        navigator.share({
          title: share.getAttribute('data-share-title') || '',
          text: share.getAttribute('data-share-text') || ''
        }).catch(function () { /* the person closed the share sheet */ });
      }
      return;
    }
    var copy = closest(e.target, '[data-hall-copy]');
    if (copy) {
      e.preventDefault();
      copyText(copy.getAttribute('data-hall-copy') || '').then(
        function () { toast('اطلاعات مراسم کپی شد؛ آن را در پیام‌رسان بچسبانید.', 'success'); },
        function () { toast('کپی انجام نشد؛ متن را دستی انتخاب کنید.', 'error'); }
      );
      return;
    }
    var link = closest(e.target, 'a[href]');
    if (link && closest(link, '[data-carry-scope] .pagination-nav')) carryLink(link);
  });

  doc.addEventListener('change', function (e) {
    var all = closest(e.target, '[data-bulk-all]');
    if (all) {
      var picks = picksIn(doc);
      for (var i = 0; i < picks.length; i++) picks[i].checked = all.checked;
      updateBulk();
      return;
    }
    if (closest(e.target, '[data-bulk-pick]')) { updateBulk(); return; }
    var mode = closest(e.target, '[data-rule] [name="rule_mode"]');
    if (mode) showRuleParts(closest(mode, '[data-rule]'));
  });

  doc.addEventListener('keydown', function (e) {
    if (e.key !== 'Enter') return;
    var box = closest(e.target, '[data-rule]');
    if (box && e.target.tagName === 'INPUT' && e.target.type !== 'checkbox' && e.target.type !== 'submit' && !e.target.hasAttribute('data-jdate')) {
      e.preventDefault(); // Enter in a pattern field builds the dates instead of sending the whole form
      applyRule(box);
    }
  });

  doc.addEventListener('submit', function (e) {
    var form = e.target;
    if (form && form.matches && form.matches('[data-carry-form]')) carryForm(form);
    if (form && form.matches && form.matches('[data-bulk-form]') && form.querySelector('[data-multidate][data-ready]')) {
      // with JavaScript the pattern fields only feed the calendar; the chosen dates travel as `dates`
      var fields = form.querySelectorAll('[data-rule] input, [data-rule] select');
      for (var i = 0; i < fields.length; i++) fields[i].disabled = true;
    }
  }, true);

  function init(root) {
    var scope = root || doc;
    var pickers = scope.querySelectorAll('[data-multidate]:not([data-ready])');
    for (var i = 0; i < pickers.length; i++) MultiDate(pickers[i]);
    var rules = scope.querySelectorAll('[data-rule]');
    for (var r = 0; r < rules.length; r++) showRuleParts(rules[r]);
    var shares = scope.querySelectorAll('[data-hall-share]');
    for (var s = 0; s < shares.length; s++) if (navigator.share) shares[s].hidden = false;
    updateBulk();
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', function () { init(doc); });
  else init(doc);
})();
