/*!
 * Atelier Sayyar - money.js
 *
 * Integer-Toman helpers.  A line-by-line port of app/services/money.py:
 * tests/test_jalali_js_parity.py runs both implementations over the same
 * cases (tests/js/money_cases.json), so keep the two in sync.
 *
 * Rules (AGENTS.md): money is an integer number of Toman, never a float.
 * Input may use Persian / Arabic-Indic / Latin digits and any thousands
 * separator; everything is normalised to Latin digits before parsing.
 *
 * Works as a plain <script> (window.AtelierMoney) and as a CommonJS module
 * (node: require('.../money.js')).
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.AtelierMoney = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  // 10^15 Toman: far below Number.MAX_SAFE_INTEGER (~9.007e15), so every
  // accepted amount is represented exactly.
  var MAX_TOMAN = 1000000000000000;
  var TOMAN_WORD = 'تومان';

  var PERSIAN_ZERO = 0x06f0; // ۰
  var ARABIC_INDIC_ZERO = 0x0660; // ٠
  var FULLWIDTH_ZERO = 0xff10; // ０

  // Any digit system -> Latin digit; separators -> ASCII counterparts.
  var TO_LATIN = {};
  var TO_PERSIAN = {};
  (function build() {
    for (var i = 0; i < 10; i++) {
      TO_LATIN[String.fromCharCode(PERSIAN_ZERO + i)] = String(i);
      TO_LATIN[String.fromCharCode(ARABIC_INDIC_ZERO + i)] = String(i);
      TO_LATIN[String.fromCharCode(FULLWIDTH_ZERO + i)] = String(i);
      TO_PERSIAN[String(i)] = String.fromCharCode(PERSIAN_ZERO + i);
      TO_PERSIAN[String.fromCharCode(ARABIC_INDIC_ZERO + i)] = String.fromCharCode(PERSIAN_ZERO + i);
      TO_PERSIAN[String.fromCharCode(FULLWIDTH_ZERO + i)] = String.fromCharCode(PERSIAN_ZERO + i);
    }
    TO_LATIN['٬'] = ','; // ٬ ARABIC THOUSANDS SEPARATOR
    TO_LATIN['،'] = ','; // ، ARABIC COMMA
    TO_LATIN['٫'] = '.'; // ٫ ARABIC DECIMAL SEPARATOR
  })();

  var LATIN_RE = /[۰-۹٠-٩０-９٬،٫]/g;
  var PERSIAN_RE = /[0-9٠-٩０-９]/g;
  // Zero-width / bidi control characters people paste along with numbers.
  var INVISIBLE_RE = /[​-‏‪-‮⁦-⁩﻿]/g;
  var MINUS_CHARS = '-−‐‑‒–';
  var UNIT_SUFFIX_RE = /(?:تومان|تومن|tomans?)$/i;
  var WHITESPACE_RE = /\s+/g;
  var PLAIN_RE = /^\d+$/;
  var GROUPED_RE = /^\d{1,3}(?:,\d{3})+$/;
  var MAX_DIGITS = String(MAX_TOMAN).length;

  /** Every Persian / Arabic-Indic / full-width digit as a Latin digit (and ٬ ، ٫ as , , .). */
  function normalizeDigits(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(LATIN_RE, function (c) {
      return TO_LATIN[c];
    });
  }

  /** Every Latin / Arabic-Indic digit as a Persian digit (۰-۹). Separators are untouched. */
  function toPersianDigits(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(PERSIAN_RE, function (c) {
      return TO_PERSIAN[c];
    });
  }

  function blank(allowBlank) {
    if (allowBlank) return 0;
    throw new Error('A Toman amount is required');
  }

  function checkRange(n, allowNegative) {
    if (n < 0 && !allowNegative) throw new Error('Negative Toman amounts are not allowed here');
    if (Math.abs(n) > MAX_TOMAN) throw new Error('Toman amount is too large (maximum ' + MAX_TOMAN + ')');
    // -0 would print as "0" but compares unequal to 0 under Object.is.
    return n === 0 ? 0 : n;
  }

  /**
   * Parse a Toman amount into an integer Number, or throw Error.
   * Mirrors parse_toman(): see app/services/money.py for the accepted forms.
   * opts: { allowBlank = true, allowNegative = false }.
   */
  function parseToman(value, opts) {
    opts = opts || {};
    var allowBlank = opts.allowBlank !== false;
    var allowNegative = !!opts.allowNegative;

    if (value === null || value === undefined) return blank(allowBlank);
    if (typeof value === 'boolean') throw new Error('Boolean is not a valid Toman amount');
    if (typeof value === 'bigint') {
      if (value > BigInt(MAX_TOMAN) || value < -BigInt(MAX_TOMAN)) {
        throw new Error('Toman amount is too large (maximum ' + MAX_TOMAN + ')');
      }
      return checkRange(Number(value), allowNegative);
    }
    if (typeof value === 'number') {
      // JS has one Number type: only integral values are "ints" (5.0 === 5).
      if (!Number.isFinite(value) || !Number.isInteger(value)) {
        throw new Error('Floats are not allowed for money; use an integer number of Toman');
      }
      if (!Number.isSafeInteger(value)) throw new Error('Toman amount is too large (maximum ' + MAX_TOMAN + ')');
      return checkRange(value, allowNegative);
    }
    if (typeof value !== 'string') throw new Error('Cannot parse ' + typeof value + ' as Toman');

    var text = normalizeDigits(value).replace(INVISIBLE_RE, '').trim();
    text = text.replace(UNIT_SUFFIX_RE, '').trim();
    if (text === '') return blank(allowBlank);

    var negative = false;
    if (MINUS_CHARS.indexOf(text.charAt(0)) !== -1) {
      negative = true;
      text = text.slice(1).trim();
    }
    if (text === '') throw new Error('Invalid Toman amount: sign without digits');
    if (negative && !allowNegative) throw new Error('Negative Toman amounts are not allowed here');

    // Spaces are accepted as thousands separators, but only in groups of three.
    text = text.replace(WHITESPACE_RE, ',');
    var digits;
    if (PLAIN_RE.test(text)) {
      digits = text;
    } else if (GROUPED_RE.test(text)) {
      digits = text.replace(/,/g, '');
    } else {
      throw new Error('Invalid Toman amount: ' + JSON.stringify(value));
    }
    digits = digits.replace(/^0+/, '') || '0';
    if (digits.length > MAX_DIGITS) throw new Error('Toman amount is too large (maximum ' + MAX_TOMAN + ')');
    var number = parseInt(digits, 10);
    return checkRange(negative ? -number : number, allowNegative);
  }

  /**
   * Forgiving variant for UI code: integer Number, or null when the text is
   * not a valid amount.  Blank text is 0.
   */
  function parseMoney(value, opts) {
    try {
      return parseToman(value, opts);
    } catch (e) {
      return null;
    }
  }

  /**
   * Format an integer Toman amount with thousands separators.
   * formatToman(1870000, {fa: false}) -> "1,870,000"
   * formatToman(1870000, {fa: true, suffix: true}) -> "۱٬۸۷۰٬۰۰۰ تومان"
   * Default is {fa: true} (this file is for on-screen display).  Python's
   * format_toman defaults to Latin digits - pass {fa: false} for parity.
   */
  function formatToman(n, opts) {
    opts = opts || {};
    var fa = opts.fa !== false;
    var suffix = !!opts.suffix;
    var number;
    if (typeof n === 'number' && !Number.isInteger(n)) {
      throw new Error('Floats are not allowed for money; use an integer number of Toman');
    }
    if (typeof n === 'number' && Number.isInteger(n)) {
      // Like Python's format_toman, plain integers are not range-checked;
      // only values a Number cannot represent exactly are refused.
      if (!Number.isSafeInteger(n)) throw new Error('Toman amount is too large (maximum ' + MAX_TOMAN + ')');
      number = n === 0 ? 0 : n;
    } else {
      number = parseToman(n, { allowBlank: true, allowNegative: true });
    }
    var text = groupDigits(String(Math.abs(number)));
    if (fa) text = toPersianDigits(text).replace(/,/g, '٬');
    if (number < 0) text = '-' + text;
    if (suffix) text = text + ' ' + TOMAN_WORD;
    return text;
  }

  /** "1870000" -> "1,870,000" (Latin digits, string in, string out). */
  function groupDigits(digits) {
    return digits.replace(/\B(?=(\d{3})+(?!\d))/g, ',');
  }

  return {
    MAX_TOMAN: MAX_TOMAN,
    TOMAN_WORD: TOMAN_WORD,
    normalizeDigits: normalizeDigits,
    toPersianDigits: toPersianDigits,
    toFaDigits: toPersianDigits,
    toLatinDigits: normalizeDigits,
    parseToman: parseToman,
    parseMoney: parseMoney,
    formatToman: formatToman,
    groupDigits: groupDigits,
  };
});
