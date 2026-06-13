#!/usr/bin/env node
'use strict';
// Renders the <!-- blueprint:screenshots --> block from a viewport snapshot
// manifest (scripts/viewport_manifest.py output). Each changed/new before/after
// pair becomes a row of markdown links to the admin viewport-shots download
// endpoint, which mints a fresh signed URL on every click (so the PR body stays
// valid past the 15-minute signed-PUT window — we deliberately link, never
// inline `![...]()`).
//
// Usage: node screenshots-pr-section.js <manifest-path> <orchestrator-url> <project-id> <task-id>
//
// Outputs the marker-wrapped markdown, or nothing (exit 0) when the manifest is
// absent, invalid, or has no changed/new pairs. This mirrors sr-pr-section.js:
// silent no-op so ./pr can call it unconditionally.
const fs = require('fs');

const manifestPath = process.argv[2];
const orchestratorUrl = (process.argv[3] || '').replace(/\/+$/, '');
const projectId = process.argv[4] || '';
const taskId = process.argv[5] || '';

const MO = '<!-- blueprint:screenshots -->';
const MC = '<!-- /blueprint:screenshots -->';
// Cap rows so a runaway manifest can't bloat the PR body. 12 pairs = 24 images,
// matching the backend upload cap (24 context entries, agent.py:4033).
const MAX_ROWS = 12;

if (!manifestPath || !fs.existsSync(manifestPath)) process.exit(0);

let manifest;
try {
  manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
} catch (e) {
  process.exit(0);
}

const pairs = Array.isArray(manifest && manifest.pairs) ? manifest.pairs : [];
if (pairs.length === 0) process.exit(0);

const shown = pairs.filter(function (p) {
  return p && (p.status === 'changed' || p.status === 'new');
});
if (shown.length === 0) process.exit(0);

// Summary counts: prefer the manifest summary, fall back to recomputing from
// pairs so the line is honest even if summary is missing/stale.
function countByStatus(status) {
  if (manifest.summary && typeof manifest.summary[status] === 'number') {
    return manifest.summary[status];
  }
  return pairs.filter(function (p) { return p && p.status === status; }).length;
}
const summaryParts = [];
['changed', 'new', 'unchanged', 'removed'].forEach(function (status) {
  const n = countByStatus(status);
  if (n > 0) summaryParts.push(n + ' ' + status);
});

// Build the download URL the admin endpoint expects. The upload sibling sends
// name=pair.test, width=pair.width, phase=before|after, and the endpoint stores
// the object under sanitize(name)-width-phase.png. The download endpoint
// re-sanitizes the whole `name` query param the same way, and `-`, digits, and
// "before"/"after" are all in the safe set, so passing `${test}-${width}-${phase}`
// reconstructs the identical object path.
function downloadUrl(test, width, phase) {
  const name = test + '-' + width + '-' + phase;
  return (
    orchestratorUrl +
    '/api/admin/viewport-shots/download-url' +
    '?project_id=' + encodeURIComponent(projectId) +
    '&task_id=' + encodeURIComponent(taskId) +
    '&name=' + encodeURIComponent(name)
  );
}

// Escape `|` so a component/test name can never break the markdown table.
function cell(text) {
  return String(text).replace(/\|/g, '\\|');
}

const rows = shown.slice(0, MAX_ROWS);
const truncated = shown.length - rows.length;

const lines = ['## Viewport Screenshots'];
if (summaryParts.length > 0) lines.push(summaryParts.join(', '));
lines.push('');
lines.push('| Component | Width | Status | Before | After |');
lines.push('| --- | --- | --- | --- | --- |');
rows.forEach(function (p) {
  const test = p.test != null ? String(p.test) : '(unknown)';
  const width = p.width != null ? String(p.width) : '?';
  // `new` pairs have no baseline ("before") capture — leave that cell empty.
  const before = p.status === 'new'
    ? ''
    : '[view](' + downloadUrl(test, width, 'before') + ')';
  const after = '[view](' + downloadUrl(test, width, 'after') + ')';
  lines.push(
    '| ' + cell(test) + ' | ' + cell(width) + ' | ' + cell(p.status) +
    ' | ' + before + ' | ' + after + ' |',
  );
});
if (truncated > 0) {
  lines.push('');
  lines.push('_+' + truncated + ' more changed/new pair(s) not shown (capped at ' + MAX_ROWS + ')._');
}

const block = MO + '\n' + lines.join('\n') + '\n' + MC;
process.stdout.write(block + '\n');
