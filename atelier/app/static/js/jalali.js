/*!
 * Atelier Sayyar - jalali.js
 *
 * Jalali (Solar Hijri) <-> Gregorian conversion, a port of
 * app/services/jalali.py.  Both use the same break-year table (jalaali-js
 * style) for 1 Farvardin, so they agree on every date of Jalali years
 * 1..3000; tests/test_jalali_js_parity.py sweeps 1300-1500 day by day and
 * the whole range by year.
 *
 * Conventions
 *  - Dates are plain integers: toJalali(gy, gm, gd) -> [jy, jm, jd].
 *    The returned arrays also carry named props (jy/jm/jd or gy/gm/gd) so
 *    both `const [y, m, d] = ...` and `r.jy` work.
 *  - Weeks are Saturday-first: weekdayIndex() is 0 for Saturday ... 6 for
 *    Friday (the Iranian weekend day).
 *  - "Today" is computed in Iran time (fixed UTC+03:30 since 2022).
 *  - Invalid input throws RangeError (Python raises ValueError).
 *
 * Works as a plain <script> (window.AtelierJalali) and as a CommonJS module.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.AtelierJalali = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MIN_YEAR = 1;
  var MAX_YEAR = 3000;
  var DAY_MS = 86400000;
  var IRAN_OFFSET_MS = (3 * 60 + 30) * 60000;

  var MONTHS = [
    'فروردین', 'اردیبهشت', 'خرداد', 'تیر', 'مرداد', 'شهریور',
    'مهر', 'آبان', 'آذر', 'دی', 'بهمن', 'اسفند',
  ];
  // Saturday first, matching weekdayIndex().
  var WEEKDAYS = ['شنبه', 'یکشنبه', 'دوشنبه', 'سه‌شنبه', 'چهارشنبه', 'پنجشنبه', 'جمعه'];
  var WEEKDAYS_SHORT = ['ش', 'ی', 'د', 'س', 'چ', 'پ', 'ج'];

  // First year of each regular 33-year-cycle segment (jalaali-js break table).
  var BREAKS = [-61, 9, 38, 199, 426, 686, 756, 818, 1111, 1181, 1210,
    1635, 2060, 2097, 2192, 2262, 2324, 2394, 2456, 3178];

  // Days before the first day of each month in a normal (non-leap) year.
  var DAYS_BEFORE = [];
  for (var m = 0; m < 12; m++) DAYS_BEFORE.push(m < 6 ? m * 31 : 186 + (m - 6) * 30);

  function isInt(x) {
    return typeof x === 'number' && Number.isInteger(x);
  }

  function div(a, b) {
    return Math.floor(a / b);
  }

  function checkYear(jy) {
    if (!isInt(jy)) throw new RangeError('Jalali year must be an integer, got ' + jy);
    if (jy < MIN_YEAR || jy > MAX_YEAR) {
      throw new RangeError('Jalali year ' + jy + ' is outside the supported range ' + MIN_YEAR + '..' + MAX_YEAR);
    }
    return jy;
  }

  // Days since 1970-01-01 of a proleptic-Gregorian date.  Date.UTC is exact
  // for the years involved (622..3621); the years 0..99 quirk never applies.
  function epochDay(gy, gm, gd) {
    return Math.round(Date.UTC(gy, gm - 1, gd) / DAY_MS);
  }

  var nowruzCache = {};
  /** Epoch-day number of 1 Farvardin of Jalali year jy. */
  function nowruzDay(jy) {
    var cached = nowruzCache[jy];
    if (cached !== undefined) return cached;
    var gy = jy + 621;
    var leapJ = -14;
    var jp = BREAKS[0];
    var jump = 0;
    for (var i = 1; i < BREAKS.length; i++) {
      var jm = BREAKS[i];
      jump = jm - jp;
      if (jy < jm) break;
      leapJ += div(jump, 33) * 8 + div(jump % 33, 4);
      jp = jm;
    }
    var n = jy - jp;
    leapJ += div(n, 33) * 8 + div((n % 33) + 3, 4);
    if (jump % 33 === 4 && jump - n === 4) leapJ += 1;
    var leapG = div(gy, 4) - div((div(gy, 100) + 1) * 3, 4) - 150;
    var marchDay = 20 + leapJ - leapG;
    var result = epochDay(gy, 3, marchDay);
    nowruzCache[jy] = result;
    return result;
  }

  /** True when Esfand of Jalali year jy has 30 days (1399, 1403, 1408, ...). */
  function isLeap(jy) {
    checkYear(jy);
    return nowruzDay(jy + 1) - nowruzDay(jy) === 366;
  }

  /** Days in Jalali month jm (1..12) of year jy. */
  function monthLength(jy, jm) {
    checkYear(jy);
    if (!isInt(jm) || jm < 1 || jm > 12) throw new RangeError('Jalali month must be 1..12, got ' + jm);
    if (jm <= 6) return 31;
    if (jm <= 11) return 30;
    return isLeap(jy) ? 30 : 29;
  }

  function tag(arr, names) {
    for (var i = 0; i < names.length; i++) arr[names[i]] = arr[i];
    return arr;
  }

  function fromEpochDay(days) {
    var d = new Date(days * DAY_MS);
    return [d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate()];
  }

  /** Jalali -> Gregorian: [gy, gm, gd] (RangeError on an impossible date). */
  function toGregorian(jy, jm, jd) {
    var length = monthLength(jy, jm); // validates jy and jm
    if (!isInt(jd) || jd < 1 || jd > length) {
      throw new RangeError('Invalid Jalali date ' + jy + '/' + jm + '/' + jd + ': day must be 1..' + length + ' for this month');
    }
    var g = fromEpochDay(nowruzDay(jy) + DAYS_BEFORE[jm - 1] + (jd - 1));
    return tag(g, ['gy', 'gm', 'gd']);
  }

  /** Gregorian -> Jalali: [jy, jm, jd] (RangeError on an impossible date). */
  function toJalali(gy, gm, gd) {
    if (!isInt(gy) || !isInt(gm) || !isInt(gd) || gm < 1 || gm > 12 || gd < 1) {
      throw new RangeError('Invalid Gregorian date ' + gy + '-' + gm + '-' + gd);
    }
    var day = epochDay(gy, gm, gd);
    var check = fromEpochDay(day);
    if (check[0] !== gy || check[1] !== gm || check[2] !== gd) {
      throw new RangeError('Invalid Gregorian date ' + gy + '-' + gm + '-' + gd);
    }
    var jy = gy - 621;
    if (jy < MIN_YEAR || jy > MAX_YEAR + 1) {
      throw new RangeError('Gregorian date ' + isoString(gy, gm, gd) + ' is outside the supported range');
    }
    if (day < nowruzDay(jy)) jy -= 1;
    if (jy < MIN_YEAR || jy > MAX_YEAR) {
      throw new RangeError('Gregorian date ' + isoString(gy, gm, gd) + ' is outside the supported range');
    }
    var doy = day - nowruzDay(jy); // 0-based
    var out;
    if (doy < 186) {
      out = [jy, 1 + div(doy, 31), (doy % 31) + 1];
    } else {
      var rest = doy - 186;
      out = [jy, 7 + div(rest, 30), (rest % 30) + 1];
    }
    return tag(out, ['jy', 'jm', 'jd']);
  }

  /** Saturday = 0, Sunday = 1, ... Friday = 6 for a Gregorian date. */
  function weekdayIndex(gy, gm, gd) {
    var day = epochDay(gy, gm, gd); // 1970-01-01 was a Thursday
    return (((day + 5) % 7) + 7) % 7;
  }

  function pad(n, width) {
    var s = String(n);
    while (s.length < width) s = '0' + s;
    return s;
  }

  /** "2025-03-21" from numbers. */
  function isoString(gy, gm, gd) {
    return pad(gy, 4) + '-' + pad(gm, 2) + '-' + pad(gd, 2);
  }

  /** "2025-03-21" -> [2025, 3, 21] or null when not a valid ISO date. */
  function parseISO(text) {
    if (typeof text !== 'string') return null;
    var m = /^\s*(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ].*)?\s*$/.exec(toLatin(text));
    if (!m) return null;
    var gy = +m[1], gm = +m[2], gd = +m[3];
    var back = fromEpochDay(epochDay(gy, gm, gd));
    if (gm < 1 || gm > 12 || back[0] !== gy || back[1] !== gm || back[2] !== gd) return null;
    return tag([gy, gm, gd], ['gy', 'gm', 'gd']);
  }

  /** Current Gregorian date in Iran time as [gy, gm, gd]. */
  function todayGregorian(now) {
    var ms = (now instanceof Date ? now.getTime() : Date.now()) + IRAN_OFFSET_MS;
    var d = new Date(ms);
    return tag([d.getUTCFullYear(), d.getUTCMonth() + 1, d.getUTCDate()], ['gy', 'gm', 'gd']);
  }

  /** Current Jalali date in Iran time as [jy, jm, jd]. */
  function todayJalali(now) {
    var g = todayGregorian(now);
    return toJalali(g[0], g[1], g[2]);
  }

  // --- text helpers (digits are converted without depending on money.js) ---
  var LATIN_MAP = {};
  var FA_MAP = {};
  for (var i = 0; i < 10; i++) {
    LATIN_MAP[String.fromCharCode(0x06f0 + i)] = String(i);
    LATIN_MAP[String.fromCharCode(0x0660 + i)] = String(i);
    LATIN_MAP[String.fromCharCode(0xff10 + i)] = String(i);
    FA_MAP[String(i)] = String.fromCharCode(0x06f0 + i);
  }
  function toLatin(s) {
    return String(s).replace(/[۰-۹٠-٩０-９]/g, function (c) {
      return LATIN_MAP[c];
    });
  }
  function toFa(s) {
    return String(s).replace(/[0-9]/g, function (c) {
      return FA_MAP[c];
    });
  }

  /**
   * Format a Jalali date.  fmt: 'numeric' (1403/05/12), 'short' (12 مرداد),
   * 'long' (12 مرداد 1403), 'weekday_long', 'month_year', 'weekday'.
   * Mirrors DATE_FORMATS in app/services/jalali.py.  opts.fa converts digits.
   */
  function format(jy, jm, jd, fmt, opts) {
    fmt = fmt || 'numeric';
    var fa = !!(opts && opts.fa);
    var month = MONTHS[jm - 1];
    var out;
    if (fmt === 'numeric') out = pad(jy, 4) + '/' + pad(jm, 2) + '/' + pad(jd, 2);
    else if (fmt === 'short') out = jd + ' ' + month;
    else if (fmt === 'long') out = jd + ' ' + month + ' ' + jy;
    else if (fmt === 'month_year') out = month + ' ' + jy;
    else if (fmt === 'weekday' || fmt === 'weekday_long') {
      var g = toGregorian(jy, jm, jd);
      var wd = WEEKDAYS[weekdayIndex(g[0], g[1], g[2])];
      out = fmt === 'weekday' ? wd : wd + ' ' + jd + ' ' + month + ' ' + jy;
    } else throw new RangeError('Unknown date format ' + fmt);
    return fa ? toFa(out) : out;
  }

  /** ISO Gregorian string -> Jalali text, '' for blank/invalid. */
  function isoToJalaliText(iso, fmt, opts) {
    var g = parseISO(iso);
    if (!g) return '';
    var j = toJalali(g[0], g[1], g[2]);
    return format(j[0], j[1], j[2], fmt, opts);
  }

  var INVISIBLE_RE = /[​-‏‪-‮⁦-⁩﻿]/g;
  var DASHES_RE = /[‐-―−]/g;
  var SEPARATED_RE = /^(\d{4})\s*([\/\-.])\s*(\d{1,2})\s*\2\s*(\d{1,2})$/;
  var COMPACT_RE = /^(\d{4})(\d{2})(\d{2})$/;

  /**
   * Parse Jalali text ("1403/05/12", Persian digits, - or . separators,
   * 14030512) to Gregorian [gy, gm, gd]; null for anything invalid.
   * Mirrors parse_jalali() in app/services/jalali.py.
   */
  function parse(text) {
    if (typeof text !== 'string') return null;
    var cleaned = toLatin(text)
      .replace(/٬|،/g, ',')
      .replace(/٫/g, '.')
      .replace(INVISIBLE_RE, '')
      .trim()
      .replace(DASHES_RE, '-')
      .replace(/\\/g, '/');
    if (cleaned === '') return null;
    var jy, jm, jd;
    var m = SEPARATED_RE.exec(cleaned);
    if (m) {
      jy = +m[1]; jm = +m[3]; jd = +m[4];
    } else {
      m = COMPACT_RE.exec(cleaned);
      if (!m) return null;
      jy = +m[1]; jm = +m[2]; jd = +m[3];
    }
    try {
      return toGregorian(jy, jm, jd);
    } catch (e) {
      return null;
    }
  }

  /**
   * Month grid, Saturday first: array of weeks of 7 cells, each a Jalali day
   * number (1..length) or 0 for padding.
   */
  function monthGrid(jy, jm) {
    var g = toGregorian(jy, jm, 1);
    var lead = weekdayIndex(g[0], g[1], g[2]);
    var length = monthLength(jy, jm);
    var cells = [];
    for (var i = 0; i < lead; i++) cells.push(0);
    for (var d = 1; d <= length; d++) cells.push(d);
    while (cells.length % 7) cells.push(0);
    var weeks = [];
    for (var w = 0; w < cells.length; w += 7) weeks.push(cells.slice(w, w + 7));
    return weeks;
  }

  return {
    MIN_YEAR: MIN_YEAR,
    MAX_YEAR: MAX_YEAR,
    MONTHS: MONTHS,
    WEEKDAYS: WEEKDAYS,
    WEEKDAYS_SHORT: WEEKDAYS_SHORT,
    isLeap: isLeap,
    monthLength: monthLength,
    toJalali: toJalali,
    toGregorian: toGregorian,
    weekdayIndex: weekdayIndex,
    isoString: isoString,
    parseISO: parseISO,
    todayGregorian: todayGregorian,
    todayJalali: todayJalali,
    format: format,
    isoToJalaliText: isoToJalaliText,
    parse: parse,
    monthGrid: monthGrid,
    toLatin: toLatin,
    toFa: toFa,
  };
});
