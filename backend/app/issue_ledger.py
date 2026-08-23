"""Authoritative blocker ledger for the review -> fix loop.

The ledger is intentionally small in concept: every CRITICAL/MAJOR finding is
open until a later exact-head review proves a fix, or an explicit reviewer or
operator disposition refutes/adjudicates it. Omission on an unchanged or
unproven head is never closure.

This module replaces the older contested-region/Jaccard state machine. That
second inference layer guessed that omission meant a fix and could therefore
contradict the binding open set. Region similarity remains only as a
conservative way to carry one blocker identity across reviewer paraphrases.
"""

import hashlib
import re
from typing import Any

from app.issue_ledger_state import (
    ISSUE_LEDGER_MAX_RESOLVED,
    ISSUE_LEDGER_SCHEMA_VERSION,
    _history_entry_key,
    _history_entry_task_id,
    _seed_open_record,
    issue_ledger_complete_for_history,  # noqa: F401 - compatibility re-export
    normalize_issue_ledger,
)


_FEEDBACK_TOKEN_RE = re.compile(r"[a-z0-9_]+")
ISSUE_MATCH_JACCARD_THRESHOLD = 0.5
LINE_WINDOW_BLOCK = 40
ISSUE_RESOLUTION_DISPOSITIONS = frozenset({"fixed", "refuted", "adjudicated"})

def _token_set(text: str) -> set[str]:
    return {t for t in _FEEDBACK_TOKEN_RE.findall(text.lower()) if len(t) >= 3}


def _jaccard(a: str, b: str) -> float:
    ta = _token_set(a)
    tb = _token_set(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _region_anchor(issue: dict) -> str:
    """Return a stable symbol anchor, then a coarse line-window fallback."""
    symbol = (issue.get("symbol") or "").strip()
    if symbol:
        return f"sym:{symbol}"
    line = issue.get("line")
    if line is not None:
        try:
            return f"win:{int(line) // LINE_WINDOW_BLOCK}"
        except (TypeError, ValueError):
            pass
    return ""


def _region_key(issue: dict) -> tuple[str, str] | None:
    file_path = (issue.get("file") or "").strip()
    if not file_path:
        return None
    return (file_path, _region_anchor(issue))


def _entry_head_sha(entry: dict) -> str:
    """Return the immutable PR head a review entry actually examined."""
    return str(entry.get("reviewed_head_sha") or entry.get("head_sha") or "").strip()


def _entry_base_sha(entry: dict) -> str:
    return str(entry.get("reviewed_base_sha") or entry.get("base_sha") or "").strip()


def _issue_fingerprint(
    issue: dict,
    *,
    reviewed_head_sha: str = "",
    iteration: int = 0,
    ordinal: int = 0,
) -> str:
    """Return a deterministic ``BP-<sha256 prefix>`` identity for a finding."""
    region = _region_key(issue)
    region_text = "|".join(region) if region is not None else "global"
    description = " ".join(sorted(_token_set(str(issue.get("description") or ""))))
    seed = (
        f"{reviewed_head_sha}|{iteration}|{ordinal}|{region_text}|"
        f"{str(issue.get('severity') or '').upper()}|{description}"
    )
    return f"BP-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:12]}"


def ensure_issue_ids(
    issues: list | None,
    *,
    reviewed_head_sha: str = "",
    iteration: int = 0,
) -> list[dict]:
    """Copy structured issues and fill a deterministic ID when absent.

    Existing IDs are preserved for lossless propagation between lifecycle
    seams.  Only :func:`build_issue_ledger` decides whether an incoming ID is a
    compatible reference to an open blocker; it assigns backend-owned IDs to
    every genuinely new finding.
    """
    normalized: list[dict] = []
    for ordinal, raw in enumerate(issues or []):
        if not isinstance(raw, dict):
            continue
        issue = dict(raw)
        issue_id = str(issue.get("issue_id") or "").strip()
        issue["issue_id"] = issue_id or _issue_fingerprint(
            issue,
            reviewed_head_sha=reviewed_head_sha,
            iteration=iteration,
            ordinal=ordinal,
        )
        normalized.append(issue)
    return normalized


def _is_blocking(issue: dict) -> bool:
    return (
        str(issue.get("severity") or "").upper() in ("CRITICAL", "MAJOR")
        and bool(str(issue.get("description") or "").strip())
    )


def _same_finding(a: dict, b: dict) -> bool:
    """Conservatively carry one blocker identity across reviewer paraphrases."""
    a_region = _region_key(a)
    b_region = _region_key(b)
    if a_region is not None and a_region == b_region:
        return _jaccard(
            str(a.get("description") or ""), str(b.get("description") or "")
        ) >= ISSUE_MATCH_JACCARD_THRESHOLD
    if a_region is None and b_region is None:
        return _jaccard(
            str(a.get("description") or ""), str(b.get("description") or "")
        ) >= 0.75
    return False


def _fresh_collision_id(
    issue: dict,
    *,
    reviewed_head_sha: str,
    iteration: int,
    ordinal: int,
    unavailable_ids: set[str],
) -> str:
    """Generate a deterministic backend ID that does not alias another finding."""
    attempt = 0
    while True:
        candidate = _issue_fingerprint(
            issue,
            reviewed_head_sha=reviewed_head_sha,
            iteration=iteration,
            # ``ensure_issue_ids`` already used ``ordinal``.  A separate stable
            # namespace keeps collision repair deterministic without trusting
            # the reviewer-supplied ID that collided.
            ordinal=ordinal + ((attempt + 1) * 1_000_000),
        )
        if candidate not in unavailable_ids:
            return candidate
        attempt += 1


def _public_open_record(record: dict) -> dict:
    issue = record["issue"]
    return {
        "issue_id": record["issue_id"],
        "severity": str(issue.get("severity") or "").lower(),
        "description": str(issue.get("description") or ""),
        **({"file": issue.get("file")} if issue.get("file") else {}),
        **({"line": issue.get("line")} if issue.get("line") is not None else {}),
        **({"symbol": issue.get("symbol")} if issue.get("symbol") else {}),
        "demanded_iteration": record["demanded_iteration"],
        "demanded_head_sha": record["demanded_head_sha"],
        "demanded_base_sha": record["demanded_base_sha"],
        "demanded_target_verified": record["demanded_target_verified"],
        "last_seen_iteration": record["last_seen_iteration"],
        "last_seen_head_sha": record["last_seen_head_sha"],
        "last_seen_base_sha": record["last_seen_base_sha"],
        "last_seen_target_verified": record["last_seen_target_verified"],
        **(
            {"pending_disposition": record["pending_disposition"]}
            if record.get("pending_disposition")
            else {}
        ),
    }


def build_issue_ledger(
    review_history: list | None,
    *,
    seed_ledger: dict | None = None,
    stamp_complete: bool = False,
) -> dict[str, Any]:
    """Derive the complete binding open set and bounded resolution history.

    Fail-closed rules:

    * APPROVE on the same head does not close an earlier blocker.
    * Omission closes a blocker as ``fixed`` only after a different exact head.
    * Every explicit closure requires a concrete reason from a backend-verified
      exact-target review.  A verified review may explicitly reconcile a
      legacy blocker whose original target was not stamped; implicit omission
      never may.
    * A resolution from an entry whose own review target could not be
      backend-verified (``target_verified_by_backend`` false, e.g. the live
      PR head raced ahead between that review starting and completing) can
      never itself close a demand, but it is recorded as a non-authoritative
      ``pending_disposition`` on the still-open record. This is bookkeeping
      only -- it does not move the record out of ``open`` -- but it lets a
      downstream consumer (the stuck-cycle detector) distinguish "reviewer
      never addressed this" from "reviewer disposed this in good faith and
      the backend simply couldn't stamp exact-target proof on this pass".
      Re-raising the same finding in a later entry's ``issues`` clears any
      stale pending claim.
    * ``fixed`` requires a changed exact head when the prior demand also has
      backend-verified SHA provenance.
    * Structured CRITICAL/MAJOR findings remain binding even when the verdict
      token is missing or malformed.  An invalid token cannot resolve or omit
      an existing demand, but it also cannot make an explicitly listed blocker
      disappear.
    * The returned ``open`` list is never capped or sliced.
    * A seed whose lineage marker is absent from the retained window is
      RECOVERED, not frozen: the whole retained window is replayed on top of the
      seed's open records and the marker advances to the new tail, at ANY window
      size.  The build reports that it did so via ``lineage_recovered`` on the
      returned ledger.  Freezing was a one-way door -- no later review could
      close anything, and no commit could clear it.
    """
    normalized_seed = (
        normalize_issue_ledger(seed_ledger) if seed_ledger is not None else None
    )
    if seed_ledger is not None and normalized_seed is None:
        raise ValueError("malformed durable issue ledger")
    open_records: dict[str, dict] = {
        row["issue_id"]: _seed_open_record(row)
        for row in (normalized_seed or {}).get("open", [])
    }
    resolved: list[dict] = list((normalized_seed or {}).get("resolved", []))
    history = list(review_history or [])
    entries_to_apply = history
    matching_index: int | None = None
    seed_through_key = str(
        (normalized_seed or {}).get("through_entry_key") or ""
    )
    seed_through_task_id = str(
        (normalized_seed or {}).get("through_entry_task_id") or ""
    )
    seed_has_lineage_marker = bool(
        normalized_seed is not None and (seed_through_key or seed_through_task_id)
    )
    # True when the seed claims a position this window cannot confirm, i.e. the
    # window was replayed whole instead of resumed from the seed's row.  It is
    # reported on the returned ledger so the fallback is visible rather than
    # silent; it deliberately does NOT change what the replay concludes.  See
    # the recovery rationale below.
    lineage_recovered = False
    if seed_has_lineage_marker:
        if seed_through_task_id:
            matching_index = next(
                (
                    index
                    for index in range(len(history) - 1, -1, -1)
                    if _history_entry_task_id(history[index]) == seed_through_task_id
                ),
                None,
            )
        if matching_index is None and seed_through_key:
            matching_index = next(
                (
                    index
                    for index in range(len(history) - 1, -1, -1)
                    if _history_entry_key(history[index]) == seed_through_key
                ),
                None,
            )
        if matching_index is not None:
            entries_to_apply = history[matching_index + 1 :]
        else:
            # LINEAGE RECOVERY.  The retained window does not contain the row
            # this seed was stamped through, so we cannot prove the window is
            # downstream of the seed.
            #
            # This used to set ``entries_to_apply = []`` and wait for "an
            # authoritative rebuild" -- but nothing in the system performs one,
            # and ``can_advance_marker`` below refuses to move a marker that did
            # not match.  Those two together made lost lineage a PERMANENT,
            # unrecoverable freeze: no later review could ever close a blocker
            # again, the merge gate held forever, and because it respawns a
            # review on every evaluation the PR burned model calls without
            # bound.  Live instance: PR #17693 reached 45 history rows and 44
            # spawned review tasks, the last 40 of them APPROVE-with-no-issues
            # on one unchanged head, each emitting correct, backend-verified,
            # reasoned ``refuted`` resolutions for both open blockers -- every
            # one discarded here.  Losing the marker is routine, not exotic:
            # ``review_request_changes`` rebuilds parent history by filtering
            # rows out by ``review_task_id``, so the marked row can simply stop
            # existing.
            #
            # Recover instead of freezing: replay the ENTIRE retained window on
            # top of the seed's open records.  That is a full recomputation, not
            # a partial catch-up, so it does not depend on knowing where the
            # seed sat -- the window is applied in its own recorded order and
            # the resulting state is the one that order implies.  Re-applying
            # rows the seed already consumed is idempotent: each row's effect is
            # a pure function of that row, so a second pass reaches the same
            # raise / resolve / omission decision.  Seeding with the seed's open
            # records keeps any blocker that only a bound-dropped row raised.
            #
            # The old comment feared a window that fakes ordering -- "a finding
            # introduced at iteration 5 appears fixed at iteration 1".  The
            # window carries its own order and is replayed in it, so that
            # inversion does not arise here.  What it would take is rows the
            # bound already dropped -- and those are invisible to the gate, the
            # reviewer and the fix agent alike, so no ledger state can be proven
            # about them and demanding one is the wedge.  Their effect survives
            # anyway, because the replay starts from the seed's open records.
            # The marker then advances to the new tail, so the ledger reconnects
            # to a provable lineage instead of staying stranded forever.
            lineage_recovered = True
            entries_to_apply = history
    # A markerless legacy seed has no claim to a position *after* this bounded
    # history window. Rebuild conservatively from the retained reviews: seeded
    # unverified blockers cannot be closed by omission, explicit exact-target
    # resolutions remain required, and newly observed blockers must still enter
    # the open set. Once the bounded tail is ingested below it receives a marker
    # so subsequent updates regain ordered incremental semantics.
    # A complete seed only remains a proof when its tail is still present in
    # the retained history (or no new history was supplied). If the marker is
    # absent, replaying the retained cap may reconstruct current-looking state
    # while silently skipping rows between the seed and that window.
    # Every path that reaches a stamp below has replayed the retained history
    # all the way to its tail, so the stamped ledger covers that history:
    #   * ``matching_index is not None`` -- the seed's own position is inside
    #     this window and every row after it was applied.
    #   * ``lineage_recovered``          -- the whole window was replayed on top
    #     of the seed's open records.
    #   * no lineage marker at all       -- a first build or a legacy seed; the
    #     whole window was replayed.
    # The disjunction is exhaustive by construction, and that is the point.  It
    # used to carry two further conditions, and each one was a one-way door:
    #
    #   ``seed_is_complete`` on the first clause -- a ledger stamped
    #   ``complete=False`` once could never be stamped complete again, because
    #   the only way to become complete was to have been complete.  Every
    #   ledger already persisted in that state stays wedged until this clause
    #   goes.
    #
    #   ``len(history) < REVIEW_HISTORY_MAX_ENTRIES`` on the others -- losing
    #   lineage while the window was full was unrecoverable, because history
    #   only grows.  The merge gate then minted BP-ledger-proof-incomplete on
    #   every pass and escalated to operator adjudication: the engine revoking
    #   its own authority because a bounded buffer overflowed.  On NeurIPS PR
    #   #1769 that cost 65 byte-identical APPROVE reviews over 78 attempts.
    #
    # "Complete" means the ledger accounts for every row of the RETAINED review
    # history -- exactly what ``issue_ledger_complete_for_history`` asks, and
    # the only history any consumer can read.  Rows the bound already dropped
    # are invisible to the gate, the reviewer and the fix agent alike, so
    # demanding proof about them is unsatisfiable the moment the window fills.
    # Fail-closed is unchanged: coverage is not clearance.  The recovery replays
    # on top of the seed's open records, so a blocker raised in a dropped row
    # stays open unless the window explicitly resolves it or an authoritative,
    # target-verified verdict inside the window is silent about it -- and the
    # live invariant that actually moves, "this stamp still matches the current
    # tail", is checked separately by ``issue_ledger_complete_for_history`` and
    # still goes False the moment a new review row lands.
    lineage_complete = bool(
        matching_index is not None
        or lineage_recovered
        or not seed_has_lineage_marker
    )

    for entry in entries_to_apply:
        if not isinstance(entry, dict):
            continue
        verdict = str(entry.get("verdict") or "").upper()
        verdict_is_authoritative = verdict in ("APPROVE", "REQUEST_CHANGES")
        try:
            iteration = int(entry.get("iteration") or 0)
        except (TypeError, ValueError):
            iteration = 0
        head_sha = _entry_head_sha(entry)
        base_sha = _entry_base_sha(entry)
        target_verified = bool(
            entry.get("target_verified_by_backend") is True
            and str(entry.get("review_task_id") or "").strip()
            and head_sha
            and base_sha
        )

        # Only a well-formed review verdict may close a demand.  Blockers below
        # are deliberately ingested regardless of this token, but accepting a
        # resolution from a COMMENT/malformed row would be the inverse
        # fail-open bug.
        for resolution in (
            (entry.get("resolutions") or []) if verdict_is_authoritative else []
        ):
            if not isinstance(resolution, dict):
                continue
            issue_id = str(resolution.get("issue_id") or "").strip()
            disposition = str(resolution.get("disposition") or "").lower().strip()
            reason = str(resolution.get("reason") or "").strip()
            record = open_records.get(issue_id)
            if record is None or disposition not in ISSUE_RESOLUTION_DISPOSITIONS:
                continue
            if not reason:
                continue
            if not target_verified:
                # This entry's own review target was not backend-verified, so
                # it cannot prove closure (fail-closed rule above). Record the
                # good-faith claim without touching open/resolved bucketing.
                record["pending_disposition"] = {
                    "disposition": disposition,
                    "reason": reason,
                    "iteration": iteration,
                    "head_sha": head_sha,
                    "review_task_id": str(entry.get("review_task_id") or "")
                    .strip()
                    or None,
                }
                continue
            if disposition == "fixed":
                # For fully proven chains, "fixed" still means the bytes
                # changed.  A legacy/unmarked blocker can only be retired by a
                # later exact-target review making an explicit, reasoned
                # disposition; omission alone remains non-authoritative.
                if record.get("last_seen_target_verified") and (
                    not record.get("last_seen_base_sha")
                    or not record.get("last_seen_head_sha")
                    or head_sha == record["last_seen_head_sha"]
                ):
                    continue
            open_records.pop(issue_id, None)
            resolved.append(
                {
                    "issue_id": issue_id,
                    "disposition": disposition,
                    "reason": reason,
                    "resolved_iteration": iteration,
                    "resolved_head_sha": head_sha,
                }
            )

        raw_current_issues = [
            i for i in (entry.get("issues") or []) if isinstance(i, dict)
        ]
        supplied_issue_ids = [
            str(issue.get("issue_id") or "").strip() for issue in raw_current_issues
        ]
        # Compute a backend-owned candidate for every finding regardless of an
        # agent-supplied ID. Supplied IDs are considered separately below and
        # are accepted only as compatible references to an already-open demand.
        current_issues: list[dict] = []
        for ordinal, raw_issue in enumerate(raw_current_issues):
            issue = dict(raw_issue)
            issue["issue_id"] = _issue_fingerprint(
                issue,
                reviewed_head_sha=head_sha,
                iteration=iteration,
                ordinal=ordinal,
            )
            current_issues.append(issue)
        blocking = [
            (ordinal, issue, supplied_issue_ids[ordinal])
            for ordinal, issue in enumerate(current_issues)
            if _is_blocking(issue)
        ]

        matched_open_ids: set[str] = set()
        normalized_current: list[dict] = []
        current_by_id: dict[str, dict] = {}
        preserve_demanded_fields: set[str] = set()
        for ordinal, issue, supplied_issue_id in blocking:
            matched_id: str | None = None

            # A copied BP ID is a reference to the original demand, not
            # permission to rewrite that demand's description or location.  It
            # only re-raises the existing blocker when the payload is also a
            # conservative semantic/region match.  Otherwise keep the old
            # blocker and give the unrelated newcomer its own backend ID.
            exact_prior = open_records.get(supplied_issue_id)
            if (
                exact_prior is not None
                and supplied_issue_id not in matched_open_ids
                and _same_finding(exact_prior["issue"], issue)
            ):
                matched_id = supplied_issue_id
                issue = dict(exact_prior["issue"])
                preserve_demanded_fields.add(supplied_issue_id)

            if matched_id is None:
                matched_id = next(
                    (
                        issue_id
                        for issue_id, prior in open_records.items()
                        if issue_id not in matched_open_ids
                        and _same_finding(prior["issue"], issue)
                    ),
                    None,
                )
            if matched_id is not None:
                issue = {**issue, "issue_id": matched_id}
                matched_open_ids.add(matched_id)

            issue_id = str(issue["issue_id"])
            duplicate_current = current_by_id.get(issue_id)
            if duplicate_current is not None:
                if _same_finding(duplicate_current, issue):
                    # Two copies of one finding are one binding blocker.
                    continue
                issue_id = _fresh_collision_id(
                    issue,
                    reviewed_head_sha=head_sha,
                    iteration=iteration,
                    ordinal=ordinal,
                    unavailable_ids={
                        *open_records.keys(),
                        *current_by_id.keys(),
                    },
                )
                issue = {**issue, "issue_id": issue_id}
            elif issue_id in open_records and issue_id not in matched_open_ids:
                # A deterministic hash collision must also fail closed.  Keep
                # both demands rather than overwriting one under a shared key.
                issue_id = _fresh_collision_id(
                    issue,
                    reviewed_head_sha=head_sha,
                    iteration=iteration,
                    ordinal=ordinal,
                    unavailable_ids={
                        *open_records.keys(),
                        *current_by_id.keys(),
                    },
                )
                issue = {**issue, "issue_id": issue_id}

            current_by_id[issue_id] = issue
            normalized_current.append(issue)

        # Closure-by-omission runs on a recovery pass too, and that is load
        # bearing rather than lax.  A recovery replays the WHOLE retained
        # window in order, so it is a full recomputation, and each row's effect
        # on the ledger is a pure function of that row: replaying a row the
        # seed already consumed reproduces the same raise / resolve / omission
        # decision it produced the first time.  Suppressing omission here
        # instead leaves every blocker the window raised early and dropped
        # later permanently open -- measured on PR #17693, suppression kept two
        # long-dead findings (first demanded at iterations 1 and 3, unmentioned
        # by the 40 authoritative APPROVEs that followed) open forever, which is
        # the same wedge in a new place.  The seed's open records are still
        # folded in first, so a blocker raised only in a row the bound dropped
        # is not lost; it closes only if a replayed authoritative, target-
        # verified verdict is silent about it, which is exactly the inference
        # the normal path makes.
        if verdict_is_authoritative and target_verified:
            for issue_id, prior in list(open_records.items()):
                if issue_id in matched_open_ids:
                    continue
                if (
                    prior.get("last_seen_target_verified")
                    and head_sha
                    and base_sha
                    and prior["last_seen_head_sha"]
                    and prior.get("last_seen_base_sha")
                    and head_sha != prior["last_seen_head_sha"]
                ):
                    open_records.pop(issue_id, None)
                    resolved.append(
                        {
                            "issue_id": issue_id,
                            "disposition": "fixed",
                            "reason": "absent from review of a changed exact PR head",
                            "resolved_iteration": iteration,
                            "resolved_head_sha": head_sha,
                        }
                    )

        # Structured blocking findings are authoritative even when the verdict
        # token says APPROVE. Treating that contradictory payload as empty would
        # be fail-open and could merge a reviewer-listed CRITICAL/MAJOR defect.
        if blocking:
            for issue in normalized_current:
                issue_id = str(issue["issue_id"])
                existing = open_records.get(issue_id)
                if existing is None:
                    open_records[issue_id] = {
                        "issue_id": issue_id,
                        "issue": issue,
                        "demanded_iteration": iteration,
                        "demanded_head_sha": head_sha,
                        "demanded_base_sha": base_sha,
                        "demanded_target_verified": target_verified,
                        "last_seen_iteration": iteration,
                        "last_seen_head_sha": head_sha,
                        "last_seen_base_sha": base_sha,
                        "last_seen_target_verified": target_verified,
                    }
                else:
                    if issue_id not in preserve_demanded_fields:
                        existing["issue"] = issue
                    existing["last_seen_iteration"] = iteration
                    if target_verified:
                        existing["last_seen_head_sha"] = head_sha
                        existing["last_seen_base_sha"] = base_sha
                        existing["last_seen_target_verified"] = True
                    # This entry re-raised the finding, so any earlier
                    # not-yet-authoritative "believed fixed" claim no longer
                    # applies -- it is once again an actively contested
                    # blocker, not a quietly-omitted one.
                    existing.pop("pending_disposition", None)

    result = {
        "open": [_public_open_record(record) for record in open_records.values()],
        "resolved": resolved[-ISSUE_LEDGER_MAX_RESOLVED:],
    }
    tail = next(
        (entry for entry in reversed(history) if isinstance(entry, dict)),
        None,
    )
    # There is no longer a branch that declines to advance the marker.  That
    # was the other half of the freeze: a seed whose marker did not match was
    # given back its own stale marker, so the next build could not match it
    # either, forever.  Every stamping path above now replays the retained
    # history to its tail, so the tail is an honest statement of how far this
    # ledger has been carried.
    if stamp_complete and tail is not None:
        result.update(
            {
                "schema_version": ISSUE_LEDGER_SCHEMA_VERSION,
                "complete": lineage_complete,
                "through_entry_key": _history_entry_key(tail),
            }
        )
        tail_task_id = _history_entry_task_id(tail)
        if tail_task_id:
            result["through_entry_task_id"] = tail_task_id
        if lineage_recovered:
            # Surface the reason, not just the state.  The ledger is complete
            # either way, but "we replayed the whole window because the seed's
            # marker had fallen out of it" is a different fact from "we resumed
            # exactly where the seed left off", and an operator reading a
            # ledger should not have to re-derive which one happened.  This is
            # a per-build observation, not durable state: ``normalize_issue_
            # ledger`` does not carry it, so it cannot latch across builds.
            result["lineage_recovered"] = True
    elif stamp_complete and normalized_seed is not None:
        # No history at all: nothing to cover, so carry the seed's own stamp
        # through verbatim rather than inventing one.
        for key in (
            "schema_version",
            "complete",
            "through_entry_key",
            "through_entry_task_id",
        ):
            if key in normalized_seed:
                result[key] = normalized_seed[key]
    return result


def exclude_self_disposed_blockers(
    open_blockers: list[dict], resolutions: list | None
) -> list[dict]:
    """Drop ledger-open blockers that the review being processed RIGHT NOW
    disposed in its own ``resolutions[]`` with a valid disposition + reason.

    Only for the current review's own verdict/issue list (caller must have
    proven the review examined the backend-assigned commit, e.g. via
    ``_assigned_target_matches``): when the live head raced ahead and
    ``build_issue_ledger`` could not stamp exact-target proof on this entry,
    the ledger keeps the blocker open (fail-closed), but the withhold override
    must not fabricate a REQUEST_CHANGES from an issue this same review just
    adjudicated. Ledger open/resolved membership is untouched.
    """
    self_disposed_ids = {
        str(res.get("issue_id") or "").strip()
        for res in (resolutions or [])
        if isinstance(res, dict)
        and str(res.get("disposition") or "").lower().strip()
        in ISSUE_RESOLUTION_DISPOSITIONS
        and str(res.get("reason") or "").strip()
    }
    if not self_disposed_ids:
        return open_blockers
    return [
        blocker
        for blocker in open_blockers
        if str(blocker.get("issue_id") or "") not in self_disposed_ids
    ]
