// Stand-alone unit tests for the front-end number/date helpers.  They need only
// node (>= 18):   node --test tests/js/
// They pin facts that must never drift (AGENTS.md test vector, Nowruz anchors,
// known weekdays) independently of the Python implementation.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const js = (name) => require(join(here, '..', '..', 'app', 'static', 'js', name));
const M = js('money.js');
const J = js('jalali.js');

test('money: AGENTS.md payroll test vector formats as 1,870,000', () => {
  const total = 1000000 + 80 * 9000 + 150000;
  assert.equal(total, 1870000);
  assert.equal(M.formatToman(total, { fa: false }), '1,870,000');
  assert.equal(M.formatToman(total, { fa: true, suffix: true }), '۱٬۸۷۰٬۰۰۰ تومان');
});

test('money: parse accepts every digit system and separator', () => {
  for (const text of ['1870000', '1,870,000', '۱٬۸۷۰٬۰۰۰', '۱،۸۷۰،۰۰۰', '١٬٨٧٠٬٠٠٠', '1 870 000', '1,870,000 تومان']) {
    assert.equal(M.parseToman(text), 1870000, text);
  }
});

test('money: floats, bad grouping, letters and overflow are rejected', () => {
  for (const text of ['1.5', '1e3', '1,87,000', '12abc', '1000000000000001', '-5']) {
    assert.throws(() => M.parseToman(text), Error, text);
    assert.equal(M.parseMoney(text), null, text);
  }
  assert.throws(() => M.parseToman(12.5));
  assert.throws(() => M.parseToman(true));
});

test('money: blank is zero unless a value is required', () => {
  assert.equal(M.parseToman(''), 0);
  assert.equal(M.parseToman(null), 0);
  assert.throws(() => M.parseToman('', { allowBlank: false }));
});

test('money: negatives only with allowNegative', () => {
  assert.equal(M.parseToman('-1,000', { allowNegative: true }), -1000);
  assert.equal(M.formatToman(-1000, { fa: false }), '-1,000');
});

test('money: digit conversion round-trips', () => {
  assert.equal(M.toFaDigits('0123456789'), '۰۱۲۳۴۵۶۷۸۹');
  assert.equal(M.toLatinDigits('۰۱۲۳۴۵۶۷۸۹'), '0123456789');
  assert.equal(M.toLatinDigits('٠١٢٣٤٥٦٧٨٩'), '0123456789');
  assert.equal(M.toLatinDigits('۱٬۲۳۴'), '1,234');
});

test('jalali: Nowruz anchors (1 Farvardin)', () => {
  assert.deepEqual([...J.toGregorian(1403, 1, 1)], [2024, 3, 20]);
  assert.deepEqual([...J.toGregorian(1404, 1, 1)], [2025, 3, 21]);
  assert.deepEqual([...J.toGregorian(1405, 1, 1)], [2026, 3, 21]);
  assert.deepEqual([...J.toJalali(2025, 3, 21)], [1404, 1, 1]);
  assert.deepEqual([...J.toJalali(2025, 3, 20)], [1403, 12, 30]);
});

test('jalali: leap years and month lengths', () => {
  for (const y of [1399, 1403, 1408]) assert.equal(J.isLeap(y), true, String(y));
  for (const y of [1400, 1401, 1402, 1404, 1405]) assert.equal(J.isLeap(y), false, String(y));
  assert.equal(J.monthLength(1403, 12), 30);
  assert.equal(J.monthLength(1404, 12), 29);
  assert.equal(J.monthLength(1404, 1), 31);
  assert.equal(J.monthLength(1404, 7), 30);
  assert.throws(() => J.monthLength(1404, 13), RangeError);
  assert.throws(() => J.toGregorian(1404, 12, 30), RangeError);
});

test('jalali: named properties on conversion results', () => {
  const j = J.toJalali(2025, 3, 21);
  assert.equal(j.jy, 1404);
  assert.equal(j.jm, 1);
  assert.equal(j.jd, 1);
  const g = J.toGregorian(1404, 1, 1);
  assert.equal(g.gy, 2025);
  assert.equal(g.gm, 3);
  assert.equal(g.gd, 21);
});

test('jalali: weekday index is Saturday-first', () => {
  assert.equal(J.weekdayIndex(2025, 3, 22), 0); // Saturday
  assert.equal(J.weekdayIndex(2025, 3, 21), 6); // Friday
  assert.equal(J.weekdayIndex(2025, 3, 23), 1); // Sunday
  assert.equal(J.weekdayIndex(1970, 1, 1), 5); // Thursday
});

test('jalali: parse and format', () => {
  assert.deepEqual([...J.parse('۱۴۰۴/۰۱/۰۱')], [2025, 3, 21]);
  assert.deepEqual([...J.parse('1404-1-1')], [2025, 3, 21]);
  assert.deepEqual([...J.parse('14040101')], [2025, 3, 21]);
  assert.equal(J.parse('1404/12/30'), null);
  assert.equal(J.parse('nonsense'), null);
  assert.equal(J.isoToJalaliText('2025-03-21', 'numeric'), '1404/01/01');
  assert.equal(J.isoToJalaliText('2025-03-21', 'long'), '1 فروردین 1404');
  assert.equal(J.isoToJalaliText('2025-03-21', 'weekday_long', { fa: true }), 'جمعه ۱ فروردین ۱۴۰۴');
  assert.equal(J.isoToJalaliText('garbage', 'numeric'), '');
});

test('jalali: month grid is Saturday-first with zero padding', () => {
  const grid = J.monthGrid(1404, 1); // 1 Farvardin 1404 is a Friday
  assert.equal(grid[0].length, 7);
  assert.deepEqual(grid[0], [0, 0, 0, 0, 0, 0, 1]);
  const days = grid.flat().filter(Boolean);
  assert.equal(days.length, 31);
  assert.ok(grid.every((w) => w.length === 7));
});

test('jalali: today is computed in Iran time (UTC+03:30)', () => {
  // 2025-03-20T20:45:00Z is already 2025-03-21 00:15 in Tehran: 1 Farvardin 1404.
  assert.deepEqual([...J.todayJalali(new Date('2025-03-20T20:45:00Z'))], [1404, 1, 1]);
  assert.deepEqual([...J.todayJalali(new Date('2025-03-20T20:15:00Z'))], [1403, 12, 30]);
});

test('jalali: round trip over a decade', () => {
  for (let jy = 1395; jy <= 1415; jy++) {
    for (let jm = 1; jm <= 12; jm++) {
      for (let jd = 1; jd <= J.monthLength(jy, jm); jd++) {
        const g = J.toGregorian(jy, jm, jd);
        assert.deepEqual([...J.toJalali(g[0], g[1], g[2])], [jy, jm, jd]);
      }
    }
  }
});
