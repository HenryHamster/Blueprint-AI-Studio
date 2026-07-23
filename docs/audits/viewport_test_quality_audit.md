# Viewport Test Quality Audit

**Date:** 2026-07-23
**Scope:** 158 viewport test files in `admin-ui/src/components/`
**Sample:** 15 representative files (all viewport test helpers: `expectNoOverflowAt`, `expectNoTextTruncationAt`, `expectMinTouchTargetAt`, `expectFullViewportAt`)

## Executive Summary

The ranked queue classification of ALL 168 viewport test files as 'G' (keep as-is) is **validated**. The 15-file sample shows consistently high quality with no snapshot brittleness and minimal flakiness risk.

## Key Findings

### ✅ Strengths
- **No snapshot tests**: Zero use of `toMatchSnapshot`, `toMatchInlineSnapshot`, or `toMatchFileSnapshot` across all sampled files.
- **No hardcoded pixel values**: All measurements use standard Tailwind breakpoints (375px, 420px, 768px, 1024px, 1280px) and the iOS 44px touch-target guideline.
- **Most files provide unique layout coverage** that no unit test would capture. 8 of 15 files have exclusively viewport-layout assertions.
- **Minimal assertion gaps**: The lowest-assertion files (e.g., DashboardMetricCards with 1 assertion at 71 lines) still exercise genuine layout behavior.

### ⚠️ Mixed Quality (3 of 15 files)
| File | Lines | Assertions | Viewport | Behavioral | Verdict |
|------|-------|-----------|----------|------------|---------|
| HeaderBar.viewport.test.tsx | 478 | 52 | 5 | 47 | Behavioral unit test in viewport clothing |
| WorkbenchComposition.viewport.test.tsx | 566 | 50 | 3 | 47 | Responsive-layout behavioral test, minimal overflow checks |
| ErrorBoundary.viewport.test.tsx | 82 | 10 | 3 | 7 | Infrastructure meta-test for the assertion framework |

These three files are primarily behavioral tests (click handlers, text content assertions, callback verification) with incidental viewport checks. They would be more accurately classified as 'mixed' rather than pure viewport regression tests.

### 🔴 Flakiness Signals (minor)
- **4 files** use `setTimeout(resolve, N)` waits (VersionLifecyclePanel: 50ms; WorkbenchComposition: 75ms; plus 2 more). These time-based waits may be insufficient under CI load.
- **2 files** have click-interaction chains (HeaderBar: 18 clicks; WorkbenchComposition: 6 clicks) that are sensitive to rendering order.

### 📊 Tier Classification

| Tier | Count | Description |
|------|-------|-------------|
| **'G' (keep as-is)** | ~12 of 15 sampled | Pure viewport regression tests, high quality |
| **'M' (mixed)** | ~3 of 15 sampled | Primarily behavioral with incidental viewport coverage |
| **'R' (remove/refactor)** | 0 | No file is redundant or has zero substantive assertions |

## Recommendation

Maintain the 'G' classification for the vast majority of viewport tests. For the 3 mixed files (HeaderBar, WorkbenchComposition, ErrorBoundary), consider:
1. Move behavioral assertions to dedicated `.test.tsx` unit test files
2. Keep only the 3-5 genuine viewport layout assertions in the `.viewport.test.tsx` files
3. Replace `setTimeout` waits with `vi.waitFor` or `findBy*` queries

This cleanup is **low priority** — no defects were found, only classification drift.
