/* Atelier Sayyar - staff panel behaviour.
 * ES2015-level syntax (no ?., ??, async) so cheap Android WebViews keep working; everything below is
 * progressive enhancement - every form also works without this file.
 * Hooks (all declarative, no inline script):
 *   form[data-availability]   day picker: live count, buttons enabled only with a selection, selected style
 *   [data-share] / [data-copy-text]   send the afish to a messenger (Web Share API, copy-to-clipboard fallback)
 *   [data-autosubmit]         submit the form when a select changes
 *   input[data-preview-target]   show a preview of the chosen image
 */
(function () {
  'use strict';
  var doc = document;

  function closest(el, selector) {
    return el && el.closest ? el.closest(selector) : null;
  }
  function toast(message, tone) {
    if (window.Atelier && window.Atelier.toast) window.Atelier.toast(message, tone || 'info');
  }
  function fa(value) {
    return window.Atelier && window.Atelier.toFaDigits ? window.Atelier.toFaDigits(String(value)) : String(value);
  }

  /* -- availability picker ---------------------------------------------------- */
  function updateAvailability(form) {
    var boxes = form.querySelectorAll('input[data-av-day]');
    var count = 0;
    for (var i = 0; i < boxes.length; i++) {
      var label = closest(boxes[i], '.av-day');
      if (boxes[i].checked) {
        count++;
        if (label) label.classList.add('is-selected');
      } else if (label) {
        label.classList.remove('is-selected');
      }
    }
    var text = form.querySelector('[data-av-count]');
    if (text) {
      text.textContent = count
        ? fa(count) + ' روز انتخاب شده؛ حالا یکی از دکمه‌ها را بزنید.'
        : 'روزهای دلخواه را انتخاب کنید، سپس یکی از دکمه‌ها را بزنید.';
    }
    var buttons = form.querySelectorAll('[data-av-action]');
    for (var j = 0; j < buttons.length; j++) buttons[j].disabled = count === 0;
  }

  function initAvailability(root) {
    var forms = (root || doc).querySelectorAll('form[data-availability]');
    for (var i = 0; i < forms.length; i++) {
      if (forms[i].__stReady) continue;
      forms[i].__stReady = true;
      updateAvailability(forms[i]);
    }
  }

  doc.addEventListener('change', function (e) {
    var box = closest(e.target, 'input[data-av-day]');
    if (box) {
      var form = closest(box, 'form[data-availability]');
      if (form) updateAvailability(form);
      return;
    }
    var select = closest(e.target, '[data-autosubmit]');
    if (select && select.form) {
      if (select.form.requestSubmit) select.form.requestSubmit();
      else select.form.submit();
      return;
    }
    var file = closest(e.target, 'input[data-preview-target]');
    if (file && file.files && file.files[0] && window.URL && window.URL.createObjectURL) {
      var img = doc.getElementById(file.getAttribute('data-preview-target'));
      if (img) {
        if (img.__stUrl) window.URL.revokeObjectURL(img.__stUrl);
        img.__stUrl = window.URL.createObjectURL(file.files[0]);
        img.src = img.__stUrl;
        img.hidden = false;
      }
    }
  });

  /* -- share ---------------------------------------------------------------------- */
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
      try { ok = doc.execCommand('copy'); } catch (err) { ok = false; }
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

  doc.addEventListener('click', function (e) {
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

  function init() {
    initAvailability(doc);
    initShare(doc);
  }
  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
  else init();
})();
