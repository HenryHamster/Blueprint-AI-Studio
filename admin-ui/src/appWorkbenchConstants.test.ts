import { COLUMNS, EXECUTING_PIPELINE_STATUSES, isLocalPipelineAttentionTask } from './appWorkbenchConstants';

describe('pipeline operator-state contracts', () => {
  it('calls only a claimed task executing', () => {
    expect(EXECUTING_PIPELINE_STATUSES.has('IN_PROGRESS')).toBe(true);
    expect(EXECUTING_PIPELINE_STATUSES.has('PROVISIONING')).toBe(false);
    expect(EXECUTING_PIPELINE_STATUSES.has('EVALUATING')).toBe(false);
  });

  it('keeps queued, starting, in-progress, and evaluating columns distinct', () => {
    expect(COLUMNS.filter(column => ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING'].includes(column.id))).toEqual([
      { id: 'QUEUED', label: 'Queued', includes: ['QUEUED'] },
      { id: 'PROVISIONING', label: 'Starting', includes: ['PROVISIONING'] },
      { id: 'IN_PROGRESS', label: 'In progress', includes: ['IN_PROGRESS'] },
      { id: 'EVALUATING', label: 'Evaluating', includes: ['EVALUATING'] },
    ]);
  });

  it('keeps healthy lifecycle rows out of Needs attention', () => {
    expect(isLocalPipelineAttentionTask('FAILED', false)).toBe(true);
    expect(isLocalPipelineAttentionTask('BLOCKED', false)).toBe(true);
    expect(isLocalPipelineAttentionTask('IN_PROGRESS', true)).toBe(true);
    expect(isLocalPipelineAttentionTask('IN_PROGRESS', false)).toBe(false);
    expect(isLocalPipelineAttentionTask('PROVISIONING', false)).toBe(false);
    expect(isLocalPipelineAttentionTask('EVALUATING', false)).toBe(false);
  });
});
