"""Tests for app.branch_cleanup — the stale-branch git-cleanup cluster.

These cases were relocated from test_services.py when the stale-branch
cleanup concern was extracted into its own module. They import the
branch short-id regex builder and the default staging-prefix constant
directly from ``app.branch_cleanup`` (the canonical home) rather than
through the ``app.services`` re-export shim.
"""


def test_build_branch_short_id_re_custom_namespace():
    """Per-project regex recognizes branches under a custom namespace."""
    from app.branch_cleanup import _build_branch_short_id_re

    pattern = _build_branch_short_id_re("autoproj")
    assert pattern.match("autoproj/refactor-abcdef12").group(1) == "abcdef12"
    assert pattern.match("autoproj/fix-12345678").group(1) == "12345678"
    # Default staging prefixes still recognized
    assert pattern.match("research/abcdef12").group(1) == "abcdef12"
    assert pattern.match("plan/abcdef12").group(1) == "abcdef12"
    # The default Blueprint namespace is NOT matched by an autoproj regex.
    assert pattern.match("blueprint/refactor-abcdef12") is None


def test_build_branch_short_id_re_custom_staging_prefixes():
    """Custom staging prefixes replace the default research/plan pair."""
    from app.branch_cleanup import _build_branch_short_id_re

    pattern = _build_branch_short_id_re(
        "autoproj", staging_prefixes=("feature/", "experiment/")
    )
    # Custom staging prefixes are matched
    assert pattern.match("feature/abcdef12").group(1) == "abcdef12"
    assert pattern.match("experiment/12345678").group(1) == "12345678"
    # Task namespace branches still matched
    assert pattern.match("autoproj/fix-abcdef12").group(1) == "abcdef12"
    # Blueprint defaults NOT matched when overridden
    assert pattern.match("research/abcdef12") is None
    assert pattern.match("plan/abcdef12") is None


def test_build_branch_short_id_re_default_staging_prefixes_preserved():
    """Blueprint default staging prefixes work without explicit override."""
    from app.branch_cleanup import _build_branch_short_id_re, _DEFAULT_STAGING_PREFIXES

    assert _DEFAULT_STAGING_PREFIXES == ("research/", "plan/")
    pattern = _build_branch_short_id_re("blueprint")
    assert pattern.match("research/abcdef12").group(1) == "abcdef12"
    assert pattern.match("plan/abcdef12").group(1) == "abcdef12"
    assert pattern.match("blueprint/task-abcdef12").group(1) == "abcdef12"
