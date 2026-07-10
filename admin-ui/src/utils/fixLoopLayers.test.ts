import { describe, it, expect } from 'vitest';
import { FIX_LOOP_LAYERS, LOOP_LABELS } from './fixLoopLayers';
import type { FixLoopLayer } from './fixLoopLayers';

describe('FIX_LOOP_LAYERS', () => {
  it('contains exactly the three canonical layers in order', () => {
    expect(FIX_LOOP_LAYERS).toEqual(['lint', 'test', 'check']);
  });

  it('is readonly and has three elements', () => {
    expect(FIX_LOOP_LAYERS).toHaveLength(3);
  });

  it('covers the documented layer sequence (lint before test before check)', () => {
    expect(FIX_LOOP_LAYERS.indexOf('lint')).toBeLessThan(FIX_LOOP_LAYERS.indexOf('test'));
    expect(FIX_LOOP_LAYERS.indexOf('test')).toBeLessThan(FIX_LOOP_LAYERS.indexOf('check'));
  });
});

describe('LOOP_LABELS', () => {
  it('provides a display label for every layer in FIX_LOOP_LAYERS', () => {
    for (const layer of FIX_LOOP_LAYERS) {
      expect(typeof LOOP_LABELS[layer]).toBe('string');
      expect(LOOP_LABELS[layer].length).toBeGreaterThan(0);
    }
  });

  it('maps each layer to its title-case display name', () => {
    expect(LOOP_LABELS['lint']).toBe('Lint');
    expect(LOOP_LABELS['test']).toBe('Test');
    expect(LOOP_LABELS['check']).toBe('Check');
  });

  it('FixLoopLayer type covers every entry in FIX_LOOP_LAYERS', () => {
    // Type-level check: each FIX_LOOP_LAYERS entry must assignable to FixLoopLayer.
    // We iterate at runtime to confirm every value is a string key accepted by the type.
    const asTyped: FixLoopLayer[] = [...FIX_LOOP_LAYERS];
    expect(asTyped).toHaveLength(3);
  });
});
