import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { AcceptanceGradeBadge } from './AcceptanceGradeBadge';
import { expectNoOverflowAt } from '../test/viewport';
import type { KnowledgeDocument } from '../api';

type BadgeDoc = Pick<
  KnowledgeDocument,
  'source_kind' | 'compute_acceptance_grade' | 'compute_acceptance_reason' | 'analysis_fallback_reason'
  | 'analysis_mode' | 'unavailable_cause'
>;

describe('AcceptanceGradeBadge — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('measured-only badge with a long analysis-cause label does not overflow at 375px', async () => {
    const doc: BadgeDoc = {
      source_kind: 'compute_experiment',
      compute_acceptance_grade: 'measured_only',
      compute_acceptance_reason: 'analysis_mode_not_llm',
      analysis_fallback_reason: 'analysis_mode_not_llm',
      analysis_mode: 'deterministic',
      unavailable_cause: null,
    };
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<AcceptanceGradeBadge doc={doc} />);
      });
    });
  });

  test('acceptance-grade badge does not overflow at 375px', async () => {
    const doc: BadgeDoc = {
      source_kind: 'compute_experiment',
      compute_acceptance_grade: 'acceptance',
      compute_acceptance_reason: 'llm_artifact_cited',
      analysis_fallback_reason: null,
      analysis_mode: 'llm',
      unavailable_cause: null,
    };
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<AcceptanceGradeBadge doc={doc} />);
      });
    });
  });
});
