"""The narrow hotfix lane must RESOLVE an operator-typed repair ref, not reject it.

INCIDENT 2026-08-23 / project goal "the engine never asks me for something it
already has".  ``narrow_repair_scope`` refused any ``repair_commit_shas`` entry
that was not already a full 40-hex sha:

    "repair commits must be full 40-hex SHAs (an abbreviated or symbolic ref is
     ambiguous about what deploys)"

A human types an abbreviated sha because that is what ``git log`` prints, and
the engine holds the token, the owner and the repo — it can do the lookup
itself.  The stated rationale is only true of a prefix that matches more than
one object; once resolved there is nothing ambiguous.  So the shape refusal is
replaced by a resolution, and the fail-closed property is kept intact: an
unknown or genuinely ambiguous ref is STILL rejected, now carrying GitHub's own
message instead of a generic string.

These tests also pin the split-brain guard: the CANONICAL resolved shas are what
the diff, the materialized commit, its message and the caller's audit trail use
— never the operator's raw input.
"""

from __future__ import annotations

import pytest

from app import github, github_refs, hotfix_narrow_candidate


class MockResponse:
    """Minimal stand-in for an httpx response used by ``_github_request``."""

    def __init__(self, status_code, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data if json_data is not None else {}
        self.text = text
        self.headers = {}

    def json(self):
        return self._json_data


# A real machinery repair: 2a5a9833 is the abbreviation an operator would type.
FULL = "2a5a98330f6c4c1f8a0e2b7d5c3e19a4b8d6f012"
ABBREV = "2a5a9833"
OTHER_ABBREV = "2a5a98330f"

MACHINERY_ENTRIES = [
    {"filename": "backend/app/release_stall_alerts.py", "patch": "@@\n+x\n"},
    {"filename": "backend/testing/test_release_stall_alerts.py", "patch": "@@\n+y\n"},
]
MACHINERY_FILES = [e["filename"] for e in MACHINERY_ENTRIES]


def _resolution_mock(mocker, *, sha=FULL, status_code=200, json_data=None):
    """Patch the single GitHub seam ``resolve_commit_sha`` reaches through."""
    payload = json_data if json_data is not None else {"sha": sha}
    return mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(return_value=MockResponse(status_code, payload)),
    )


def _compare_mock(mocker, entries=None):
    """Patch the own-diff compare; records the (base_ref, head_ref) it is given."""
    seen: list[tuple[str, str]] = []

    async def _fake_compare(token, owner, repo, base_ref, head_ref, project_id=None):
        seen.append((base_ref, head_ref))
        return list(MACHINERY_ENTRIES if entries is None else entries)

    mocker.patch.object(
        github_refs, "get_changed_files_with_patches_between_refs", _fake_compare
    )
    return seen


# --------------------------- resolution behaviour ---------------------------


@pytest.mark.asyncio
async def test_abbreviated_repair_sha_is_resolved_and_scope_checked(mocker):
    """THE FIX: an abbreviated repair sha resolves, and the scope check then runs
    against the CANONICAL sha — not the prefix the operator typed."""
    request_mock = _resolution_mock(mocker)
    seen = _compare_mock(mocker)

    scope = await hotfix_narrow_candidate.narrow_repair_scope(
        "t", "o", "r", [ABBREV], project_id="p1"
    )

    assert scope["status"] == "eligible", scope["reason"]
    assert scope["changed_files"] == MACHINERY_FILES
    # The engine did the lookup itself instead of handing it back.
    url = request_mock.call_args.args[1]
    assert url == f"https://api.github.com/repos/o/r/commits/{ABBREV}"
    assert request_mock.call_args.kwargs["project_id"] == "p1"
    # ...and the own-diff was taken against the RESOLVED sha.
    assert seen == [(f"{FULL}~1", FULL)]
    # The canonical sha is carried out so nothing downstream re-derives it.
    assert scope["repair_commit_shas"] == [FULL]


@pytest.mark.asyncio
async def test_full_repair_sha_makes_no_resolution_network_call(mocker):
    """Behaviour for an already-canonical sha is UNCHANGED: no extra round-trip.

    This is the regression guard on the fix itself — the ordinary path where the
    operator (or the engine) already supplies a full sha must not gain a GitHub
    call per named commit.
    """
    request_mock = mocker.patch("app.github._github_request", new=mocker.AsyncMock())
    seen = _compare_mock(mocker)

    scope = await hotfix_narrow_candidate.narrow_repair_scope("t", "o", "r", [FULL])

    assert scope["status"] == "eligible", scope["reason"]
    assert scope["repair_commit_shas"] == [FULL]
    assert seen == [(f"{FULL}~1", FULL)]
    request_mock.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_repair_ref_is_still_rejected_quoting_github(mocker):
    """Fail-closed is preserved: a ref GitHub cannot resolve is REJECTED, and the
    reason carries GitHub's own words rather than a generic string."""
    _resolution_mock(
        mocker,
        status_code=422,
        json_data={"message": "No commit found for SHA: deadbee"},
    )
    seen = _compare_mock(mocker)

    scope = await hotfix_narrow_candidate.narrow_repair_scope(
        "t", "o", "r", ["deadbee"]
    )

    assert scope["status"] == "scope_rejected"
    assert "No commit found for SHA" in scope["reason"], "GitHub's words must survive"
    assert "deadbee" in scope["reason"], "the ref the operator typed must be named"
    assert scope["changed_files"] == []
    assert scope["repair_commit_shas"] == []
    assert seen == [], "a ref that will not resolve must never be diffed"


@pytest.mark.asyncio
async def test_ambiguous_prefix_is_still_rejected(mocker):
    """The ONE case the old rationale was right about stays rejected.

    'Abbreviated is ambiguous about what deploys' is only true of a prefix that
    matches more than one object.  GitHub says so explicitly; the lane must
    refuse and repeat what it said.
    """
    _resolution_mock(
        mocker,
        status_code=422,
        json_data={"message": "Ambiguous match, found 2 commits for SHA: 2a5a983"},
    )
    seen = _compare_mock(mocker)

    # Well-shaped (7 hex, git's own default abbreviation) — so this is the
    # RESOLVER's verdict, not the shape gate's. A prefix can be perfectly
    # well-formed and still name two objects; only GitHub can say so.
    scope = await hotfix_narrow_candidate.narrow_repair_scope(
        "t", "o", "r", ["2a5a983"]
    )

    assert scope["status"] == "scope_rejected"
    assert "Ambiguous match" in scope["reason"]
    assert seen == []


@pytest.mark.asyncio
async def test_transport_failure_degrades_to_diff_unavailable(mocker):
    """An indeterminate lookup is NOT a verdict about the operator's input.

    A transport failure must not be reported as 'your repair commit is bad'; it
    is the same class as an indeterminate compare — ``diff_unavailable``, which
    the router maps to 502 rather than 400.
    """
    mocker.patch(
        "app.github._github_request",
        new=mocker.AsyncMock(side_effect=RuntimeError("connection reset")),
    )
    seen = _compare_mock(mocker)

    scope = await hotfix_narrow_candidate.narrow_repair_scope("t", "o", "r", [ABBREV])

    assert scope["status"] == "diff_unavailable"
    assert "connection reset" in scope["reason"]
    assert seen == []


# --------------------------- the bounds that stay ---------------------------


@pytest.mark.asyncio
async def test_empty_repair_list_still_fails_closed(mocker):
    request_mock = mocker.patch("app.github._github_request", new=mocker.AsyncMock())

    scope = await hotfix_narrow_candidate.narrow_repair_scope("t", "o", "r", [])

    assert scope["status"] == "scope_rejected"
    assert "no repair commits named" in scope["reason"]
    request_mock.assert_not_called()


@pytest.mark.asyncio
async def test_count_cap_still_fires_and_costs_no_resolution_calls(mocker):
    """The cap is applied to the RAW list, BEFORE resolution.

    Deliberate ordering: the cap bounds the work this owner-only lever may ask
    for, so an over-long list must be refused without first spending one GitHub
    round-trip per entry.
    """
    request_mock = mocker.patch("app.github._github_request", new=mocker.AsyncMock())
    too_many = [
        f"{i:040x}" for i in range(hotfix_narrow_candidate.MAX_REPAIR_COMMITS + 1)
    ]

    scope = await hotfix_narrow_candidate.narrow_repair_scope("t", "o", "r", too_many)

    assert scope["status"] == "scope_rejected"
    assert "max 10" in scope["reason"]
    assert hotfix_narrow_candidate.MAX_REPAIR_COMMITS == 10, (
        "literal pin: the surgical-repair bound is policy, not a tunable"
    )
    request_mock.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ref",
    [
        "main",
        "HEAD",
        "automain",
        "v40",
        "refs/heads/main",
        "HEAD~1",
        "abc123",  # hex, but shorter than git's own default abbreviation
        "",
    ],
)
async def test_symbolic_ref_is_refused_by_shape_before_any_lookup(mocker, ref):
    """A branch names a POSITION; this lane names what deploys.

    ``GET /repos/{owner}/{repo}/commits/{ref}`` resolves ``main``, ``HEAD`` and
    tags perfectly well, so resolution alone is not a gate. Whatever such a name
    points at in the instant the request lands is not a repair the operator can
    be said to have named, and the resolved sha would be recorded in the audit
    trail as though they had. Refused on shape, with no round-trip spent.
    """
    request_mock = mocker.patch("app.github._github_request", new=mocker.AsyncMock())

    scope = await hotfix_narrow_candidate.narrow_repair_scope("t", "o", "r", [ref])

    assert scope["status"] == "scope_rejected"
    assert "symbolic ref" in scope["reason"]
    assert scope["repair_commit_shas"] == []
    request_mock.assert_not_called()
    # Literal pin: the admitted shape is policy for a lane that selects what
    # gets DEPLOYED, not a tunable. 7 is git's default abbreviation length.
    assert hotfix_narrow_candidate._REPAIR_REF_RE.pattern == r"^[0-9a-fA-F]{7,40}$"


@pytest.mark.asyncio
async def test_dedup_after_resolution_does_not_rescue_an_over_cap_list(mocker):
    """Capping BEFORE resolution is the strictly stricter ordering.

    Eleven names that all resolve to ONE commit would deduplicate down to a
    single entry.  Admitting that list would mean the cap depended on an
    accident of how the operator typed it, so the raw count is what is capped.
    """
    request_mock = mocker.patch("app.github._github_request", new=mocker.AsyncMock())

    scope = await hotfix_narrow_candidate.narrow_repair_scope(
        "t", "o", "r", [ABBREV] * (hotfix_narrow_candidate.MAX_REPAIR_COMMITS + 1)
    )

    assert scope["status"] == "scope_rejected"
    assert "max 10" in scope["reason"]
    request_mock.assert_not_called()


@pytest.mark.asyncio
async def test_two_abbreviations_of_the_same_commit_collapse_to_one(mocker):
    """Dedup happens AFTER resolution — two prefixes of one commit are one repair.

    Otherwise the same commit would be diffed twice, named twice in the
    materialized commit message and recorded twice in the audit trail.
    """
    _resolution_mock(mocker)
    seen = _compare_mock(mocker)

    scope = await hotfix_narrow_candidate.narrow_repair_scope(
        "t", "o", "r", [ABBREV, OTHER_ABBREV, FULL]
    )

    assert scope["status"] == "eligible", scope["reason"]
    assert scope["repair_commit_shas"] == [FULL], "one commit, named once"
    assert seen == [(f"{FULL}~1", FULL)], "the same commit is diffed exactly once"


@pytest.mark.asyncio
async def test_a_resolved_repair_touching_product_code_still_fails_closed(mocker):
    """Resolution changes the INPUT, never the authorization predicate."""
    _resolution_mock(mocker)
    _compare_mock(
        mocker,
        entries=[
            {"filename": "backend/app/release_stall_alerts.py", "patch": None},
            {"filename": "backend/app/reaper.py", "patch": None},
        ],
    )

    scope = await hotfix_narrow_candidate.narrow_repair_scope("t", "o", "r", [ABBREV])

    assert scope["status"] == "scope_rejected"
    assert scope["offending_files"] == ["backend/app/reaper.py"]


# ------------------------ canonical shas flow downstream ---------------------


@pytest.mark.asyncio
async def test_materialization_keys_everything_off_the_canonical_sha(mocker):
    """No split brain: the content ref, the commit message and the returned
    provenance are the RESOLVED sha, never the abbreviation that was typed."""
    prior_deployed = "d" * 40
    _resolution_mock(mocker)
    _compare_mock(mocker)

    blob_refs: list[str] = []

    async def _fake_blob_sha(path, *, token, owner, repo, ref, project_id=None):
        blob_refs.append(ref)
        return (True, f"blob-{path}")

    mocker.patch.object(github, "get_file_blob_sha_at_ref", _fake_blob_sha)
    dangling = mocker.patch(
        "app.github.create_dangling_commit_from_blob_refs",
        new=mocker.AsyncMock(return_value="e" * 40),
    )

    result = await hotfix_narrow_candidate.materialize_narrow_hotfix_candidate(
        "t",
        "o",
        "r",
        prior_deployed_sha=prior_deployed,
        repair_commit_shas=[ABBREV],
    )

    assert result["status"] == "materialized", result
    assert result["candidate_sha"] == "e" * 40
    assert result["changed_files"] == MACHINERY_FILES
    # content_ref defaults to the LAST named repair commit — canonicalized.
    assert result["content_ref"] == FULL
    assert set(blob_refs) == {FULL}
    # The provenance the caller persists is the verified sha.
    assert result["repair_commit_shas"] == [FULL]
    message = dangling.await_args.kwargs["message"]
    assert FULL in message, "the commit message must name what actually shipped"
    assert f"{ABBREV}," not in message and f"{ABBREV} " not in message


@pytest.mark.asyncio
async def test_materialize_creates_nothing_when_the_ref_will_not_resolve(mocker):
    """A rejected resolution must cut no tree, blob or commit."""
    _resolution_mock(
        mocker,
        status_code=404,
        json_data={"message": "No commit found for SHA: deadbee"},
    )
    _compare_mock(mocker)
    blob_mock = mocker.patch(
        "app.github.get_file_blob_sha_at_ref", new=mocker.AsyncMock()
    )
    dangling = mocker.patch(
        "app.github.create_dangling_commit_from_blob_refs", new=mocker.AsyncMock()
    )

    result = await hotfix_narrow_candidate.materialize_narrow_hotfix_candidate(
        "t", "o", "r", prior_deployed_sha="d" * 40, repair_commit_shas=["deadbee"]
    )

    assert result["status"] == "scope_rejected"
    assert "No commit found for SHA" in result["reason"]
    blob_mock.assert_not_awaited()
    dangling.assert_not_awaited()
