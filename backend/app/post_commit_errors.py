"""Leaf module for the post-commit exception types.

``app.task_lifecycle`` owns the post-commit stage machinery but must not be
imported by the sibling stage modules that the fdd07b88 decomposition plan
(S2-S12) extracts from ``_process_post_commit`` — importing it from a stage
module would create an import cycle. These two exceptions are the typed
signals those stages raise, so they live here in a dependency-free leaf any
module may import.

This module imports ONLY the standard library. Do not add an ``app.*`` import
here, or the cycle this module was created to break returns.
"""


class PostCommitLocked(Exception):
    """Raised when post-commit cannot acquire its project/task lock pair."""

    def __init__(self, message: str, *, lock_scope: str = "unknown", resource_id=None):
        super().__init__(message)
        self.lock_scope = lock_scope
        self.resource_id = resource_id


class PostCommitFinalCommitFailed(Exception):
    """Raised when _process_post_commit's FINAL commit fails after the
    post-terminal chain ran.

    The final commit flushes the best-effort post-terminal side effects
    (spawned follow-up / remediation tasks, rollup nudges, analytics,
    artifacts). If it fails, those side effects are DROPPED — they were never
    persisted. The task's own terminal status is safe (it was committed on the
    early-commit / status-endpoint session before post-commit ran), so this
    is NOT a fatal error, but the dropped side effects MUST be retried.

    Distinct signal so the callers (inline status endpoint, the terminal
    idempotency retry path, _sweep_post_commit_pending) PRESERVE the
    write-ahead ``post_commit_pending`` marker for the sweep to retry instead
    of clearing it on what would otherwise look like a clean return. Crucially
    the inline / retry callers translate it to a clean 200 ("post_commit_retry
    _pending") rather than re-raising a 500 to the agent — the work survives in
    the marker, so there is nothing for the agent to do. See incident
    eefbed05: a swallowed final-commit failure cleared the marker, the sweep
    never retried, and the dependent chain orphaned.
    """
