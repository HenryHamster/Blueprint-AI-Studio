'use strict';

/**
 * Flow-to-criterion gate definitions for the v26 live walkthrough harness.
 *
 * Each criterion's PASS requires ALL listed step-name substrings to match a
 * result with verdict === 'PASS'. Step names are matched with String#includes.
 *
 * c-dfdc9d82 (runner enrollment): requires 1a (register 200), 1b (fleet-visible),
 *   1d (daemon heartbeat-confirmed), 6a (bad-token 401), 6b (two-button UI).
 *   API registration and fleet visibility alone are insufficient — the daemon
 *   must connect and heartbeat, and the bad-token path must return an explicit
 *   error, not an endless spinner.
 *
 * c-f52fd975 (iframe subresource token): requires 2b (asset-token auth passes),
 *   2c (path-embedded route registered), 2g (valid-JWT yields 200), 7a-7e (full
 *   tools lifecycle: create / disable / re-enable / add-same-kind /
 *   no-Placement-dropdown). Asset-token auth alone is insufficient.
 *
 * c-workday-session-lifecycle: requires 5b (start), 5c (liveness reflects session),
 *   5d (UI Stop control rendered), 5e (clean stop), 5f (UI click-to-talk).
 */
const CRITERION_STEP_REQUIREMENTS = {
  'c-dfdc9d82': ['1a.', '1b.', '1d.', '6a.', '6b.'],
  'c-f52fd975': ['2b.', '2c.', '2g.', '7a.', '7b.', '7c.', '7d.', '7e.'],
  'c-workday-session-lifecycle': ['5b.', '5c.', '5d.', '5e.', '5f.'],
};

/**
 * Flow groups mirroring backend/app/acceptance_criteria.py
 * WALKTHROUGH_E2E_FLOW_MARKERS. A criterion whose text contains any of a
 * group's markers (case-insensitive substring) is walkthrough-provable by the
 * flow that drives those steps. Steps are shared with CRITERION_STEP_REQUIREMENTS
 * so the two contracts cannot drift.
 */
const WALKTHROUGH_FLOW_GROUPS = [
  {
    flow: 'runner',
    markers: ['runner enroll', 'runner enrollment'],
    steps: CRITERION_STEP_REQUIREMENTS['c-dfdc9d82'],
  },
  {
    flow: 'iframe',
    markers: ['iframe', 'subresource token', 'project tool'],
    steps: CRITERION_STEP_REQUIREMENTS['c-f52fd975'],
  },
  {
    flow: 'workday',
    markers: ['workday'],
    steps: CRITERION_STEP_REQUIREMENTS['c-workday-session-lifecycle'],
  },
];

/**
 * Legacy harness canonical keys — mirrors backend/app/acceptance_criteria.py
 * WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS. A criterion carrying one of these keys is
 * walkthrough-provable for backward compatibility even when its text matches no
 * flow marker. The meta key c-5214996f is included so a rubric that registers
 * the canonical meta criterion still resolves it.
 */
const WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS = [
  'c-dfdc9d82',
  'c-f52fd975',
  'c-5214996f',
];

/**
 * Substantive legacy harness keys — the legacy raw rows that carry actual flow
 * evidence (mirrors the backend's partial-legacy trigger set, i.e.
 * _WALKTHROUGH_E2E_EXPECTED_RAW_RESULT_KEYS minus the meta c-5214996f). The
 * meta criterion is deliberately excluded: it is the canonical meta key that
 * participates in BOTH the retired v26 report and the open rubric, so a rubric
 * that re-mints its substantive criteria under fresh keys while keeping the
 * canonical meta row must be treated as an OPEN-rubric report, not a legacy one.
 */
const WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_KEYS = [
  'c-dfdc9d82',
  'c-f52fd975',
  'c-workday-session-lifecycle',
];

/** Canonical meta criterion key — mirrors backend _WALKTHROUGH_META_CRITERION_KEY. */
const WALKTHROUGH_META_CRITERION_KEY = 'c-5214996f';

/** Legacy key → flow group mapping used before text-marker matching. */
const LEGACY_KEY_FLOWS = {
  'c-dfdc9d82': 'runner',
  'c-f52fd975': 'iframe',
};

/**
 * Compute per-criterion pass/fail from collected walkthrough step results.
 *
 * @param {Array<{name: string, verdict: string}>} results - step results from step()
 * @param {boolean} shaValid - git cat-file -t <sha> === 'commit'
 * @returns {{
 *   runnerEnrollmentPassed: boolean,
 *   iframeTokenPassed: boolean,
 *   workdaySessionPassed: boolean,
 *   walkthroughMetaPassed: boolean,
 * }}
 */
function computeGates(results, shaValid) {
  function allPass(stepPatterns) {
    if (!shaValid) return false;
    return stepPatterns.every((pattern) => {
      const r = results.find((s) => s.name.includes(pattern));
      return r != null && r.verdict === 'PASS';
    });
  }

  const runnerEnrollmentPassed = allPass(CRITERION_STEP_REQUIREMENTS['c-dfdc9d82']);
  const iframeTokenPassed = allPass(CRITERION_STEP_REQUIREMENTS['c-f52fd975']);
  const workdaySessionPassed = allPass(CRITERION_STEP_REQUIREMENTS['c-workday-session-lifecycle']);
  const walkthroughMetaPassed = shaValid
    && runnerEnrollmentPassed
    && iframeTokenPassed
    && workdaySessionPassed;

  return { runnerEnrollmentPassed, iframeTokenPassed, workdaySessionPassed, walkthroughMetaPassed };
}

/** True iff every step pattern in the flow group is PASS and the SHA is valid. */
function flowAllPass(flow, results, shaValid) {
  if (!shaValid) return false;
  return flow.steps.every((pattern) => {
    const r = results.find((s) => s.name.includes(pattern));
    return r != null && r.verdict === 'PASS';
  });
}

/**
 * Map a criterion to its walkthrough flow group: legacy key membership wins,
 * then case-insensitive text-marker matching against WALKTHROUGH_FLOW_GROUPS.
 * Returns null when the criterion is not walkthrough-provable.
 *
 * @param {string} rawKey - the criterion's raw key
 * @param {string} text - the criterion's text
 * @returns {string|null}
 */
function resolveFlowForCriterion(rawKey, text) {
  if (Object.prototype.hasOwnProperty.call(LEGACY_KEY_FLOWS, rawKey)) {
    return LEGACY_KEY_FLOWS[rawKey];
  }
  const lower = String(text || '').toLowerCase();
  for (const fg of WALKTHROUGH_FLOW_GROUPS) {
    if (fg.markers.some((m) => lower.includes(m))) return fg.flow;
  }
  return null;
}

/**
 * Build the diagnostic notes for one substantive flow row.
 *
 * @param {string} criterionKey
 * @param {object} flowGroup - the WALKTHROUGH_FLOW_GROUPS entry for this row
 */
function buildFlowNotes(criterionKey, flowGroup, passed, shaValid, results) {
  if (passed) {
    return `Walkthrough flow '${flowGroup.flow}' PASSED for ${criterionKey}: all required steps confirmed`;
  }
  const missing = flowGroup.steps
    .map((pattern) => {
      const r = results.find((s) => s.name.includes(pattern));
      return r == null || r.verdict !== 'PASS'
        ? `step ${pattern.replace(/\.$/, '')}: ${r ? r.verdict : 'NOT RUN'}`
        : null;
    })
    .filter(Boolean);
  const parts = missing.length > 0 ? [`required steps not passed: ${missing.join('; ')}`] : [];
  if (!shaValid) parts.push('SHA integrity check failed');
  return parts.join('; ') || `Walkthrough flow '${flowGroup.flow}' not evaluated for ${criterionKey}`;
}

/**
 * Build the diagnostic notes for the meta conjunction row.
 */
function buildMetaNotes(passed, substantive, passByFlow, shaValid) {
  if (passed) {
    return `Walkthrough meta criterion PASSED: all ${substantive.length} substantive walkthrough sub-criteria passed`;
  }
  if (substantive.length === 0) {
    return 'Walkthrough meta criterion FAILED (fail-closed): no substantive walkthrough sub-criteria registered';
  }
  const failed = substantive
    .filter((sub) => !passByFlow[sub.flow])
    .map((sub) => `${sub.criterion_key} (${sub.flow})`);
  const parts = failed.length > 0 ? [`required sub-criteria not passed: ${failed.join('; ')}`] : [];
  if (!shaValid) parts.push('SHA integrity check failed');
  return parts.join('; ') || 'Walkthrough meta criterion not evaluated';
}

/**
 * Resolve a batch of ACTIVE walkthrough-provable criteria into per-criterion
 * result rows for the machine-readable walkthrough report.
 *
 * Mirrors backend/app/acceptance_criteria.py:
 *   - :func:`_walkthrough_meta_required_subcriteria` (flow-marker OR legacy-key
 *     walkthrough-provable membership) and
 *   - :func:`_walkthrough_meta_criterion_gate` (the meta conjunction).
 *
 * Each substantive (non-meta) ACTIVE criterion that is walkthrough-provable maps
 * to one flow group (runner / iframe / workday); its row passes only when ALL of
 * that group's steps are PASS AND the SHA is valid. The meta criterion
 * (c-5214996f) passes only when every other resolved row in the same batch
 * passes, and FAILS CLOSED when no substantive sub-criteria resolve.
 *
 * @param {Array<{criterion_key: string, text: string}>} activeCriteria
 * @param {Array<{name: string, verdict: string}>} results - step results from step()
 * @param {boolean} shaValid
 * @returns {Array<{criterion_key: string, passed: boolean, notes: string}>}
 */
function resolveCriterionResults(activeCriteria, results, shaValid) {
  const substantive = []; // {criterion_key, flow} in active-criteria order
  let metaRow = null; // {criterion_key}

  for (const row of activeCriteria || []) {
    if (!row || typeof row.criterion_key !== 'string') continue;
    const rawKey = row.criterion_key.trim();
    if (!rawKey) continue;
    if (rawKey === WALKTHROUGH_META_CRITERION_KEY) {
      metaRow = { criterion_key: rawKey };
      continue;
    }
    const flow = resolveFlowForCriterion(rawKey, String(row.text || ''));
    if (flow) substantive.push({ criterion_key: rawKey, flow });
  }

  const passByFlow = {};
  for (const fg of WALKTHROUGH_FLOW_GROUPS) {
    passByFlow[fg.flow] = flowAllPass(fg, results, shaValid);
  }

  const rows = [];
  for (const sub of substantive) {
    const fg = WALKTHROUGH_FLOW_GROUPS.find((g) => g.flow === sub.flow);
    rows.push({
      criterion_key: sub.criterion_key,
      passed: passByFlow[sub.flow],
      notes: buildFlowNotes(sub.criterion_key, fg, passByFlow[sub.flow], shaValid, results),
    });
  }

  if (metaRow) {
    const metaPassed = substantive.length > 0
      && substantive.every((sub) => passByFlow[sub.flow]);
    rows.push({
      criterion_key: metaRow.criterion_key,
      passed: metaPassed,
      notes: buildMetaNotes(metaPassed, substantive, passByFlow, shaValid),
    });
  }

  return rows;
}

/**
 * Decide whether resolved open-rubric rows should be emitted as the report, or
 * the harness must fall back to the legacy four-row contract.
 *
 * The meta criterion c-5214996f is NOT a legacy signal: it is the canonical
 * meta key that participates in both the retired v26 report and the open
 * rubric.  A rubric that re-mints its substantive criteria under fresh keys
 * while keeping the canonical meta row therefore yields an open-rubric report
 * (emit the resolved fresh rows).  Only a resolved batch that still carries a
 * SUBSTANTIVE legacy key means the rubric is the legacy v26 rubric — those rows
 * are exactly the legacy report, and an empty resolved batch is a fetch
 * failure / no-walkthrough-provable-criteria signal, both of which fall back to
 * the legacy report.
 *
 * @param {Array<{criterion_key: string, passed: boolean, notes: string}>} resolvedRows
 * @returns {boolean} true when the resolved rows should be written as-is
 */
function shouldEmitResolvedRows(resolvedRows) {
  if (!Array.isArray(resolvedRows) || resolvedRows.length === 0) return false;
  const anyLegacyKey = resolvedRows.some(r =>
    WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_KEYS.includes(r.criterion_key),
  );
  return !anyLegacyKey;
}

module.exports = {
  computeGates,
  CRITERION_STEP_REQUIREMENTS,
  resolveCriterionResults,
  shouldEmitResolvedRows,
  WALKTHROUGH_FLOW_GROUPS,
  WALKTHROUGH_E2E_LEGACY_HARNESS_KEYS,
  WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_KEYS,
  WALKTHROUGH_META_CRITERION_KEY,
};
