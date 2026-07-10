"""Alembic migration-chain validation used by rollup auto-merge."""

import os

from fastapi import HTTPException

import app.github as github
from app.migration_chain import (
    _filename_revision_prefix,
    build_migration_chain,
    find_content_divergence_records,
    find_content_divergent_revisions,
    find_deployed_migration_content_edits,
    find_filename_revision_mismatches,
    find_heads,
    plan_integration_side_renumber,
    plan_migration_reconciliation,
    validate_added_migrations,
    validate_combined_migration_chain,
)
from app.migration_path import is_migration_file
from app.migration_reconciliation import (
    AUTHORIZED_DIVERGENCES,
    AuthorizedDivergence,
)

# Alembic versions directory, used to resolve a bare migration basename (as
# returned by ``github.list_versions_at_ref``) back to its full repo path for a
# per-file text fetch.
_ALEMBIC_VERSIONS_DIR = "backend/alembic/versions"


class PremergeMigrationGuardResult:
    """Two-value-unpackable guard result with the detailed verdict retained.

    Existing call sites do ``ok, reason = await enforce_premerge_migration_guard``.
    Keep that contract while exposing ``.verdict`` to callers that must
    distinguish deterministic ``block`` from transient ``defer``.
    """

    __slots__ = ("ok", "reason", "verdict")

    def __init__(self, verdict: str, reason: str | None):
        if verdict not in {"ok", "block", "defer"}:
            raise ValueError(f"unknown migration guard verdict: {verdict!r}")
        self.verdict = verdict
        self.ok = verdict == "ok"
        self.reason = None if self.ok else reason

    def __iter__(self):
        yield self.ok
        yield self.reason

    def __len__(self):
        return 2

    def __getitem__(self, index: int):
        if index == 0:
            return self.ok
        if index == 1:
            return self.reason
        raise IndexError(index)

    def __eq__(self, other):
        if isinstance(other, tuple) and len(other) == 2:
            return (self.ok, self.reason) == other
        return super().__eq__(other)

    def __repr__(self) -> str:
        return (
            "PremergeMigrationGuardResult("
            f"ok={self.ok!r}, reason={self.reason!r}, verdict={self.verdict!r})"
        )


def _exc_detail(e: BaseException) -> str:
    """Format an exception for use in hold-reason strings.

    HTTPException.__str__ omits the detail field, producing blank diagnostics
    like 'HTTPException: '. This helper includes the detail when present.
    """
    if isinstance(e, HTTPException):
        return f"HTTPException({e.status_code}): {e.detail}"
    return f"{type(e).__name__}: {e}"


def _extract_rollup_migration_sources(diff: str) -> dict[str, str]:
    """Reconstruct added/changed Alembic migration file contents from a PR diff.

    Returns a mapping of ``alembic/versions`` file path -> reconstructed source
    (the post-image, from the diff's ``+`` lines). Newly-added migration files
    appear in full; this is enough to read their in-file revision/down_revision.
    An empty result means the rollup diff does not touch the versions directory.
    """
    sources: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            current = None
        elif line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if is_migration_file(path) and path.endswith(".py"):
                current = sources.setdefault(path, [])
            else:
                current = None
        elif (
            current is not None and line.startswith("+") and not line.startswith("+++")
        ):
            current.append(line[1:])
    return {path: "\n".join(lines) for path, lines in sources.items()}


def _extract_source_from_patch(patch: str) -> str:
    """Reconstruct a file's post-image from a GitHub per-file unified diff patch.

    The GitHub per-file patch contains hunk headers and +/-/context lines but
    NOT the ``diff --git`` or ``+++`` header. Extracts only the added lines.
    """
    lines = [
        line[1:]
        for line in patch.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    return "\n".join(lines)


async def _collect_added_migration_sources(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr_files: list[dict],
) -> dict[str, str]:
    """Collect added Alembic migration sources from per-file patches.

    For each migration file in *pr_files* that has a ``'patch'`` key, the
    source is reconstructed via :func:`_extract_source_from_patch`. For any
    migration file that is listed but lacks a ``'patch'`` (e.g. binary,
    oversized, or absent from the 100-file window), the GitHub contents API is
    used as a fallback (fetching the file at the PR head SHA).

    Raises on any I/O failure so callers can return a ``'defer'`` verdict.
    A fetch error is never silently treated as an empty migration set.
    """
    migration_files = [
        f
        for f in pr_files
        if _touches_alembic_versions(f.get("filename", ""))
        and f.get("status") != "removed"
    ]
    if not migration_files:
        return {}

    sources: dict[str, str] = {}
    files_without_patch = [f for f in migration_files if "patch" not in f]

    for f in migration_files:
        if "patch" in f:
            sources[f["filename"]] = _extract_source_from_patch(f["patch"])

    if files_without_patch:
        pr = await github.get_pull_request(gh_token, owner, repo, pr_number)
        head_sha = (pr.get("head") or {}).get("sha") or ""
        if not head_sha:
            raise RuntimeError(
                f"PR #{pr_number} has no head SHA — cannot fetch migration "
                f"source(s) that are missing per-file patches"
            )
        for f in files_without_patch:
            path = f["filename"]
            text = await github.get_file_text_at_ref(
                path,
                token=gh_token,
                owner=owner,
                repo=repo,
                ref=head_sha,
            )
            if not text:
                raise RuntimeError(
                    f"could not fetch migration source '{path}' at PR head SHA "
                    f"'{head_sha}' (no patch in PR files, contents API returned empty)"
                )
            sources[path] = text

    return sources


def _touches_alembic_versions(filename: str) -> bool:
    return is_migration_file(filename) and filename.endswith(".py")


async def pr_touches_migrations(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
) -> bool:
    """Fail-safe check: does *pr_number* add/modify an Alembic migration file?

    Returns ``True`` when any file in the PR touches ``alembic/versions``. On an
    inability to list the PR's files, returns ``True`` (conservative: an unknown
    file set must NOT be treated as migration-free, since the caller uses a
    ``False`` result to take a GitHub-native-auto-merge fast path that bypasses
    Blueprint's re-entry guard). Only a positive "no migration files" answer
    returns ``False``.

    Used by the task-PR and rollup merge paths to decide whether the final
    landing may be delegated to GitHub native auto-merge (non-migration PRs) or
    must stay inside Blueprint's own retry/re-entry path so the combined-chain
    guard is re-evaluated against the live target branch before merge.
    """
    try:
        files = await github.get_pr_files(gh_token, owner, repo, pr_number)
    except Exception:
        return True
    return any(_touches_alembic_versions(f.get("filename", "")) for f in files)


async def _validate_rollup_migration_chain(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    target_branch: str | None = None,
    promotion_target: str | None = None,
) -> tuple[str, str | None]:
    """Fail-safe Alembic-chain guard for rollup auto-merge.

    Returns ``("ok", None)`` when the rollup does not touch ``alembic/versions``
    or the combined migration set is valid (single head, unique revisions).
    Returns ``("defer", reason)`` when GitHub metadata cannot be fetched (retry
    next sweep) and ``("block", reason)`` when the combined chain is invalid or
    a touched migration file is unparseable. Never waves a merge through on an
    inability to fetch or parse the migration files.

    When *target_branch* is given, the added migrations are validated against
    the target branch's *current* migration chain (head + existing revision
    ids), catching the parallel-development collision where two branches each
    add the same revision id or anchor to a revision that is no longer the
    head — a class the in-isolation check is blind to. A failure to list the
    target branch's migrations is fail-safe (``"defer"``), mirroring the
    diff-fetch handling. With no *target_branch* the guard degrades to the
    in-isolation check.

    When *promotion_target* is given (and distinct from *target_branch*), the
    added migrations are ALSO compared against the DEPLOYED promotion target's
    current chain — catching the cross-branch revision-identity collision that
    birthed the revision-0143 incident (prod 2026-06-04): a migration added on
    the integration branch reusing a revision id the deployed target already
    stamped to a DIFFERENT migration. That is a clean addition against the
    integration branch (so the target-branch checks above pass) yet a deploy-
    time collision that only surfaces at promotion — too late to renumber
    cheaply. Caught here it flows to the same ``block`` -> auto-reconcile
    renumber path. A failure to list the promotion target's migrations is
    fail-safe (``"defer"``), mirroring the target-branch handling.
    """
    try:
        files = await github.get_pr_files(
            gh_token, owner, repo, pr_number, include_patch=True
        )
    except Exception as e:
        return (
            "defer",
            f"could not list rollup files to check Alembic migrations: "
            f"{_exc_detail(e)}",
        )
    if not any(_touches_alembic_versions(f.get("filename", "")) for f in files):
        return ("ok", None)

    try:
        added_sources = await _collect_added_migration_sources(
            gh_token, owner, repo, pr_number, files
        )
    except Exception as e:
        return (
            "defer",
            f"could not fetch migration sources to validate Alembic migrations: "
            f"{_exc_detail(e)}",
        )

    if not target_branch:
        reason = validate_added_migrations(added_sources)
        return ("ok", None) if reason is None else ("block", reason)

    try:
        existing_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, target_branch
        )
    except Exception as e:
        return (
            "defer",
            f"could not list target branch '{target_branch}' migrations to "
            f"validate the combined Alembic chain: {_exc_detail(e)}",
        )
    reason = validate_combined_migration_chain(existing_filenames, added_sources)
    if reason is not None:
        return ("block", reason)

    # Cross-ref content-divergence guard. The combined-chain check proves the
    # added migrations don't collide with or dangle off the target's chain by
    # revision id, but it is blind to a RENUMBER that keeps both checkouts
    # internally valid while remapping a revision the live DB already applied —
    # the main<->automain back-merge class (prod incident 2026-06-04). Comparing
    # the target's existing migrations against the ones this PR adds catches the
    # tell: the same migration (by filename description) appearing under a
    # different revision id, or a revision id reused for a different migration.
    # Uses only data already fetched (no extra I/O), so it can't add a new
    # fail-safe surface to the shared guard.
    divergences = find_content_divergent_revisions(
        existing_filenames, list(added_sources.keys())
    )
    if divergences:
        return (
            "block",
            f"Alembic revision-identity divergence between the candidate and "
            f"target '{target_branch}' (a renumber/reuse that would skip or "
            f"mishandle a migration the live DB already applied): "
            + "; ".join(divergences),
        )

    # Cross-BRANCH revision-identity guard against the DEPLOYED promotion target.
    # The checks above validate the rollup only against the INTEGRATION branch it
    # merges into; they are blind to a migration added on integration that reuses
    # a revision id the DEPLOYED promotion target already stamped to a DIFFERENT
    # migration — the revision-0143 collision class (prod incident 2026-06-04).
    # Comparing the added migrations against the promotion target's CURRENT chain
    # catches it BEFORE it lands (the block flows to the auto-reconcile renumber
    # path) instead of discovering it at qualification/deploy. Normal integration
    # ahead-ness is NOT flagged: find_content_divergent_revisions only reports a
    # SHARED revision id whose migration identity differs (or a shared migration
    # under a different id), never a revision present on only one side. A failed
    # fetch is fail-safe (``"defer"``), mirroring the target-branch handling.
    if promotion_target and promotion_target != target_branch:
        try:
            promotion_filenames = await github.list_versions_at_ref(
                gh_token, owner, repo, promotion_target
            )
        except Exception as e:
            return (
                "defer",
                f"could not list promotion target '{promotion_target}' migrations "
                f"to validate cross-branch Alembic revision identity: "
                f"{_exc_detail(e)}",
            )

        # Defect class 2: in-place edit of a deployed migration.
        # Any PR file with status "modified" whose basename exists on the
        # promotion target is a deploy-fatal tamper: Alembic considers it
        # already-applied and skips the schema change silently. The
        # filename-identity detector is blind to this class because the file
        # keeps its name (and therefore its revision prefix + description),
        # so only the file-status check can catch it.
        promotion_basenames = set(promotion_filenames)
        modified_deployed = [
            f["filename"]
            for f in files
            if f.get("status") == "modified"
            and _touches_alembic_versions(f.get("filename", ""))
            and f["filename"].rsplit("/", 1)[-1] in promotion_basenames
        ]
        if modified_deployed:
            return (
                "block",
                f"PR modifies deployed migration(s) present on promotion target "
                f"'{promotion_target}' (deployed migrations are immutable — they "
                f"have already been applied to the production DB and cannot be "
                f"changed in-place): " + ", ".join(sorted(modified_deployed)),
            )

        # Defect class 1: pre-PR rename/renumber on the FULL integration branch.
        # The PR-diff checks above compare only the PR's added/modified files
        # against the promotion target; a rename committed BEFORE this PR
        # (e.g. a back-merge renumber that reused a revision id the deployed DB
        # already applied) leaves no trace in the PR diff and is invisible to
        # those checks. Comparing the FULL integration branch against the
        # promotion target catches it: a shared revision id mapping to a
        # different migration, or a migration that moved revision ids, is a
        # deploy-fatal divergence regardless of which PR introduced it.
        # Authorized divergences (recorded in AUTHORIZED_DIVERGENCES and handled
        # by the deploy-time reconciliation) are filtered out — they are already
        # safe and must not block new PRs on an integration branch that carries
        # the known renumber, mirroring the qual-time guard's behavior.
        full_branch_records = find_content_divergence_records(
            promotion_filenames, existing_filenames
        )
        unauthorized_full_branch = [
            r for r in full_branch_records if not _divergence_record_is_authorized(r)
        ]
        if unauthorized_full_branch:
            return (
                "block",
                f"Alembic revision-identity divergence between the full integration "
                f"branch '{target_branch}' and the deployed promotion target "
                f"'{promotion_target}' (a pre-PR renumber/reuse on the integration "
                f"branch would skip or mishandle a migration the live DB already "
                f"applied): "
                + "; ".join(r["message"] for r in unauthorized_full_branch),
            )

        promotion_divergences = find_content_divergent_revisions(
            promotion_filenames, list(added_sources.keys())
        )
        if promotion_divergences:
            return (
                "block",
                f"Alembic revision-identity collision between the candidate and "
                f"the deployed promotion target '{promotion_target}' (a rollup "
                f"migration reuses a revision id the deployed DB already stamped "
                f"to a different migration; it would be silently skipped at "
                f"deploy): " + "; ".join(promotion_divergences),
            )

        # Content-immutability guard against the deployed promotion target.
        # find_content_divergent_revisions above is filename-only (reuse/renumber
        # class). An in-place body edit — same filename on both refs but different
        # content — is invisible to that check. Here we fetch any migration whose
        # basename appears on the promotion target from that ref and compare bodies
        # directly; a diff means a deployed-immutable migration's body was changed,
        # which corrupts any live database that already applied the original.
        promotion_basenames = {os.path.basename(fn) for fn in promotion_filenames}
        shared_sources = {
            path: src
            for path, src in added_sources.items()
            if os.path.basename(path) in promotion_basenames
        }
        if shared_sources:
            deployed_sources: dict[str, str] = {}
            for path in shared_sources:
                basename = os.path.basename(path)
                full_path = f"{_ALEMBIC_VERSIONS_DIR}/{basename}"
                try:
                    text = await github.get_file_text_at_ref(
                        full_path,
                        token=gh_token,
                        owner=owner,
                        repo=repo,
                        ref=promotion_target,
                    )
                except Exception as e:
                    return (
                        "defer",
                        f"could not fetch migration '{basename}' from promotion "
                        f"target '{promotion_target}' for content-immutability "
                        f"check: {_exc_detail(e)}",
                    )
                if not text:
                    return (
                        "defer",
                        f"empty response fetching migration '{basename}' from "
                        f"promotion target '{promotion_target}' — deferring "
                        f"content-immutability check",
                    )
                deployed_sources[basename] = text

            content_edits = find_deployed_migration_content_edits(
                deployed_sources, shared_sources
            )
            if content_edits:
                return (
                    "block",
                    f"deployed migration body modified in-place on promotion target "
                    f"'{promotion_target}' — deployed migrations are immutable once "
                    f"applied: " + "; ".join(content_edits),
                )
    return ("ok", None)


async def enforce_premerge_migration_guard(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    target_branch: str | None,
    promotion_target: str | None = None,
) -> PremergeMigrationGuardResult:
    """Fail-safe pre-merge Alembic dual-head guard for non-autoreconcile paths.

    Wraps :func:`_validate_rollup_migration_chain` and collapses its
    ``("ok"|"block"|"defer", reason)`` verdict into a single "is it safe to
    merge?" decision. Both ``block`` (the combined chain is invalid — duplicate
    revision id / dual head) and ``defer`` (GitHub metadata could not be
    fetched, so migration safety is unknown) mean DO NOT MERGE and return
    ``(False, reason)``. Only an ``ok`` verdict returns ``(True, None)``.

    This is the shared enforcement point for the operator-approval and normal
    task-PR merge paths which — unlike the autonomous rollup path — do not run
    the mechanical auto-reconcile; for them, holding the merge and surfacing the
    dual-head reason is the correct conservative behavior. Reuses the same
    combined-chain validation policy as the rollup guard rather than inventing a
    second migration-safety rule set.
    """
    verdict, reason = await _validate_rollup_migration_chain(
        gh_token,
        owner,
        repo,
        pr_number,
        target_branch=target_branch,
        promotion_target=promotion_target,
    )
    return PremergeMigrationGuardResult(verdict, reason)


def _divergence_record_is_authorized(
    record: dict,
    authorized: tuple[AuthorizedDivergence, ...] = AUTHORIZED_DIVERGENCES,
) -> bool:
    """Is *record* an EXACT match for an authorized, already-handled divergence?

    The deploy-time reconciliation (:mod:`app.migration_reconciliation`) makes a
    SPECIFIC back-merge renumber safe; that registry is the single source of
    truth for which divergences are handled. A detected divergence matches an
    authorized entry only when BOTH the revision id AND the pair of migration
    identities match exactly — a different revision id, or a different identity
    pair, is NOT authorized and must still hard-block.

    The reused/renumbered classes are two complementary views of the same
    authorized renumber: ``reused`` matches the colliding revision + its identity
    pair; ``renumbered`` matches the deployed identity moving from the colliding
    revision to its renumbered slot.
    """
    for auth in authorized:
        if (
            record["kind"] == "reused"
            and record["revision"] == auth.revision
            and {
                record["base_identity"],
                record["head_identity"],
            }
            == {auth.deployed_identity, auth.integration_identity}
        ):
            return True
        if (
            record["kind"] == "renumbered"
            and record["identity"] == auth.deployed_identity
            and {
                record["base_revision"],
                record["head_revision"],
            }
            == {auth.revision, auth.integration_revision}
        ):
            return True
    return False


async def check_qualification_migration_identity(
    gh_token: str,
    owner: str,
    repo: str,
    candidate_ref: str,
    promotion_target: str,
    *,
    project_id: str | None = None,
) -> tuple[str, str | None]:
    """Deterministic Alembic revision-identity verdict for release qualification.

    The release qualifier scores an ``automain`` candidate against the deployed
    ``main`` promotion target, but its quality dimensions (audit / test-erosion /
    smoketest) have NO deterministic migration-safety check — whether a
    revision-identity collision blocks the release depends on the spawned audit
    agent noticing it by reasoning. That is exactly how the revision-0143
    collision (prod incident 2026-06-04) became a soft, score-eroding audit
    finding instead of a clear gate, and a future collision could slip through.

    This makes migration-identity safety DETERMINISTIC at qualification time by
    reusing the SAME detector + fetch helpers the merge-time guards use — it does
    NOT introduce a second migration-safety rule set. It lists the candidate's
    Alembic migration filenames (at *candidate_ref* — the candidate SHA the
    qualifier snapshotted) and the deployed promotion target's CURRENT migration
    filenames (at *promotion_target*), then runs
    :func:`find_content_divergent_revisions` over the two sets.

    Returns one of:
      ``("ok", None)``      — the two refs agree on every shared revision id and
                              migration identity (no collision).
      ``("block", reason)`` — a revision-identity collision was found; *reason*
                              names the specific diverging revision. A HARD
                              qualification blocker.
      ``("defer", reason)`` — either side's migration list could not be fetched.
                              Migration safety is UNKNOWN, so the release must be
                              HELD (never waved through); the next qualification
                              cycle retries.

    Fail-safe by construction: an inability to fetch either side's list is
    ``"defer"``, never ``"ok"`` — a fetch error is not "no collision".
    """
    try:
        candidate_shas = await github.list_version_shas_at_ref(
            gh_token, owner, repo, candidate_ref, project_id=project_id
        )
    except Exception as e:
        return (
            "defer",
            f"could not list candidate '{candidate_ref}' Alembic migrations to "
            f"check revision-identity safety: {_exc_detail(e)}",
        )
    try:
        target_shas = await github.list_version_shas_at_ref(
            gh_token, owner, repo, promotion_target, project_id=project_id
        )
    except Exception as e:
        return (
            "defer",
            f"could not list promotion target '{promotion_target}' Alembic "
            f"migrations to check revision-identity safety: "
            f"{_exc_detail(e)}",
        )
    candidate_filenames = list(candidate_shas.keys())
    target_filenames = list(target_shas.keys())

    # Compare the deployed promotion target against the candidate. A divergence
    # means a revision id the deployed DB already applied maps to a DIFFERENT
    # migration on the candidate (so it would be silently skipped), or a
    # migration moved revision ids (so it would re-run / mishandle).
    records = find_content_divergence_records(target_filenames, candidate_filenames)
    unauthorized = [r for r in records if not _divergence_record_is_authorized(r)]
    authorized = [r for r in records if _divergence_record_is_authorized(r)]

    # Any divergence NOT on the authorized-reconciliation registry still hard-
    # blocks — the registry is purely additive and never weakens an unknown
    # collision.
    if unauthorized:
        return (
            "block",
            f"Alembic revision-identity collision between the candidate and the "
            f"deployed promotion target '{promotion_target}' (a renumber/reuse "
            f"that would skip or mishandle a migration the deployed DB already "
            f"applied): " + "; ".join(r["message"] for r in unauthorized),
        )

    # Only authorized, already-reconciled divergences remain. The deploy-time
    # reconciliation makes this exact renumber safe, so it is non-blocking — but
    # record an informational note so the qualification record shows WHY this
    # known divergence was waved through (it self-clears once the version
    # promotes and the branches agree).
    if authorized:
        auth_note = (
            f"authorized reconciled Alembic revision-identity divergence(s) vs "
            f"deployed promotion target '{promotion_target}' (handled by deploy-"
            f"time reconciliation, non-blocking): "
            + "; ".join(r["message"] for r in authorized)
        )
    else:
        auth_note = None

    # Body-immutability guard: the filename-identity checks above are blind to an
    # IN-PLACE edit where the same migration file exists on both refs under the
    # same name but with a different body. Alembic considers such a migration
    # already-applied and silently skips it, so the schema change is never
    # applied — deploy-fatal corruption. Fetch each migration shared by basename
    # between the candidate and the promotion target; if any body differs, block.
    # A failed or empty fetch returns 'defer' (migration safety unknown) — never
    # 'ok'. Mirrors the identical block in _validate_rollup_migration_chain.
    # Only a migration whose git blob sha DIFFERS between the two refs can be an
    # in-place body edit; an identical blob sha is byte-identical content (git
    # content addressing), so the body is provably unchanged and needs no fetch.
    # Comparing shas — already returned by the two directory listings above at no
    # extra request cost — collapses this check from ~2 fetches per shared
    # migration (hundreds of GitHub contents calls per attempt, enough to drain
    # the token's rate-limit budget and wedge the release into a defer loop) down
    # to fetching only the migrations that actually changed. A missing sha on
    # either side is treated as "unknown" and still fetched, preserving the
    # fail-safe (never skip a body we could not content-address).
    target_basenames = {os.path.basename(fn) for fn in target_filenames}
    shared_basenames = []
    for fn in candidate_filenames:
        basename = os.path.basename(fn)
        if basename not in target_basenames:
            continue
        cand_sha = candidate_shas.get(basename)
        tgt_sha = target_shas.get(basename)
        if cand_sha is None or tgt_sha is None or cand_sha != tgt_sha:
            shared_basenames.append(basename)
    if shared_basenames:
        deployed_sources: dict[str, str] = {}
        candidate_sources: dict[str, str] = {}
        for basename in shared_basenames:
            full_path = f"{_ALEMBIC_VERSIONS_DIR}/{basename}"
            try:
                deployed_text = await github.get_file_text_at_ref(
                    full_path,
                    token=gh_token,
                    owner=owner,
                    repo=repo,
                    ref=promotion_target,
                )
            except Exception as e:
                return (
                    "defer",
                    f"could not fetch migration '{basename}' from promotion "
                    f"target '{promotion_target}' for body-immutability check "
                    f"at qualification: {_exc_detail(e)}",
                )
            if not deployed_text:
                return (
                    "defer",
                    f"empty response fetching migration '{basename}' from "
                    f"promotion target '{promotion_target}' — deferring "
                    f"body-immutability check at qualification",
                )
            deployed_sources[basename] = deployed_text

            try:
                candidate_text = await github.get_file_text_at_ref(
                    full_path,
                    token=gh_token,
                    owner=owner,
                    repo=repo,
                    ref=candidate_ref,
                )
            except Exception as e:
                return (
                    "defer",
                    f"could not fetch migration '{basename}' from candidate "
                    f"'{candidate_ref}' for body-immutability check at "
                    f"qualification: {_exc_detail(e)}",
                )
            if not candidate_text:
                return (
                    "defer",
                    f"empty response fetching migration '{basename}' from "
                    f"candidate '{candidate_ref}' — deferring body-immutability "
                    f"check at qualification",
                )
            candidate_sources[basename] = candidate_text

        content_edits = find_deployed_migration_content_edits(
            deployed_sources, candidate_sources
        )
        if content_edits:
            return (
                "block",
                f"deployed migration body modified in-place on candidate "
                f"'{candidate_ref}' vs promotion target '{promotion_target}' "
                f"— deployed migrations are immutable once applied: "
                + "; ".join(content_edits),
            )

    return ("ok", auth_note)


async def _validate_migration_chain_post_merge(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    target_branch: str,
) -> tuple[str, str | None]:
    """Post-merge Alembic chain health check for the release-reconcile paths.

    Unlike the pre-merge guard (:func:`_validate_rollup_migration_chain`), this
    is safe to call AFTER the candidate has already merged onto *target_branch*.
    The pre-merge guard uses an added-vs-existing collision set that always
    reports a false self-collision at this point (the candidate's own revisions
    are now in both the "added" set and the "existing" set).

    Instead, this check lists the current migration filenames on *target_branch*
    and looks for any revision id (NNNN filename prefix) that appears in more
    than one file — a genuine third-party duplicate that the pre-merge guard
    should have prevented but may have slipped through.  It then reads every
    target-branch migration's source, builds the in-file revision/down_revision
    chain over the WHOLE set as one (NOT an added-vs-existing comparison, which
    would falsely flag the candidate's own just-merged revisions — see #7731),
    and blocks on the SAME migration-corruption taxonomy the pre-merge guard
    enforces (reusing the shared chain helpers rather than a second rule set):

      * a duplicate IN-FILE revision id — two distinct migration files declaring
        the same ``revision = '...'`` (a copy-paste where the filename ``NNNN``
        was bumped but the in-file id was not).  Chain-building collapses the two
        into one node, so the filename-prefix pass cannot see it;
      * an UNPARSEABLE migration source — a numeric-prefixed file with no
        parseable in-file ``revision``, silently dropped from the analyzed chain;
      * a filename/revision DESYNC — the ``NNNN`` filename prefix disagrees with
        the in-file ``revision`` id;
      * a genuine third-party dual head (more than one Alembic head built from
        distinct revision ids), or a broken chain (a ``down_revision`` pointing at
        a revision absent from the branch).

    The first two classes are weighted ahead of the dual-head / broken-chain
    checks: a collapsed (duplicate-id) or dropped (unparseable) node can MASK a
    genuine dual head or broken chain that would otherwise be detected, so the
    reconciler would record a spurious promotion of a target that actually fails
    the schema-revision startup gate — the exact failure the guard prevents.  A
    clean target (every revision id unique and parseable, filenames matching, the
    chain a single head with no dangling anchor) returns ``("ok", None)``.

    Returns ``("defer", reason)`` on GitHub I/O failure — including any per-file
    source fetch failure or empty body — consistent with the pre-merge guard's
    fail-safe semantics, and ``("block", reason)`` when any corruption class above
    is found.  When the PR does not touch Alembic migrations, returns
    ``("ok", None)`` immediately.
    """
    try:
        files = await github.get_pr_files(gh_token, owner, repo, pr_number)
    except Exception as e:
        return (
            "defer",
            f"could not list PR #{pr_number} files to check Alembic migrations: "
            f"{_exc_detail(e)}",
        )
    if not any(_touches_alembic_versions(f.get("filename", "")) for f in files):
        return ("ok", None)

    try:
        existing_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, target_branch
        )
    except Exception as e:
        return (
            "defer",
            f"could not list target branch '{target_branch}' migrations for "
            f"post-merge chain health check: {_exc_detail(e)}",
        )

    # Detect genuine third-party duplicates: any NNNN revision-id prefix that
    # appears in more than one migration file on the target branch.  The
    # candidate's own just-merged revisions are expected to appear exactly once;
    # a second occurrence of the same id means a different PR independently used
    # the same slot — a genuine collision the pre-merge guard should have caught.
    seen: dict[str, str] = {}
    for fn in existing_filenames:
        rev = _filename_revision_prefix(fn)
        if rev is None:
            continue
        if rev in seen:
            return (
                "block",
                f"Alembic revision id '{rev}' appears in multiple migration files "
                f"on '{target_branch}': {seen[rev]!r} and {fn!r}; genuine "
                f"third-party revision collision — chain needs manual resolution",
            )
        seen[rev] = fn

    # Full-chain health check: the filename-prefix pass above only catches a
    # duplicate revision id.  A genuine third-party collision can also surface as
    # a dual head built from DISTINCT revision ids (two leaves), or a broken
    # chain (a down_revision pointing at a revision no longer on the branch) —
    # both make the target unbootable (the schema-revision startup gate fails)
    # yet leave every filename prefix unique.  To detect those we must read each
    # migration's in-file revision/down_revision (filenames alone are not enough)
    # and reason over the WHOLE target-branch set as a single chain.  Validating
    # added-vs-existing here would falsely flag the candidate's own just-merged
    # revisions (#7731), so we deliberately build one combined chain instead.
    sources: dict[str, str] = {}
    for fn in existing_filenames:
        if _filename_revision_prefix(fn) is None:
            # Not a migration-style NNNN file — skip rather than fetch/parse it.
            continue
        path = f"{_ALEMBIC_VERSIONS_DIR}/{fn}"
        try:
            text = await github.get_file_text_at_ref(
                path,
                token=gh_token,
                owner=owner,
                repo=repo,
                ref=target_branch,
            )
        except Exception as e:
            return (
                "defer",
                f"could not fetch migration source '{path}' at '{target_branch}' "
                f"for post-merge chain health check: {_exc_detail(e)}",
            )
        if not text:
            return (
                "defer",
                f"empty migration source for '{path}' at '{target_branch}' — "
                f"deferring post-merge chain health check",
            )
        sources[fn] = text

    if sources:
        chain, duplicates, unparseable = build_migration_chain(sources)

        # Weight the unambiguously chain-breaking classes first: a collapsed
        # (duplicate in-file id) or dropped (unparseable) node can MASK a genuine
        # dual head / broken chain below, so surface them before the head/dangling
        # checks. This mirrors the pre-merge guard's corruption taxonomy
        # (validate_added_migrations) exactly, reusing the same chain helpers.
        if unparseable:
            return (
                "block",
                f"migration file(s) on '{target_branch}' have no parseable in-file "
                f"revision and were dropped from the chain: "
                f"{', '.join(sorted(unparseable))} — an unparseable migration makes "
                f"the target unbootable and can mask a dual head; chain needs "
                f"manual resolution.",
            )
        if duplicates:
            dup = sorted(set(duplicates))[0]
            return (
                "block",
                f"in-file Alembic revision id '{dup}' is declared by more than one "
                f"migration file on '{target_branch}' — distinct files collapsing to "
                f"one chain node can mask a dual head and the schema-revision startup "
                f"gate would fail; chain needs manual resolution.",
            )
        mismatches = find_filename_revision_mismatches(sources)
        if mismatches:
            return (
                "block",
                f"migration filename prefix disagrees with its in-file revision on "
                f"'{target_branch}': {'; '.join(sorted(mismatches))} — chain needs "
                f"manual resolution.",
            )

        heads = find_heads(chain)
        if len(heads) > 1:
            return (
                "block",
                f"target branch '{target_branch}' has {len(heads)} Alembic heads "
                f"({', '.join(sorted(heads))}) after the candidate landed — a "
                f"genuine third-party dual head; the schema-revision startup gate "
                f"would fail. Chain needs manual resolution.",
            )
        dangling = sorted(
            rev for rev, down in chain.items() if down is not None and down not in chain
        )
        if dangling:
            rev = dangling[0]
            return (
                "block",
                f"Alembic migration '{rev}' on '{target_branch}' has "
                f"down_revision '{chain[rev]}', which is not present on the "
                f"branch — broken migration chain; the schema-revision startup "
                f"gate would fail. Chain needs manual resolution.",
            )

    return ("ok", None)


async def _plan_rollup_migration_reconciliation(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    target_branch: str | None,
    *,
    promotion_target: str | None = None,
    project_id: str | None = None,
) -> dict | None:
    """Plan an automatic renumber for a rollup blocked by a head collision.

    Re-fetches the rollup's added migration sources (from the PR diff) and the
    target branch's current migration filenames, then delegates to the pure
    :func:`app.migration_chain.plan_migration_reconciliation`. Returns the plan
    dict only for the unambiguous single-added-migration collision / stale-anchor
    case; returns ``None`` for everything else AND fail-safe on any fetch error
    (a failed fetch must never produce an unsafe auto-rewrite — the caller falls
    back to the operator-alert / agent-fix path).
    """
    if not target_branch:
        return None
    try:
        pr_files = await github.get_pr_files(
            gh_token, owner, repo, pr_number, include_patch=True
        )
        added_sources = await _collect_added_migration_sources(
            gh_token, owner, repo, pr_number, pr_files
        )
    except Exception:
        return None
    if not added_sources:
        return None
    try:
        existing_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, target_branch, project_id=project_id
        )
    except Exception:
        return None
    protected_revisions: set[str] = set()
    if promotion_target and promotion_target != target_branch:
        try:
            promotion_filenames = await github.list_versions_at_ref(
                gh_token, owner, repo, promotion_target, project_id=project_id
            )
        except Exception:
            return None
        protected_revisions = {
            rev
            for fn in promotion_filenames
            if (rev := _filename_revision_prefix(fn)) is not None
        }
    return plan_migration_reconciliation(
        existing_filenames,
        added_sources,
        protected_revisions=protected_revisions,
    )


async def _plan_backmerge_migration_reconciliation(
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    integration_branch: str,
    promotion_target: str,
) -> dict | None:
    """Plan an integration-side renumber for a back-merge blocked by a dual head.

    The BACK-MERGE mirror of :func:`_plan_rollup_migration_reconciliation`, with
    the asymmetry the back-merge case demands: when ``promotion_target`` (e.g.
    ``main``) is back-merged into ``integration_branch`` (e.g. ``automain``), the
    promotion-target hotfix migration is ALREADY APPLIED IN PRODUCTION and is
    therefore immutable — it must never be renumbered. The migration that must
    move is the one on the INTEGRATION branch whose revision id independently
    reused the same ``NNNN`` slot.

    Steps (every one fail-safe to ``None`` — a failed fetch / empty result must
    never produce an unsafe auto-rewrite; the caller falls back to the guided
    resolver):

      1. Fetch the back-merge PR diff and reconstruct its added (promotion-target)
         migration sources. An empty result means the back-merge adds no migration
         — nothing to reconcile — so return ``None``.
      2. List the promotion target's current migration filenames; they supply the
         immutable colliding revision id and the unified head.
      3. List the integration branch's migration filenames, keep only the
         POST-DIVERGENCE ones (basename not shared with the promotion target — so
         the pre-divergence migrations common to both branches are dropped and
         only the colliding integration migration + its descendant tail remain),
         and read each one's full source at the integration ref.
      4. Delegate to the pure :func:`plan_integration_side_renumber` planner,
         which returns a plan only for the unambiguous single-collision case and
         ``None`` for everything else.
    """
    try:
        diff = await github.get_pull_request_diff(gh_token, owner, repo, pr_number)
    except Exception:
        return None
    if not diff:
        return None
    added_sources = _extract_rollup_migration_sources(diff)
    if not added_sources:
        return None
    try:
        promotion_target_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, promotion_target
        )
    except Exception:
        return None
    if not promotion_target_filenames:
        return None
    try:
        integration_filenames = await github.list_versions_at_ref(
            gh_token, owner, repo, integration_branch
        )
    except Exception:
        return None

    # Drop the migrations shared with the promotion target (pre-divergence
    # history) so only the colliding integration migration and its tail remain.
    promotion_target_basenames = set(promotion_target_filenames)
    post_divergence = [
        fn for fn in integration_filenames if fn not in promotion_target_basenames
    ]
    if not post_divergence:
        return None

    integration_sources: dict[str, str] = {}
    for basename in post_divergence:
        path = f"{_ALEMBIC_VERSIONS_DIR}/{basename}"
        try:
            text = await github.get_file_text_at_ref(
                path,
                token=gh_token,
                owner=owner,
                repo=repo,
                ref=integration_branch,
            )
        except Exception:
            return None
        if not text:
            return None
        integration_sources[path] = text

    return plan_integration_side_renumber(
        promotion_target_filenames, integration_sources
    )
