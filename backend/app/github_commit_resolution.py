"""Resolve operator-supplied commit-ish refs to a canonical full 40-hex sha.

Operator levers (``promote-recover force-promote --candidate-sha``,
``hotfix-promote``) accept a commit the human typed. A human types an
ABBREVIATED sha, because that is what ``git log`` prints. GitHub's
``POST /git/refs`` requires the full 40-character sha and answers anything
shorter with a 422 "Object does not exist", so an abbreviated candidate died
deep inside the promotion machinery as a bare ``release_branch_failed``
(incident 2026-08-23).

The engine can answer that question itself — it holds the token, the owner and
the repo — so it must, rather than handing the operator back a lookup it could
have performed. This module is the single normalization seam: resolve the
operator's ref ONCE, at the server-side entry point, before the diff hash, the
CI resolution and ``promote()`` each key work off the raw string.

It deliberately has no module-level ``app`` imports (the GitHub seam and the
error-message helper are resolved at call time), so it can never participate in
an ``app.github`` import cycle.
"""

import logging
from urllib.parse import quote

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

FULL_COMMIT_SHA_LENGTH = 40
_HEX_DIGITS = frozenset("0123456789abcdef")


def _github_request(*args, **kwargs):
    """Delegate to the live ``app.github._github_request`` at call time so
    monkeypatching ``app.github._github_request`` stays effective here."""
    from app import github as _gh

    return _gh._github_request(*args, **kwargs)


def is_full_commit_sha(value) -> bool:
    """True iff *value* is already a full 40-character lowercase-hex commit sha."""
    text = str(value or "")
    return len(text) == FULL_COMMIT_SHA_LENGTH and all(c in _HEX_DIGITS for c in text)


async def resolve_commit_sha(
    token: str, owner: str, repo: str, ref: str, *, project_id: str | None = None
) -> str:
    """Resolve any commit-ish *ref* to its FULL 40-hex commit sha.

    An already-full lowercase-hex sha is returned unchanged with NO network
    call, so a caller that already passes a canonical sha behaves EXACTLY as it
    does today. Anything else — an abbreviated sha, a branch/tag name, an
    uppercase sha — is resolved through
    ``GET /repos/{owner}/{repo}/commits/{ref}``, which reports the canonical sha.

    An unresolvable or ambiguous ref raises ``HTTPException`` whose ``detail``
    QUOTES GitHub's own message and names the remedy — that IS an operator
    error, and it must be reported precisely instead of being passed through to
    fail opaquely several layers deeper.
    """
    from app.github_refs import github_error_message

    text = str(ref or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot resolve an empty commit ref to a full 40-character sha.",
        )
    if is_full_commit_sha(text):
        return text

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github.v3+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = await _github_request(
        "GET",
        f"https://api.github.com/repos/{owner}/{repo}/commits/{quote(text, safe='')}",
        headers,
        project_id=project_id,
    )
    if response.status_code != 200:
        message = github_error_message(response)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Could not resolve '{text}' to a full 40-character commit sha "
                f"(GitHub {response.status_code}"
                + (f": {message}" if message else "")
                + f"). Check the ref, or resolve it with `git rev-parse {text}` "
                "and pass the full sha."
            ),
        )
    resolved = str((response.json() or {}).get("sha") or "").strip().lower()
    if not is_full_commit_sha(resolved):
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"GitHub resolved '{text}' to {resolved!r}, which is not a full "
                "40-character commit sha."
            ),
        )
    logger.info("Resolved commit ref '%s' to full sha %s", text, resolved)
    return resolved
