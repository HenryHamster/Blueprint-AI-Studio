#!/usr/bin/env node
'use strict';
// Renders the <!-- blueprint:guard-suppressions --> block from the cross-lane
// aggregate written by `scripts/guard_suppression_receipt.py aggregate --out`.
// That aggregate merges every guard lane's per-lane receipt (static, vitest,
// typecheck, viewport, backend pytest) into one `{lanes, totals}` object — this
// renderer is the human-readable projection of it in the PR body, alongside
// (not replacing) the backend-only "Suppressed baseline failures" section that
// render_suppressed_baseline_section already produces.
//
// Usage: node guard-suppressions-pr-section.js <aggregate-json-path>
//
// Outputs the marker-wrapped markdown, or nothing (exit 0) when the aggregate
// is absent, invalid, or has no lane with a suppression to show. Mirrors
// sr-pr-section.js / screenshots-pr-section.js: silent no-op so ./pr can call
// it unconditionally without affecting the publication verdict.
const fs = require('fs');

const aggregatePath = process.argv[2];

const MO = '<!-- blueprint:guard-suppressions -->';
const MC = '<!-- /blueprint:guard-suppressions -->';
// Bound rows so a runaway aggregate can't bloat the PR body — the producer
// already caps each lane's bucket at 25 entries, this bounds how many of
// those we surface per lane in the human-readable section.
const MAX_LANES = 12;
const MAX_IDENTITIES_PER_LANE = 10;
const MAX_LEN = 2000;
// Bucket order mirrors guard_suppression_receipt.py's _BUCKETS.
const BUCKETS = ['suppressed', 'disappeared', 'skipped'];

if (!aggregatePath || !fs.existsSync(aggregatePath)) process.exit(0);

let aggregate;
try {
  aggregate = JSON.parse(fs.readFileSync(aggregatePath, 'utf8'));
} catch (e) {
  process.exit(0);
}

const lanes = aggregate && typeof aggregate.lanes === 'object' && aggregate.lanes !== null ? aggregate.lanes : null;
if (!lanes) process.exit(0);

const laneNames = Object.keys(lanes).sort();
if (laneNames.length === 0) process.exit(0);

const lines = ['### Cross-lane guard suppressions'];
let rendered = 0;
for (const lane of laneNames) {
  if (rendered >= MAX_LANES) {
    lines.push('- ' + (laneNames.length - rendered) + ' more lane(s) not shown');
    break;
  }
  const record = lanes[lane];
  if (!record || typeof record !== 'object') continue;
  const state = typeof record.state === 'string' && record.state ? record.state : 'unknown';
  const buckets = record.buckets && typeof record.buckets === 'object' ? record.buckets : {};
  // All three buckets are rendered: some lanes (the viewport lane's
  // capture-skipped branch) record their only identity outside `suppressed`,
  // and the static lane's disappeared entries are the anti-gaming signal.
  const parts = [];
  for (const bucket of BUCKETS) {
    const payload = buckets[bucket] && typeof buckets[bucket] === 'object' ? buckets[bucket] : {};
    const entries = payload.entries && typeof payload.entries === 'object' ? payload.entries : {};
    const identities = Object.keys(entries).sort();
    if (identities.length === 0) continue;
    const observedCount =
      typeof payload.observed_count === 'number' && payload.observed_count >= identities.length
        ? payload.observed_count
        : identities.length;
    const shown = identities.slice(0, MAX_IDENTITIES_PER_LANE);
    const shownText = shown.map(function (id) { return '`' + id + '`'; }).join(', ');
    const more = observedCount - shown.length;
    parts.push(bucket + ': ' + shownText + (more > 0 ? ' (+' + more + ' more)' : ''));
  }

  lines.push('- lane `' + lane + '`: state=' + state + (parts.length ? '; ' + parts.join('; ') : ''));
  rendered += 1;
}

if (rendered === 0) process.exit(0);

let block = MO + '\n' + lines.join('\n') + '\n' + MC;
if (block.length > MAX_LEN) {
  block = block.slice(0, MAX_LEN - (MC.length + 1)) + '\n' + MC;
}
process.stdout.write(block);
