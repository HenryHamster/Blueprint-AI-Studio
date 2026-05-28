import { describe, it, expect } from 'vitest';
import { annotateResolvedIssues } from './review-fix-issue-dedup.js';

function defined<T>(v: T | undefined | null, msg = 'expected value to be defined'): T {
  if (v === undefined || v === null) throw new Error(msg);
  return v;
}

describe('annotateResolvedIssues — marks issues not re-flagged by next iteration', () => {
  it('annotates an issue with resolvedInIteration when it does not reappear next', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [
        { severity: 'major', description: 'missing null check', file: 'a.ts' },
        { severity: 'minor', description: 'unused import' },
      ] },
      { iteration: 2, feedback: '', issues: [
        { severity: 'major', description: 'missing null check', file: 'a.ts' },
      ] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    const issues1 = defined(it1.issues);
    const nullCheck = defined(issues1.find(i => i.description === 'missing null check'));
    const unused = defined(issues1.find(i => i.description === 'unused import'));
    expect(nullCheck.resolvedInIteration).toBeUndefined();
    expect(unused.resolvedInIteration).toBe(2);
  });

  it('never annotates the most recent iteration', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [{ severity: 'major', description: 'x' }] },
      { iteration: 2, feedback: '', issues: [{ severity: 'major', description: 'y' }] },
    ]);
    const it2 = defined(result.find(e => e.iteration === 2));
    expect(defined(it2.issues)[0].resolvedInIteration).toBeUndefined();
  });

  it('normalizes whitespace/case when matching descriptions', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [{ severity: 'major', description: 'Missing  NULL check' }] },
      { iteration: 2, feedback: '', issues: [{ severity: 'major', description: 'missing null check' }] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    expect(defined(it1.issues)[0].resolvedInIteration).toBeUndefined();
  });

  it('leaves feedback-only (no issues) entries untouched', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: 'plain text' },
      { iteration: 2, feedback: 'also plain' },
    ]);
    expect(result[0].issues).toBeUndefined();
  });

  it('treats rephrased issues with high token overlap as still-present (not resolved)', () => {
    // The example from the task description: same conceptual issue, reworded.
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [
        { severity: 'major', description: 'missing null check' },
      ] },
      { iteration: 2, feedback: '', issues: [
        { severity: 'major', description: 'null check not present' },
      ] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    expect(defined(it1.issues)[0].resolvedInIteration).toBeUndefined();
  });

  it('treats reordered/rephrased multi-word issues as still-present', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [
        { severity: 'major', description: 'Missing null check in handler' },
      ] },
      { iteration: 2, feedback: '', issues: [
        { severity: 'major', description: 'Handler lacks null check' },
      ] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    expect(defined(it1.issues)[0].resolvedInIteration).toBeUndefined();
  });

  it('still marks unrelated issues as resolved when next iteration has different problems', () => {
    const result = annotateResolvedIssues([
      { iteration: 1, feedback: '', issues: [
        { severity: 'major', description: 'missing null check' },
      ] },
      { iteration: 2, feedback: '', issues: [
        { severity: 'minor', description: 'add unit tests for parser' },
      ] },
    ]);
    const it1 = defined(result.find(e => e.iteration === 1));
    expect(defined(it1.issues)[0].resolvedInIteration).toBe(2);
  });
});
