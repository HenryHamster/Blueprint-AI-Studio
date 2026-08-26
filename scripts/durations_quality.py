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
# records a dense 12–15s plateau and no observations through 27.4s. Keep the
# detector deliberately conservative: the pattern must have at least 100 entries,
# and the first observed value after the plateau must be beyond that diagnostic
# gap. The current first post-gap observation is 27.430318s.
IN_BAND_SETUP_MIN = 12.0
IN_BAND_SETUP_MAX = 15.0
IN_BAND_SETUP_MIN_COUNT = 100
IN_BAND_SETUP_GAP_MAX = 27.4


def derive_in_band_setup_gap_max(data: dict[str, float]) -> float:
    """Return the first observed duration after the setup-attribution band.

    The first value at or above IN_BAND_SETUP_MAX is the data-derived end of
    the empty gap. Callers compare it with IN_BAND_SETUP_GAP_MAX so a real
    observation in the pinned gap keeps the sanitizer conservative.
    """
    following = [value for value in data.values() if value >= IN_BAND_SETUP_MAX]
    if not following:
        return IN_BAND_SETUP_MAX
    return min(following)


def in_band_setup_artifacts(data: dict[str, float]) -> set[str]:
    """Return entries matching the measured in-band setup-attribution pattern."""
    band = {
        key
        for key, value in data.items()
        if IN_BAND_SETUP_MIN <= value < IN_BAND_SETUP_MAX
    }
    if len(band) < IN_BAND_SETUP_MIN_COUNT:
        return set()
    first_post_plateau = derive_in_band_setup_gap_max(data)
    has_following_gap = (
        first_post_plateau > IN_BAND_SETUP_GAP_MAX
        and not any(
            IN_BAND_SETUP_MAX <= value <= IN_BAND_SETUP_GAP_MAX
            for value in data.values()
        )
    )
    return band if has_following_gap else set()

# Quality thresholds for the generated .test_durations file.
MIN_ENTRIES = 5000   # minimum total entry count
MIN_UNIQUE = 100     # minimum distinct duration values
MIN_CV = 1.0         # minimum coefficient of variation (stdev / mean)


def quality_ok(n: int, uv: int, cv: float) -> bool:
    """Return True if all quality thresholds are met."""
    return n >= MIN_ENTRIES and uv >= MIN_UNIQUE and cv > MIN_CV


def impute_missing_mean(known_durations: dict) -> float:
    """Return the fallback duration pytest-split assigns to an uncovered test.

    pytest-split falls back to the mean of known durations for any collected
    test absent from the durations file, so shard-balance simulations must
    model that same fallback rather than silently dropping uncovered tests
    from the weight they will actually carry in CI.
    """
    if not known_durations:
        return 0.0
    return sum(known_durations.values()) / len(known_durations)
