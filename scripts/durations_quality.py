"""
Shared quality constants for backend/.test_durations.

Imported by:
  - scripts/regenerate-durations.py
  - scripts/merge_durations.py  (invoked by scripts/batch-store-durations.sh)

Editing a value here changes behavior in both generators simultaneously.

NOT imported by backend/testing/test_test_durations_freshness.py: that guard
runs standalone under --noconftest in the CI smoke image, where the repo is
mounted at /app with no sibling scripts/ dir, so this module is unavailable to
it. It keeps its own _IMPLAUSIBLE_OUTLIER_CAP literal that must match OUTLIER_CAP
here by convention (60.0).
"""

# Durations above this threshold are fixture-setup misattribution (Docker Postgres
# cold-start is charged to the first test in a batch and can produce 400-600s values).
# 60s is well above any legitimate single-test cost (~27s max) and well below the
# fixture-attribution range.
OUTLIER_CAP = 60.0  # seconds

# Quality thresholds for the generated .test_durations file.
MIN_ENTRIES = 5000   # minimum total entry count
MIN_UNIQUE = 100     # minimum distinct duration values
MIN_CV = 1.0         # minimum coefficient of variation (stdev / mean)


def quality_ok(n: int, uv: int, cv: float) -> bool:
    """Return True if all quality thresholds are met."""
    return n >= MIN_ENTRIES and uv >= MIN_UNIQUE and cv > MIN_CV
