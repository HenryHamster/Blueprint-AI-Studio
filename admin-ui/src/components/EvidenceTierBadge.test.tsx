import { describe, test, expect } from 'vitest';
import { render } from '@testing-library/react';
import { EvidenceTierBadge } from './EvidenceTierBadge';

describe('EvidenceTierBadge — label rendering', () => {
  test('covered tier renders Machine verified label', () => {
    const { getByText } = render(<EvidenceTierBadge tier="covered" />);
    expect(getByText('Machine verified')).toBeTruthy();
  });

  test('attested tier renders Attested label', () => {
    const { getByText } = render(<EvidenceTierBadge tier="attested" />);
    expect(getByText('Attested')).toBeTruthy();
  });

  test('uncovered tier renders Self-reported label', () => {
    const { getByText } = render(<EvidenceTierBadge tier="uncovered" />);
    expect(getByText('Self-reported')).toBeTruthy();
  });

  test('backfilled tier renders Backfilled label', () => {
    const { getByText } = render(<EvidenceTierBadge tier="backfilled" />);
    expect(getByText('Backfilled')).toBeTruthy();
  });

  test('partial tier renders Partial label', () => {
    const { getByText } = render(<EvidenceTierBadge tier="partial" />);
    expect(getByText('Partial')).toBeTruthy();
  });

  test('machine-verified, attested, and self-reported labels are distinct from each other', () => {
    const { getByText: byCovered } = render(<EvidenceTierBadge tier="covered" />);
    const { getByText: byAttested } = render(<EvidenceTierBadge tier="attested" />);
    const { getByText: byUncovered } = render(<EvidenceTierBadge tier="uncovered" />);
    const machineLabel = byCovered('Machine verified').textContent;
    const attestedLabel = byAttested('Attested').textContent;
    const selfReportedLabel = byUncovered('Self-reported').textContent;
    expect(machineLabel).not.toEqual(attestedLabel);
    expect(machineLabel).not.toEqual(selfReportedLabel);
    expect(attestedLabel).not.toEqual(selfReportedLabel);
  });

  test('null tier renders nothing', () => {
    const { container } = render(<EvidenceTierBadge tier={null} />);
    expect(container.firstChild).toBeNull();
  });

  test('undefined tier renders nothing', () => {
    const { container } = render(<EvidenceTierBadge tier={undefined} />);
    expect(container.firstChild).toBeNull();
  });

  test('unrecognized tier renders nothing', () => {
    const { container } = render(<EvidenceTierBadge tier="unknown-tier" />);
    expect(container.firstChild).toBeNull();
  });

  test('covered badge tooltip describes operator-hardware execution', () => {
    const { getByTitle } = render(<EvidenceTierBadge tier="covered" />);
    expect(getByTitle('Executed and verified on operator hardware')).toBeTruthy();
  });

  test('attested badge tooltip describes cloud execution', () => {
    const { getByTitle } = render(<EvidenceTierBadge tier="attested" />);
    expect(getByTitle('Executed on cloud infrastructure')).toBeTruthy();
  });

  test('uncovered badge tooltip describes self-reported nature', () => {
    const { getByTitle } = render(<EvidenceTierBadge tier="uncovered" />);
    expect(getByTitle('No execution evidence — result is self-reported')).toBeTruthy();
  });
});
