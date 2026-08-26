# Test Estate Classification Rubric

> Created: 2026-07-22 | Direction: d-4efbf4ef | Source audit: 6f1ba239
> Scope: the current backend pytest, admin-ui Vitest, and agent Vitest inventories.
> Mutable counts and rankings live only in the generated `test-estate-ranked-queue.md`.

## How To Use This Rubric

Assign each test file or test group one of three classifications:
- **Good** (G) — Keep as-is; meets all quality bars.
- **Improve** (I) — Tests live functionality but has structural/quality issues.
- **Cut** (C) — Tests removed functionality (the only valid cut reason per project policy).

The rubric below provides concrete thresholds. Classifications are **relative to the estate**: a "Good" file in a weak area may still warrant improvement; the ranking queue prioritizes improvement order. The thresholds are deliberately fixed policy, not measurements copied from the generated report.

---

## 1. Cut — Removed Functionality Only

Project policy (CLAUDE.md): *"Tests may be removed only when the tested functionality itself was removed."*

### 1.1 Signs the tested functionality is gone

| Indicator | Threshold | Example |
|-----------|-----------|---------|
| Import of a deleted module | `ModuleNotFoundError` at module or function level | `import app.strategic_resident_activation_sweep` (module no longer exists) |
| Hardcoded refs to deleted task types | Task-type string that no task router handles | `"strategic_resident_turn"` no longer in `task_capabilities.py` |
| Test for a deleted function | The function/class the test targets no longer exists in source | `_panel_lens_passed` (panel_review lens verdict normalizer — panel_review is being removed) |
| References to deleted schema columns | Tests asserting DB column values for columns that were migrated away | Design doc god-column blobs moved to `project_telemetry` / `project_efficiency` tables |

### 1.2 Not cut-eligible

- **Slow tests** — improve, don't cut.
- **Large tests** — split/improve, don't cut.
- **Low-value coverage** — improve or leave; the project rejects weakening assertions.
- **Flaky tests** — fix or quarantine, don't cut. (Viewport flakiness rooted in infra, not test logic, is handled separately.)

---

## 2. Good — Keep As-Is

A **Good** test file meets ALL criteria in its tier AND in all lower tiers. There are two tiers:

### 2.1 Tier-1 (Fast + Specific)

| Criterion | Threshold | How to check |
|-----------|-----------|-------------|
| **Speed** | Standalone (no DB) | `--noconftest` compatible; no `AsyncSession`, no `get_session` override |
| **Assertion quality** | Specific value/behavior assertions | `assert x == 3`, `assert result.status == 201` (not `assert x`, not `assert result`) |
| **No dead code path** | All imports resolve at module load | No `ModuleNotFoundError` on pytest collection |
| **Focused scope** | < 1500 lines | `wc -l` |
| **No mock over-mocking** | Real module under test is imported directly | Mock surface is limited to side effects (HTTP, FS, clock) |
| **Naming clarity** | Test names describe scenario and expected outcome | `test_handles_empty_list_gracefully` (not `test_empty`, not `test_things`) |

### 2.2 Tier-2 (Acceptable)

| Criterion | Threshold | How to check |
|-----------|-----------|-------------|
| **Speed** | Any speed is acceptable | DB-backed is OK (but faster is better) |
| **Assertion quality** | At least one specific assertion per test function | Mix of smoke + specific is acceptable |
| **Focused scope** | < 3000 lines | Beyond 3000: split or cull test functions |
| **Test environment** | Consistent pass/fail | No per-run flakiness > 5% |

---

## 3. Improve — Needs Intervention

A test that targets live functionality but fails one or more "Good" criteria. The `action` field determines what kind of improvement is needed.

### 3.1 Action: split

Applies when a single test file exceeds thresholds:

| Severity | Line threshold | What to do |
|----------|---------------|------------|
| Moderate | 1500–2999 lines | Consider splitting by domain |
| High | 3000–5000 lines | Split into 2–3 files by logical grouping |
| Critical | > 5000 lines | Must split; file is a serial bottleneck for test execution and maintenance |

### 3.2 Action: de-flake

| Indicator | What to do |
|-----------|------------|
| Viewport tests failing on CI due to Playwright/Chromium infra | Flag as infra-dependent; test is valid but CI-only |
| Async timing-dependent assertions | Add `await waitFor()` or explicit polling |
| Race conditions in mock state | Use `vi.hoisted()` or per-test state reset |

### 3.3 Action: strengthen assertions

Applies when a test passes but doesn't meaningfully validate behavior:

| Pattern | What to do |
|---------|------------|
| `assert result` (truthiness only) | Assert the specific value or type |
| `assert response.status_code` without body check | Add `response.json()` body assertions |
| `expect(screen.getByText('foo'))` exists-only | Assert visibility, class, or interaction outcome |
| Mock returning fixture and asserting mock was called | Assert the *side effect* of the call, not just the call itself |

### 3.4 Action: fix broken import

Applies when a test references a module/function that was removed, renamed, or refactored, but the test targets live functionality (the broken ref is incidental):

| Pattern | What to do |
|---------|------------|
| Lazy import of deleted module in test function body | Update import to point at the replacement module; update assertions for new API |
| Module-level import of renamed module | Point to new module path |

### 3.5 Action: reduce mock surface

Applies when the test mocks the very module it's supposed to test, or mocks so broadly that the test proves nothing:

| Indicator | What to do |
|-----------|------------|
| `vi.mock('./myModule')` re-exports with canned values | Remove mock, test real implementation |
| 10+ `vi.mock()` calls in a single describe block | Review whether real modules can be used instead |
| All assertions check `expect(mockedFn).toHaveBeenCalledWith(...)` | Add assertions about actual behavior/outcome, not just call tracing |

### 3.6 Action: make standalone

Applies when a test uses a DB session but only tests pure logic that doesn't need one:

| Indicator | Benefit |
|-----------|---------|
| Test only calls a pure Python function but sets up a DB session | Removing DB makes test 10–100× faster and eliminates async complexity |
| Test only checks data shape / schema validation | Can run without DB |
| Test instantiates SQLModel but doesn't persist | Switch to plain dataclass or Pydantic model without session |

### 3.7 Action: add test coverage

Applies when:
- A production module has no test file at all
- A newly added function/class is untested
- An invariant or edge case has no coverage

---

## 4. Evidence Anchors

Each classification must cite concrete evidence. Acceptable anchors:

| Evidence type | Format |
|---------------|--------|
| File size | `wc -l path/to/test.py = N` |
| Broken import | `grep -n "import.*deleted_module" path/to/test.py` + `modulename not found on PYTHONPATH` |
| Module-level import | `grep "^from app\.\|^import app\." path/to/test.py` |
| Standalone marker | File appears in `smoketest.yaml` curated/regression suite |
| DB session usage | `grep -c "AsyncSession\|get_session\|@pytest.mark.asyncio"` — >0 = DB-backed |
| Assertion quality | String search of assertion patterns (`assert.*==\|assert.*is\|\.toHaveBeenCalledWith\|\.toContain`) |
| Mock surface | `grep -c "vi\.mock"` in test file |
| Viewport dependency | `*.viewport.test.*` in filename |

---

## 5. Ranking Priority

The ranked queue (companion document `test-estate-ranked-queue.md`) uses these priority levels:

| Priority | Meaning | Action required within |
|----------|---------|----------------------|
| P0 | Broken imports preventing test collection | Next sprint |
| P1 | Tests for removed functionality blocking pipeline | Next sprint |
| P2 | Files > 5000 lines (critical size) | Within 2 sprints |
| P3 | Files 3000–5000 lines (high size) | Within 4 sprints |
| P4 | Over-mocked tests / weak assertions / non-standalone-eligible | When touched |
| P5 | Files 1500–3000 lines (moderate size) | When touched |
| P6 | Viewport infra-dependency documentation | Informational |

---

## 6. Generated queue contract

`test-estate-ranked-queue.md` is generated by
`scripts/generate_test_estate_ranked_queue.py`. The generator is stdlib-only and
has two explicit modes:

- `python3 scripts/generate_test_estate_ranked_queue.py --write` regenerates the
  report from the current checkout.
- `python3 scripts/generate_test_estate_ranked_queue.py --check` exits nonzero
  and prints a unified diff when the checked-in bytes are stale.

The report must include every file in the three current inventories, each file's
line count, threshold class, and test count from the canonical collectors in
`backend/testing/identity_extraction.py`. Path discovery is shared with the
duration freshness guards through `backend/testing/estate_inventory.py`.

### Merge conflicts in the generated queue

The queue is a total function of the tree, and Rank plus the estate-summary row
are global, so ANY two branches that add or remove a test file conflict here --
measured at 8 of 20 real `automain` re-sync merges (2026-08-26), more than every
source conflict in that window combined. No merge driver can fix it: the correct
content depends on the MERGED tree, which does not exist while git is merging
content, so a driver could only pick a side and would trade a loud conflict for
a silently stale report.

Do not resolve it by hand and do not pick a side. Run:

    bash scripts/resolve-derived-conflicts.sh

which takes either side purely to clear the marker, regenerates from the merged
tree, verifies with `--check`, and stages the result. It handles
`backend/.test_durations` and `backend/testing/module_size_limits.json` in the
same pass and refuses -- without touching the tree -- to guess at anything else.

The Workday protection manifest is read-only input: its `test_count` values are
reported as declared floors and are never rewritten or lowered by regeneration.
Backend duration coverage is intentionally partial. Files without a committed
backend duration entry are rendered as `unknown (no committed measurement)`;
they must not receive invented zero values or be ranked as cheap. Admin-ui and
agent duration artifacts are complete directional cost signals and are rendered
in seconds from their measured milliseconds.

The output has no timestamps or environment-dependent values, so a clean-tree
regeneration is byte-identical. Adding or changing a test file changes the
derived report; check mode must fail until write mode regenerates it. Threshold
policy is `under 1500 LOC` (G), `1500–2999` (P5), `3000–5000` (P3), and `>5000`
(P2).

## 7. Baseline Measurement Specification (future work)

This rubric defines **what** a quantitative baseline must measure when run (sequencing: rubric first, baseline follow-on). Required measurements:

1. **Per-file runtime** — median of 3 runs in CI-like environment (not a cold agent VM)
2. **Per-file flake rate** — failures that don't reproduce on retry, over last 100 CI runs
3. **Per-file DB footprint** — number of DB tables touched, rows created
4. **Standalone eligibility ratio** — (n assertions testing pure logic) / (total n assertions)
5. **Assertion specificity ratio** — (n "specific" assertions) / (total n assertions) — specific = `==`, `is`, `in`, `raises`, `toHaveBeenCalledWith`, `toContain`, `toThrow`, numeric/string matching matchers. Truthiness-only assertions (`assert x`, `assertTrue(x)`) are NOT specific.
