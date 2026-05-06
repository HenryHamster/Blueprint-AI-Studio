import { page } from 'vitest/browser';

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
    throw new Error(
      `expectNoOverflowAt(${width}): document.documentElement.scrollWidth=${docScrollWidth}px > ${limit}px`,
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
    throw new Error(
      `expectNoOverflowAt(${width}): ${overflowing.length} element(s) overflow at width=${width}px:\n${lines}${more}`,
    );
  }
}
