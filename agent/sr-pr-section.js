#!/usr/bin/env node
'use strict';
// Renders the <!-- blueprint:self-review --> block from self-review JSON.
// Usage: node sr-pr-section.js <sr-json-path> [<skip-reason-path>]
// Outputs the marker-wrapped markdown, or nothing when JSON is absent/invalid.
const fs = require('fs');
const srPath = process.argv[2];
const skipPath = process.argv[3];
const MO = '<!-- blueprint:self-review -->';
const MC = '<!-- /blueprint:self-review -->';
const MAX_LEN = 600;

if (!srPath || !fs.existsSync(srPath)) process.exit(0);
let sr;
try { sr = JSON.parse(fs.readFileSync(srPath, 'utf8')); } catch (e) { process.exit(0); }

const verdict = sr.verdict || 'unknown';
let block;

if (verdict === 'unknown') {
  let reason = 'unknown';
  if (skipPath) { try { const t = fs.readFileSync(skipPath, 'utf8').trim(); if (t) reason = t; } catch (e) {} }
  if (reason === 'unknown') reason = sr.skip_reason || 'unknown';
  block = MO + '\n## Self-Review\nSelf-review skipped — reason: ' + reason + '\n' + MC;
} else {
  const mark = verdict === 'pass' ? '✓' : '✗';
  const fixes = sr.fixes_applied ? 'yes' : 'no';
  const cr = sr.check_results || {};
  const fail = Object.entries(cr).filter(function (entry) { return !entry[1].passed; }).map(function (entry) { return entry[0]; });
  const edges = Array.isArray(sr.edges_considered) ? sr.edges_considered : [];
  const rfind = Array.isArray(sr.reviewer_findings) ? sr.reviewer_findings : [];
  const pv = sr.prompt_version || '';

  const lines = ['## Self-Review'];
  lines.push('- **Verdict:** ' + verdict + ' ' + mark + ' — fixes_applied: ' + fixes);
  if (fail.length > 0) {
    const MAX_CATS = 5;
    let cats = fail.slice(0, MAX_CATS).join(', ');
    if (fail.length > MAX_CATS) cats += ', +' + (fail.length - MAX_CATS) + ' more';
    lines.push('- **Failing categories (' + fail.length + '):** ' + cats);
  }
  if (edges.length > 0) lines.push('- **Edges considered (' + edges.length + '):**');
  if (rfind.length > 0) {
    const c = { CRITICAL: 0, HIGH: 0, MEDIUM: 0, LOW: 0 };
    rfind.forEach(function (f) { const s = (f.severity || '').toUpperCase(); if (Object.prototype.hasOwnProperty.call(c, s)) c[s]++; });
    const parts = Object.entries(c).filter(function (e) { return e[1] > 0; }).map(function (e) { return e[0] + ': ' + e[1]; });
    if (parts.length > 0) lines.push('- **Fresh-reviewer findings:** ' + parts.join(', '));
  }
  if (pv) lines.push('- **prompt_version:** ' + pv);
  block = MO + '\n' + lines.join('\n') + '\n' + MC;
}

if (block.length > MAX_LEN) block = block.slice(0, MAX_LEN - MC.length - 5) + '…\n' + MC;
process.stdout.write(block + '\n');
