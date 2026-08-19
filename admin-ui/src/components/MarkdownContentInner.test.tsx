import { describe, test, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
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

describe('MarkdownContentInner — citation links', () => {
  test('a direction id navigates via onOpenDirection when clicked', () => {
    const onOpenDirection = vi.fn();
    const { getByText } = render(
      <MarkdownContentInner content="see d-dec033c2 for context" onOpenDirection={onOpenDirection} />
    );
    fireEvent.click(getByText('d-dec033c2'));
    expect(onOpenDirection).toHaveBeenCalledWith('dec033c2');
  });

  test('a task id navigates via onOpenTask when clicked', () => {
    const onOpenTask = vi.fn();
    const id = '0e0689a0-1234-4abc-89ab-1234567890ab';
    const { getByText } = render(
      <MarkdownContentInner content={`task ${id} is stuck`} onOpenTask={onOpenTask} />
    );
    fireEvent.click(getByText(id));
    expect(onOpenTask).toHaveBeenCalledWith(id);
  });

  test('a PR reference links out to GitHub when githubRepo is provided', () => {
    const { getByText } = render(
      <MarkdownContentInner content="landed in #17493 yesterday" githubRepo="HenryHamster/BlueprintAIStudio" />
    );
    const link = getByText('#17493').closest('a');
    expect(link).not.toBeNull();
    expect(link?.getAttribute('href')).toBe('https://github.com/HenryHamster/BlueprintAIStudio/pull/17493');
    expect(link?.getAttribute('target')).toBe('_blank');
  });

  test('degrades to literal text when no handler/repo is wired to resolve the target', () => {
    const { getByText, container } = render(
      <MarkdownContentInner content="see d-dec033c2 and #17493" />
    );
    expect(getByText('d-dec033c2', { exact: false })).toBeTruthy();
    // No button or anchor should be rendered for an unresolvable citation.
    expect(container.querySelector('button')).toBeNull();
    expect(container.querySelector('a')).toBeNull();
  });

  test('a bare 8-hex string with no anchoring context is left as plain text', () => {
    const { container } = render(
      <MarkdownContentInner content="commit sha c0f26a99 landed" onOpenTask={vi.fn()} />
    );
    expect(container.textContent).toContain('commit sha c0f26a99 landed');
    expect(container.querySelector('button')).toBeNull();
  });
});
