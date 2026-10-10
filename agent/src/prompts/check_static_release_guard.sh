
# ── Attributed standalone static corpus (ALWAYS-ON, stack-independent) ───────
#
# This lane used to live inside the generated gate's `if touches backend/;`
# block, which made every drift sentinel whose surface is admin-ui/, agent/ or
# scripts/ unreachable in practice: a PR confined to those trees ran only
# `static_release_guard.py --size-only`, and --size-only explicitly skips the
# corpus. Every stale derived artifact that failed promotion PR #17738
# (manifest test counts, the websocket emitter fixture and its hand-kept
# workbench_contracts twin, backend/.test_durations, backend/testing/test-estate-ranked-queue.md)
# entered through exactly that hole and stayed invisible afterwards, because
# base attribution correctly suppresses inherited failures on every later PR
# until the promotion matrix — which has no base to attribute against — surfaces
# all of it at once.
#
# Hoisting is safe because SELECTION IS THE GUARD'S JOB, not the gate's:
# static_release_guard.py scopes itself from the changed set (STATIC_PATH_PREFIXES
# plus every surface glob in backend/testing/drift_sentinel_surfaces.json) and
# prints "no static release-guard paths changed; skipping" without starting the
# harness when nothing is in scope. So a diff that really touches nothing
# static pays one Python start-up, not a corpus run.
#
# --corpus-only, not the full mode: the size half of this guard is owned by the
# always-on size guardrail lane below, and running both would double-charge it.
#
# The harness invocation stays inside static_release_guard.py
# (scripts/test-backend.sh --standalone --no-postgres) — never a bare pytest —
# so the agent VM's memory ceiling and the roster's identity keep being the
# harness's business. The agent VM is e2-standard-2 (8 GB) and cannot fit
# Docker build + Postgres sidecar + full pytest suite + agent + Claude
# subprocess in memory; the full suite OOMs (exit 137) during the mandatory
# pre-PR gate (53ef061b, b150b4d6, b15b6813 all died this way 2026-05-05).
#
# The guard runs the same corpus on the task and base trees and attributes
# failures by structured route/test/failure-kind identity. It must remain the
# single baseline decision point: a non-zero base run does not waive unrelated
# failures introduced by this task.
if [ "$WITH_TESTS" = "1" ] && [ "@MERGE_GATE@" = "1" ]; then
  run "backend pytest (attributed)" python3 scripts/static_release_guard.py \
    --corpus-only --base "$BASE" --repo-root "$WORKSPACE"
fi
