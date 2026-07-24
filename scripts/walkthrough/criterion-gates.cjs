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

module.exports = { computeGates, CRITERION_STEP_REQUIREMENTS };
