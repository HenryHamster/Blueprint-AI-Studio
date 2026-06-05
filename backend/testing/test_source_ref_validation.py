"""Unit tests for the shared file-existence checkers in source_ref_validation.

Covers the generalized helpers extracted so design-doc system verification and
playbook stale-ref flagging share one partition path:
- ``partition_missing`` — pure sync partitioner over an arbitrary predicate.
- ``classify_existing_paths`` — async git-HEAD-backed classifier.
"""

from unittest.mock import AsyncMock

import pytest

from app.source_ref_validation import (
    classify_existing_paths,
    detect_absent_symbols,
    partition_missing,
    summarize_ungrounded_claims,
    validate_source_refs,
)


# ── partition_missing (pure) ──────────────────────────────────────────────────


def test_partition_missing_all_present():
    present, missing = partition_missing(
        ["a.py", "b.py"], lambda p: True
    )
    assert present == ["a.py", "b.py"]
    assert missing == []


def test_partition_missing_all_absent():
    present, missing = partition_missing(
        ["a.py", "b.py"], lambda p: False
    )
    assert present == []
    assert missing == ["a.py", "b.py"]


def test_partition_missing_mixed_preserves_order():
    existing = {"a.py", "c.py"}
    present, missing = partition_missing(
        ["a.py", "b.py", "c.py", "d.py"], lambda p: p in existing
    )
    assert present == ["a.py", "c.py"]
    assert missing == ["b.py", "d.py"]


def test_partition_missing_non_string_treated_as_missing():
    present, missing = partition_missing(
        ["a.py", None, 42], lambda p: True
    )
    assert present == ["a.py"]
    assert missing == [None, 42]


# ── classify_existing_paths (async, git-backed via _file_exists_at_head) ──────


@pytest.mark.asyncio
async def test_classify_existing_paths_all_present(mocker):
    async def _exists(p):
        return True

    mocker.patch("app.source_ref_validation._file_exists_at_head", side_effect=_exists)
    present, missing = await classify_existing_paths(["x.py", "y.py"])
    assert present == ["x.py", "y.py"]
    assert missing == []


@pytest.mark.asyncio
async def test_classify_existing_paths_all_missing(mocker):
    async def _exists(p):
        return False

    mocker.patch("app.source_ref_validation._file_exists_at_head", side_effect=_exists)
    present, missing = await classify_existing_paths(["x.py", "y.py"])
    assert present == []
    assert missing == ["x.py", "y.py"]


@pytest.mark.asyncio
async def test_classify_existing_paths_mixed(mocker):
    existing = {"keep.py"}

    async def _exists(p):
        return p in existing

    mocker.patch("app.source_ref_validation._file_exists_at_head", side_effect=_exists)
    present, missing = await classify_existing_paths(["keep.py", "gone.py"])
    assert present == ["keep.py"]
    assert missing == ["gone.py"]


@pytest.mark.asyncio
async def test_classify_existing_paths_non_string_missing(mocker):
    async def _exists(p):
        return True

    mocker.patch("app.source_ref_validation._file_exists_at_head", side_effect=_exists)
    present, missing = await classify_existing_paths(["ok.py", None])
    assert present == ["ok.py"]
    assert missing == [None]


@pytest.mark.asyncio
async def test_classify_existing_paths_uses_repo_branch_context_when_provided(mocker):
    async def _exists(p, **kwargs):
        assert kwargs == {
            "token": "ghs_test",
            "owner": "acme",
            "repo": "widget",
            "branch": "automain",
            "project_id": "proj-123",
        }
        return p == "x.py"

    local_exists = mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        side_effect=AssertionError("local HEAD fallback should not be used"),
    )
    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch", side_effect=_exists
    )
    present, missing = await classify_existing_paths(
        ["x.py", "y.py"],
        token="ghs_test",
        owner="acme",
        repo="widget",
        branch="automain",
        project_id="proj-123",
    )
    assert present == ["x.py"]
    assert missing == ["y.py"]
    local_exists.assert_not_called()


# ── summarize_ungrounded_claims (async, verifies symbol definitions) ──────────


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_all_grounded(mocker):
    """A claim with symbols that ARE defined returns None (no ungrounded refs)."""
    async def _symbol_defined(path, symbol):
        return True

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_symbol_defined
    )
    result = await summarize_ungrounded_claims(
        ["backend/app/x.py:my_func stores Y"]
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_single_absent_symbol(mocker):
    """A claim with a symbol NOT defined is flagged and counted."""
    async def _symbol_defined(path, symbol):
        return False

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_symbol_defined
    )
    result = await summarize_ungrounded_claims(
        ["backend/app/x.py:missing_func does something"]
    )
    assert result is not None
    assert result["ungrounded_claim_count"] == 1
    assert result["ungrounded_refs"] == ["backend/app/x.py:missing_func"]


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_multiple_refs_in_one_claim(mocker):
    """A claim with multiple path:symbol refs counts as one claim if any ref is absent."""

    async def _symbol_defined(path, symbol):
        return symbol != "missing_func"

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_symbol_defined
    )
    result = await summarize_ungrounded_claims(
        ["backend/app/x.py:present_func calls backend/app/y.py:missing_func"]
    )
    assert result is not None
    assert result["ungrounded_claim_count"] == 1
    assert result["ungrounded_refs"] == ["backend/app/y.py:missing_func"]


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_deduplicates_refs_across_claims(mocker):
    """Same ungrounded ref in multiple claims is deduplicated in ungrounded_refs."""
    async def _symbol_defined(path, symbol):
        return symbol != "bug_func"

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_symbol_defined
    )
    result = await summarize_ungrounded_claims(
        [
            "backend/app/x.py:bug_func has an issue",
            "backend/app/x.py:bug_func also causes this",
        ]
    )
    assert result is not None
    assert result["ungrounded_claim_count"] == 2
    assert result["ungrounded_refs"] == ["backend/app/x.py:bug_func"]


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_mixed_grounded_ungrounded(mocker):
    """Mix of grounded and ungrounded claims counts only ungrounded ones."""
    async def _symbol_defined(path, symbol):
        return symbol in {"good_func", "another_good"}

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_symbol_defined
    )
    result = await summarize_ungrounded_claims(
        [
            "backend/app/x.py:good_func is implemented",
            "backend/app/y.py:missing_func does not exist",
            "backend/app/z.py:another_good is also good",
        ]
    )
    assert result is not None
    assert result["ungrounded_claim_count"] == 1
    assert result["ungrounded_refs"] == ["backend/app/y.py:missing_func"]


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_indeterminate_git_not_flagged(mocker):
    """When git is unavailable (None result), refs are NOT flagged (fail-open)."""
    async def _symbol_defined(path, symbol):
        return None

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_symbol_defined
    )
    result = await summarize_ungrounded_claims(
        ["backend/app/x.py:maybe_func might exist"]
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_no_refs_returns_none(mocker):
    """Claims with no path:symbol refs return None."""
    mocker.patch("app.source_ref_validation._symbol_defined_at_head")
    result = await summarize_ungrounded_claims(
        ["This is a plain text claim", "Another claim without refs"]
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_line_number_not_symbol(mocker):
    """Line-number refs like file.py:42 are not treated as symbols."""
    mocker.patch("app.source_ref_validation._symbol_defined_at_head")
    result = await summarize_ungrounded_claims(
        ["See backend/app/x.py:42 for details"]
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_with_sources_excerpt(mocker):
    """Sources with file-type and excerpt/quote are checked but don't count claims."""
    async def _symbol_defined(path, symbol):
        return symbol != "bad_symbol"

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_symbol_defined
    )
    result = await summarize_ungrounded_claims(
        claims=["backend/app/x.py:good_func works"],
        sources=[
            {
                "type": "file",
                "ref": "backend/app/y.py:10",
                "quote": "backend/app/y.py:bad_symbol is used here",
            }
        ],
    )
    assert result is not None
    assert result["ungrounded_claim_count"] == 0
    assert result["ungrounded_refs"] == ["backend/app/y.py:bad_symbol"]


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_non_file_sources_ignored(mocker):
    """Non-file sources (PR, message) don't count as claims even if ungrounded."""
    async def _symbol_defined(path, symbol):
        return True

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_symbol_defined
    )
    result = await summarize_ungrounded_claims(
        claims=["backend/app/x.py:good_func"],
        sources=[
            {"type": "pr", "ref": "org/repo#123"},
            {"type": "message", "text": "some message"},
        ],
    )
    assert result is None


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_none_claims_list(mocker):
    """None claims list is treated as empty."""
    mocker.patch("app.source_ref_validation._symbol_defined_at_head")
    result = await summarize_ungrounded_claims(None)
    assert result is None


@pytest.mark.asyncio
async def test_summarize_ungrounded_claims_complex_scenario(mocker):
    """Complex scenario: multiple claims, some with multiple refs, mixed grounding."""
    defined_symbols = {
        ("backend/app/models.py", "Task"),
        ("backend/app/orchestrator.py", "dispatch"),
        ("agent/src/index.ts", "pollForNextRole"),
    }

    async def _symbol_defined(path, symbol):
        return (path, symbol) in defined_symbols

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_symbol_defined
    )
    result = await summarize_ungrounded_claims(
        [
            "backend/app/models.py:Task is the base class",
            "backend/app/orchestrator.py:dispatch handles work units",
            "agent/src/index.ts:pollForNextRole polls and backend/app/missing.py:nonexistent does not exist",
            "Some plain text claim",
        ]
    )
    assert result is not None
    assert result["ungrounded_claim_count"] == 1
    assert result["ungrounded_refs"] == [
        "backend/app/missing.py:nonexistent",
    ]


# ── reconcile_design_doc_presence (GitHub-backed + local fallback) ────────────


@pytest.mark.asyncio
async def test_reconcile_presence_repo_branch_all_absent_downgrades(mocker):
    """With GitHub context, an entry whose every module is absent on the branch
    is downgraded to ``missing`` via _file_exists_in_repo_branch (no local git)."""
    from app.source_ref_validation import reconcile_design_doc_presence

    async def _in_branch(path, **kwargs):
        assert kwargs["token"] == "ghs_tok"
        assert kwargs["owner"] == "acme"
        assert kwargs["repo"] == "widget"
        assert kwargs["branch"] == "automain"
        return False

    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch", side_effect=_in_branch
    )
    local = mocker.patch(
        "app.source_ref_validation._path_exists_at_head",
        side_effect=AssertionError("local HEAD should not be consulted"),
    )
    value = {"status": "mature", "modules": ["backend/app/gone.py"]}
    result = await reconcile_design_doc_presence(
        value,
        "systems/auth",
        repo_root="/nonexistent/path/xyz",  # must NOT gate the repo-branch path
        token="ghs_tok",
        owner="acme",
        repo="widget",
        branch="automain",
        project_id="proj-1",
    )
    assert result["status"] == "missing"
    assert "presence_warning" in result
    assert result["description"].startswith("[reconciled:")
    local.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_presence_repo_branch_partial_hit_unchanged(mocker):
    """With GitHub context, a partial hit (one module present) leaves the entry
    untouched — only all-absent entries downgrade."""
    from app.source_ref_validation import reconcile_design_doc_presence

    async def _in_branch(path, **kwargs):
        return path == "backend/app/present.py"

    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch", side_effect=_in_branch
    )
    value = {
        "status": "working",
        "notes": "backend/app/present.py and backend/app/gone.py",
    }
    result = await reconcile_design_doc_presence(
        value,
        "capabilities/research",
        token="ghs_tok",
        owner="acme",
        repo="widget",
        branch="automain",
    )
    assert result["status"] == "working"
    assert "presence_warning" not in result


@pytest.mark.asyncio
async def test_reconcile_presence_local_indeterminate_unchanged(mocker, tmp_path):
    """Without GitHub creds, an indeterminate local check (None) is a no-op."""
    from app.source_ref_validation import reconcile_design_doc_presence

    async def _head(path, repo_root=None):
        return None

    mocker.patch(
        "app.source_ref_validation._path_exists_at_head", side_effect=_head
    )
    value = {"status": "mature", "modules": ["backend/app/gone.py"]}
    result = await reconcile_design_doc_presence(
        value, "systems/auth", repo_root=str(tmp_path)
    )
    assert result["status"] == "mature"
    assert "presence_warning" not in result


@pytest.mark.asyncio
async def test_reconcile_presence_backward_compat_two_arg_downgrades(mocker, tmp_path):
    """The legacy two-positional-arg call (value, key) still downgrades via the
    local-git path when no GitHub context is supplied."""
    from app.source_ref_validation import reconcile_design_doc_presence

    async def _head(path, repo_root=None):
        return False

    mocker.patch(
        "app.source_ref_validation._path_exists_at_head", side_effect=_head
    )
    value = {"status": "mature", "modules": ["backend/app/gone.py"]}
    result = await reconcile_design_doc_presence(
        value, "systems/auth", repo_root=str(tmp_path)
    )
    assert result["status"] == "missing"
    assert "presence_warning" in result


# ── reconcile_design_doc (doc-level walker) ───────────────────────────────────


@pytest.mark.asyncio
async def test_reconcile_design_doc_walks_keys_and_skips_others(mocker):
    """reconcile_design_doc downgrades all-absent systems/capabilities and leaves
    non-system/capability keys (and partial hits) untouched."""
    from app.source_ref_validation import reconcile_design_doc

    async def _in_branch(path, **kwargs):
        return path == "backend/app/real.py"

    mocker.patch(
        "app.source_ref_validation._file_exists_in_repo_branch", side_effect=_in_branch
    )
    doc = {
        "systems/gone": {"status": "mature", "modules": ["backend/app/gone.py"]},
        "capabilities/kept": {
            "status": "working",
            "notes": "backend/app/real.py exists",
        },
        "playbook/tip": {"status": "mature", "modules": ["backend/app/gone.py"]},
        "summary": "free text, not a record",
    }
    result = await reconcile_design_doc(
        doc,
        token="ghs_tok",
        owner="acme",
        repo="widget",
        branch="automain",
    )
    assert result["systems/gone"]["status"] == "missing"
    assert result["capabilities/kept"]["status"] == "working"
    # Non system/capability keys are passed through verbatim.
    assert result["playbook/tip"]["status"] == "mature"
    assert "presence_warning" not in result["playbook/tip"]
    assert result["summary"] == "free text, not a record"
    # Input is not mutated.
    assert doc["systems/gone"]["status"] == "mature"


@pytest.mark.asyncio
async def test_reconcile_design_doc_non_dict_passthrough():
    from app.source_ref_validation import reconcile_design_doc

    assert await reconcile_design_doc(None) is None
    assert await reconcile_design_doc(["x"]) == ["x"]


# ── resolve_branch_verification_context ───────────────────────────────────────


@pytest.mark.asyncio
async def test_resolve_branch_context_includes_creds_when_resolved(mocker):
    from app.source_ref_validation import resolve_branch_verification_context

    mocker.patch(
        "app.branch_resolution.get_project_integration_branch",
        return_value="automain",
    )

    async def _resolve(session, project):
        return ("ghs_tok", "acme", "widget")

    mocker.patch(
        "app.release_promotion._resolve_github_with_session", side_effect=_resolve
    )

    class _P:
        id = "proj-1"

    ctx = await resolve_branch_verification_context(object(), _P())
    assert ctx == {
        "branch": "automain",
        "project_id": "proj-1",
        "token": "ghs_tok",
        "owner": "acme",
        "repo": "widget",
    }


@pytest.mark.asyncio
async def test_resolve_branch_context_branch_only_when_creds_raise(mocker):
    """When credential resolution raises, only branch + project_id are returned
    so callers fall back to local git HEAD."""
    from app.source_ref_validation import resolve_branch_verification_context

    mocker.patch(
        "app.branch_resolution.get_project_integration_branch",
        return_value="automain",
    )

    async def _boom(session, project):
        raise RuntimeError("no creds")

    mocker.patch(
        "app.release_promotion._resolve_github_with_session", side_effect=_boom
    )

    class _P:
        id = "proj-2"

    ctx = await resolve_branch_verification_context(object(), _P())
    assert ctx == {"branch": "automain", "project_id": "proj-2"}


# ── detect_absent_symbols (symbol-level grounding depth) ──────────────────────


@pytest.mark.asyncio
async def test_detect_absent_symbols_present_symbol_not_flagged(mocker):
    """File present + symbol present → grounded, nothing flagged."""

    async def _content(path, **kwargs):
        return True, "def do_thing():\n    pass\n"

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch", side_effect=_content
    )
    absent = await detect_absent_symbols(
        ["backend/app/x.py:do_thing"],
        token="ghs_tok",
        owner="acme",
        repo="widget",
        branch="automain",
        project_id="proj-1",
    )
    assert absent == []


@pytest.mark.asyncio
async def test_detect_absent_symbols_absent_symbol_in_present_file_flagged(mocker):
    """File present but cited symbol gone → flagged as effectively-absent."""

    async def _content(path, **kwargs):
        return True, "def live_func():\n    pass\n"

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch", side_effect=_content
    )
    absent = await detect_absent_symbols(
        ["backend/app/x.py:gone_func"],
        ["the impl in backend/app/x.py:gone_func handles Y"],
        token="ghs_tok",
        owner="acme",
        repo="widget",
        branch="automain",
        project_id="proj-1",
    )
    # Deduplicated: the module pointer and the free-text ref collapse to one.
    assert absent == ["backend/app/x.py:gone_func"]


@pytest.mark.asyncio
async def test_detect_absent_symbols_indeterminate_not_flagged(mocker):
    """Indeterminate git result (None) → conservatively not flagged."""

    async def _content(path, **kwargs):
        return None, None

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch", side_effect=_content
    )
    absent = await detect_absent_symbols(
        ["backend/app/x.py:maybe_func"],
        token="ghs_tok",
        owner="acme",
        repo="widget",
        branch="automain",
        project_id="proj-1",
    )
    assert absent == []


@pytest.mark.asyncio
async def test_detect_absent_symbols_partial_hit_only_flags_absent(mocker):
    """Mixed: only the unambiguously-absent symbol is reported."""

    async def _defined(path, symbol, **kwargs):
        return symbol != "gone_func"

    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head", side_effect=_defined
    )
    absent = await detect_absent_symbols(
        ["backend/app/x.py:gone_func", "backend/app/x.py:live_func"]
    )
    assert absent == ["backend/app/x.py:gone_func"]


@pytest.mark.asyncio
async def test_detect_absent_symbols_no_symbol_refs_returns_empty(mocker):
    """Plain file paths with no `:symbol` suffix carry no symbol claim."""
    called = mocker.patch("app.source_ref_validation._symbol_defined_at_head")
    absent = await detect_absent_symbols(
        ["backend/app/x.py"], ["prose with no path:symbol refs"]
    )
    assert absent == []
    called.assert_not_called()


# ── validate_source_refs (integration-branch grounding + local fallback) ──────


@pytest.mark.asyncio
async def test_validate_source_refs_repo_branch_absent_flags_invalid(mocker):
    """With GitHub context, an unambiguous absence (False) marks the ref invalid
    via the GitHub contents path — the local HEAD must NOT be consulted."""

    async def _content(path, **kwargs):
        assert kwargs["token"] == "ghs_tok"
        assert kwargs["owner"] == "acme"
        assert kwargs["repo"] == "widget"
        assert kwargs["branch"] == "automain"
        assert kwargs["project_id"] == "proj-1"
        return (False, None)

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch", side_effect=_content
    )
    local = mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        side_effect=AssertionError("local HEAD should not be consulted"),
    )
    result = await validate_source_refs(
        [{"type": "file", "ref": "backend/app/gone.py"}],
        token="ghs_tok",
        owner="acme",
        repo="widget",
        branch="automain",
        project_id="proj-1",
    )
    assert result[0]["valid"] is False
    local.assert_not_called()


@pytest.mark.asyncio
async def test_validate_source_refs_repo_branch_present_in_bounds_valid(mocker):
    """A present file with an in-range line number is not flagged."""

    async def _content(path, **kwargs):
        return (True, 120)

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch", side_effect=_content
    )
    result = await validate_source_refs(
        [{"type": "file", "ref": "backend/app/x.py:50-90"}],
        token="t",
        owner="o",
        repo="r",
        branch="b",
    )
    assert result[0].get("valid") is not False


@pytest.mark.asyncio
async def test_validate_source_refs_repo_branch_line_out_of_range_invalid(mocker):
    """A present file whose cited line exceeds the file length is flagged."""

    async def _content(path, **kwargs):
        return (True, 10)

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch", side_effect=_content
    )
    result = await validate_source_refs(
        [{"type": "file", "ref": "backend/app/x.py:50"}],
        token="t",
        owner="o",
        repo="r",
        branch="b",
    )
    assert result[0]["valid"] is False


@pytest.mark.asyncio
async def test_validate_source_refs_repo_branch_indeterminate_never_invalid(mocker):
    """An indeterminate GitHub result (None) NEVER marks a citation invalid."""

    async def _content(path, **kwargs):
        return (None, None)

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch", side_effect=_content
    )
    result = await validate_source_refs(
        [{"type": "file", "ref": "backend/app/x.py:50"}],
        token="t",
        owner="o",
        repo="r",
        branch="b",
    )
    assert result[0].get("valid") is not False


@pytest.mark.asyncio
async def test_validate_source_refs_local_fallback_absent_flags_invalid(mocker):
    """Without creds, a confirmed-absent local HEAD result (False) flags the ref
    and the GitHub contents path is not used."""

    async def _exists(path, *, cwd=None):
        return False

    mocker.patch(
        "app.source_ref_validation._file_exists_at_head", side_effect=_exists
    )
    repo = mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch",
        side_effect=AssertionError("repo-branch path should not be used"),
    )
    result = await validate_source_refs(
        [{"type": "file", "ref": "backend/app/gone.py"}]
    )
    assert result[0]["valid"] is False
    repo.assert_not_called()


@pytest.mark.asyncio
async def test_validate_source_refs_local_indeterminate_never_invalid(mocker):
    """The production-bug guard: no creds + no checkout (None) keeps citations
    valid instead of marking every file ref broken."""

    async def _exists(path, *, cwd=None):
        return None

    mocker.patch(
        "app.source_ref_validation._file_exists_at_head", side_effect=_exists
    )
    result = await validate_source_refs(
        [{"type": "file", "ref": "backend/app/x.py:999"}]
    )
    assert result[0].get("valid") is not False


@pytest.mark.asyncio
async def test_validate_source_refs_end_before_start_invalid(mocker):
    """An intrinsically malformed range (end < start) is invalid even when the
    file is present and the bounds would otherwise fit."""

    async def _content(path, **kwargs):
        return (True, 100)

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch", side_effect=_content
    )
    result = await validate_source_refs(
        [{"type": "file", "ref": "backend/app/x.py:50-10"}],
        token="t",
        owner="o",
        repo="r",
        branch="b",
    )
    assert result[0]["valid"] is False


@pytest.mark.asyncio
async def test_validate_source_refs_passthrough_non_file(mocker):
    """PR/message sources pass through with no existence lookup at all."""
    repo = mocker.patch("app.source_ref_validation._file_content_in_repo_branch")
    local = mocker.patch("app.source_ref_validation._file_exists_at_head")
    result = await validate_source_refs(
        [{"type": "pr", "ref": "o/r#1"}, {"type": "message", "text": "hi"}]
    )
    assert result == [
        {"type": "pr", "ref": "o/r#1"},
        {"type": "message", "text": "hi"},
    ]
    repo.assert_not_called()
    local.assert_not_called()


# ── _symbol_defined_in_repo_branch (branch-aware symbol grounding) ────────────


class _GhResp:
    """Minimal stand-in for the httpx response _github_request returns."""

    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


def _file_payload(text: str) -> dict:
    import base64

    return {
        "type": "file",
        "content": base64.b64encode(text.encode()).decode(),
    }


@pytest.mark.asyncio
async def test_symbol_defined_in_repo_branch_present_true(mocker):
    """File present on the branch + a plausible definition → True."""
    from app.source_ref_validation import _symbol_defined_in_repo_branch

    resp = _GhResp(200, _file_payload("def do_thing():\n    return 1\n"))
    mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=resp),
    )
    result = await _symbol_defined_in_repo_branch(
        "backend/app/x.py",
        "do_thing",
        token="t",
        owner="o",
        repo="r",
        branch="main",
        project_id="p1",
    )
    assert result is True


@pytest.mark.asyncio
async def test_symbol_defined_in_repo_branch_file_present_symbol_gone_false(mocker):
    """File present but no plausible definition → unambiguous absence (False)."""
    from app.source_ref_validation import _symbol_defined_in_repo_branch

    resp = _GhResp(200, _file_payload("def something_else():\n    pass\n"))
    mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=resp),
    )
    result = await _symbol_defined_in_repo_branch(
        "backend/app/x.py",
        "gone_func",
        token="t",
        owner="o",
        repo="r",
        branch="main",
    )
    assert result is False


@pytest.mark.asyncio
async def test_symbol_defined_in_repo_branch_404_indeterminate(mocker):
    """A 404 (file absent) is a file-level concern → indeterminate None, never
    flagged by the symbol path."""
    from app.source_ref_validation import _symbol_defined_in_repo_branch

    resp = _GhResp(404, None)
    mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=resp),
    )
    result = await _symbol_defined_in_repo_branch(
        "backend/app/x.py",
        "maybe_func",
        token="t",
        owner="o",
        repo="r",
        branch="main",
    )
    assert result is None


@pytest.mark.asyncio
async def test_symbol_defined_in_repo_branch_api_error_indeterminate(mocker):
    """A non-200/non-404 (e.g. 500) → indeterminate None, never flagged."""
    from app.source_ref_validation import _symbol_defined_in_repo_branch

    resp = _GhResp(500, None)
    mocker.patch(
        "app.github._github_request",
        new=AsyncMock(return_value=resp),
    )
    result = await _symbol_defined_in_repo_branch(
        "backend/app/x.py",
        "do_thing",
        token="t",
        owner="o",
        repo="r",
        branch="main",
    )
    assert result is None


# ── summarize_bug_report_citations (free-text bug-report grounding) ────────────

_CITE_CREDS = dict(token="t", owner="o", repo="r", branch="main", project_id="p")


@pytest.mark.asyncio
async def test_bug_report_citations_flags_absent_file(mocker):
    """A cited file absent on the branch yields citation_state == 'absent'."""
    from app.source_ref_validation import summarize_bug_report_citations

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch",
        new=AsyncMock(return_value=(False, None)),
    )
    signal = await summarize_bug_report_citations(
        "Crash originates in backend/app/gone.py during startup.", **_CITE_CREDS
    )
    assert signal["citation_state"] == "absent"
    assert "backend/app/gone.py" in signal["broken_refs"]
    assert signal["ungrounded_refs"] == []


@pytest.mark.asyncio
async def test_bug_report_citations_present_file_not_flagged(mocker):
    """A cited file present on the branch is not flagged (state == 'present')."""
    from app.source_ref_validation import summarize_bug_report_citations

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch",
        new=AsyncMock(return_value=(True, 500)),
    )
    signal = await summarize_bug_report_citations(
        "Issue reproduces in backend/app/real.py reliably.", **_CITE_CREDS
    )
    assert signal["citation_state"] == "present"
    assert signal["broken_refs"] == []


@pytest.mark.asyncio
async def test_bug_report_citations_no_refs_is_indeterminate(mocker):
    """Prose with no extractable references degrades to 'indeterminate'."""
    from app.source_ref_validation import summarize_bug_report_citations

    spy = mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch",
        new=AsyncMock(return_value=(False, None)),
    )
    signal = await summarize_bug_report_citations(
        "Something feels slow when many users hit the dashboard.", **_CITE_CREDS
    )
    assert signal["citation_state"] == "indeterminate"
    assert signal["broken_refs"] == []
    assert signal["ungrounded_refs"] == []
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_bug_report_citations_indeterminate_validation_not_flagged(mocker):
    """No creds and no checkout → indeterminate even with an extractable path."""
    from app.source_ref_validation import summarize_bug_report_citations

    mocker.patch(
        "app.source_ref_validation._file_exists_at_head",
        new=AsyncMock(return_value=None),
    )
    signal = await summarize_bug_report_citations(
        "Look at backend/app/maybe.py for the regression.",
        cwd="/nonexistent-checkout-xyz",
    )
    assert signal["citation_state"] == "indeterminate"
    assert signal["broken_refs"] == []


@pytest.mark.asyncio
async def test_bug_report_citations_flags_absent_symbol(mocker):
    """A present file whose cited symbol is gone is flagged as ungrounded/absent."""
    from app.source_ref_validation import summarize_bug_report_citations

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch",
        new=AsyncMock(return_value=(True, 500)),
    )
    mocker.patch(
        "app.source_ref_validation._symbol_defined_at_head",
        new=AsyncMock(return_value=False),
    )
    signal = await summarize_bug_report_citations(
        "The guard backend/app/real.py:deleted_fn no longer validates input.",
        **_CITE_CREDS,
    )
    assert signal["citation_state"] == "absent"
    assert "backend/app/real.py:deleted_fn" in signal["ungrounded_refs"]


@pytest.mark.asyncio
async def test_bug_report_citations_uses_structured_file_path(mocker):
    """A structured file_path (e.g. annotation) is grounded even if prose omits it."""
    from app.source_ref_validation import summarize_bug_report_citations

    mocker.patch(
        "app.source_ref_validation._file_content_in_repo_branch",
        new=AsyncMock(return_value=(False, None)),
    )
    signal = await summarize_bug_report_citations(
        "This whole flow is broken.",
        structured_context={"file_path": "backend/app/annotated.py"},
        **_CITE_CREDS,
    )
    assert signal["citation_state"] == "absent"
    assert "backend/app/annotated.py" in signal["broken_refs"]
