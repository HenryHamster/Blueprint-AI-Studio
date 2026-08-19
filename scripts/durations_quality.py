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

# A second fixture-attribution failure mode is smaller than the outlier cap:
# when a worker's in-band setup cost is charged to a batch of tests, pytest-split
# records a dense 15–21s plateau and no observations in the following 21.1–27.4s
# interval.  Keep the detector deliberately conservative: the pattern must have
# at least 100 entries and the diagnostic gap must be present.
IN_BAND_SETUP_MIN = 15.0
IN_BAND_SETUP_MAX = 21.1
IN_BAND_SETUP_MIN_COUNT = 100
IN_BAND_SETUP_GAP_MAX = 27.4


def in_band_setup_artifacts(data: dict[str, float]) -> set[str]:
    """Return entries matching the measured in-band setup-attribution pattern."""
    band = {
        key
        for key, value in data.items()
        if IN_BAND_SETUP_MIN <= value < IN_BAND_SETUP_MAX
    }
    if len(band) < IN_BAND_SETUP_MIN_COUNT:
        return set()
    has_following_gap = not any(
        IN_BAND_SETUP_MAX <= value <= IN_BAND_SETUP_GAP_MAX
        for value in data.values()
    )
    return band if has_following_gap else set()

# Quality thresholds for the generated .test_durations file.
MIN_ENTRIES = 5000   # minimum total entry count
MIN_UNIQUE = 100     # minimum distinct duration values
MIN_CV = 1.0         # minimum coefficient of variation (stdev / mean)


def quality_ok(n: int, uv: int, cv: float) -> bool:
    """Return True if all quality thresholds are met."""
    return n >= MIN_ENTRIES and uv >= MIN_UNIQUE and cv > MIN_CV
