import type { ReactNode, MouseEvent } from 'react';
import type { StatusTone } from '../../utils/projectStatus';
import { statusBadgeClasses } from '../../utils/projectStatus';
import { verdictToneBadgeClass, formatVerdict } from '../../utils/verdictFormatting';

export type { StatusTone };

export interface BadgeProps {
  /** Semantic tone for token-backed coloring via @theme status tokens. */
  tone?: StatusTone;
  /** Additional Tailwind classes — for layout, font-weight, or non-token colors. */
  className?: string;
  children: ReactNode;
  title?: string;
  'aria-label'?: string;
  /** When provided, renders as a <button> instead of <span>. */
  onClick?: (e: MouseEvent) => void;
}

const BASE = 'inline-flex items-center text-ui-sm font-medium px-1.5 py-0.5 rounded';

export function Badge({
  tone,
  className = '',
  children,
  title,
  'aria-label': ariaLabel,
  onClick,
}: BadgeProps) {
  const cls = [BASE, tone ? statusBadgeClasses(tone) : '', className].filter(Boolean).join(' ');

  if (onClick) {
    return (
      <button type="button" className={cls} title={title} aria-label={ariaLabel} onClick={onClick}>
        {children}
      </button>
    );
  }
  return (
    <span className={cls} title={title} aria-label={ariaLabel}>
      {children}
    </span>
  );
}

type VerdictTone = Parameters<typeof verdictToneBadgeClass>[0];

const BADGE_BASE = 'inline-flex items-center text-label font-medium border px-1.5 py-0.5 rounded';

/** A badge chip styled by semantic tone. */
export function StatusToneBadge({ tone, label }: { tone: VerdictTone; label: string }) {
  return <span className={`${BADGE_BASE} ${verdictToneBadgeClass(tone)}`}>{label}</span>;
}

/** Renders a raw verdict string as a styled badge (label + tone via verdictFormatting). */
export function VerdictBadge({ verdict }: { verdict: string }) {
  const { label, tone } = formatVerdict(verdict);
  return <StatusToneBadge tone={tone} label={label} />;
}
