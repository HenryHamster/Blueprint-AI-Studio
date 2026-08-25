"""Rollup preflight: assembled-tree module size-cap guard.

Runs in the auto-merge unlocked window alongside the security scan and
Alembic chain guard.  For every changed file in the rollup PR that has a
cap entry in ``backend/testing/module_size_limits.json``, it fetches the
file content at the staging-branch tip, counts lines, and auto-bumps the
cap when the combined tree breaches it.  The bump is committed to the
staging branch so subsequent CI picks up the relaxed limit.

Verdicts (mirroring the Alembic chain guard vocabulary):
  "ok"     — no breach (or the repo carries no module_size_limits.json at all,
             so no size governance applies here); merge can proceed
  "bumped" — one or more caps auto-bumped and committed; caller defers one
             sweep so the updated JSON propagates to CI
  "hold"   — a chronic offender needs operator decomposition, the auto-bump
             budget is exhausted, or the auto-bump commit failed with a genuine
             (non-retryable) error; caller sets requires_approval / escalates
  "defer"  — a transient condition (fetch error, an unreadable-but-present caps
             file, or a retryable commit failure such as rate-limit 403/429, a
             non-fast-forward 422, or a network blip); caller retries next cycle
"""

import asyncio
import json
import logging
import re
from dataclasses import dataclass

import httpx

from app.github import (
    StaleBranchHeadError,
    commit_file_changes,
    get_branch_sha,
    get_file_content_at_ref,
    get_file_text_at_ref,
    get_pr_files,
)
from app.github_repo_snapshot import authoritative_texts_at_commit
from app.json_extract import async_json_loads
from app.size_limit_policy import (
    SECTION_PATH_PREFIXES,
    classify_breach,
    eligible_baseline_count,
    governance_caps_from_source_text,
    governance_source_relpath,
    is_breach,
    governing_cap_source_path,
    ledger_with_pending_bumps,
    prune_ledger_for_current_caps,
    resolve_effective_cap,
    rewrite_governing_cap_constant,
)

logger = logging.getLogger(__name__)

_CAPS_JSON_PATH = "backend/testing/module_size_limits.json"
_CAP_BUMP_HISTORY_LEDGER_RELPATH = "backend/testing/cap_bump_history.json"

# HTTP status codes surfaced by a failed auto-bump commit that are transient
# and clear on a later cycle, so they must DEFER (retry) rather than HOLD
# (escalate):
#   403 / 429 — primary / secondary rate limit (the token bucket refills)
#   422       — non-fast-forward ref update (the staging tip moved under us; a
#               fresh read next cycle rebases the bump)
#   408       — request timeout
#   5xx       — GitHub server-side errors
# ``commit_file_changes`` raises RuntimeError whose message embeds "HTTP <code>"
# for any unexpected GitHub status (see github.commit_file_changes._json).
_TRANSIENT_COMMIT_HTTP_CODES = frozenset({403, 408, 422, 429, 500, 502, 503, 504})
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")


def _is_transient_commit_error(exc: BaseException) -> bool:
    """Return True when *exc* from the auto-bump commit is transient/retryable.

    ``commit_file_changes`` raises ``RuntimeError`` whose message embeds
    ``HTTP <code>`` for any unexpected GitHub status, and lets
    ``_github_request`` re-raise ``httpx.HTTPError`` for transport-layer
    failures.  Rate-limit (403/429), non-fast-forward (422), 5xx and network
    conditions all clear on a later cycle, so they DEFER (retry) instead of
    HOLD (hard escalation).  Genuinely broken states (a persistent 4xx contract
    error, a code defect) are NOT transient and still HOLD so the operator is
    surfaced rather than the rollup looping forever.
    """
    if isinstance(exc, (httpx.HTTPError, StaleBranchHeadError)):
        return True
    msg = str(exc)
    m = _HTTP_STATUS_RE.search(msg)
    if m is not None and int(m.group(1)) in _TRANSIENT_COMMIT_HTTP_CODES:
        return True
    lowered = msg.lower()
    return any(
        kw in lowered
        for kw in (
            "rate limit",
            "connection",
            "timeout",
            "temporarily unavailable",
            "name resolution",
            "ssl",
        )
    )

# Maps each JSON section to the repo-relative prefix that anchors its source
# files. Single-sourced from ``size_limit_policy`` (mirrors the resolution in
# check_size_limits.resolve_section_root) so a ``scripts`` cap breach on a
# rollup is auto-bumped the same as any other section instead of being
# invisible to this preflight.
_SECTION_PREFIXES: dict[str, str] = SECTION_PATH_PREFIXES


@dataclass(frozen=True)
class SizeCapPreflightResult:
    """A size-cap verdict bound to the target and immutable head it examined.

    ``head_sha`` is present only when the branch was successfully pinned and
    remained unchanged through evaluation.  Merge callers must use that exact
    SHA as their expected head.  ``target_branch`` identifies the merge target
    whose assembled tree was checked; a status/reason pair by itself is never
    merge authority.
    """

    status: str
    reason: str | None
    head_sha: str | None = None
    target_branch: str | None = None
    base_sha: str | None = None

    def __iter__(self):
        """Retain the two-value read-only unpacking used by diagnostic callers."""
        yield self.status
        yield self.reason

    def is_bound_to(
        self, expected_head_sha: str, expected_base_sha: str | None = None
    ) -> bool:
        """Whether this verdict was evaluated against exact candidate/base SHAs."""
        return (
            bool(expected_head_sha)
            and self.head_sha == expected_head_sha
            and (expected_base_sha is None or self.base_sha == expected_base_sha)
        )


def _result(
    status: str,
    reason: str | None = None,
    *,
    head_sha: str | None = None,
    base_sha: str | None = None,
) -> SizeCapPreflightResult:
    return SizeCapPreflightResult(
        status=status, reason=reason, head_sha=head_sha, base_sha=base_sha
    )


async def preflight_rollup_size_caps(
    gh_token: str,
    owner: str,
    repo: str,
    rollup_pr_number: int,
    staging_branch: str,
    *,
    project_id: str | None = None,
    commit_bump: bool = True,
    expected_head_sha: str | None = None,
    target_branch: str | None = None,
    expected_base_sha: str | None = None,
) -> SizeCapPreflightResult:
    """Check assembled-tree module sizes against caps; auto-bump on breach.

    The returned result carries the merge target and exact immutable candidate
    and base SHAs that were examined.
    Callers may merge only an ``"ok"`` result whose ``head_sha`` equals both
    their already-checked PR head and the merge API's expected head, and whose
    ``target_branch`` equals the merge target.
    """
    def _result(
        status: str, reason: str | None = None, *, head_sha: str | None = None
    ) -> SizeCapPreflightResult:
        return SizeCapPreflightResult(
            status=status,
            reason=reason,
            head_sha=head_sha,
            target_branch=target_branch,
            base_sha=expected_base_sha,
        )

    # Baseline attribution is part of the authority contract, not optional
    # metadata. Without an immutable merge base, an over-cap file cannot be
    # distinguished from inherited debt, so absolute evaluation is unsafe.
    if not expected_base_sha:
        return _result(
            "defer",
            "size-cap preflight lacks immutable merge-base SHA",
        )
    if not target_branch:
        return _result(
            "defer",
            "size-cap preflight lacks merge target branch",
        )

    # Pin every read to one immutable branch tip. Branch-name content reads are
    # cached for ten minutes and were the direct cause of repeated stale bumps:
    # after the first commit, each poll kept re-reading the old caps document.
    try:
        observed_head_sha = await get_branch_sha(
            gh_token,
            owner,
            repo,
            staging_branch,
            project_id=project_id,
            live=True,
        )
    except Exception as exc:
        return _result("defer", f"could not resolve live staging head: {exc}")

    if expected_head_sha is not None and observed_head_sha != expected_head_sha:
        return _result(
            "defer",
            "staging branch head differs from the immutable merge candidate "
            f"({expected_head_sha[:12]} != {observed_head_sha[:12]})",
            head_sha=observed_head_sha,
        )

    # 1. Fetch caps JSON and changed source at the pinned immutable tip.
    try:
        caps_text = await get_file_text_at_ref(
            _CAPS_JSON_PATH,
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=observed_head_sha,
            project_id=project_id,
        )
    except Exception as exc:
        return _result("defer", f"could not fetch module_size_limits.json: {exc}")

    if caps_text is None:
        # ``get_file_text_at_ref`` collapses "file genuinely absent (404)" and
        # "transient read failure" both to None (its contract: None means
        # "could not read", never "empty").  Disambiguate with the tri-state
        # existence probe so a repo that simply doesn't use this governance file
        # is NEVER livelocked (proceed), while a transient read blip still
        # DEFERS — a real breach must not slip through on a GitHub hiccup.
        exists, _ = await get_file_content_at_ref(
            _CAPS_JSON_PATH,
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=observed_head_sha,
            project_id=project_id,
        )
        if exists is False:
            logger.info(
                "[ROLLUP-SIZE] PR #%s: no %s at %s — repo has no module size "
                "governance; proceeding",
                rollup_pr_number,
                _CAPS_JSON_PATH,
                staging_branch,
            )
            return _result("ok", head_sha=observed_head_sha)
        # exists is True (present but text unreadable) or None (indeterminate):
        # both are transient — retry next cycle rather than merge unchecked.
        return _result(
            "defer",
            "module_size_limits.json present but unreadable at staging branch tip",
        )

    try:
        caps_data: dict = await async_json_loads(caps_text)
    except Exception as exc:
        return _result("defer", f"could not parse module_size_limits.json: {exc}")

    # 2. List files changed by the rollup PR.
    try:
        pr_files = await get_pr_files(gh_token, owner, repo, rollup_pr_number, project_id)
    except Exception as exc:
        return _result("defer", f"could not fetch PR #{rollup_pr_number} files: {exc}")

    # Build status map so we can distinguish deleted files from fetch failures.
    pr_file_status: dict[str, str] = {
        f["filename"]: f.get("status", "")
        for f in pr_files
        if isinstance(f, dict) and "filename" in f
    }
    changed_paths: set[str] = set(pr_file_status)

    # A cap is only meaningful while its file exists.  Track removals across
    # every static-guarded section (including backend/testing) so this repair
    # is committed through the same CAS-protected preflight as cap bumps.
    removed_caps: list[tuple[str, str]] = []
    for section, prefix in _SECTION_PREFIXES.items():
        section_caps = caps_data.get(section)
        if not isinstance(section_caps, dict):
            continue
        removed_caps.extend(
            (section, key)
            for key in section_caps
            if pr_file_status.get(prefix + key) == "removed"
        )

    # 3. Fetch every capped source from the immutable candidate and base in two
    #    bounded archive reads.  The full cap set is intentional: inherited
    #    over-cap files outside the PR diff still need to be visible in the
    #    merge verdict, even though they do not block. A baseline is usable
    #    only if its own tree breached its own effective (JSON +
    #    source-governance) cap; this prevents a candidate cap edit from
    #    laundering new debt as old.
    capped_paths: list[tuple[str, str, int, str]] = []
    for section, prefix in _SECTION_PREFIXES.items():
        section_caps = caps_data.get(section)
        if not isinstance(section_caps, dict):
            continue
        for key, cap in section_caps.items():
            full_path = prefix + key
            if isinstance(cap, int) and pr_file_status.get(full_path) != "removed":
                capped_paths.append((section, key, cap, full_path))

    candidate_texts: dict[str, str] | None = None
    base_texts: dict[str, str] | None = None
    base_caps_data: dict | None = None
    if expected_base_sha and capped_paths:
        source_paths = {full_path for _section, _key, _cap, full_path in capped_paths}
        source_paths.update(
            source_path
            for section, key, _cap, _full_path in capped_paths
            if (source_path := governance_source_relpath(section, key)) is not None
        )
        try:
            candidate_texts, base_texts = await asyncio.gather(
                authoritative_texts_at_commit(
                    source_paths,
                    token=gh_token,
                    owner=owner,
                    repo=repo,
                    commit_sha=observed_head_sha,
                    project_id=project_id,
                    allow_missing_paths=True,
                ),
                authoritative_texts_at_commit(
                    source_paths | {_CAPS_JSON_PATH},
                    token=gh_token,
                    owner=owner,
                    repo=repo,
                    commit_sha=expected_base_sha,
                    project_id=project_id,
                    allow_missing_paths=True,
                ),
            )
        except Exception as exc:
            return _result(
                "defer",
                "could not fetch immutable size-cap baseline evidence for "
                f"{', '.join(sorted(source_paths))}: {exc}",
            )
        if candidate_texts is None or base_texts is None:
            return _result("defer", "could not fetch immutable size-cap baseline evidence")
        try:
            base_caps_data = await async_json_loads(base_texts[_CAPS_JSON_PATH])
        except Exception as exc:
            return _result("defer", f"could not parse baseline module_size_limits.json: {exc}")

    # 4. For each capped file present in the candidate, compare its count to an
    #    eligible baseline before considering repair. A stale cap whose file is
    #    absent from the candidate is harmless; a changed file missing from the
    #    exact candidate archive is unavailable evidence and must defer.
    breaches: list[tuple[str, str, int, int, int]] = []  # (section, key, count, cap, new_cap)
    inherited_breaches: list[tuple[str, int, int]] = []
    # Chronic offenders: (section, key, bump_count) — must hold without bumping.
    chronic_holders: list[tuple[str, str, int]] = []
    for section, key, cap, full_path in capped_paths:
            if candidate_texts is not None and full_path not in candidate_texts:
                if full_path in changed_paths and pr_file_status.get(full_path) != "removed":
                    return _result(
                        "defer",
                        f"could not fetch {full_path} at {staging_branch}",
                    )
                logger.info(
                    "[ROLLUP-SIZE] PR #%s: stale cap %s is absent from candidate; skipping",
                    rollup_pr_number,
                    full_path,
                )
                continue
            try:
                content = (
                    candidate_texts[full_path]
                    if candidate_texts is not None
                    else await get_file_text_at_ref(
                        full_path,
                        token=gh_token,
                        owner=owner,
                        repo=repo,
                        ref=observed_head_sha,
                        project_id=project_id,
                    )
                )
            except Exception as exc:
                logger.warning(
                    "[ROLLUP-SIZE] PR #%s: could not fetch %s at %s: %s",
                    rollup_pr_number,
                    full_path,
                    staging_branch,
                    exc,
                )
                return _result("defer", f"could not fetch {full_path} at {staging_branch}: {exc}")
            if content is None:
                # Fetch returned no content for a non-deleted file — treat as a
                # transient failure and defer rather than silently skipping.
                logger.warning(
                    "[ROLLUP-SIZE] PR #%s: %s returned no content at %s (non-deleted); deferring",
                    rollup_pr_number,
                    full_path,
                    staging_branch,
                )
                return _result("defer", f"no content returned for {full_path} at {staging_branch}")
            line_count = len(content.splitlines())
            governance_path = governance_source_relpath(section, key)
            candidate_governance_caps = (
                governance_caps_from_source_text(candidate_texts[governance_path])
                if candidate_texts is not None
                and governance_path is not None
                and governance_path in candidate_texts
                else None
            )
            effective_cap = resolve_effective_cap(
                key, section, cap, governance_caps=candidate_governance_caps
            )
            base_count: int | None = None
            if base_texts is not None and base_caps_data is not None:
                base_section_caps = base_caps_data.get(section)
                base_json_cap = (
                    base_section_caps.get(key)
                    if isinstance(base_section_caps, dict)
                    and isinstance(base_section_caps.get(key), int)
                    else None
                )
                base_governance_caps = (
                    governance_caps_from_source_text(
                        base_texts.get(governance_path, "")
                    )
                    if governance_path is not None
                    else []
                )
                base_effective_cap = resolve_effective_cap(
                    key,
                    section,
                    base_json_cap,
                    governance_caps=base_governance_caps,
                )
                baseline_source = base_texts.get(full_path)
                if baseline_source is not None:
                    candidate_base_count = len(baseline_source.splitlines())
                    base_count = eligible_baseline_count(
                        candidate_base_count, base_effective_cap
                    )
            if (
                base_count is not None
                and line_count > (effective_cap or line_count)
                and line_count <= base_count
            ):
                inherited_breaches.append((full_path, line_count, effective_cap or cap))
                logger.warning(
                    "[ROLLUP-SIZE] PR #%s: inherited breach %s has %d lines "
                    "(cap %d; base %d) — reporting without repair",
                    rollup_pr_number,
                    full_path,
                    line_count,
                    effective_cap or cap,
                    base_count,
                )
            elif is_breach(line_count, effective_cap, base_count=base_count):
                verdict = await asyncio.to_thread(
                    classify_breach,
                    module_key=key, current_size=line_count, section=section
                )
                if verdict["is_chronic"] and verdict["suggested_new_limit"] is None:
                    chronic_holders.append((section, key, verdict["bump_count"]))
                    logger.warning(
                        "[ROLLUP-SIZE] PR #%s: %s is a chronic offender (%d bumps)"
                        " — holding for operator decomposition",
                        rollup_pr_number,
                        full_path,
                        verdict["bump_count"],
                    )
                else:
                    new_cap = verdict["suggested_new_limit"]
                    breaches.append((section, key, line_count, cap, new_cap))
                    logger.info(
                        "[ROLLUP-SIZE] PR #%s: %s has %d lines (cap %d) — bumping to %d",
                        rollup_pr_number,
                        full_path,
                        line_count,
                        cap,
                        new_cap,
                    )

    # Verify the PR branch did not move while we assembled the verdict. This
    # protects every outcome (including "ok") from authorizing a merge based on
    # a stale tree; the write path below also enforces the same head as a CAS.
    try:
        current_head_sha = await get_branch_sha(
            gh_token,
            owner,
            repo,
            staging_branch,
            project_id=project_id,
            live=True,
        )
    except Exception as exc:
        return _result("defer", f"could not revalidate live staging head: {exc}")
    if current_head_sha != observed_head_sha:
        return _result(
            "defer",
            "staging branch advanced during size-cap preflight "
            f"({observed_head_sha[:12]} -> {current_head_sha[:12]})",
        )

    inherited_summary = "; ".join(
        f"inherited breach {path}: {count} lines (cap {cap})"
        for path, count, cap in inherited_breaches
    )

    def _with_inherited_summary(reason: str | None) -> str | None:
        """Keep inherited debt visible on every post-evaluation verdict."""
        if not inherited_summary:
            return reason
        if not reason:
            return inherited_summary
        return f"{reason}; {inherited_summary}"

    # Any chronic offender blocks ALL auto-bumps — never partially commit caps
    # while an operator decomposition decision is pending.
    if chronic_holders:
        names = ", ".join(
            f"{key} ({count} bumps)" for _, key, count in chronic_holders
        )
        logger.warning(
            "[ROLLUP-SIZE] PR #%s: holding for operator — chronic offender(s): %s",
            rollup_pr_number,
            names,
        )
        return _result(
            "hold",
            _with_inherited_summary(
                f"chronic offender(s) require decomposition: {names}"
            ),
            head_sha=observed_head_sha,
        )

    if not breaches and not removed_caps:
        return _result("ok", inherited_summary or None, head_sha=observed_head_sha)

    summary_parts = [
        f"{key}: {old_cap} -> {new_cap}"
        for _section, key, _count, old_cap, new_cap in breaches
    ]
    summary_parts.extend(f"remove {section}/{key}" for section, key in removed_caps)
    summary = "; ".join(summary_parts)

    if breaches and not commit_bump:
        # Caller has exhausted the auto-bump budget — report a hold so the
        # caller can require manual approval without performing another commit.
        logger.warning(
            "[ROLLUP-SIZE] PR #%s: size-cap breach detected but commit_bump=False "
            "(budget exhausted); returning hold: %s",
            rollup_pr_number,
            summary,
        )
        return _result(
            "hold",
            _with_inherited_summary(f"auto-bump budget exhausted; breach: {summary}"),
            head_sha=observed_head_sha,
        )

    # 4. Write repaired caps into a copy of the parsed JSON and commit.
    updated: dict = dict(caps_data)
    for section, key in removed_caps:
        updated[section] = dict(updated.get(section, {}))
        updated[section].pop(key, None)
    for section, key, _count, _old_cap, new_cap in breaches:
        updated[section] = dict(updated.get(section, {}))
        updated[section][key] = new_cap

    updated_json = json.dumps(updated, indent=2, ensure_ascii=False) + "\n"
    commit_msg = f"auto-repair module size caps for rollup PR #{rollup_pr_number}: {summary}"

    # Keep the committed cap-bump-history ledger in sync: the deployed backend
    # reads the ledger (not git), so an auto-bump must be recorded there too or
    # production would classify from a stale history. A repo whose staging
    # branch has no ledger yet (pre-feature baseline) simply skips the ledger
    # write — a partial ledger with only pending records would be worse. A
    # transient read failure defers the whole bump so the mirror never goes
    # stale silently.
    upserts: dict[str, str] = {_CAPS_JSON_PATH: updated_json}
    # A governed cap is declared in its source module too.  Fetch and rewrite
    # it before attempting the commit; an absent or malformed declaration
    # defers the entire repair instead of committing a JSON-only cap drift.
    for section, key, _count, _old_cap, new_cap in breaches:
        source_path = governing_cap_source_path(section, key)
        if source_path is None:
            continue
        try:
            source_text = await get_file_text_at_ref(
                source_path,
                token=gh_token,
                owner=owner,
                repo=repo,
                ref=observed_head_sha,
                project_id=project_id,
            )
        except Exception as exc:
            return _result(
                "defer",
                _with_inherited_summary(
                    f"could not fetch governing cap source {source_path}: {exc}"
                ),
            )
        if source_text is None:
            return _result(
                "defer",
                _with_inherited_summary(
                    f"governing cap source {source_path} was unreadable"
                ),
            )
        try:
            upserts[source_path] = rewrite_governing_cap_constant(
                section, key, source_text, new_cap
            )
        except ValueError as exc:
            return _result(
                "hold",
                _with_inherited_summary(
                    f"invalid governing cap declaration in {source_path}: {exc}"
                ),
                head_sha=observed_head_sha,
            )
    try:
        ledger_text = await get_file_text_at_ref(
            _CAP_BUMP_HISTORY_LEDGER_RELPATH,
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=observed_head_sha,
            project_id=project_id,
        )
    except Exception as exc:
        return _result(
            "defer",
            _with_inherited_summary(
                f"could not fetch cap-bump-history ledger before auto-bump commit: {exc}"
            ),
        )
    if ledger_text is None:
        # ``get_file_text_at_ref`` collapses "file genuinely absent (404)" and
        # "transient read failure" both to None (its contract: None means
        # "could not read", never "empty"). Disambiguate with the tri-state
        # existence probe, mirroring the caps-file handling above: a repo
        # whose staging branch has no ledger yet (pre-feature baseline) simply
        # skips the ledger write, while a transient read blip DEFERS the whole
        # bump so the ledger mirror never goes stale silently — a caps bump
        # committed without its ledger record would leave production
        # classifying from a stale history.
        ledger_exists, _ = await get_file_content_at_ref(
            _CAP_BUMP_HISTORY_LEDGER_RELPATH,
            token=gh_token,
            owner=owner,
            repo=repo,
            ref=observed_head_sha,
            project_id=project_id,
        )
        if ledger_exists is False:
            logger.info(
                "[ROLLUP-SIZE] PR #%s: no %s at %s — skipping ledger write "
                "(pre-feature baseline)",
                rollup_pr_number,
                _CAP_BUMP_HISTORY_LEDGER_RELPATH,
                staging_branch,
            )
        else:
            return _result(
                "defer",
                _with_inherited_summary(
                    "cap-bump-history ledger present but unreadable at staging branch tip"
                ),
            )
    else:
        try:
            ledger_data = json.loads(ledger_text)
        except Exception as exc:
            return _result(
                "defer",
                _with_inherited_summary(
                    f"could not parse cap-bump-history ledger: {exc}"
                ),
            )
        pending = [
            (section, key, cap, new_cap)
            for section, key, _count, cap, new_cap in breaches
        ]
        ledger_data = prune_ledger_for_current_caps(ledger_data, updated)
        ledger_data = ledger_with_pending_bumps(ledger_data, pending)
        upserts[_CAP_BUMP_HISTORY_LEDGER_RELPATH] = (
            json.dumps(ledger_data, indent=2, ensure_ascii=False) + "\n"
        )
        commit_msg += "; record bumps in cap-bump-history ledger"

    try:
        committed_sha = await commit_file_changes(
            gh_token,
            owner,
            repo,
            staging_branch,
            upserts=upserts,
            message=commit_msg,
            project_id=project_id,
            expected_head_sha=observed_head_sha,
        )
    except Exception as exc:
        # A transient/retryable commit failure (rate-limit 403/429, a
        # non-fast-forward 422 from a moved staging tip, a 5xx, or a network
        # blip) is NOT a reason to hard-escalate — the next cycle re-reads the
        # tip and re-commits.  Only a genuine, non-retryable error HOLDs.
        if _is_transient_commit_error(exc):
            logger.warning(
                "[ROLLUP-SIZE] PR #%s: auto-bump commit failed transiently "
                "(%s: %s); deferring to next cycle",
                rollup_pr_number,
                type(exc).__name__,
                exc,
            )
            return _result(
                "defer",
                _with_inherited_summary(
                    f"auto-bump commit failed (transient): {exc}"
                ),
            )
        logger.warning(
            "[ROLLUP-SIZE] PR #%s: auto-bump commit failed (%s: %s); holding for approval",
            rollup_pr_number,
            type(exc).__name__,
            exc,
        )
        return _result(
            "hold",
            _with_inherited_summary(f"auto-bump commit failed: {exc}"),
            head_sha=observed_head_sha,
        )

    if committed_sha == observed_head_sha:
        logger.info(
            "[ROLLUP-SIZE] PR #%s: size-cap update was already present on %s; "
            "no commit created",
            rollup_pr_number,
            staging_branch,
        )
        return _result("ok", inherited_summary or None, head_sha=observed_head_sha)

    logger.info(
        "[ROLLUP-SIZE] PR #%s: committed size-cap bumps on %s: %s",
        rollup_pr_number,
        staging_branch,
        summary,
    )
    return _result(
        "bumped",
        _with_inherited_summary(summary),
        head_sha=observed_head_sha,
    )
