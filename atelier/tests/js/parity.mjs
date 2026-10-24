// Prints a JSON document describing what app/static/js/jalali.js computes, so
// that tests/test_jalali_js_parity.py can compare it with app/services/jalali.py.
//
//   node tests/js/parity.mjs            # full sweep (Jalali 1300-1500 day by day)
//
// Output shape:
//   sweep.jalali  : [[jy, jm, jd], ...]   every Jalali day of 1300..1500
//   sweep.greg    : [[gy, gm, gd], ...]   toGregorian() of each of those days
//   sweep.back    : [[jy, jm, jd], ...]   toJalali() of each Gregorian day above
//   sweep.weekday : [0..6, ...]           weekdayIndex() of each Gregorian day
//   years         : { "<jy>": { leap, lengths: [12], nowruz: [gy, gm, gd] } }  for 1..3000
//   parse         : [{ text, result: [gy, gm, gd] | null }, ...]
//   format        : [{ iso, fmt, fa, text }, ...]
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const J = require(join(here, '..', '..', 'app', 'static', 'js', 'jalali.js'));

const FIRST_YEAR = 1300;
const LAST_YEAR = 1500;

const sweep = { jalali: [], greg: [], back: [], weekday: [] };
for (let jy = FIRST_YEAR; jy <= LAST_YEAR; jy++) {
  for (let jm = 1; jm <= 12; jm++) {
    const len = J.monthLength(jy, jm);
    for (let jd = 1; jd <= len; jd++) {
      const g = J.toGregorian(jy, jm, jd);
      const back = J.toJalali(g[0], g[1], g[2]);
      sweep.jalali.push([jy, jm, jd]);
      sweep.greg.push([g[0], g[1], g[2]]);
      sweep.back.push([back[0], back[1], back[2]]);
      sweep.weekday.push(J.weekdayIndex(g[0], g[1], g[2]));
    }
  }
}

const years = {};
for (let jy = J.MIN_YEAR; jy <= J.MAX_YEAR; jy++) {
  const lengths = [];
  for (let jm = 1; jm <= 12; jm++) lengths.push(J.monthLength(jy, jm));
  const n = J.toGregorian(jy, 1, 1);
  years[jy] = { leap: J.isLeap(jy), lengths, nowruz: [n[0], n[1], n[2]] };
}

const cases = JSON.parse(readFileSync(join(here, 'jalali_cases.json'), 'utf8'));
const parse = cases.parse.map((text) => {
  const r = J.parse(text);
  return { text, result: r ? [r[0], r[1], r[2]] : null };
});
const format = [];
for (const iso of cases.format_dates) {
  for (const fmt of cases.format_fmts) {
    for (const fa of [false, true]) {
      format.push({ iso, fmt, fa, text: J.isoToJalaliText(iso, fmt, { fa }) });
    }
  }
}

process.stdout.write(JSON.stringify({ sweep, years, parse, format }));
