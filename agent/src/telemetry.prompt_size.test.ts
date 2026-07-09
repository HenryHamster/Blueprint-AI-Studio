import { describe, it, expect } from 'vitest';
import { buildPromptSizeTelemetry } from './telemetry.js';

// ── buildPromptSizeTelemetry (prompt size telemetry) ────────────────────
// Acceptance evidence for the prompt-size telemetry cap change from 3 → 10.
// Exercises the production code change in telemetry.ts and verifies that the
// function correctly sorts, caps, and filters prompt section sizes.
describe('buildPromptSizeTelemetry', () => {
  it('sorts sections by descending size and caps the telemetry payload at 10', () => {
    const sections: Array<[string, string]> = [
      ['big', 'x'.repeat(500)],
      ['medium', 'y'.repeat(300)],
      ['tiny', 'z'.repeat(10)],
      ['small_a', 'a'.repeat(50)],
      ['small_b', 'b'.repeat(40)],
      ['small_c', 'c'.repeat(30)],
      ['small_d', 'd'.repeat(20)],
    ];
    const prompt = sections.map(([, text]) => text).join('\n');
    const result = buildPromptSizeTelemetry(prompt, sections);
    // The full prompt byte size should be non-zero and reasonable
    expect(result.prompt_size_bytes).toBeGreaterThan(0);
    // section_count matches all sections
    expect(result.prompt_section_count).toBe(sections.length);
    // With 7 sections, all fit within the cap of 10 — none dropped
    expect(result.prompt_section_sizes).toHaveLength(7);
    // First entry is the largest (big)
    expect(result.prompt_section_sizes[0].name).toBe('big');
    expect(result.prompt_section_sizes[0].bytes).toBeGreaterThanOrEqual(
      result.prompt_section_sizes[1].bytes,
    );
    // All entries have positive bytes and are sorted descending
    for (const s of result.prompt_section_sizes) {
      expect(s.bytes).toBeGreaterThan(0);
    }
    for (let i = 0; i < result.prompt_section_sizes.length - 1; i++) {
      expect(result.prompt_section_sizes[i].bytes).toBeGreaterThanOrEqual(
        result.prompt_section_sizes[i + 1].bytes,
      );
    }
    // With 12 sections, the cap at 10 drops the 2 smallest
    const manySections: Array<[string, string]> = Array.from({ length: 12 }, (_, i) => [
      `s${i + 1}`,
      'x'.repeat((12 - i) * 50), // s1 largest, s12 smallest
    ]);
    const bigPrompt = manySections.map(([, text]) => text).join('\n');
    const capped = buildPromptSizeTelemetry(bigPrompt, manySections);
    expect(capped.prompt_section_sizes).toHaveLength(10);
    // The dropped sections (s11, s12) are the smallest
    const cappedNames = capped.prompt_section_sizes.map(s => s.name);
    expect(cappedNames).not.toContain('s11');
    expect(cappedNames).not.toContain('s12');
    // Sorted descending
    for (let i = 0; i < capped.prompt_section_sizes.length - 1; i++) {
      expect(capped.prompt_section_sizes[i].bytes).toBeGreaterThanOrEqual(
        capped.prompt_section_sizes[i + 1].bytes,
      );
    }
  });

  it('returns empty array when all sections have zero bytes', () => {
    const result = buildPromptSizeTelemetry('', [['empty', ''], ['alsoempty', '']]);
    expect(result.prompt_section_count).toBe(0);
    expect(result.prompt_section_sizes).toHaveLength(0);
    expect(result.prompt_size_bytes).toBe(0);
  });

  it('omits zero-byte sections from the result count', () => {
    const result = buildPromptSizeTelemetry('some prompt', [
      ['real', 'hello world'],
      ['empty', ''],
    ]);
    expect(result.prompt_section_count).toBe(1);
    expect(result.prompt_section_sizes).toHaveLength(1);
    expect(result.prompt_section_sizes[0].name).toBe('real');
  });
});
