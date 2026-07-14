# ── size guardrail (baseline-aware) ────────────────────────────────────────
# Fast module size-guardrail check: no DB/Docker/network, sub-second. Mirrors
# the line-count caps that backend/testing/test_invariants.py enforces, so a
# size breach surfaces HERE (before ./pr open) instead of only after a PR is
# open and a full CI/Docker round-trip is burned. Runs UNCONDITIONALLY (all
# sections, not diff-scoped): a rebase/merge can grow a file in a stack the
# diff never touched.
#
# BASELINE: a breach can also be INHERITED — an upstream merge pushes a file
# over its cap on the base branch itself, and every unrelated task then fails
# this gate on work it cannot remediate (chronic-offender cap bumps need
# operator approval). On failure we therefore re-run the checker with
# --base-root pointing at a detached worktree of origin/$BASE: the checker
# compares PER FILE and exits 0 when every offender already exists at the
# same-or-larger size on base (WARN, not blocking — mirrors the viewport and
# backend-pytest baseline-recheck pattern). A breach that is NEW here, or that
# GREW past its base size, still fails closed. If the baseline cannot be
# computed (no base ref, worktree failure) the original failure stands.
# './check full' skips the baseline recheck (the result IS the new baseline),
# matching the viewport gate — it lets an agent force a blocking view of a
# baseline-warned WARN.
echo "[check] size guardrail..."
SIZE_GR_LOG=/tmp/check_size_guardrail.log
if python3 backend/testing/check_size_limits.py > "$SIZE_GR_LOG" 2>&1; then
  echo "[check] size guardrail OK"
elif [ "$FULL_SCOPE" = "1" ]; then
  echo "[check] size guardrail FAILED (--full, no baseline recheck)"
  cat "$SIZE_GR_LOG" || true
  echo "[check] Remediate with: python3 backend/testing/check_size_limits.py --fix"
  FAILED+=("size guardrail")
else
  echo "[check] size guardrail FAILED — checking baseline on origin/$BASE..."
  SIZE_GR_BASE_REF="origin/$BASE"
  SIZE_GR_BASELINE_REASON=""
  if ! git rev-parse --verify "$SIZE_GR_BASE_REF" >/dev/null 2>&1; then
    SIZE_GR_BASE_REF="$BASE"
    git rev-parse --verify "$SIZE_GR_BASE_REF" >/dev/null 2>&1 || SIZE_GR_BASELINE_REASON="base ref unavailable"
  fi
  SIZE_GR_RC=1
  if [ -z "$SIZE_GR_BASELINE_REASON" ]; then
    SIZE_GR_WT=/tmp/check_size_guardrail_baseline_wt
    # Stale worktree from a prior run can wedge 'worktree add'.
    # 'worktree remove --force' is the safe cleanup (no rm -rf).
    git worktree remove --force "$SIZE_GR_WT" >/dev/null 2>&1 || true
    if ! git worktree add --detach "$SIZE_GR_WT" "$SIZE_GR_BASE_REF" >/tmp/check_size_guardrail_wt.log 2>&1; then
      SIZE_GR_BASELINE_REASON="worktree add failed (see /tmp/check_size_guardrail_wt.log)"
    else
      # PR-side checker code judges the PR tree against the base checkout;
      # exit 0 means every offender pre-exists on base at the same-or-larger
      # size (the log then carries [pre-existing] lines with the reason).
      if python3 backend/testing/check_size_limits.py --base-root "$SIZE_GR_WT" > "$SIZE_GR_LOG" 2>&1; then
        SIZE_GR_RC=0
      fi
      git worktree remove --force "$SIZE_GR_WT" >/dev/null 2>&1 || true
    fi
  fi
  if [ "$SIZE_GR_RC" = "0" ]; then
    echo "[check] size guardrail WARN — every offender pre-exists on $SIZE_GR_BASE_REF; not blocking this PR"
    echo "[check] Upstream remediation (separate chore PR): python3 backend/testing/check_size_limits.py --fix (chronic offenders need operator approval via --force-chronic)"
    cat "$SIZE_GR_LOG" || true
  else
    echo "[check] size guardrail FAILED — size breach introduced or grown by this change."
    [ -n "$SIZE_GR_BASELINE_REASON" ] && echo "[check] (baseline check skipped: $SIZE_GR_BASELINE_REASON — treating failure as real)"
    cat "$SIZE_GR_LOG" || true
    echo "[check] Remediate with: python3 backend/testing/check_size_limits.py --fix"
    FAILED+=("size guardrail")
  fi
fi
# ── end size guardrail ──────────────────────────────────────────────────────
