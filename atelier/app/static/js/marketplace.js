/* Atelier Sayyar - marketplace behaviour.
 * ES2015-level syntax (no ?., ??, async) so cheap Android WebViews keep working.  No inline script:
 * everything hangs on data-* hooks.
 *   [data-mk-filters]                     <details> that is open on wide screens
 *   [data-tiers] / [data-tiers-add] / [data-tier-remove]   bonus-tier rows of the post / invite forms
 *   [data-pay-example]                    live "what would this pay" line (the server computes it)
 *   [data-mk-form][data-presets]          refill the pay terms from the atelier preset when the role changes
 *   [data-mk-share] / [data-mk-copy]      share or copy the link of a post
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
  function digits(text) {
    var n = parseInt(latin(text).replace(/\D/g, ''), 10);
    return isNaN(n) ? NaN : n;
  }

  /* -- filters: open on wide screens ------------------------------------- */
  function initFilters() {
    var boxes = doc.querySelectorAll('[data-mk-filters]');
    if (!boxes.length || !window.matchMedia) return;
    if (window.matchMedia('(min-width: 900px)').matches) {
      for (var i = 0; i < boxes.length; i++) boxes[i].open = true;
    }
  }

  /* -- bonus tiers --------------------------------------------------------- */
  function tierRows(host) {
    return host.querySelectorAll('[data-tier-row]');
  }

  function addTier(host, threshold, amount) {
    var template = host.querySelector('[data-tiers-template]');
    var rows = host.querySelector('[data-tiers-rows]');
    if (!template || !rows) return null;
    if (tierRows(host).length >= MAX_TIERS) {
      toast('حداکثر ' + window.Atelier.toFaDigits(MAX_TIERS) + ' پلهٔ پاداش مجاز است.', 'warning');
      return null;
    }
    var row = doc.importNode(template.content, true).firstElementChild;
    rows.appendChild(row);
    if (window.Atelier) window.Atelier.init(row);
    var inputs = row.querySelectorAll('input');
    if (threshold !== undefined) {
      inputs[0].value = window.Atelier.toFaDigits(threshold);
      if (inputs[1] && window.Atelier.setValue) window.Atelier.setValue(inputs[1], amount);
    } else if (inputs[0]) {
      inputs[0].focus();
    }
    return row;
  }

  function clearTiers(host) {
    var rows = tierRows(host);
    for (var i = 0; i < rows.length; i++) rows[i].parentNode.removeChild(rows[i]);
  }

  function removeTier(button) {
    var row = closest(button, '[data-tier-row]');
    var host = closest(button, '[data-tiers]');
    if (!row || !host) return;
    if (tierRows(host).length <= 1) {
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

  /* -- live example (formula stays on the server: payroll.compute_pay) ----- */
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
      var threshold = t ? digits(t.value) : NaN;
      var amount = a ? digits(a.value) : NaN;
      if (!isNaN(threshold) && !isNaN(amount)) tiers.push({ threshold: threshold, amount: amount });
    }
    return {
      base: digits(fieldValue(form, 'base')) || 0,
      commission_per_unit: digits(fieldValue(form, 'commission')) || 0,
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
      if (seq !== previewSeq || !res || !res.data || !line) return;
      line.textContent = res.data.sentence;
      line.classList.remove('text-muted');
    }, function () {
      if (seq !== previewSeq || !line) return;
      line.textContent = 'با تکمیل مبالغ، نمونه این‌جا نمایش داده می‌شود.';
      line.classList.add('text-muted');
    });
  }

  /* -- presets: role changed -> refill the terms unless the person typed their own ------ */
  var typedTerms = false;

  function presetsOf(form) {
    try { return JSON.parse(form.getAttribute('data-presets') || '{}'); } catch (e) { return {}; }
  }

  function applyPreset(form) {
    var presets = presetsOf(form);
    var roleSelect = form.querySelector('select[name="role"]');
    var atelierSelect = form.querySelector('select[name="atelier_id"]');
    if (!roleSelect || typedTerms) return;
    var atelierId = atelierSelect ? atelierSelect.value : Object.keys(presets)[0];
    var terms = (presets[atelierId] || {})[roleSelect.value];
    if (!terms || !window.Atelier) return;
    window.Atelier.setValue('base', terms.base || 0);
    window.Atelier.setValue('commission', terms.commission_per_unit || 0);
    var host = form.querySelector('[data-tiers]');
    if (host) {
      clearTiers(host);
      var tiers = terms.bonus_tiers || [];
      for (var i = 0; i < tiers.length; i++) addTier(host, tiers[i].threshold, tiers[i].amount);
      if (!tiers.length) addTier(host);
    }
    toast('شرایط پیش‌فرض اتلیه برای این نقش گذاشته شد؛ می‌توانید تغییرش دهید.', 'info');
    schedulePreview(form);
  }

  /* -- share --------------------------------------------------------------- */
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

  function initShare() {
    var boxes = doc.querySelectorAll('[data-mk-share-box]');
    for (var i = 0; i < boxes.length; i++) {
      boxes[i].hidden = false;
      var share = boxes[i].querySelector('[data-mk-share]');
      if (share && !navigator.share) share.hidden = true; // copy stays as the fallback
    }
  }

  /* -- events -------------------------------------------------------------- */
  doc.addEventListener('click', function (e) {
    var add = closest(e.target, '[data-tiers-add]');
    if (add) { e.preventDefault(); addTier(closest(add, '[data-tiers]')); return; }
    var remove = closest(e.target, '[data-tier-remove]');
    if (remove) { e.preventDefault(); removeTier(remove); return; }
    var share = closest(e.target, '[data-mk-share]');
    if (share) {
      e.preventDefault();
      navigator.share({
        title: share.getAttribute('data-share-title') || '',
        text: share.getAttribute('data-share-text') || '',
        url: window.location.href
      }).catch(function () { /* the person closed the share sheet */ });
      return;
    }
    var copy = closest(e.target, '[data-mk-copy]');
    if (copy) {
      e.preventDefault();
      copyText(window.location.href).then(
        function () { toast('پیوند آگهی کپی شد؛ آن را در پیام‌رسان بچسبانید.', 'success'); },
        function () { toast('کپی انجام نشد؛ نشانی صفحه را دستی کپی کنید.', 'error'); }
      );
    }
  });

  function onTermsInput(e) {
    var form = closest(e.target, '[data-mk-form]');
    if (!form) return;
    if (e.target && e.target.name !== 'role' && e.target.name !== 'atelier_id' && closest(e.target, '[data-terms-editor]')) typedTerms = true;
    schedulePreview(e.target);
  }
  doc.addEventListener('input', onTermsInput);
  doc.addEventListener('atelier:input', onTermsInput);

  doc.addEventListener('change', function (e) {
    var form = closest(e.target, '[data-mk-form][data-presets]');
    if (form && e.target && (e.target.name === 'role' || e.target.name === 'atelier_id')) applyPreset(form);
  });

  function init() {
    initFilters();
    initShare();
    var forms = doc.querySelectorAll('[data-mk-form]');
    for (var i = 0; i < forms.length; i++) {
      var inputs = forms[i].querySelectorAll('[data-terms-editor] input[name="base"], [data-terms-editor] input[name="commission"]');
      // a form that arrives with terms (edit, prefilled from the afish page) counts as "typed"
      for (var j = 0; j < inputs.length; j++) if (digits(inputs[j].value) > 0) typedTerms = true;
      if (forms[i].querySelector('[data-pay-example]')) runPreview(forms[i]);
    }
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init); else init();
})();
