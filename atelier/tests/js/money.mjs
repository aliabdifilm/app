// Prints a JSON document describing what app/static/js/money.js computes, so
// that tests/test_jalali_js_parity.py can compare it with app/services/money.py.
//
//   node tests/js/money.mjs
//
// Output shape:
//   parse           : [{ text, ok, value }]                parseToman(text)
//   parse_negative  : [{ text, ok, value }]                parseToman(text, { allowNegative: true })
//   parse_required  : [{ text, ok, value }]                parseToman(text, { allowBlank: false })
//   format          : [{ n, fa, suffix, text }]            formatToman(n, { fa, suffix })
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const here = dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const M = require(join(here, '..', '..', 'app', 'static', 'js', 'money.js'));
const cases = JSON.parse(readFileSync(join(here, 'money_cases.json'), 'utf8'));

function run(text, opts) {
  try {
    return { text, ok: true, value: M.parseToman(text, opts) };
  } catch (err) {
    return { text, ok: false, value: null };
  }
}

const out = {
  parse: cases.parse.map((t) => run(t)),
  parse_negative: cases.parse_negative_ok.map((t) => run(t, { allowNegative: true })),
  parse_required: cases.parse_required.map((t) => run(t, { allowBlank: false })),
  format: [],
};
for (const n of cases.format) {
  for (const fa of [false, true]) {
    for (const suffix of [false, true]) {
      let text;
      try {
        text = M.formatToman(n, { fa, suffix });
      } catch (err) {
        text = null;
      }
      out.format.push({ n, fa, suffix, text });
    }
  }
}
process.stdout.write(JSON.stringify(out));
