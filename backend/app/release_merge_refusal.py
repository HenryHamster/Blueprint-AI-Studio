"""Classification of a promotion merge that did not land.

``github_pulls.merge_pull_request`` collapses three very different outcomes
onto ``merged=False``:

* a real conflict (GitHub 409) — ``conflict=True``;
* a local credential refusal — ``pat_fallback_blocked=True``, returned BEFORE
  any PUT because the only credential the resolver could produce was the
  operator's own personal access token, and merging with it would mint a
  commit GitHub attributes to the operator for automated pipeline work;
* required checks still pending (GitHub 405) — ``conflict`` unset.

Only the third can self-heal.  The first two are permanent and must be
recorded as promotion failures, or the version parks in the managed check
waiter forever while the reconciler re-submits and is refused identically on
every pass — with ``required_checks_pending`` as the operator-facing reason,
which for a credential refusal is simply untrue.

Both ``release_promotion.promote`` and the ``release_reconciliation`` managed
waiter consume this, so the classification stays single-sourced.
"""

from __future__ import annotations

from typing import TypedDict

_MODULE_SIZE_GOVERNANCE_CAP: int = 59


class PromotionMergeRefusal(TypedDict):
    """A permanent, recordable reason a promotion merge did not land."""

    error: str
    detail: str
    conflict: bool


def promotion_merge_refusal(
    merge_result: dict, owner: str, repo: str
) -> PromotionMergeRefusal | None:
    """Return the recordable failure for a permanent non-merge, else ``None``."""
    if merge_result.get("pat_fallback_blocked"):
        return PromotionMergeRefusal(
            error="merge_credential_blocked",
            detail=(
                "Promotion merge was not submitted: only the operator's personal "
                "access token is available for this project, and the pipeline must "
                "not mint operator-attributed merge commits. Install or repair the "
                f"GitHub App installation for {owner}/{repo}, then re-run promotion."
            )[:300],
            conflict=False,
        )
    if merge_result.get("conflict"):
        return PromotionMergeRefusal(
            error="merge_blocked",
            detail=str(merge_result.get("message", "merge not completed"))[:300],
            conflict=True,
        )
    return None
