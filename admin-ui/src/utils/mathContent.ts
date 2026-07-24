/**
 * Shared math-detection and delimiter-normalization utilities.
 *
 * MarkdownContentInner, MessagesComposer, and NotesTool all need to detect
 * math delimiters — this module is the single source of truth so the heuristics
 * can't diverge between the lazy-CSS gate and the draft-preview strip.
 */

/**
 * Returns true when the string contains any math delimiter form:
 *   $...$ / $$...$$ (remark-math dollar syntax)
 *   \(...\) / \[...\] (LaTeX-style backslash variants)
 */
export function hasMathDelimiters(content: string): boolean {
  if (typeof content !== 'string') return false;
  return /(?<!\$)\${1,2}(?!\$)/.test(content) || /\\\(|\\\[/.test(content);
}

/**
 * Normalize LaTeX-style backslash delimiters to the remark-math dollar syntax:
 *   \(...\)  →  $...$   (inline)
 *   \[...\]  →  $$...$$ (block)
 *
 * Code-fence-aware: content inside fenced code blocks (```...```) and inline
 * code spans (`...`) is passed through unchanged, so backslash patterns inside
 * code never get corrupted.
 */
export function normalizeMathDelimiters(content: string): string {
  // Split on fenced code blocks then inline code spans.
  // The capture group keeps the delimiters in the result array so we can rejoin.
  // Odd-indexed parts are code spans/fences — leave them untouched.
  const parts = content.split(/(```[\s\S]*?```|`[^`\n]+`)/g);
  return parts
    .map((part, i) => {
      if (i % 2 === 1) return part;
      let out = part.replace(/\\\(([\s\S]+?)\\\)/g, (_, m) => `$${m}$`);
      out = out.replace(/\\\[([\s\S]+?)\\\]/g, (_, m) => `$$${m}$$`);
      return out;
    })
    .join('');
}
