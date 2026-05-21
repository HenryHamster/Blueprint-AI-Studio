import { page } from 'vitest/browser';

// Extract the *.viewport.test.tsx filename from the call stack so failures
// embed the component reference that the backend regression classifier needs
// (pattern: `(\w+)\.viewport\.test\.tsx`).
function _callerTestFile(): string {
  const stack = new Error().stack ?? '';
  const m = stack.match(/(\w+\.viewport\.test\.tsx)/i);
  return m ? m[1] : '';
}

// When VITE_CAPTURE_BASELINES=true (set on main-branch CI runs), save a PNG
// of the component after each passing assertion. One file per component per
// width — overwriting keeps storage bounded to one baseline per slot.
async function _captureBaselineIfEnabled(callerFile: string, width: number): Promise<void> {
  if (import.meta.env.VITE_CAPTURE_BASELINES !== 'true') return;
  const name = callerFile.replace(/\.viewport\.test\.tsx$/i, '') || 'unknown';
  try {
    await page.screenshot({ path: `viewport-baselines/${name}-${width}px.png` });
  } catch {
    // Never fail a passing test because of screenshot I/O
  }
}

/**
 * Internal guard: if an ErrorBoundary fallback is present in the rendered tree,
 * throw immediately with a descriptive [assertion_type:error_boundary] message.
 * Called by each exported assertion function after layout settles, so a crashed
 * panel can never produce a false-green by presenting a tiny fallback box that
 * doesn't overflow, isn't truncated, and has no controls.
 */
function _assertNoErrorBoundaryFallback(width: number, callerFile: string): void {
  const fallbacks = document.querySelectorAll<HTMLElement>('[data-error-boundary-fallback]');
  if (fallbacks.length === 0) return;
  const excerpts = Array.from(fallbacks)
    .slice(0, 3)
    .map((el) => el.outerHTML.slice(0, 200))
    .join('\n');
  const fileTag = callerFile ? ` [${callerFile}]` : '';
  throw new Error(
    `[assertion_type:error_boundary]${fileTag}: ${fallbacks.length} ErrorBoundary fallback(s) found at width=${width}px — component did not render real content:\n${excerpts}`,
  );
}

/**
 * Options for `expectNoOverflowAt`.
 *
 * - `tolerance`: pixels of slack allowed past the viewport's right edge
 *   (default 1px — accounts for sub-pixel rounding).
 * - `height`: viewport height in px (default 800).
 * - `waitMs`: extra milliseconds to pause after `render()` before asserting
 *   (default 0). Use for components that load data asynchronously — a value
 *   of 50–100ms is usually enough to let initial fetch mocks resolve.
 */
export interface ExpectNoOverflowOptions {
  tolerance?: number;
  height?: number;
  waitMs?: number;
}

/**
 * Assert that the document does not horizontally overflow at `width` px.
 *
 * Caller mounts the component (or page tree) in `render` — the helper resizes
 * the browser viewport, runs `render`, lets layout settle, and then asserts:
 *   1. `document.documentElement.scrollWidth <= width + tolerance`
 *   2. Every element matched by `body *:not([data-viewport-allow-overflow])`
 *      has `getBoundingClientRect().right <= width + tolerance`.
 *
 * Authors can opt a node out of (2) by adding `data-viewport-allow-overflow`
 * to it (e.g. tooltips that intentionally extend off-screen). Any descendant
 * of an opted-out node is skipped as well.
 *
 * On failure, the thrown Error names up to five offending elements with their
 * tag, computed right edge in px, and a short outerHTML excerpt — so CI logs
 * are diagnosable without re-running locally.
 */
export async function expectNoOverflowAt(
  width: number,
  render: () => Promise<void> | void,
  options?: ExpectNoOverflowOptions,
): Promise<void> {
  const callerFile = _callerTestFile();
  const tolerance = options?.tolerance ?? 1;
  const height = options?.height ?? 800;
  const waitMs = options?.waitMs ?? 0;

  await page.viewport(width, height);
  await render();

  // Let layout / fonts / async images settle for one frame.
  await new Promise<void>((resolve) => {
    requestAnimationFrame(() => resolve());
  });

  // Optional extra wait for components that resolve async data after mount.
  if (waitMs > 0) {
    await new Promise<void>((resolve) => setTimeout(resolve, waitMs));
  }

  _assertNoErrorBoundaryFallback(width, callerFile);

  const limit = width + tolerance;
  const docScrollWidth = document.documentElement.scrollWidth;

  if (docScrollWidth > limit) {
    const fileTag = callerFile ? ` [${callerFile}]` : '';
    throw new Error(
      `expectNoOverflowAt(${width})${fileTag} [assertion_type:overflow]: document.documentElement.scrollWidth=${docScrollWidth}px > ${limit}px`,
    );
  }

  const overflowing: { tag: string; right: number; html: string }[] = [];
  const all = document.querySelectorAll<HTMLElement>('body *:not([data-viewport-allow-overflow])');
  for (const el of all) {
    if (el.closest('[data-viewport-allow-overflow]')) continue;
    const rect = el.getBoundingClientRect();
    // Skip zero-sized elements (display:none collapses to 0×0; off-screen
    // hidden trees would otherwise spam the failure message).
    if (rect.width === 0 && rect.height === 0) continue;
    if (rect.right > limit) {
      overflowing.push({
        tag: el.tagName.toLowerCase(),
        right: Math.round(rect.right),
        html: el.outerHTML.slice(0, 160),
      });
    }
  }

  if (overflowing.length > 0) {
    const lines = overflowing
      .slice(0, 5)
      .map((e) => `  <${e.tag}> right=${e.right}px (limit ${limit}px) — ${e.html}`)
      .join('\n');
    const more = overflowing.length > 5 ? `\n  ... and ${overflowing.length - 5} more` : '';
    const fileTag = callerFile ? ` [${callerFile}]` : '';
    throw new Error(
      `expectNoOverflowAt(${width})${fileTag} [assertion_type:overflow]: ${overflowing.length} element(s) overflow at width=${width}px:\n${lines}${more}`,
    );
  }

  await _captureBaselineIfEnabled(callerFile, width);
}

/**
 * Options for `expectNoTextTruncationAt`.
 *
 * - `selector`: CSS selector to find elements to check (default 'body *').
 * - `height`: viewport height in px (default 800).
 * - `waitMs`: extra milliseconds to pause after `render()` before asserting.
 */
export interface ExpectNoTextTruncationOptions {
  selector?: string;
  height?: number;
  waitMs?: number;
}

/**
 * Assert that no visible text-bearing element has its text silently clipped at
 * `width` px without an ellipsis.
 *
 * After rendering at the target viewport width, every element that has text
 * content is checked: if its `scrollWidth > clientWidth` (content is wider than
 * the container) AND its computed `text-overflow` style is not `'ellipsis'`,
 * the text is being silently clipped — a mobile readability bug.
 *
 * Authors can opt a node out by adding `data-viewport-allow-truncation` to it.
 * Any descendant of an opted-out node is skipped as well.
 *
 * Intentional ellipsis via Tailwind `truncate` (which applies
 * `text-overflow: ellipsis`) is automatically excluded — only silent clipping
 * is flagged.
 *
 * On failure, the thrown Error names up to five offending elements with their
 * tag, overflow amount in px, and a short outerHTML excerpt.
 */
export async function expectNoTextTruncationAt(
  width: number,
  render: () => Promise<void> | void,
  options?: ExpectNoTextTruncationOptions,
): Promise<void> {
  const callerFile = _callerTestFile();
  const selector = options?.selector ?? 'body *';
  const height = options?.height ?? 800;
  const waitMs = options?.waitMs ?? 0;

  await page.viewport(width, height);
  await render();

  await new Promise<void>((resolve) => {
    requestAnimationFrame(() => resolve());
  });

  if (waitMs > 0) {
    await new Promise<void>((resolve) => setTimeout(resolve, waitMs));
  }

  _assertNoErrorBoundaryFallback(width, callerFile);

  const truncated: { tag: string; overflow: number; html: string }[] = [];
  const all = document.querySelectorAll<HTMLElement>(selector);
  for (const el of all) {
    if (el.closest('[data-viewport-allow-truncation]')) continue;
    // Skip hidden/zero-sized elements (display:none collapses to 0×0)
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    // Skip elements with no text content
    if (!el.textContent?.trim()) continue;
    // Skip elements where text-overflow is intentionally ellipsis (e.g. Tailwind `truncate`)
    const style = window.getComputedStyle(el);
    if (style.textOverflow === 'ellipsis') continue;
    // scrollWidth > clientWidth means text content is wider than the visible container
    if (el.scrollWidth > el.clientWidth) {
      truncated.push({
        tag: el.tagName.toLowerCase(),
        overflow: el.scrollWidth - el.clientWidth,
        html: el.outerHTML.slice(0, 160),
      });
    }
  }

  if (truncated.length > 0) {
    const lines = truncated
      .slice(0, 5)
      .map((e) => `  <${e.tag}> overflow=${e.overflow}px — ${e.html}`)
      .join('\n');
    const more = truncated.length > 5 ? `\n  ... and ${truncated.length - 5} more` : '';
    const fileTag = callerFile ? ` [${callerFile}]` : '';
    throw new Error(
      `expectNoTextTruncationAt(${width})${fileTag} [assertion_type:text_truncation]: ${truncated.length} element(s) have text clipped without ellipsis at width=${width}px:\n${lines}${more}`,
    );
  }

  await _captureBaselineIfEnabled(callerFile, width);
}

/**
 * Options for `expectMinTouchTargetAt`.
 *
 * - `selector`: CSS selector to find buttons to check (default 'button').
 * - `minSize`: minimum touch target size in px (default 44).
 * - `height`: viewport height in px (default 800).
 * - `waitMs`: extra milliseconds to pause after `render()` before asserting.
 */
export interface ExpectMinTouchTargetOptions {
  selector?: string;
  minSize?: number;
  height?: number;
  waitMs?: number;
}

/**
 * Assert that all buttons have minimum touch target size at `width` px.
 *
 * Checks that every button (or element matching `selector`) visible at the
 * given viewport width has a computed bounding box of at least `minSize` x
 * `minSize` (default 44x44px, iOS guideline).
 *
 * Authors can opt a node out by adding `data-viewport-allow-small-target`
 * to it (e.g. decorative icons that are not clickable).
 */
export async function expectMinTouchTargetAt(
  width: number,
  render: () => Promise<void> | void,
  options?: ExpectMinTouchTargetOptions,
): Promise<void> {
  const callerFile = _callerTestFile();
  const selector = options?.selector ?? 'button';
  const minSize = options?.minSize ?? 44;
  const height = options?.height ?? 800;
  const waitMs = options?.waitMs ?? 0;

  await page.viewport(width, height);
  await render();

  // Let layout settle.
  await new Promise<void>((resolve) => {
    requestAnimationFrame(() => resolve());
  });

  if (waitMs > 0) {
    await new Promise<void>((resolve) => setTimeout(resolve, waitMs));
  }

  _assertNoErrorBoundaryFallback(width, callerFile);

  const undersized: { tag: string; width: number; height: number; html: string }[] = [];
  const all = document.querySelectorAll<HTMLElement>(selector);
  for (const el of all) {
    if (el.closest('[data-viewport-allow-small-target]')) continue;
    const rect = el.getBoundingClientRect();
    // Skip hidden elements
    if (rect.width === 0 && rect.height === 0) continue;
    if (rect.width < minSize || rect.height < minSize) {
      undersized.push({
        tag: el.tagName.toLowerCase(),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
        html: el.outerHTML.slice(0, 160),
      });
    }
  }

  if (undersized.length > 0) {
    const lines = undersized
      .slice(0, 5)
      .map((e) => `  <${e.tag}> ${e.width}×${e.height}px (min ${minSize}×${minSize}px) — ${e.html}`)
      .join('\n');
    const more = undersized.length > 5 ? `\n  ... and ${undersized.length - 5} more` : '';
    const fileTag = callerFile ? ` [${callerFile}]` : '';
    throw new Error(
      `expectMinTouchTargetAt(${width})${fileTag} [assertion_type:touch_target]: ${undersized.length} element(s) below ${minSize}px touch target:\n${lines}${more}`,
    );
  }

  await _captureBaselineIfEnabled(callerFile, width);
}

/**
 * Options for `expectFullViewportAt`.
 *
 * Merges the options of all three sub-checks. Per-check overrides (selector,
 * minTouchSize, tolerance) let callers tune individual assertions without
 * giving up the convenience of a single call site.
 *
 * - `tolerance`: overflow tolerance in px (default 1).
 * - `textSelector`: CSS selector for the text-truncation check (default 'body *').
 * - `touchSelector`: CSS selector for the touch-target check (default 'button').
 * - `minTouchSize`: minimum touch target in px (default 44).
 * - `height`: viewport height in px (default 800).
 * - `waitMs`: extra ms to pause after `render()` before each sub-check.
 */
export interface ExpectFullViewportOptions {
  tolerance?: number;
  textSelector?: string;
  touchSelector?: string;
  minTouchSize?: number;
  height?: number;
  waitMs?: number;
}

/**
 * Run all three viewport assertions — overflow, text-truncation, and touch
 * target — in a single call.
 *
 * Use this for components that show operator-facing text AND interactive
 * controls. When one check is intentionally inapplicable (e.g. a read-only
 * panel with no buttons), use the individual helpers and add a one-line
 * comment explaining the omission so reviewers don't flag it as missing.
 *
 * Opt individual nodes out of specific checks via:
 *   `data-viewport-allow-overflow`       — skips the overflow check for that subtree
 *   `data-viewport-allow-truncation`     — skips text-truncation for that subtree
 *   `data-viewport-allow-small-target`   — skips touch-target for that subtree
 */
export async function expectFullViewportAt(
  width: number,
  render: () => Promise<void> | void,
  options?: ExpectFullViewportOptions,
): Promise<void> {
  const shared = { height: options?.height, waitMs: options?.waitMs };
  await expectNoOverflowAt(width, render, { ...shared, tolerance: options?.tolerance });
  await expectNoTextTruncationAt(width, render, { ...shared, selector: options?.textSelector });
  await expectMinTouchTargetAt(width, render, {
    ...shared,
    selector: options?.touchSelector,
    minSize: options?.minTouchSize,
  });
}
