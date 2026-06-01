"""Per-project branch-resolver coverage for non-self / custom-pair repos.

The release pipeline's qualify -> promote -> reconcile tail keys several
short-circuits on the per-project branch resolver rather than on hardcoded
``automain`` / ``main`` literals.  These tests pin that resolver behaviour for
the configurations an onboarded (non-self) repo actually uses:

  * a custom integration / promotion pair (``develop`` -> ``release``),
  * the single-branch external-repo default where the integration branch IS the
    promotion target (the common case when both columns backfill to the repo's
    GitHub default branch), and
  * Blueprint's own self-repo pair, which must stay a two-branch LTS config.

The single-branch predicate (:func:`is_single_branch_project`) is the canonical
"LTS model inactive" check that ``release_promotion.promote``,
``hotfix_backmerge`` and ``release_reconciliation`` each short-circuit on; these
tests resolve branches through the resolver instead of asserting literals so a
project configured for any branch pair is covered, not just the self-repo
fixture.
"""

from app.branch_resolution import (
    get_project_integration_branch,
    get_project_promotion_target,
    is_single_branch_project,
)


class _FakeProject:
    """Minimal stand-in for a Project row — only the columns the resolver reads.

    Mirrors the fake-project convention in
    ``test_invariants.TestSelfRepoBranchResolution``: a plain object with the
    resolver-relevant attributes set, so no DB session is required.
    """

    def __init__(self, *, integration_branch, promotion_target, is_self_repo=False):
        self.integration_branch = integration_branch
        self.promotion_target = promotion_target
        self.is_self_repo = is_self_repo
        self.github_repo = "external-repo"


class TestCustomBranchPairResolution:
    """A non-self project resolves to its configured integration/promotion pair."""

    def test_custom_pair_resolves_through_resolver(self):
        project = _FakeProject(
            integration_branch="develop", promotion_target="release"
        )
        assert get_project_integration_branch(project) == "develop"
        assert get_project_promotion_target(project) == "release"

    def test_custom_pair_is_not_single_branch(self):
        """develop != release -> two-branch LTS model is active."""
        project = _FakeProject(
            integration_branch="develop", promotion_target="release"
        )
        assert is_single_branch_project(project) is False


class TestSingleBranchExternalRepo:
    """The common onboarded-repo default: integration branch IS the promotion target."""

    def test_single_branch_external_repo_is_detected(self):
        """Both columns backfilled to the repo's default branch -> single-branch."""
        project = _FakeProject(integration_branch="main", promotion_target="main")
        assert get_project_integration_branch(project) == "main"
        assert get_project_promotion_target(project) == "main"
        assert is_single_branch_project(project) is True

    def test_single_branch_with_nondefault_name(self):
        """The predicate compares the resolved names, not the literal 'main'."""
        project = _FakeProject(
            integration_branch="trunk", promotion_target="trunk"
        )
        assert is_single_branch_project(project) is True


class TestSelfRepoStaysTwoBranch:
    """Blueprint's own self-repo keeps a distinct integration/promotion pair."""

    def test_self_repo_pair_is_not_single_branch(self):
        project = _FakeProject(
            integration_branch="automain",
            promotion_target="main",
            is_self_repo=True,
        )
        assert is_single_branch_project(project) is False

    def test_self_repo_null_columns_resolve_to_two_branch(self):
        """A self-repo with unset columns falls back to automain / main -> distinct.

        Exercises the resolve-through-resolver path: the predicate must read the
        self-repo NULL fallbacks (integration -> 'automain', promotion -> 'main')
        rather than a stored pair, and still conclude the LTS model is active.
        """
        project = _FakeProject(
            integration_branch=None, promotion_target=None, is_self_repo=True
        )
        assert get_project_integration_branch(project) == "automain"
        assert get_project_promotion_target(project) == "main"
        assert is_single_branch_project(project) is False
