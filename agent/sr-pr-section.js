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
const MAX_LEN = 1500;

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
  const cats = Array.isArray(sr.categories) ? sr.categories : [];
  const truncFiles = Array.isArray(sr.truncated_files_read) ? sr.truncated_files_read : [];
  const pv = sr.prompt_version || '';

  const lines = ['## Self-Review'];
  lines.push('- **Verdict:** ' + verdict + ' ' + mark + ' — fixes_applied: ' + fixes);

  // High-priority: unhandled edges (author-flagged)
  const unhandledEdges = edges.filter(function (e) { return e && e.handled === false; });
  if (unhandledEdges.length > 0) {
    const MAX_EDGES = 3;
    const edgesToShow = unhandledEdges.slice(0, MAX_EDGES);
    lines.push('- **⚠ Unhandled edges (author-flagged):**');
    edgesToShow.forEach(function (e) {
      const desc = e.case || 'edge';
      const blind = e.blind_spot ? ' — ' + e.blind_spot : '';
      lines.push('  - ' + desc + blind);
    });
    if (unhandledEdges.length > MAX_EDGES) {
      lines.push('  - +' + (unhandledEdges.length - MAX_EDGES) + ' more');
    }
  }

  // Medium-priority: unverified files when incomplete_review
  if ((cats.includes('incomplete_review') || truncFiles.length > 0) && truncFiles.length > 0) {
    lines.push('- **Files not re-verified:**');
    truncFiles.forEach(function (f) {
      lines.push('  - ' + f);
    });
  }

  // Lower-priority details
  if (fail.length > 0) {
    const MAX_CATS = 5;
    let catStr = fail.slice(0, MAX_CATS).join(', ');
    if (fail.length > MAX_CATS) catStr += ', +' + (fail.length - MAX_CATS) + ' more';
    lines.push('- **Failing categories (' + fail.length + '):** ' + catStr);
  }

  const handledEdges = edges.filter(function (e) { return e && e.handled !== false; });
  if (handledEdges.length > 0) {
    lines.push('- **Edges considered (handled): ' + handledEdges.length + '**');
  }

  if (pv) lines.push('- **prompt_version:** ' + pv);
  block = MO + '\n' + lines.join('\n') + '\n' + MC;
}

if (block.length > MAX_LEN) {
  // Truncation with priority: unhandled-edges > unverified > everything else
  // For now, simple truncation. In future could implement smarter priority-based truncation.
  block = block.slice(0, MAX_LEN - MC.length - 5) + '…\n' + MC;
}
process.stdout.write(block + '\n');
