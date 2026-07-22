import { describe, test, expect } from 'vitest';
import { render } from '@testing-library/react';
import MarkdownContentInner from './MarkdownContentInner';

describe('MarkdownContentInner — math rendering', () => {
  test('renders inline math ($...$) as a KaTeX span', () => {
    const { container } = render(<MarkdownContentInner content="Energy $E = mc^2$ here." />);
    // rehype-katex renders math into .katex spans
    const katexEl = container.querySelector('.katex');
    expect(katexEl).toBeTruthy();
  });

  test('renders block math ($$...$$) as KaTeX display math', () => {
    const { container } = render(
      <MarkdownContentInner content={"$$\nE = mc^2\n$$"} />
    );
    const katexDisplay = container.querySelector('.katex-display');
    expect(katexDisplay).toBeTruthy();
  });

  test('does not mis-parse a lone dollar sign as math', () => {
    const { container } = render(
      <MarkdownContentInner content="Save $100 or more today." />
    );
    // No katex elements should appear for an unmatched $
    expect(container.querySelector('.katex')).toBeNull();
    // The dollar sign should appear as plain text
    expect(container.textContent).toContain('$100');
  });

  test('two adjacent dollar-sign amounts are paired by remark-math as a math span', () => {
    // "$10 and $20" has balanced $ delimiters, so remark-math treats "10 and " as math.
    // This is expected remark-math behavior — the important safety case is a lone $.
    const { container } = render(
      <MarkdownContentInner content="Prices: $10 and $20 per unit." />
    );
    // A katex span WILL appear (the paired $...$), and the surrounding text remains
    const katexEl = container.querySelector('.katex');
    expect(katexEl).toBeTruthy();
    expect(container.textContent).toContain('per unit');
  });

  test('renders \\(...\\) inline math via KaTeX', () => {
    const { container } = render(
      <MarkdownContentInner content={'Inline \\(E = mc^2\\) formula.'} />
    );
    expect(container.querySelector('.katex')).toBeTruthy();
  });

  test('renders \\[...\\] block math via KaTeX', () => {
    const { container } = render(
      <MarkdownContentInner content={'\\[\nE = mc^2\n\\]'} />
    );
    expect(container.querySelector('.katex')).toBeTruthy();
  });
});
