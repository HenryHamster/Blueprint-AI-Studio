import { describe, test, expect } from 'vitest';
import { hasMathDelimiters, normalizeMathDelimiters } from './mathContent';

describe('hasMathDelimiters', () => {
  test('returns true for $...$ inline math', () => {
    expect(hasMathDelimiters('Energy $E = mc^2$ here.')).toBe(true);
  });

  test('returns true for $$...$$ block math', () => {
    expect(hasMathDelimiters('$$\nE = mc^2\n$$')).toBe(true);
  });

  test('returns true for \\(...\\) inline LaTeX', () => {
    expect(hasMathDelimiters('Inline \\(E = mc^2\\) formula.')).toBe(true);
  });

  test('returns true for \\[...\\] block LaTeX', () => {
    expect(hasMathDelimiters('\\[\nE = mc^2\n\\]')).toBe(true);
  });

  test('returns false for plain text with no delimiters', () => {
    expect(hasMathDelimiters('Hello, just a plain message.')).toBe(false);
  });

  test('returns false for empty string', () => {
    expect(hasMathDelimiters('')).toBe(false);
  });
});

describe('normalizeMathDelimiters', () => {
  test('converts \\(…\\) to $…$', () => {
    expect(normalizeMathDelimiters('Inline \\(E = mc^2\\) formula.')).toBe('Inline $E = mc^2$ formula.');
  });

  test('converts \\[…\\] to $$…$$', () => {
    expect(normalizeMathDelimiters('\\[E = mc^2\\]')).toBe('$$E = mc^2$$');
  });

  test('leaves content inside fenced code blocks untouched', () => {
    const input = '```\n\\(E = mc^2\\)\n```';
    expect(normalizeMathDelimiters(input)).toBe(input);
  });

  test('leaves content inside inline code spans untouched', () => {
    const input = 'Use `\\(x\\)` to write inline math.';
    expect(normalizeMathDelimiters(input)).toBe(input);
  });

  test('normalizes math outside code fences but not inside', () => {
    const input = 'Before \\(a\\) ```code \\(b\\)``` after \\(c\\).';
    const expected = 'Before $a$ ```code \\(b\\)``` after $c$.';
    expect(normalizeMathDelimiters(input)).toBe(expected);
  });

  test('passes through plain text unchanged', () => {
    const input = 'No math here at all.';
    expect(normalizeMathDelimiters(input)).toBe(input);
  });
});
