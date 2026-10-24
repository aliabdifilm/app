/* Atelier Sayyar - atelier manager panel behaviour.
 * ES2015-level syntax (no ?., ??, async) so cheap Android WebViews keep working.
 * Hooks (all declarative, no inline script):
 *   [data-tiers] / [data-tiers-add] / [data-tier-remove]   bonus-tier rows
 *   [data-pay-example]                                      live "what would this pay" line (server computes)
 *   [data-share] / [data-copy-text]                         send the afish to a messenger
 *   [data-autosubmit]                                       submit the form when a select changes
 */
(function () {
  'use strict';
  var doc = document;
  var MAX_TIERS = 8;

  function closest(el, selector) {
    return el && el.closest ? el.closest(selector) : null;
  }
  function toast(message, tone) {
    if (window.Atelier && window.Atelier.toast) window.Atelier.toast(message, tone || 'info');
  }
  function latin(text) {
    return window.Atelier ? window.Atelier.toLatinDigits(String(text)) : String(text);
  }

  /* -- bonus tiers ------------------------------------------------------- */
  function tierRows(host) {
    return host.querySelectorAll('[data-tier-row]');
  }

  function addTier(host) {
    var template = host.querySelector('[data-tiers-template]');
    var rows = host.querySelector('[data-tiers-rows]');
    if (!template || !rows) return;
    if (tierRows(host).length >= MAX_TIERS) {
      toast('حداکثر ' + window.Atelier.toFaDigits(MAX_TIERS) + ' پلهٔ پاداش مجاز است.', 'warning');
      return;
    }
    var fragment = doc.importNode(template.content, true);
    var row = fragment.firstElementChild;
    rows.appendChild(row);
    if (window.Atelier) window.Atelier.init(row);
    var first = row.querySelector('input');
    if (first) first.focus();
    schedulePreview(host);
  }

  function removeTier(button) {
    var row = closest(button, '[data-tier-row]');
    var host = closest(button, '[data-tiers]');
    if (!row || !host) return;
    var rows = tierRows(host);
    if (rows.length <= 1) {
      // keep one blank row so the editor never looks broken
      var inputs = row.querySelectorAll('input');
      for (var i = 0; i < inputs.length; i++) {
        if (inputs[i].__aSet) inputs[i].__aSet('', true);
        else inputs[i].value = '';
      }
    } else {
      row.parentNode.removeChild(row);
    }
    schedulePreview(host);
  }

  /* -- pay example (formula stays on the server: payroll.compute_pay) ----- */
  var previewTimer = null;
  var previewSeq = 0;

  function fieldValue(form, name) {
    var input = form.querySelector('input[name="' + name + '"]');
    return input ? input.value : '';
  }

  function collectTerms(form) {
    var tiers = [];
    var rows = form.querySelectorAll('[data-tier-row]');
    for (var i = 0; i < rows.length; i++) {
      var t = rows[i].querySelector('input[name="tier_threshold[]"]');
      var a = rows[i].querySelector('input[name="tier_amount[]"]');
      var threshold = t ? parseInt(latin(t.value).replace(/\D/g, ''), 10) : NaN;
      var amount = a ? parseInt(latin(a.value).replace(/\D/g, ''), 10) : NaN;
      if (!isNaN(threshold) && !isNaN(amount)) tiers.push({ threshold: threshold, amount: amount });
    }
    return {
      base: parseInt(latin(fieldValue(form, 'base')).replace(/\D/g, ''), 10) || 0,
      commission_per_unit: parseInt(latin(fieldValue(form, 'commission')).replace(/\D/g, ''), 10) || 0,
      bonus_tiers: tiers
    };
  }

  function schedulePreview(node) {
    var form = closest(node, 'form');
    if (!form || !form.querySelector('[data-pay-example]')) return;
    clearTimeout(previewTimer);
    previewTimer = setTimeout(function () { runPreview(form); }, 350);
  }

  function runPreview(form) {
    var box = form.querySelector('[data-pay-example]');
    if (!box || !window.Atelier) return;
    var terms = collectTerms(form);
    terms.units = parseInt(box.getAttribute('data-units'), 10) || 80;
    var seq = ++previewSeq;
    var line = box.querySelector('[data-example-line]');
    window.Atelier.fetchJSON(box.getAttribute('data-url'), { method: 'POST', json: terms }).then(function (res) {
      if (seq !== previewSeq || !res || !res.data) return;
      var d = res.data;
      var fmt = function (n) { return window.Atelier.formatToman(n); };
      var text = 'پایه ' + fmt(d.base) + ' + پورسانت ' + fmt(d.commission) + ' + پاداش ' + fmt(d.bonus);
      var total = box.querySelector('[data-example-total]');
      if (line) line.textContent = text;
      if (total) total.textContent = fmt(d.total);
      else if (line) line.parentNode.innerHTML = '<span data-example-line>' + text + '</span> = <strong><span data-example-total>' + fmt(d.total) + '</span> تومان</strong>';
    }, function () {
      if (seq !== previewSeq) return;
      if (line) line.textContent = 'با تکمیل مبالغ، نمونه این‌جا نمایش داده می‌شود.';
    });
  }

  /* -- share ------------------------------------------------------------- */
  function copyText(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(text);
    }
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

  function initShare(root) {
    var buttons = (root || doc).querySelectorAll('[data-share]');
    for (var i = 0; i < buttons.length; i++) {
      if (navigator.share) buttons[i].hidden = false;
    }
  }

  /* -- events ------------------------------------------------------------ */
  doc.addEventListener('click', function (e) {
    var add = closest(e.target, '[data-tiers-add]');
    if (add) { e.preventDefault(); addTier(closest(add, '[data-tiers]')); return; }
    var remove = closest(e.target, '[data-tier-remove]');
    if (remove) { e.preventDefault(); removeTier(remove); return; }
    var share = closest(e.target, '[data-share]');
    if (share) {
      e.preventDefault();
      navigator.share({
        title: share.getAttribute('data-share-title') || '',
        text: share.getAttribute('data-share-text') || ''
      }).catch(function () { /* the person closed the share sheet */ });
      return;
    }
    var copy = closest(e.target, '[data-copy-text]');
    if (copy) {
      e.preventDefault();
      copyText(copy.getAttribute('data-copy-text') || '').then(
        function () { toast('متن افیش کپی شد؛ آن را در پیام‌رسان بچسبانید.', 'success'); },
        function () { toast('کپی انجام نشد؛ متن را دستی انتخاب کنید.', 'error'); }
      );
    }
  });

  doc.addEventListener('input', function (e) { schedulePreview(e.target); });
  doc.addEventListener('atelier:input', function (e) { schedulePreview(e.target); });

  doc.addEventListener('change', function (e) {
    var select = closest(e.target, '[data-autosubmit]');
    if (select && select.form) {
      if (select.form.requestSubmit) select.form.requestSubmit(); else select.form.submit();
    }
  });

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', function () { initShare(doc); });
  else initShare(doc);
})();
