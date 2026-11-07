// Core's tiny local assertion helper; NOT WPT testharness.js or a compatibility API.
const rows = [];
let assertionCount = 0;
function assert(value, message) {
  assertionCount++;
  if (value !== true) throw new Error(message + ' (actual: ' + String(value) + ')');
}
function equal(actual, expected, message) {
  assertionCount++;
  if (actual !== expected) throw new Error(message + ': expected ' + String(expected) + ', got ' + String(actual));
}
function near(actual, expected, message) {
  assertionCount++;
  // CSS geometry only: 1/64 CSS px is the explicitly bounded layout rounding allowance.
  if (!Number.isFinite(actual) || Math.abs(actual - expected) > 1 / 64)
    throw new Error(message + ': expected ' + expected + ' +/- 1/64, got ' + actual);
}
async function check(name, body) {
  const start = assertionCount;
  try {
    await body();
    rows.push({name, status: 'PASS', assertions: assertionCount - start});
  } catch (error) {
    rows.push({name, status: 'FAIL', assertions: assertionCount - start, message: String(error), stack: error.stack || ''});
  }
}
