import { afterEach, describe, test, expect } from 'vitest';
import {
  hiddenManifestToolsKey,
  loadHiddenManifestSlugs,
  saveHiddenManifestSlugs,
} from './projectToolHidden';

afterEach(() => {
  localStorage.clear();
});

describe('projectToolHidden', () => {
  test('key is namespaced per project', () => {
    expect(hiddenManifestToolsKey('proj-a')).toBe('project_tools_hidden:proj-a');
    expect(hiddenManifestToolsKey('')).toBe('project_tools_hidden:default');
  });

  test('save then load round-trips the slug set', () => {
    saveHiddenManifestSlugs('proj-a', new Set(['viewer', 'canvas']));
    const loaded = loadHiddenManifestSlugs('proj-a');
    expect([...loaded].sort()).toEqual(['canvas', 'viewer']);
  });

  test('hidden state is isolated per project', () => {
    saveHiddenManifestSlugs('proj-a', new Set(['viewer']));
    expect(loadHiddenManifestSlugs('proj-b').size).toBe(0);
  });

  test('empty set removes the key rather than storing []', () => {
    saveHiddenManifestSlugs('proj-a', new Set(['viewer']));
    saveHiddenManifestSlugs('proj-a', new Set());
    expect(localStorage.getItem(hiddenManifestToolsKey('proj-a'))).toBeNull();
    expect(loadHiddenManifestSlugs('proj-a').size).toBe(0);
  });

  test('load returns empty set on malformed / non-array JSON', () => {
    localStorage.setItem(hiddenManifestToolsKey('proj-a'), 'not json');
    expect(loadHiddenManifestSlugs('proj-a').size).toBe(0);
    localStorage.setItem(hiddenManifestToolsKey('proj-a'), '{"a":1}');
    expect(loadHiddenManifestSlugs('proj-a').size).toBe(0);
  });

  test('load filters out non-string and empty entries', () => {
    localStorage.setItem(hiddenManifestToolsKey('proj-a'), JSON.stringify(['ok', 1, '', null]));
    expect([...loadHiddenManifestSlugs('proj-a')]).toEqual(['ok']);
  });
});
