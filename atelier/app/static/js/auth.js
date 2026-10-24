/*
 * Atelier Sayyar - auth.js
 * Progressive enhancement for the code-entry page (everything also works without it):
 *   - [data-otp-input]          auto-submits once the full code is typed or pasted (any digit script)
 *   - Web OTP API               on Android Chrome the code arrives from the SMS without typing
 *   - [data-resend-seconds]     the "send again" button is enabled when the cooldown is over
 * ES2015 level on purpose (cheap Android WebViews); no inline script, CSP safe.
 */
(function () {
  'use strict';

  var doc = document;

  function toLatin(text) {
    var map = { '۰': 0, '۱': 1, '۲': 2, '۳': 3, '۴': 4, '۵': 5, '۶': 6, '۷': 7, '۸': 8, '۹': 9,
                '٠': 0, '١': 1, '٢': 2, '٣': 3, '٤': 4, '٥': 5, '٦': 6, '٧': 7, '٨': 8, '٩': 9 };
    return String(text).replace(/[۰-۹٠-٩]/g, function (ch) { return map[ch]; });
  }

  function toFa(text) {
    return window.Atelier && window.Atelier.toFaDigits ? window.Atelier.toFaDigits(String(text)) : String(text);
  }

  function submit(form) {
    if (form.dataset.otpSubmitted) return;
    form.dataset.otpSubmitted = '1';
    if (typeof form.requestSubmit === 'function') form.requestSubmit();
    else form.submit();
  }

  function initOtp(form) {
    var input = form.querySelector('[data-otp-input]');
    if (!input) return;
    var length = parseInt(form.getAttribute('data-otp-length'), 10) || 6;

    input.addEventListener('input', function () {
      var digits = toLatin(input.value).replace(/\D/g, '');
      if (digits !== toLatin(input.value)) input.value = digits;
      if (digits.length >= length) submit(form);
    });

    if ('OTPCredential' in window && window.navigator.credentials) {
      var abort = typeof AbortController !== 'undefined' ? new AbortController() : null;
      form.addEventListener('submit', function () { if (abort) abort.abort(); });
      window.navigator.credentials.get({ otp: { transport: ['sms'] }, signal: abort ? abort.signal : undefined })
        .then(function (credential) {
          if (credential && credential.code) {
            input.value = toLatin(credential.code);
            submit(form);
          }
        })
        .catch(function () { /* user dismissed or unsupported: typing still works */ });
    }
  }

  function initResend(button) {
    var seconds = parseInt(button.getAttribute('data-resend-seconds'), 10) || 0;
    var label = button.querySelector('[data-resend-label]') || button;
    var base = 'ارسال دوبارهٔ کد';
    function paint() {
      if (seconds > 0) {
        button.disabled = true;
        label.textContent = base + ' (' + toFa(seconds) + ' ثانیه)';
      } else {
        button.disabled = false;
        label.textContent = base;
      }
    }
    paint();
    if (seconds <= 0) return;
    var timer = window.setInterval(function () {
      seconds -= 1;
      paint();
      if (seconds <= 0) window.clearInterval(timer);
    }, 1000);
  }

  function init() {
    Array.prototype.forEach.call(doc.querySelectorAll('[data-otp-form]'), initOtp);
    Array.prototype.forEach.call(doc.querySelectorAll('[data-resend-seconds]'), initResend);
  }

  if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', init);
  else init();
})();
