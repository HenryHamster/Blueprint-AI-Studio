"""Tests for ``app.github_refs.create_branch`` sha-safety.

On HTTP 422 ("Reference already exists") ``create_branch`` used to fetch the
live SHA of the existing ref and return a success-shaped
``{"object": {"sha": existing_sha}}`` with NO comparison against the REQUESTED
sha. A caller that only checks "did this raise?" therefore silently adopted a
branch pointing at a completely different commit — e.g. a stale tip left behind
by a crashed prior run.

``release_promotion.promote()`` already defended against exactly this by hand
(compare → delete_branch → recreate), which proves the hazard is real. The
comparison now lives in ``create_branch`` itself: reuse is reported explicitly
(``reused`` / ``existing_sha`` / ``requested_sha`` / ``sha_matches``) so no
caller can forget it, while ``object.sha`` keeps its historic shape so existing
callers keep working.
"""

from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


class MockResponse:
    """Minimal stand-in for an httpx response used by ``_github_request``."""

    def __init__(self, status_code, json_data=None, text="", headers=None):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.headers = headers or {}

    def json(self):
        return self._json_data


REQUESTED = "a" * 40
STALE = "b" * 40


@pytest.mark.asyncio
async def test_create_branch_201_returns_ref_and_is_not_flagged_reused(mocker):
    """A real create (201) carries no ``reused`` key — callers can rely on
    ``result.get("reused")`` being falsy iff the branch is guaranteed at ``sha``."""
    from app import github_refs

    created = {"ref": "refs/heads/plan/x", "object": {"sha": REQUESTED}}
    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(201, created)),
    )

    result = await github_refs.create_branch("t", "o", "r", "plan/x", REQUESTED)

    assert result == created
    assert not result.get("reused")


@pytest.mark.asyncio
async def test_create_branch_422_same_sha_reports_matching_reuse(mocker):
    """The benign case (our own retried write after a 502 that actually landed):
    the ref exists AT the sha we asked for — reused, but sha_matches=True."""
    from app import github_refs

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(422, {"message": "Reference already exists"})
        ),
    )
    mocker.patch(
        "app.github.get_branch_sha", new=mocker.AsyncMock(return_value=REQUESTED)
    )

    result = await github_refs.create_branch("t", "o", "r", "plan/x", REQUESTED)

    assert result["reused"] is True
    assert result["sha_matches"] is True
    assert result["existing_sha"] == REQUESTED
    # Historic shape preserved for callers that read object.sha (release_promotion).
    assert result["object"]["sha"] == REQUESTED


@pytest.mark.asyncio
async def test_create_branch_422_different_sha_is_distinguishable(mocker):
    """THE BUG: the ref exists at a DIFFERENT sha (stale tip from a crashed run).
    The result must be distinguishable — not a success-shaped adoption."""
    from app import github_refs

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(422, {"message": "Reference already exists"})
        ),
    )
    mocker.patch(
        "app.github.get_branch_sha", new=mocker.AsyncMock(return_value=STALE)
    )

    result = await github_refs.create_branch("t", "o", "r", "plan/x", REQUESTED)

    assert result["reused"] is True
    assert result["sha_matches"] is False, (
        "a branch adopted at a sha the caller did NOT request must be flagged"
    )
    assert result["existing_sha"] == STALE
    assert result["requested_sha"] == REQUESTED
    # object.sha still reports where the ref ACTUALLY points, so
    # release_promotion's existing `existing_sha != candidate_sha` compare
    # (→ delete + recreate) keeps working unchanged.
    assert result["object"]["sha"] == STALE


@pytest.mark.asyncio
async def test_create_branch_422_with_failed_sha_lookup_raises(mocker):
    """When the 422 fallback lookup ALSO fails we must not fabricate a result —
    fall through to the hard error."""
    from app import github_refs

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(422, {"message": "Reference already exists"})
        ),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=mocker.AsyncMock(side_effect=RuntimeError("github down")),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_refs.create_branch("t", "o", "r", "plan/x", REQUESTED)

    assert exc_info.value.status_code == 500


@pytest.mark.asyncio
async def test_create_branch_non_201_raises(mocker):
    """Any other non-201 remains a hard failure."""
    from app import github_refs

    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(404, {"message": "Not Found"})),
    )

    with pytest.raises(HTTPException) as exc_info:
        await github_refs.create_branch("t", "o", "r", "plan/x", REQUESTED)

    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# compare_ref_status — the RAW compare status, and why a bool is not enough
#
# ``is_ancestor_of`` answers "is X reachable from Y?" by collapsing GitHub's four
# compare statuses onto a bool: identical/ahead -> True, behind/diverged -> False.
# That collapse is correct for a read-only reachability question and CATASTROPHIC
# for a destructive one, because "behind" and "diverged" have OPPOSITE answers to
# "may I delete this ref?":
#
#   behind   -> the ref has NO commits of its own; resetting it is a fast-forward
#               and destroys nothing.  Safe to delete + recreate.
#   diverged -> the ref has commits the source lacks (e.g. children merged into a
#               staging branch that is the head of an open rollup PR).  Deleting it
#               closes the PR and destroys that work.
#
# Any caller about to delete a ref must therefore read the RAW status.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["identical", "ahead", "behind", "diverged"])
async def test_compare_ref_status_returns_the_raw_status_verbatim(mocker, raw):
    """All four statuses survive as-is — no collapsing."""
    from app import github_refs

    mocker.patch(
        "app.github_refs._github_request",
        new=AsyncMock(return_value=MockResponse(200, {"status": raw})),
    )

    assert await github_refs.compare_ref_status("t", "o", "r", REQUESTED, STALE) == raw


@pytest.mark.asyncio
async def test_compare_ref_status_distinguishes_behind_from_diverged(mocker):
    """THE distinction the destructive branch-reset path turns on.

    ``is_ancestor_of`` cannot make it — it returns False for BOTH. This is the
    regression guard: if someone reroutes the reset decision back through the bool,
    a diverged staging branch (merged children + advanced source) gets deleted.
    """
    from app import github_refs

    for raw in ("behind", "diverged"):
        mocker.patch(
            "app.github_refs._github_request",
            new=AsyncMock(return_value=MockResponse(200, {"status": raw})),
        )
        # The bool helper flattens them together...
        assert (
            await github_refs.is_ancestor_of("t", "o", "r", REQUESTED, STALE) is False
        )
        # ...the raw helper keeps them apart.
        assert await github_refs.compare_ref_status("t", "o", "r", REQUESTED, STALE) == raw


@pytest.mark.asyncio
async def test_compare_ref_status_non_200_is_indeterminate_not_false(mocker):
    """An API error must be reported as None (indeterminate), never as a status a
    caller could act destructively on."""
    from app import github_refs

    mocker.patch(
        "app.github_refs._github_request",
        new=AsyncMock(return_value=MockResponse(404, {}, text="not found")),
    )

    assert await github_refs.compare_ref_status("t", "o", "r", REQUESTED, STALE) is None


@pytest.mark.asyncio
async def test_compare_ref_status_unparseable_body_is_indeterminate(mocker):
    """An HTML 5xx page (not JSON) is indeterminate, not a status."""
    from app import github_refs

    bad = MockResponse(200)
    bad.json = lambda: (_ for _ in ()).throw(ValueError("not json"))
    mocker.patch("app.github_refs._github_request", new=AsyncMock(return_value=bad))

    assert await github_refs.compare_ref_status("t", "o", "r", REQUESTED, STALE) is None


@pytest.mark.asyncio
async def test_is_ancestor_of_still_answers_reachability(mocker):
    """The bool helper keeps its contract for read-only reachability callers."""
    from app import github_refs

    for raw, expected in [
        ("identical", True),
        ("ahead", True),
        ("behind", False),
        ("diverged", False),
    ]:
        mocker.patch(
            "app.github_refs._github_request",
            new=AsyncMock(return_value=MockResponse(200, {"status": raw})),
        )
        assert (
            await github_refs.is_ancestor_of("t", "o", "r", REQUESTED, STALE) is expected
        )
