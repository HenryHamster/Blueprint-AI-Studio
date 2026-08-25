"""Tests for app.github_repo_snapshot — cached branch-tree snapshot reads.

The module exists to collapse the per-file ``/contents`` fan-out that drained
the GitHub App installation quota (one call per cited file per grounding pass)
into ≤2 API calls per pass, and to guarantee — via the quota reserve floor —
that advisory grounding can never starve the critical PR/merge/CI machinery.
These tests pin both the call-count economics and the conservative tri-state
degradation.
"""

import asyncio
import base64
import io
import tarfile
import time

import pytest

import app.github_repo_snapshot as snapshot
import app.shared_state as shared_state
from app.github_repo_snapshot import (
    authoritative_texts_at_commit,
    file_text_on_branch,
    glob_exists_on_branch,
    path_exists_on_branch,
)

CTX = {"token": "ghs_test", "owner": "acme", "repo": "widget", "branch": "automain"}
COMMIT_SHA = "c" * 40
BLOB_A = "a" * 40
BLOB_B = "b" * 40


class _FakeResponse:
    def __init__(self, status_code: int, payload=None, content: bytes = b""):
        self.status_code = status_code
        self._payload = payload or {}
        self.content = content

    def json(self):
        return self._payload


def _ref_response(sha=COMMIT_SHA):
    return _FakeResponse(200, {"object": {"sha": sha}})


def _tree_response(paths=("backend/app/x.py", "backend/app/y.py"), truncated=False):
    return _FakeResponse(
        200,
        {
            "tree": [
                {"path": p, "type": "blob", "sha": BLOB_A if i == 0 else BLOB_B}
                for i, p in enumerate(paths)
            ]
            + [{"path": "backend", "type": "tree", "sha": "d" * 40}],
            "truncated": truncated,
        },
    )


def _blob_response(text="def real_symbol():\n    pass\n"):
    return _FakeResponse(
        200, {"content": base64.b64encode(text.encode()).decode(), "encoding": "base64"}
    )


def _tarball(**texts: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path, text in texts.items():
            data = text.encode()
            member = tarfile.TarInfo(f"owner-repo-sha/{path}")
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_authoritative_exact_sha_read_uses_one_archive_without_quota_gate(mocker):
    """Merge reads are exact-SHA archive reads, independent of advisory quota reserve."""
    shared_state.github_quota.update({"remaining": 0, "reset_at": int(time.time()) + 60})
    calls = _patch_requests(
        mocker,
        [
            _FakeResponse(
                200,
                content=_tarball(
                    **{"backend/app/x.py": "x\n", "caps.json": "{}\n"}
                ),
            )
        ],
    )

    texts = await authoritative_texts_at_commit(
        {"backend/app/x.py", "caps.json"},
        token="ghs_test",
        owner="acme",
        repo="widget",
        commit_sha=COMMIT_SHA,
    )

    assert texts == {"backend/app/x.py": "x\n", "caps.json": "{}\n"}
    assert calls == [
        ("GET", f"https://api.github.com/repos/acme/widget/tarball/{COMMIT_SHA}")
    ]


@pytest.mark.asyncio
async def test_authoritative_archive_bytes_reused_for_different_path_sets(mocker):
    """One immutable archive serves independent extraction contracts."""
    calls = _patch_requests(
        mocker,
        [
            _FakeResponse(
                200,
                content=_tarball(
                    **{"backend/app/x.py": "x\n", "caps.json": "{}\n"}
                ),
            )
        ],
    )

    first = await authoritative_texts_at_commit(
        {"backend/app/x.py"},
        token="ghs_test",
        owner="acme",
        repo="widget",
        commit_sha=COMMIT_SHA,
    )
    strict = await authoritative_texts_at_commit(
        {"caps.json", "missing.py"},
        token="ghs_test",
        owner="acme",
        repo="widget",
        commit_sha=COMMIT_SHA,
    )
    second = await authoritative_texts_at_commit(
        {"caps.json", "missing.py"},
        token="ghs_test",
        owner="acme",
        repo="widget",
        commit_sha=COMMIT_SHA,
        allow_missing_paths=True,
    )

    assert first == {"backend/app/x.py": "x\n"}
    assert strict is None
    assert second == {"caps.json": "{}\n"}
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_authoritative_archive_fetch_is_single_flight_per_exact_sha(mocker):
    """Concurrent callers for one archive share the in-flight download."""
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()
    response = _FakeResponse(
        200, content=_tarball(**{"backend/app/x.py": "x\n"})
    )

    async def _fake_request(method, url, headers, **kwargs):
        calls.append((method, url))
        started.set()
        await release.wait()
        return response

    mocker.patch("app.github._github_request", side_effect=_fake_request)
    first_task = asyncio.create_task(
        authoritative_texts_at_commit(
            {"backend/app/x.py"},
            token="ghs_test",
            owner="acme",
            repo="widget",
            commit_sha=COMMIT_SHA,
        )
    )
    await started.wait()
    second_task = asyncio.create_task(
        authoritative_texts_at_commit(
            {"backend/app/x.py"},
            token="ghs_test",
            owner="acme",
            repo="widget",
            commit_sha=COMMIT_SHA,
        )
    )
    await asyncio.sleep(0)
    assert len(calls) == 1
    release.set()

    assert await first_task == {"backend/app/x.py": "x\n"}
    assert await second_task == {"backend/app/x.py": "x\n"}


@pytest.mark.asyncio
async def test_authoritative_archive_cache_is_scoped_to_repository_and_sha(mocker):
    """Different immutable repositories or commits never share archive bytes."""
    archive = _tarball(**{"backend/app/x.py": "x\n"})
    calls = _patch_requests(
        mocker,
        [
            _FakeResponse(200, content=archive),
            _FakeResponse(200, content=archive),
            _FakeResponse(200, content=archive),
        ],
    )

    for repo, sha in (
        ("widget", COMMIT_SHA),
        ("other", COMMIT_SHA),
        ("widget", "d" * 40),
    ):
        texts = await authoritative_texts_at_commit(
            {"backend/app/x.py"},
            token="ghs_test",
            owner="acme",
            repo=repo,
            commit_sha=sha,
        )
        assert texts == {"backend/app/x.py": "x\n"}

    assert len(calls) == 3


@pytest.fixture(autouse=True)
def _reset_snapshot_state():
    """Each test starts with cold caches and a healthy quota observation."""
    snapshot._branch_sha_cache.clear()
    snapshot._tree_cache.clear()
    snapshot._blob_cache.clear()
    snapshot._blob_cache_bytes = 0
    snapshot._archive_cache.clear()
    snapshot._archive_cache_bytes = 0
    snapshot._tree_fetch_locks.clear()
    snapshot._archive_fetch_locks.clear()
    saved = dict(shared_state.github_quota)
    shared_state.github_quota.update(
        {"remaining": 4000, "limit": 5000, "reset_at": int(time.time()) + 1800}
    )
    yield
    shared_state.github_quota.update(saved)


def _patch_requests(mocker, responses):
    """Patch the GitHub request layer with a scripted response sequence."""
    calls = []

    async def _fake_request(method, url, headers, **kwargs):
        calls.append((method, url))
        result = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(result, Exception):
            raise result
        return result

    mocker.patch("app.github._github_request", side_effect=_fake_request)
    return calls


# ── call-count economics ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_many_existence_checks_cost_two_api_calls(mocker):
    """N path checks against one branch = 1 ref call + 1 tree call, total."""
    calls = _patch_requests(mocker, [_ref_response(), _tree_response()])

    assert await path_exists_on_branch("backend/app/x.py", **CTX) is True
    assert await path_exists_on_branch("backend/app/y.py", **CTX) is True
    for i in range(20):
        assert await path_exists_on_branch(f"backend/app/gone{i}.py", **CTX) is False

    assert len(calls) == 2
    assert "/git/ref/heads/automain" in calls[0][1]
    assert f"/git/trees/{COMMIT_SHA}?recursive=1" in calls[1][1]


@pytest.mark.asyncio
async def test_branch_tip_cached_within_ttl(mocker):
    """A second pass inside the TTL re-uses both the ref and the tree."""
    calls = _patch_requests(mocker, [_ref_response(), _tree_response()])
    assert await path_exists_on_branch("backend/app/x.py", **CTX) is True
    assert await path_exists_on_branch("backend/app/x.py", **CTX) is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_tree_reused_across_branch_tip_refreshes(mocker):
    """When the TTL lapses but the branch did not advance, the immutable
    tree snapshot (keyed by commit SHA) is reused — only the ref is re-fetched."""
    calls = _patch_requests(
        mocker, [_ref_response(), _tree_response(), _ref_response()]
    )
    assert await path_exists_on_branch("backend/app/x.py", **CTX) is True
    # Force the branch-tip TTL to lapse without monkeying with the clock.
    key = ("acme", "widget", "automain")
    sha, fetched_at = snapshot._branch_sha_cache[key]
    snapshot._branch_sha_cache[key] = (sha, fetched_at - 10_000)
    assert await path_exists_on_branch("backend/app/y.py", **CTX) is True
    assert len(calls) == 3  # ref, tree, ref — no second tree fetch


@pytest.mark.asyncio
async def test_file_text_cached_by_blob_sha(mocker):
    """Repeated content reads of the same file cost one blob fetch."""
    text = "def real_symbol():\n    pass\n"
    calls = _patch_requests(
        mocker, [_ref_response(), _tree_response(), _blob_response(text)]
    )
    exists, got = await file_text_on_branch("backend/app/x.py", **CTX)
    assert (exists, got) == (True, text)
    exists, got = await file_text_on_branch("backend/app/x.py", **CTX)
    assert (exists, got) == (True, text)
    assert len(calls) == 3
    assert f"/git/blobs/{BLOB_A}" in calls[2][1]


# ── conservative degradation ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ref_failure_is_indeterminate(mocker):
    _patch_requests(mocker, [_FakeResponse(403)])
    assert await path_exists_on_branch("backend/app/x.py", **CTX) is None


@pytest.mark.asyncio
async def test_tree_failure_is_indeterminate(mocker):
    _patch_requests(mocker, [_ref_response(), _FakeResponse(403)])
    assert await path_exists_on_branch("backend/app/x.py", **CTX) is None
    exists, text = await file_text_on_branch("backend/app/x.py", **CTX)
    assert (exists, text) == (None, None)


@pytest.mark.asyncio
async def test_truncated_tree_never_reports_absence(mocker):
    """A truncated tree answers True for visible paths, None for the rest."""
    calls = _patch_requests(
        mocker, [_ref_response(), _tree_response(truncated=True)]
    )
    assert await path_exists_on_branch("backend/app/x.py", **CTX) is True
    assert await path_exists_on_branch("backend/app/invisible.py", **CTX) is None
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_glob_exists_on_branch_matches_cached_tree(mocker):
    calls = _patch_requests(
        mocker,
        [
            _ref_response(),
            _tree_response(paths=("backend/app/routers/admin.py", "package.json")),
        ],
    )
    assert await glob_exists_on_branch("backend/app/routers/*.py", **CTX) is True
    assert await glob_exists_on_branch("*.json", **CTX) is True
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_glob_exists_on_branch_complete_miss_false(mocker):
    _patch_requests(
        mocker,
        [_ref_response(), _tree_response(paths=("backend/app/models.py",))],
    )
    assert await glob_exists_on_branch("backend/app/routers/*.py", **CTX) is False


@pytest.mark.asyncio
async def test_glob_exists_on_branch_truncated_miss_indeterminate(mocker):
    _patch_requests(
        mocker,
        [
            _ref_response(),
            _tree_response(paths=("backend/app/models.py",), truncated=True),
        ],
    )
    assert await glob_exists_on_branch("backend/app/routers/*.py", **CTX) is None


@pytest.mark.asyncio
async def test_glob_exists_on_branch_unavailable_snapshot_indeterminate(mocker):
    _patch_requests(mocker, [_FakeResponse(403)])
    assert await glob_exists_on_branch("backend/app/routers/*.py", **CTX) is None


@pytest.mark.asyncio
async def test_blob_failure_yields_present_but_unreadable(mocker):
    _patch_requests(mocker, [_ref_response(), _tree_response(), _FakeResponse(500)])
    exists, text = await file_text_on_branch("backend/app/x.py", **CTX)
    assert exists is True
    assert text is None


@pytest.mark.asyncio
async def test_transport_error_is_indeterminate(mocker):
    _patch_requests(mocker, [RuntimeError("boom")])
    assert await path_exists_on_branch("backend/app/x.py", **CTX) is None


# ── quota reserve floor ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_quota_reserve_short_circuits_without_network(mocker):
    """Below the reserve floor, grounding spends ZERO quota and degrades to
    indeterminate — the structural guarantee that advisory validation can
    never again starve PR/merge/CI operations."""
    calls = _patch_requests(mocker, [_ref_response(), _tree_response()])
    shared_state.github_quota.update(
        {"remaining": snapshot.GROUNDING_QUOTA_RESERVE - 1,
         "reset_at": int(time.time()) + 600}
    )
    assert await path_exists_on_branch("backend/app/x.py", **CTX) is None
    exists, text = await file_text_on_branch("backend/app/x.py", **CTX)
    assert (exists, text) == (None, None)
    assert calls == []


@pytest.mark.asyncio
async def test_quota_reserve_ignores_stale_window(mocker):
    """A low reading whose reset time already passed does not block."""
    calls = _patch_requests(mocker, [_ref_response(), _tree_response()])
    shared_state.github_quota.update(
        {"remaining": 5, "reset_at": int(time.time()) - 10}
    )
    assert await path_exists_on_branch("backend/app/x.py", **CTX) is True
    assert len(calls) == 2


# ── integration with source_ref_validation ───────────────────────────────────


@pytest.mark.asyncio
async def test_validate_source_refs_uses_snapshot_not_contents(mocker):
    """validate_source_refs flows through the snapshot layer: many refs, two
    API calls, and line-bound validation still works from blob content."""
    from app.source_ref_validation import validate_source_refs

    text = "line1\nline2\nline3\n"
    calls = _patch_requests(
        mocker, [_ref_response(), _tree_response(), _blob_response(text)]
    )
    sources = [
        {"type": "file", "ref": "backend/app/x.py"},
        {"type": "file", "ref": "backend/app/x.py:2"},
        {"type": "file", "ref": "backend/app/x.py:99"},
        {"type": "file", "ref": "backend/app/gone.py"},
    ]
    validated = await validate_source_refs(sources, project_id="p1", **CTX)
    assert [s.get("valid", True) for s in validated] == [True, True, False, False]
    assert len(calls) == 3  # ref + tree + one blob — not one call per source


@pytest.mark.asyncio
async def test_symbol_grounding_shares_blob_cache(mocker):
    """detect_absent_symbols re-uses the same cached blob as the file checks."""
    from app.source_ref_validation import detect_absent_symbols

    text = "def present_symbol():\n    pass\n"
    calls = _patch_requests(
        mocker, [_ref_response(), _tree_response(), _blob_response(text)]
    )
    absent = await detect_absent_symbols(
        ["backend/app/x.py:present_symbol", "backend/app/x.py:deleted_symbol"],
        [],
        **CTX,
    )
    assert absent == ["backend/app/x.py:deleted_symbol"]
    assert len(calls) == 3  # both symbol checks share one blob fetch


@pytest.mark.asyncio
async def test_reconcile_design_doc_indeterminate_leaves_entry_untouched(mocker):
    """An indeterminate branch lookup (e.g. rate limit) must never downgrade a
    design-doc entry — previously an API failure read as absence."""
    from unittest.mock import AsyncMock

    from app.source_ref_validation import reconcile_design_doc_presence

    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch",
        new=AsyncMock(return_value=None),
    )
    entry = {"status": "mature", "modules": ["backend/app/x.py"]}
    out = await reconcile_design_doc_presence(
        entry, "systems/auth", project_id="p1", **CTX
    )
    assert out["status"] == "mature"
    assert "presence_warning" not in out


@pytest.mark.asyncio
async def test_playbook_branch_grounding_indeterminate_never_downgrades(mocker):
    """The playbook design-doc grounding sweep leaves entries untouched when
    every path lookup is indeterminate."""
    from unittest.mock import AsyncMock

    from app.playbook_storage import ground_design_doc_systems_branch_aware

    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch",
        new=AsyncMock(return_value=None),
    )
    doc = {"systems/auth": {"status": "mature", "modules": ["backend/app/x.py"]}}
    out = await ground_design_doc_systems_branch_aware(doc, project_id="p1", **CTX)
    assert out["systems/auth"]["status"] == "mature"
    assert "_meta" not in out


@pytest.mark.asyncio
async def test_classify_paths_tristate_buckets(mocker):
    from unittest.mock import AsyncMock

    from app.source_ref_validation import classify_paths_tristate

    async def _exists(path, **_kwargs):
        return {"a.py": True, "gone.py": False}.get(path)

    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch",
        new=AsyncMock(side_effect=_exists),
    )
    present, missing, indeterminate = await classify_paths_tristate(
        ["a.py", "gone.py", "unknown.py"], project_id="p1", **CTX
    )
    assert present == ["a.py"]
    assert missing == ["gone.py"]
    assert indeterminate == ["unknown.py"]


def test_looks_like_file_path_rejects_globs_and_whitespace():
    """Glob/whitespace tokens are rejected before they cost an API call —
    grounding passes were literally requesting ``/contents/routers/%2A.py``."""
    from app.source_ref_validation import _looks_like_file_path

    assert _looks_like_file_path("backend/app/models.py") is True
    assert _looks_like_file_path("backend/app/routers/*.py") is False
    assert _looks_like_file_path("backend/app/foo?.py") is False
    assert _looks_like_file_path("backend/app/{a,b}.py") is False
    assert _looks_like_file_path("backend/app/has space.py") is False
    assert _looks_like_file_path(" backend/app/models.py") is False


# ── behind-detection base-tip cache wiring (GitHub-quota burn fix, 2026-06-15) ─


@pytest.mark.asyncio
async def test_get_branch_sha_caches_by_default(mocker):
    """Cache-by-default: the 2nd read serves from the shared ~60s branch-tip
    cache — collapses the repeated identical git/ref reads that dominate quota
    burn (no per-call opt-in needed anymore)."""
    from app.github import get_branch_sha

    calls = _patch_requests(mocker, [_ref_response()])
    sha1 = await get_branch_sha("ghs_test", "acme", "widget", "automain")
    sha2 = await get_branch_sha("ghs_test", "acme", "widget", "automain")
    assert sha1 == sha2 == COMMIT_SHA
    ref_calls = [c for c in calls if "/git/ref/heads/automain" in c[1]]
    assert len(ref_calls) == 1
    assert shared_state.github_cache_hits.get("branch_sha_snapshot", 0) >= 1


# ── list_dir_children_on_branch ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_dir_children_root_listing(mocker):
    """Root listing (dir_path='') classifies blobs as 'file' and infers 'dir'
    entries from nested paths."""
    from app.github_repo_snapshot import list_dir_children_on_branch

    tree_paths = ("README.md", "backend/app/main.py", "frontend/index.ts")
    _patch_requests(mocker, [_ref_response(), _tree_response(tree_paths)])

    children = await list_dir_children_on_branch(
        "", token="ghs_test", owner="acme", repo="widget", branch="automain"
    )
    assert children is not None
    by_name = {c["name"]: c for c in children}
    assert by_name["README.md"]["type"] == "file"
    assert by_name["backend"]["type"] == "dir"
    assert by_name["frontend"]["type"] == "dir"
    # No deeper entries (backend/app/main.py must NOT appear at root)
    assert "app" not in by_name
    assert "main.py" not in by_name


@pytest.mark.asyncio
async def test_list_dir_children_subdir_returns_only_immediate_children(mocker):
    """Subdir listing returns only direct children, not grandchildren."""
    from app.github_repo_snapshot import list_dir_children_on_branch

    tree_paths = (
        "backend/app/main.py",
        "backend/app/models.py",
        "backend/app/routers/admin.py",
        "frontend/index.ts",
    )
    _patch_requests(mocker, [_ref_response(), _tree_response(tree_paths)])

    children = await list_dir_children_on_branch(
        "backend/app", token="ghs_test", owner="acme", repo="widget", branch="automain"
    )
    assert children is not None
    by_name = {c["name"]: c for c in children}
    assert by_name["main.py"]["type"] == "file"
    assert by_name["models.py"]["type"] == "file"
    assert by_name["routers"]["type"] == "dir"
    # Grandchild must not appear
    assert "admin.py" not in by_name
    # Sibling branch must not appear
    assert "frontend" not in by_name


@pytest.mark.asyncio
async def test_list_dir_children_truncated_snapshot_returns_none(mocker):
    """A truncated tree snapshot returns None so callers fall back to direct API."""
    from app.github_repo_snapshot import list_dir_children_on_branch

    _patch_requests(mocker, [_ref_response(), _tree_response(truncated=True)])

    result = await list_dir_children_on_branch(
        "backend", token="ghs_test", owner="acme", repo="widget", branch="automain"
    )
    assert result is None


@pytest.mark.asyncio
async def test_list_dir_children_unavailable_snapshot_returns_none(mocker):
    """When the snapshot is unavailable (e.g. branch tip lookup fails), returns None."""
    from app.github_repo_snapshot import list_dir_children_on_branch

    # 404 on the branch-tip lookup makes _get_snapshot return None.
    _patch_requests(mocker, [_FakeResponse(404)])

    result = await list_dir_children_on_branch(
        "backend", token="ghs_test", owner="acme", repo="widget", branch="automain"
    )
    assert result is None


@pytest.mark.asyncio
async def test_get_branch_sha_live_observes_live_tip(mocker):
    """live=True re-fetches every time and never populates the cache — SHA-
    critical callers (promotion/merge verification, ref/branch anchors) must
    observe the live tip, never a stale cached SHA, and must not pollute the
    shared cache with a value other reads would then treat as the tip."""
    from app.github import get_branch_sha

    calls = _patch_requests(mocker, [_ref_response()])
    await get_branch_sha("ghs_test", "acme", "widget", "automain", live=True)
    await get_branch_sha("ghs_test", "acme", "widget", "automain", live=True)
    ref_calls = [c for c in calls if "/git/ref/heads/automain" in c[1]]
    assert len(ref_calls) == 2
    assert ("acme", "widget", "automain") not in snapshot._branch_sha_cache


@pytest.mark.asyncio
async def test_pr_is_not_behind_for_reuses_cached_base_tip(mocker):
    """Behind-detection wiring: two checks of the same base branch (as happens
    when many concurrent PRs all check 'am I behind main?') fetch the base tip
    from GitHub only ONCE."""
    from unittest.mock import AsyncMock

    from app.github import pr_is_not_behind_for

    calls = _patch_requests(mocker, [_ref_response()])
    # Isolate the base-tip fetch: stub the actual not-behind comparison so it
    # makes no further GitHub calls.
    mocker.patch("app.github.pr_is_not_behind", new=AsyncMock(return_value=True))

    detail = {"head": {"sha": "h" * 40}, "mergeable_state": "clean"}
    await pr_is_not_behind_for(
        "ghs_test", "acme", "widget", pr_detail=detail, base_branch="automain"
    )
    await pr_is_not_behind_for(
        "ghs_test", "acme", "widget", pr_detail=detail, base_branch="automain"
    )

    ref_calls = [c for c in calls if "/git/ref/heads/automain" in c[1]]
    assert len(ref_calls) == 1


@pytest.mark.asyncio
async def test_file_text_at_commit_skips_branch_tip_cache(mocker):
    """A commit-pinned read never consults the branch-tip cache: even with a
    freshly-cached PRE-merge tip, the tree is fetched at the exact commit SHA —
    so a file the merge itself just added is found, not reported absent."""
    from app.github_repo_snapshot import file_text_at_commit

    # Poison the tip cache with a pre-merge commit whose tree lacks the file.
    snapshot._branch_sha_cache[("acme", "widget", "automain")] = (
        "e" * 40,
        time.monotonic(),
    )
    merge_sha = "f" * 40
    calls = _patch_requests(
        mocker,
        [
            _tree_response(
                paths=("backend/app/x.py", "backend/testing/test_new.py")
            ),
            _blob_response("def test_new():\n    pass\n"),
        ],
    )
    exists, text = await file_text_at_commit(
        "backend/testing/test_new.py",
        token="ghs_test",
        owner="acme",
        repo="widget",
        commit_sha=merge_sha,
    )
    assert exists is True
    assert "test_new" in (text or "")
    # No /git/ref call at all — the first request is the tree at the exact
    # merge commit, immune to any cached (stale) branch tip.
    assert f"/git/trees/{merge_sha}?recursive=1" in calls[0][1]
    assert all("/git/ref/" not in url for _, url in calls)


@pytest.mark.asyncio
async def test_file_text_at_commit_quota_reserve_indeterminate(mocker):
    """The commit-pinned read honors the quota reserve floor exactly like the
    branch read: below the reserve it degrades to indeterminate, never absent."""
    from app.github_repo_snapshot import file_text_at_commit

    shared_state.github_quota.update(
        {"remaining": 10, "limit": 5000, "reset_at": int(time.time()) + 1800}
    )
    calls = _patch_requests(mocker, [_tree_response()])
    exists, text = await file_text_at_commit(
        "backend/app/x.py",
        token="ghs_test",
        owner="acme",
        repo="widget",
        commit_sha="f" * 40,
    )
    assert exists is None
    assert text is None
    assert calls == []
