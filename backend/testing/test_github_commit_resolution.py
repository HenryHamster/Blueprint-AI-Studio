"""An operator-typed ref must be resolved, not handed back as a question.

INCIDENT 2026-08-23. ``force-promote --candidate-sha 4b6681871`` (nine
characters — what ``git log`` prints, and therefore what a human types) failed
with a bare ``release_branch_failed``. Re-running with the full 40-character
sha promoted immediately. The engine held the token, the owner and the repo the
whole time: it could have resolved the ref itself.
"""

import pytest
from fastapi import HTTPException

from app.github_commit_resolution import (
    FULL_COMMIT_SHA_LENGTH,
    is_full_commit_sha,
    resolve_commit_sha,
)


class MockResponse:
    """Minimal stand-in for an httpx response used by ``_github_request``."""

    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._json_data


FULL = "4b6681871a41b1b8955fc66b67f6b2790f692715"
ABBREV = "4b6681871"


def test_full_commit_sha_length_is_forty():
    """Literal pin — 40 is git's sha-1 hex length, not a tunable."""
    assert FULL_COMMIT_SHA_LENGTH == 40


@pytest.mark.parametrize(
    "value,expected",
    [
        (FULL, True),
        (ABBREV, False),
        ("", False),
        (None, False),
        (FULL.upper(), False),  # canonical form is lowercase
        ("z" * 40, False),  # right length, not hex
        (FULL + "0", False),
        ("main", False),
    ],
)
def test_is_full_commit_sha(value, expected):
    assert is_full_commit_sha(value) is expected


@pytest.mark.asyncio
async def test_full_sha_is_returned_unchanged_with_no_network_call(mocker):
    """A caller that already passes a canonical sha behaves EXACTLY as before.

    This is the guard on the regression risk of the fix itself: the normal
    automatic promotion path resolves the integration HEAD to a full sha and
    must not gain a GitHub round-trip.
    """
    request_mock = mocker.patch(
        "app.github._github_request", new=mocker.AsyncMock()
    )

    assert await resolve_commit_sha("t", "o", "r", FULL) == FULL

    request_mock.assert_not_called()


@pytest.mark.asyncio
async def test_abbreviated_sha_is_resolved_to_the_full_sha(mocker):
    """THE FIX: resolve rather than reject — the engine can answer this."""
    request_mock = mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(200, {"sha": FULL})),
    )

    resolved = await resolve_commit_sha("t", "o", "r", ABBREV, project_id="p1")

    assert resolved == FULL
    method, url = request_mock.call_args.args[0], request_mock.call_args.args[1]
    assert method == "GET"
    assert url == f"https://api.github.com/repos/o/r/commits/{ABBREV}"
    assert request_mock.call_args.kwargs["project_id"] == "p1"


@pytest.mark.asyncio
async def test_uppercase_and_symbolic_refs_are_canonicalized(mocker):
    """A branch name or an uppercase sha resolves to the canonical lowercase sha."""
    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(200, {"sha": FULL.upper()})),
    )

    assert await resolve_commit_sha("t", "o", "r", "automain") == FULL


@pytest.mark.asyncio
async def test_unresolvable_ref_quotes_githubs_own_message(mocker):
    """An unresolvable ref IS an operator error — reported precisely, HERE,
    instead of dying four layers deeper as a reasonless promotion failure."""
    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(
            return_value=MockResponse(422, {"message": "No commit found for SHA: zz"})
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await resolve_commit_sha("t", "o", "r", "zz")

    assert exc_info.value.status_code == 400
    detail = exc_info.value.detail
    assert "No commit found for SHA" in detail, "GitHub's own words must survive"
    assert "zz" in detail, "the ref the operator actually typed must be named"
    assert "git rev-parse" in detail, "the message must state the remedy"


@pytest.mark.asyncio
async def test_empty_ref_is_refused_without_a_network_call(mocker):
    request_mock = mocker.patch(
        "app.github._github_request", new=mocker.AsyncMock()
    )

    with pytest.raises(HTTPException) as exc_info:
        await resolve_commit_sha("t", "o", "r", "   ")

    assert exc_info.value.status_code == 400
    request_mock.assert_not_called()


@pytest.mark.asyncio
async def test_a_non_sha_answer_from_github_is_refused(mocker):
    """Never return something that is not a full sha — the whole contract is
    that downstream consumers can stop worrying about the shape."""
    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(200, {"sha": "short"})),
    )

    with pytest.raises(HTTPException) as exc_info:
        await resolve_commit_sha("t", "o", "r", ABBREV)

    assert exc_info.value.status_code == 502
