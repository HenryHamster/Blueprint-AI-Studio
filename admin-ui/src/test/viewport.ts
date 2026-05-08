import { page } from 'vitest/browser';

// Extract the *.viewport.test.tsx filename from the call stack so failures
// embed the component reference that the backend regression classifier needs
// (pattern: `(\w+)\.viewport\.test\.tsx`).
function _callerTestFile(): string {
  const stack = new Error().stack ?? '';
  const m = stack.match(/(\w+\.viewport\.test\.tsx)/i);
  return m ? m[1] : '';
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

  const limit = width + tolerance;
  const docScrollWidth = document.documentElement.scrollWidth;

  if (docScrollWidth > limit) {
    const fileTag = callerFile ? ` [${callerFile}]` : '';
    throw new Error(
      `expectNoOverflowAt(${width})${fileTag}: document.documentElement.scrollWidth=${docScrollWidth}px > ${limit}px`,
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
      `expectNoOverflowAt(${width})${fileTag}: ${overflowing.length} element(s) overflow at width=${width}px:\n${lines}${more}`,
    );
  }
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
      `expectMinTouchTargetAt(${width})${fileTag}: ${undersized.length} element(s) below ${minSize}px touch target:\n${lines}${more}`,
    );
  }
}
