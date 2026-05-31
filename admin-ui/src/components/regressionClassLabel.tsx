import { parseViewportSubkey } from '../utils/viewportRegression';

/** Prefix-to-chip color mapping for recognized composite regression classes. */
const CLASS_PREFIX_CHIPS: Record<string, string> = {
  viewport_regression: 'bg-violet-900/60 text-violet-300 border-violet-700/50',
  fix_loop_divergence: 'bg-amber-900/60 text-amber-300 border-amber-700/50',
  sr_false_positive: 'bg-rose-900/60 text-rose-300 border-rose-700/50',
  sr_catch_rate: 'bg-sky-900/60 text-sky-300 border-sky-700/50',
};

const ASSERTION_TYPE_CHIPS: Record<string, string> = {
  overflow: 'bg-rose-900/60 text-rose-300 border-rose-700/50',
  text_truncation: 'bg-amber-900/60 text-amber-300 border-amber-700/50',
  touch_target: 'bg-blue-900/60 text-blue-300 border-blue-700/50',
};

/**
 * Format a regression_class string for display. For recognized composite
 * classes (prefix:subkey), renders a colored chip + subkey identifier.
 * All other classes render as raw monospace text.
 *
 * @param fallbackTextCls - Tailwind text color class for unrecognized / simple
 *   classes. Defaults to `text-neutral-300`. Callers that show resolved
 *   regressions pass `text-emerald-300` to match their color scheme.
 */
export function RegressionClassLabel({
  cls,
  fallbackTextCls = 'text-neutral-300',
}: {
  cls: string;
  fallbackTextCls?: string;
}) {
  const colon = cls.indexOf(':');
  if (colon === -1) {
    return <span className={`font-mono ${fallbackTextCls}`}>{cls}</span>;
  }
  const prefix = cls.slice(0, colon);
  const subkey = cls.slice(colon + 1);
  const chipCls = CLASS_PREFIX_CHIPS[prefix];
  if (!chipCls) {
    return <span className={`font-mono ${fallbackTextCls}`}>{cls}</span>;
  }

  if (prefix === 'viewport_regression') {
    const { component, assertionType, width } = parseViewportSubkey(subkey);
    const assertionChipCls = assertionType ? ASSERTION_TYPE_CHIPS[assertionType] : undefined;
    return (
      <span className="inline-flex items-center gap-1 min-w-0 flex-wrap">
        <span className={`inline-block text-[9px] font-semibold px-1 py-px rounded border ${chipCls} shrink-0`}>
          {prefix.replace(/_/g, ' ')}
        </span>
        <span className={`font-mono ${fallbackTextCls}`}>{component}</span>
        {assertionType && (
          <span
            data-testid="assertion-type-chip"
            className={`inline-block text-[9px] font-semibold px-1 py-px rounded border shrink-0 ${assertionChipCls ?? 'bg-neutral-800/60 text-neutral-400 border-neutral-700/50'}`}
          >
            {assertionType.replace(/_/g, ' ')}
          </span>
        )}
        {width && <span className="font-mono text-neutral-500 text-[9px]">@{width}</span>}
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-1 min-w-0">
      <span className={`inline-block text-[9px] font-semibold px-1 py-px rounded border ${chipCls} shrink-0`}>
        {prefix.replace(/_/g, ' ')}
      </span>
      <span className={`font-mono ${fallbackTextCls} truncate`}>{subkey}</span>
    </span>
  );
}
