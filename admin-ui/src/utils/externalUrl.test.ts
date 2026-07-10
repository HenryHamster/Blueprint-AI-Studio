import { describe, it, expect } from 'vitest';
import { safeExternalUrl } from './externalUrl';

describe('safeExternalUrl', () => {
  it('returns the href for a valid https URL', () => {
    expect(safeExternalUrl('https://github.com/owner/repo/pull/1')).toBe(
      'https://github.com/owner/repo/pull/1',
    );
  });

  it('returns the href for a valid http URL', () => {
    expect(safeExternalUrl('http://example.com/page')).toBe('http://example.com/page');
  });

  it('normalizes a URL by returning the parsed href form', () => {
    // new URL normalizes trailing slash for bare origins
    const result = safeExternalUrl('https://example.com');
    expect(result).toBe('https://example.com/');
  });

  it('returns null for a javascript: scheme (XSS vector)', () => {
    expect(safeExternalUrl('javascript:alert(1)')).toBeNull();
  });

  it('returns null for a data: scheme', () => {
    expect(safeExternalUrl('data:text/html,<script>alert(1)</script>')).toBeNull();
  });

  it('returns null for a file: scheme', () => {
    expect(safeExternalUrl('file:///etc/passwd')).toBeNull();
  });

  it('returns null for a ftp: scheme', () => {
    expect(safeExternalUrl('ftp://files.example.com/resource')).toBeNull();
  });

  it('returns null for an unparseable string', () => {
    expect(safeExternalUrl('not a url at all')).toBeNull();
  });

  it('returns null for an empty string', () => {
    expect(safeExternalUrl('')).toBeNull();
  });

  it('returns null for null', () => {
    expect(safeExternalUrl(null)).toBeNull();
  });

  it('returns null for undefined', () => {
    expect(safeExternalUrl(undefined)).toBeNull();
  });

  it('preserves query parameters and fragments', () => {
    const url = 'https://example.com/path?foo=bar&baz=qux#section';
    expect(safeExternalUrl(url)).toBe(url);
  });
});
